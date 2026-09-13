"""Moving Claude Code onto the next account when the current one is spent.

A Claude subscription is metered in a rolling session window and a weekly one,
and once either is used up every run fails until it resets — which, for an
executor that polls around the clock, means hours of tasks that simply do not
get done. An installation with more than one subscription can carry on through
that, but only if something notices and switches account, because the CLI reads
its credentials from one file and takes no token on its command line.

That is this module. It owns three pieces of state that have to agree:

* the connected accounts, from SQLite (each a completed sign-in),
* which one is current, from SQLite too, so a settings save cannot put a spent
  account back,
* and what ``~/.claude/.credentials.json`` actually holds — what the CLI will
  use, and the only one of the three that makes anything happen.

Every check reconciles them in that order before looking at usage at all, which
is what makes a fresh executor start on the right account rather than on
whatever was left behind.
"""
import threading
import time

from codee_agent_claude_code import credentials, oauth
from codee_agent_claude_code.account import AccountUnavailable, fetch_account
from codee_agent_claude_code.usage import UsageUnavailable, fetch_usage
from codee_main_context.context import CodeeMainContext, load_settings
from codee_main_context.logging import get_logger

from codee_database import claude_code_accounts
from codee_database.claude_code_accounts import Account

log = get_logger(__name__)

# How often the current account's usage is checked. A window fills over hours,
# so there is nothing to gain from asking more often, and the endpoint is a
# network round trip on an account that is also running agents.
CHECK_INTERVAL = 300  # 5 minutes

# Renew an access token this long before it expires. Comfortably longer than
# the gap between checks, so a token is never handed to an agent in its last
# minutes — and the CLI renews it itself mid-run anyway, since it is given the
# refresh token too.
REFRESH_MARGIN_MS = 30 * 60 * 1000

# How long an account may sit untouched before it is renewed anyway, whether or
# not it is the one in use. A refresh token lasts on the order of a month and
# every renewal extends it, so an account in regular rotation never runs out —
# but a spare subscription that only gets used when the others are exhausted
# would quietly rot to the point of needing a manual sign-in. Renewing every
# few days costs one request per account per five days and removes the whole
# failure mode.
IDLE_REFRESH_MS = 5 * 24 * 60 * 60 * 1000

THREAD_NAME = "claude-key-rotation"


class ClaudeKeyRotation:
    """One executor's rotation state: the account it signed in, and the checks.

    A class rather than a function because the useful unit to test is a single
    check, and because "which account did we last write" has to survive between
    them — without it every check would rewrite the credentials file whether or
    not anything had changed.
    """

    def __init__(self, main_context: CodeeMainContext,
                 interval: int = CHECK_INTERVAL) -> None:
        self._context = main_context
        self._interval = interval
        # The account whose credentials this process last wrote. Zero until it
        # writes one, which is what makes the first check after a restart sign
        # in rather than assume the file is already right.
        self._signed_in = 0
        # Set once the option is found switched off, so the log says so once
        # rather than every interval for the life of the process.
        self._reported_off = False
        self._reported_empty = False

    def tick(self) -> None:
        """One check: reconcile the account in use, then rotate if it is spent."""
        if not load_settings(self._context.data_dir).claude_code_rotate_keys:
            if not self._reported_off:
                log.debug("Claude Code account rotation is off; nothing to check.")
                self._reported_off = True
            self._signed_in = 0
            return
        self._reported_off = False

        accounts = claude_code_accounts.accounts(self._context)
        if not accounts:
            if not self._reported_empty:
                log.warning("Claude Code account rotation is on but no accounts "
                            "are connected; connect one in Settings.")
                self._reported_empty = True
            return
        self._reported_empty = False

        current = self._current_account(accounts)
        current = self._apply(current)
        self._renew_idle_accounts(accounts, current)

        try:
            usage = fetch_usage(current.access_token)
        except UsageUnavailable as error:
            # Keep the account we have: a question we could not ask says
            # nothing about the answer, and rotating on it would work through
            # the whole list on nothing worse than a flaky network.
            log.warning("Could not read Claude Code usage, keeping %s: %s",
                        describe(current), error)
            return

        if not usage.limited:
            log.debug("Claude Code account %s is within its limits (%s).",
                      describe(current), usage.describe() or "no windows reported")
            return

        log.info("Claude Code account %s has hit its limit (%s); rotating.",
                 describe(current), usage.reason)
        self._rotate(accounts, current)

    def run_forever(self) -> None:
        """Check, sleep, repeat. Never raises: this thread must outlive a bad tick."""
        log.debug("Claude Code account rotation checking every %ss.",
                  self._interval)
        while True:
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 - a dead thread rotates nothing
                log.error("Claude Code account rotation check failed: %s", error,
                          exc_info=True)
            time.sleep(self._interval)

    def _current_account(self, accounts: list[Account]) -> Account:
        """The account in use, choosing the first when there is no valid answer.

        Covers both ways there can be none: nothing has ever been chosen (a
        fresh installation, or rotation was only just switched on), and what
        was chosen has since been disconnected. Both mean the same thing —
        start from the top — and both are recorded, so the next check does not
        have to work it out again.
        """
        current_id = claude_code_accounts.current_account_id(self._context)
        for account in accounts:
            if account.id == current_id:
                return account
        if current_id:
            log.info("Claude Code account %s is no longer connected; falling "
                     "back to the first one.", current_id)
        claude_code_accounts.set_current_account(accounts[0].id, self._context)
        return accounts[0]

    def _apply(self, account: Account) -> Account:
        """Leave the CLI signed in as ``account``, and return its live tokens.

        The file already holding our token is the ordinary case and costs
        nothing. Beyond that it depends on whether this account is the one the
        file is supposed to hold:

        * Rotating *to* a different account — ours wins outright. That is the
          whole point of the switch, and whoever the file names is being
          replaced on purpose.
        * Otherwise, including the first check after a restart — find out whose
          token is in there before overwriting it. Claude Code renews our token
          by itself, because Codee hands it the refresh token on purpose, so
          the file may well hold a *newer* credential for this very account.
          Writing our copy over it would push the CLI back onto an older token
          and, worse, back onto a refresh token Anthropic may already have
          replaced — leaving neither side able to renew.

        Only that last case costs a round trip, and only when the file has
        actually changed.
        """
        stored = credentials.read_credentials()
        if stored.access_token and stored.access_token == account.access_token:
            self._signed_in = account.id
            return account

        # Zero is a fresh process, which has written nothing and so cannot
        # claim the file is wrong: it has to look first.
        switching = self._signed_in not in (0, account.id)
        if switching:
            account = self._refreshed(account)
            self._sign_in(account)
            log.info("Claude Code is now running as %s.", describe(account))
            return account
        return self._reconcile_file(account, stored)

    def _reconcile_file(self, account: Account,
                        stored: credentials.StoredCredentials) -> Account:
        """Decide who the token now in the file belongs to, and act on it."""
        if not stored.access_token:
            account = self._refreshed(account)
            self._sign_in(account)
            return account
        try:
            whose = fetch_account(stored.access_token)
        except AccountUnavailable as error:
            # Cannot tell whose it is. Ours is the one we know is right for the
            # account rotation has chosen, so put it back.
            log.debug("Could not identify the token in the credentials file "
                      "(%s); signing back in as %s.", error, describe(account))
            account = self._refreshed(account)
            self._sign_in(account)
            return account

        if account.label and whose == account.label:
            # The CLI renewed our own token — mid-run, or while Codee was not
            # even running. Its copy is the newer one, and adopting it keeps the
            # refresh token in step: Anthropic may hand back a new one, and an
            # account left holding the spent one would be unusable when its turn
            # came round again.
            log.debug("Adopting the token Claude Code renewed for %s.",
                      describe(account))
            claude_code_accounts.update_tokens(
                account.id, stored.access_token, stored.refresh_token,
                stored.expires_at, self._context)
            account = Account(**{**account.__dict__,
                                 "access_token": stored.access_token,
                                 "refresh_token": stored.refresh_token,
                                 "expires_at": stored.expires_at,
                                 "refreshed_at": _now_ms()})
            self._signed_in = account.id
            # Adopted, not written — so if what we adopted is itself close to
            # expiring, renew it now rather than handing an agent a token with
            # minutes left.
            renewed = self._refreshed(account)
            if renewed.access_token != account.access_token:
                self._sign_in(renewed)
            return renewed

        log.info("Claude Code was signed in as %s; putting %s back.",
                 whose or "somebody else", describe(account))
        account = self._refreshed(account)
        self._sign_in(account)
        return account

    def _sign_in(self, account: Account) -> None:
        credentials.write_credentials(
            account.access_token, account.refresh_token, account.expires_at,
            account.scopes, account.subscription_type)
        self._signed_in = account.id

    def _renew_idle_accounts(self, accounts: list[Account],
                             current: Account) -> None:
        """Keep the accounts waiting their turn from expiring while they wait.

        A refresh token is good for weeks and every renewal extends it, so the
        account in use looks after itself. The ones behind it do not: a spare
        subscription that is only reached when the others are spent could sit
        untouched past its refresh window and be dead by the time it is needed
        — on exactly the day the others ran out. Touching each one every few
        days is what stops that.
        """
        for account in accounts:
            if account.id == current.id:
                continue
            if _now_ms() - account.refreshed_at < IDLE_REFRESH_MS:
                continue
            if account.needs_reconnect(_now_ms()):
                continue  # already known to be finished; saying so again helps nobody
            log.debug("Renewing %s to keep it from expiring while it waits.",
                      describe(account))
            self._refreshed(account, force=True)

    def _refreshed(self, account: Account, force: bool = False) -> Account:
        return ensure_fresh(account, self._context, force)

    def _rotate(self, accounts: list[Account], current: Account) -> None:
        """Move to the next account that still has allowance, if there is one."""
        replacement = self._next_usable(accounts, current)
        if replacement is None:
            log.warning("Every connected Claude Code account is at its limit; "
                        "staying on %s until one resets.", describe(current))
            return
        claude_code_accounts.set_current_account(replacement.id, self._context)
        self._apply(replacement)

    def _next_usable(self, accounts: list[Account],
                     current: Account) -> Account | None:
        """The next account after ``current`` with allowance left, wrapping round.

        Each candidate is renewed before it is asked, since one that has been
        out of rotation for hours is holding an expired token and would answer
        "rejected" for a reason that has nothing to do with its allowance.

        An account that can no longer renew itself is skipped outright — there
        is nothing to sign in with. An account whose *usage* cannot be read is
        taken rather than skipped: the one being left is known to be spent, so
        an unknown is strictly better than staying, and the next check will
        move on again if it turns out not to be.
        """
        ids = [account.id for account in accounts]
        start = ids.index(current.id) + 1 if current.id in ids else 0
        for offset in range(len(accounts)):
            candidate = accounts[(start + offset) % len(accounts)]
            if candidate.id == current.id:
                continue
            if candidate.needs_reconnect(_now_ms()):
                # Nothing to renew it with. Signing in as it would hand the CLI
                # a dead token and cost the executor a poll to find out.
                log.debug("Skipping %s: it has to be connected again.",
                          describe(candidate))
                continue
            candidate = self._refreshed(candidate)
            try:
                usage = fetch_usage(candidate.access_token)
            except UsageUnavailable as error:
                log.debug("Could not read usage for %s, trying it anyway: %s",
                          describe(candidate), error)
                return candidate
            if not usage.limited:
                log.debug("Claude Code account %s has allowance left (%s).",
                          describe(candidate), usage.describe())
                return candidate
            log.debug("Claude Code account %s is also at its limit (%s).",
                      describe(candidate), usage.reason)
        return None


def ensure_fresh(account: Account, main_context: CodeeMainContext,
                 force: bool = False) -> Account:
    """The account with an access token good for a while yet.

    An account that has sat out of rotation for hours is holding a token that
    expired long ago, so this is what makes switching back to it work at all.
    ``force`` renews a token that has life left anyway, which is how an idle
    account's refresh window gets extended before it closes.

    A renewal that could not be *made* is not fatal: the token we have may
    still be good, and whatever asked for it is about to find out either way.
    A renewal that was *refused* is another matter — that is the API saying the
    refresh token is finished, and the account is retired so the settings page
    can ask for it to be connected again.

    Module-level rather than a method because the admin UI needs it too: it
    reads each account's usage for the dashboard, and an account holding an
    expired token would report itself rejected rather than report its
    allowance.
    """
    if not account.refresh_token:
        return account
    if not force and account.expires_at - _now_ms() > REFRESH_MARGIN_MS:
        return account
    try:
        tokens = oauth.refresh_tokens(account.refresh_token)
    except oauth.OAuthRefused as error:
        log.warning("%s can no longer be renewed and has to be connected "
                    "again: %s", describe(account), error)
        claude_code_accounts.retire_account(account.id, main_context)
        return account
    except oauth.OAuthApiError as error:
        log.warning("Could not renew the token for %s: %s",
                    describe(account), error)
        return account
    claude_code_accounts.update_tokens(
        account.id, tokens.access_token,
        tokens.refresh_token or account.refresh_token, tokens.expires_at,
        main_context, tokens.refresh_expires_at)
    log.debug("Renewed the access token for %s.", describe(account))
    return Account(**{**account.__dict__,
                      "access_token": tokens.access_token,
                      "refresh_token": tokens.refresh_token
                      or account.refresh_token,
                      "expires_at": tokens.expires_at,
                      "refresh_expires_at": tokens.refresh_expires_at
                      or account.refresh_expires_at,
                      "refreshed_at": _now_ms()})


def describe(account: Account) -> str:
    """One account as a log fragment: its email, or its id when it has none."""
    return account.label or f"account {account.id}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def start(main_context: CodeeMainContext,
          interval: int = CHECK_INTERVAL) -> threading.Thread:
    """Run the rotation checks on a daemon thread of their own.

    Separate from the poll loop because the two have nothing to do with each
    other's timing: a check is a network round trip that must not delay a tick,
    and a tick can be busy for hours handing tasks to agents while a window
    quietly fills up. Daemon, so a stopped executor doesn't wait on it.
    """
    thread = threading.Thread(
        target=ClaudeKeyRotation(main_context, interval).run_forever,
        name=THREAD_NAME, daemon=True)
    thread.start()
    return thread
