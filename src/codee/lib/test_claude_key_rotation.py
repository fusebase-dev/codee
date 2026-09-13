import json
import os
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from codee_database.database import get_db_connection

from codee_agent_claude_code import credentials, oauth
from codee_agent_claude_code.account import AccountUnavailable
from codee_agent_claude_code.usage import Usage, UsageUnavailable
from codee_main_context.context import (
    CodeeMainContext, Settings, save_settings)

from codee.lib.claude_key_rotation import ClaudeKeyRotation
from codee_database import claude_code_accounts

SPENT = Usage(limited=True, reason="five_hour at 100%",
              windows={"five_hour": 100.0})
FRESH = Usage(limited=False, windows={"five_hour": 12.0, "seven_day": 40.0})

# Far enough out that nothing under test decides the token needs renewing.
LONG_LIFE_MS = 30 * 24 * 60 * 60 * 1000
# What a freshly connected account's refresh token is good for, roughly as
# Anthropic issues it.
REFRESH_LIFE_MS = 30 * 24 * 60 * 60 * 1000
SIX_DAYS_MS = 6 * 24 * 60 * 60 * 1000


def _in(milliseconds: int) -> int:
    return int(time.time() * 1000) + milliseconds


class RotationTestCase(unittest.TestCase):
    """A temp data dir, a temp credentials file, and accounts to connect into it."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.data_dir = Path(self._temporary.name) / "data"
        self.data_dir.mkdir()
        claude_home = Path(self._temporary.name) / "claude"
        claude_home.mkdir()
        environment = patch.dict(os.environ,
                                 {credentials.CONFIG_DIR_ENV: str(claude_home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.credentials_file = claude_home / credentials.CREDENTIALS_NAME
        self.context = CodeeMainContext(data_dir=self.data_dir)
        self._enable()

    def _enable(self, enabled: bool = True) -> None:
        save_settings(self.data_dir, Settings(claude_code_rotate_keys=enabled))

    def _connect(self, label: str, expires_in_ms: int = LONG_LIFE_MS,
                 refresh_expires_in_ms: int = REFRESH_LIFE_MS) -> int:
        """One connected account, as a completed sign-in would leave it."""
        return claude_code_accounts.add_account(
            label, f"token-for-{label}", f"refresh-for-{label}",
            _in(expires_in_ms), " ".join(oauth.SCOPES), "max", self.context,
            _in(refresh_expires_in_ms))

    def _idle_since(self, account_id: int, milliseconds_ago: int) -> None:
        """Backdate when an account was last renewed, as time passing would."""
        with closing(get_db_connection(self.context)) as conn, conn:
            conn.execute("UPDATE claude_code_account SET refreshed_at = ?"
                         " WHERE id = ?",
                         (_in(-milliseconds_ago), account_id))

    def _rotation(self) -> ClaudeKeyRotation:
        return ClaudeKeyRotation(self.context)

    def _stored(self) -> dict:
        return json.loads(self.credentials_file.read_text())["claudeAiOauth"]

    def _account(self, account_id: int):
        return {account.id: account for account
                in claude_code_accounts.accounts(self.context)}[account_id]

    def _tick(self, *usage, rotation: ClaudeKeyRotation | None = None) -> None:
        """Run one check, answering each usage call with the next given verdict."""
        with patch("codee.lib.claude_key_rotation.fetch_usage",
                   side_effect=list(usage)):
            (rotation or self._rotation()).tick()


class ClaudeKeyRotationTest(RotationTestCase):
    """One rotation check, against a real settings file and a real credentials file."""

    def test_the_first_check_signs_in_as_the_first_account(self) -> None:
        # Nothing has ever been current, so the list starts from the top — and
        # the credentials file has to say so before the executor hands anything
        # to an agent.
        first = self._connect("one@example.com")
        self._connect("two@example.com")

        self._tick(FRESH)

        self.assertEqual(self._stored()["accessToken"],
                         "token-for-one@example.com")
        self.assertEqual(
            claude_code_accounts.current_account_id(self.context), first)

    def test_the_whole_credential_is_written_not_just_the_token(self) -> None:
        # A session token is good for hours and an agent run can take two, so
        # the CLI has to be left holding the refresh token and the real expiry
        # that let it renew mid-run.
        self._connect("one@example.com")

        self._tick(FRESH)

        stored = self._stored()
        self.assertEqual(stored["refreshToken"], "refresh-for-one@example.com")
        self.assertGreater(stored["expiresAt"], int(time.time() * 1000))
        self.assertIn("user:profile", stored["scopes"])
        self.assertEqual(stored["subscriptionType"], "max")

    def test_an_account_within_its_limits_is_left_alone(self) -> None:
        self._connect("one@example.com")
        second = self._connect("two@example.com")
        claude_code_accounts.set_current_account(second, self.context)

        self._tick(FRESH)

        self.assertEqual(self._stored()["accessToken"],
                         "token-for-two@example.com")
        self.assertEqual(
            claude_code_accounts.current_account_id(self.context), second)

    def test_a_spent_account_is_replaced_by_the_next_one(self) -> None:
        self._connect("one@example.com")
        second = self._connect("two@example.com")

        self._tick(SPENT, FRESH)

        self.assertEqual(self._stored()["accessToken"],
                         "token-for-two@example.com")
        self.assertEqual(
            claude_code_accounts.current_account_id(self.context), second)

    def test_rotation_wraps_round_to_the_start_of_the_list(self) -> None:
        # The last account being spent must not end the rotation: the first
        # one's window may well have reset by the time the last one filled up.
        first = self._connect("one@example.com")
        second = self._connect("two@example.com")
        claude_code_accounts.set_current_account(second, self.context)

        self._tick(SPENT, FRESH)

        self.assertEqual(
            claude_code_accounts.current_account_id(self.context), first)

    def test_an_account_whose_usage_cannot_be_read_is_kept(self) -> None:
        # A flaky network says nothing about the allowance, and rotating on it
        # would work through the whole list for no reason.
        first = self._connect("one@example.com")
        self._connect("two@example.com")

        self._tick(UsageUnavailable("connection reset"))

        self.assertEqual(
            claude_code_accounts.current_account_id(self.context), first)
        self.assertEqual(self._stored()["accessToken"],
                         "token-for-one@example.com")

    def test_every_account_being_spent_leaves_the_current_one_in_place(self) -> None:
        first = self._connect("one@example.com")
        self._connect("two@example.com")
        self._connect("three@example.com")

        self._tick(SPENT, SPENT, SPENT)

        self.assertEqual(
            claude_code_accounts.current_account_id(self.context), first)
        self.assertEqual(self._stored()["accessToken"],
                         "token-for-one@example.com")

    def test_a_current_account_no_longer_connected_falls_back_to_the_first(self) -> None:
        # Disconnected in Settings while the executor was running. Without this
        # the rotation would keep using an account the user believes is gone.
        first = self._connect("one@example.com")
        claude_code_accounts.set_current_account(4242, self.context)

        self._tick(FRESH)

        self.assertEqual(
            claude_code_accounts.current_account_id(self.context), first)

    def test_the_option_being_off_touches_nothing(self) -> None:
        self._enable(False)
        self._connect("one@example.com")
        self.credentials_file.write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "signed-in-by-hand"}}))

        with patch("codee.lib.claude_key_rotation.fetch_usage") as usage:
            self._rotation().tick()

        usage.assert_not_called()
        self.assertEqual(self._stored()["accessToken"], "signed-in-by-hand")

    def test_the_option_being_on_with_no_accounts_touches_nothing(self) -> None:
        self.credentials_file.write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "signed-in-by-hand"}}))

        with patch("codee.lib.claude_key_rotation.fetch_usage") as usage:
            self._rotation().tick()

        usage.assert_not_called()
        self.assertEqual(self._stored()["accessToken"], "signed-in-by-hand")

    def test_the_rest_of_the_credentials_file_survives(self) -> None:
        # Only the claudeAiOauth section is Codee's to write.
        self._connect("one@example.com")
        self.credentials_file.write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "old"},
             "somethingElse": {"kept": True}}))

        self._tick(FRESH)

        stored = json.loads(self.credentials_file.read_text())
        self.assertEqual(stored["somethingElse"], {"kept": True})

    def test_the_credentials_file_is_readable_by_its_owner_alone(self) -> None:
        self._connect("one@example.com")

        self._tick(FRESH)

        self.assertEqual(self.credentials_file.stat().st_mode & 0o777, 0o600)


class ClaudeKeyRefreshTest(RotationTestCase):
    """Keeping an account's token usable across its turn out of rotation."""

    def test_an_expired_token_is_renewed_before_it_is_used(self) -> None:
        # An account that sat out of rotation for hours is holding a token that
        # expired long ago; without this, switching back to it would sign the
        # CLI in with something already dead.
        self._connect("one@example.com", expires_in_ms=-1000)
        renewed = oauth.Tokens("fresh-token", "fresh-refresh", _in(LONG_LIFE_MS))

        with patch("codee_agent_claude_code.oauth.refresh_tokens",
                   return_value=renewed) as refresh:
            self._tick(FRESH)

        refresh.assert_called_once_with("refresh-for-one@example.com")
        self.assertEqual(self._stored()["accessToken"], "fresh-token")
        self.assertEqual(self._stored()["refreshToken"], "fresh-refresh")

    def test_a_renewed_token_is_stored_for_the_next_check(self) -> None:
        # Otherwise every check would renew again, and the account would be
        # left holding a refresh token Anthropic may already have rotated.
        account = self._connect("one@example.com", expires_in_ms=-1000)
        renewed = oauth.Tokens("fresh-token", "fresh-refresh", _in(LONG_LIFE_MS))

        with patch("codee_agent_claude_code.oauth.refresh_tokens",
                   return_value=renewed):
            self._tick(FRESH)

        self.assertEqual(self._account(account).access_token, "fresh-token")
        self.assertEqual(self._account(account).refresh_token, "fresh-refresh")

    def test_a_token_with_life_left_is_not_renewed(self) -> None:
        self._connect("one@example.com")

        with patch("codee_agent_claude_code.oauth.refresh_tokens") as refresh:
            self._tick(FRESH)

        refresh.assert_not_called()

    def test_a_refusal_to_renew_is_not_fatal(self) -> None:
        # The token we hold may still work, and the usage check is about to say
        # so either way — failing here would stop the executor rotating at all.
        self._connect("one@example.com", expires_in_ms=-1000)

        with patch("codee_agent_claude_code.oauth.refresh_tokens",
                   side_effect=oauth.OAuthApiError("refresh refused")):
            self._tick(FRESH)

        self.assertEqual(self._stored()["accessToken"],
                         "token-for-one@example.com")

    def test_the_account_being_rotated_to_is_renewed_before_it_is_asked(self) -> None:
        # It has been out of rotation and its token has expired, so asking for
        # its usage first would answer "rejected" for a reason that has nothing
        # to do with its allowance — and the rotation would skip right past it.
        self._connect("one@example.com")
        self._connect("two@example.com", expires_in_ms=-1000)
        renewed = oauth.Tokens("fresh-token", "fresh-refresh", _in(LONG_LIFE_MS))

        with patch("codee_agent_claude_code.oauth.refresh_tokens",
                   return_value=renewed):
            self._tick(SPENT, FRESH)

        self.assertEqual(self._stored()["accessToken"], "fresh-token")


class ClaudeKeyFileReconcileTest(RotationTestCase):
    """What happens when the credentials file no longer holds what Codee wrote."""

    def _signed_in(self, label: str) -> ClaudeKeyRotation:
        """A rotation that has already signed the CLI in as ``label``."""
        self._connect(label)
        rotation = self._rotation()
        self._tick(FRESH, rotation=rotation)
        return rotation

    def test_a_token_the_cli_renewed_for_us_is_adopted(self) -> None:
        # The CLI renews mid-run, because Codee hands it the refresh token on
        # purpose. Its copy is the newer one; overwriting it with ours would
        # undo a renewal and leave our refresh token out of step.
        account = self._connect("one@example.com")
        rotation = self._rotation()
        self._tick(FRESH, rotation=rotation)
        credentials.write_credentials("cli-renewed", "cli-refresh",
                                      _in(LONG_LIFE_MS))

        with patch("codee.lib.claude_key_rotation.fetch_account",
                   return_value="one@example.com"):
            self._tick(FRESH, rotation=rotation)

        self.assertEqual(self._stored()["accessToken"], "cli-renewed")
        self.assertEqual(self._account(account).access_token, "cli-renewed")
        self.assertEqual(self._account(account).refresh_token, "cli-refresh")

    def test_a_token_belonging_to_somebody_else_is_replaced(self) -> None:
        # Somebody ran `claude auth login` on the machine. With rotation on,
        # this file is Codee's — otherwise the agent quietly runs on an account
        # the dashboard has no idea about.
        rotation = self._signed_in("one@example.com")
        credentials.write_credentials("someone-elses", "their-refresh",
                                      _in(LONG_LIFE_MS))

        with patch("codee.lib.claude_key_rotation.fetch_account",
                   return_value="someone@else.com"):
            self._tick(FRESH, rotation=rotation)

        self.assertEqual(self._stored()["accessToken"],
                         "token-for-one@example.com")

    def test_a_token_nobody_can_identify_is_replaced(self) -> None:
        # Ours is the one known to be right for the account rotation chose, so
        # an unidentifiable file loses to it.
        rotation = self._signed_in("one@example.com")
        credentials.write_credentials("mystery", "", 0)

        with patch("codee.lib.claude_key_rotation.fetch_account",
                   side_effect=AccountUnavailable("rejected")):
            self._tick(FRESH, rotation=rotation)

        self.assertEqual(self._stored()["accessToken"],
                         "token-for-one@example.com")

    def test_an_emptied_file_is_signed_back_in_without_asking_anyone(self) -> None:
        rotation = self._signed_in("one@example.com")
        self.credentials_file.write_text("{}")

        with patch("codee.lib.claude_key_rotation.fetch_account") as whose:
            self._tick(FRESH, rotation=rotation)

        whose.assert_not_called()
        self.assertEqual(self._stored()["accessToken"],
                         "token-for-one@example.com")

    def test_an_unchanged_file_costs_no_round_trip(self) -> None:
        # The common case by far, and it must stay free: a check that asked who
        # the file belongs to every five minutes would be a network call per
        # tick for an answer that never changes.
        rotation = self._signed_in("one@example.com")

        with patch("codee.lib.claude_key_rotation.fetch_account") as whose:
            self._tick(FRESH, rotation=rotation)

        whose.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ClaudeKeyIdleRenewalTest(RotationTestCase):
    """Keeping the accounts waiting their turn from expiring while they wait."""

    def _renewed(self) -> oauth.Tokens:
        """What a renewal hands back. Built now, not at import: the window it
        carries has to be measurably later than the one the account already
        holds, and a class attribute would have been computed first."""
        return oauth.Tokens("fresh-token", "fresh-refresh", _in(LONG_LIFE_MS),
                            refresh_expires_at=_in(REFRESH_LIFE_MS * 2))

    def test_an_account_idle_for_days_is_renewed_though_nothing_needs_it(self) -> None:
        # The failure this exists to prevent: a spare subscription sits
        # untouched past its refresh window and is dead on exactly the day the
        # others run out and it is finally needed.
        self._connect("one@example.com")
        second = self._connect("two@example.com")
        self._idle_since(second, SIX_DAYS_MS)

        with patch("codee_agent_claude_code.oauth.refresh_tokens",
                   return_value=self._renewed()) as refresh:
            self._tick(FRESH)

        refresh.assert_called_once_with("refresh-for-two@example.com")
        self.assertEqual(self._account(second).access_token, "fresh-token")

    def test_renewing_an_idle_account_pushes_its_refresh_window_out(self) -> None:
        # The point of the exercise: every renewal extends the window, so an
        # account touched every few days never reaches the end of it.
        self._connect("one@example.com")
        second = self._connect("two@example.com")
        self._idle_since(second, SIX_DAYS_MS)
        was = self._account(second).refresh_expires_at

        with patch("codee_agent_claude_code.oauth.refresh_tokens",
                   return_value=self._renewed()):
            self._tick(FRESH)

        self.assertGreater(self._account(second).refresh_expires_at, was)

    def test_an_account_touched_recently_is_left_alone(self) -> None:
        self._connect("one@example.com")
        self._connect("two@example.com")

        with patch("codee_agent_claude_code.oauth.refresh_tokens") as refresh:
            self._tick(FRESH)

        refresh.assert_not_called()

    def test_the_account_in_use_is_not_renewed_by_this_pass(self) -> None:
        # It looks after itself: it is renewed when its own token nears expiry,
        # and the CLI renews it mid-run besides.
        first = self._connect("one@example.com")
        self._idle_since(first, SIX_DAYS_MS)

        with patch("codee_agent_claude_code.oauth.refresh_tokens") as refresh:
            self._tick(FRESH)

        refresh.assert_not_called()

    def test_an_idle_renewal_that_cannot_be_made_changes_nothing(self) -> None:
        self._connect("one@example.com")
        second = self._connect("two@example.com")
        self._idle_since(second, SIX_DAYS_MS)

        with patch("codee_agent_claude_code.oauth.refresh_tokens",
                   side_effect=oauth.OAuthApiError("no route")):
            self._tick(FRESH)

        self.assertFalse(self._account(second).needs_reconnect(_in(0)))


class ClaudeKeyRetirementTest(RotationTestCase):
    """An account whose refresh token is finished, and what rotation does about it."""

    def test_a_refused_renewal_retires_the_account(self) -> None:
        # The API saying the refresh token is finished is the only reliable
        # signal there is; without acting on it the executor would retry a dead
        # account every five minutes and never say so.
        self._connect("one@example.com")
        second = self._connect("two@example.com")
        self._idle_since(second, SIX_DAYS_MS)

        with patch("codee_agent_claude_code.oauth.refresh_tokens",
                   side_effect=oauth.OAuthRefused("invalid_grant")):
            self._tick(FRESH)

        self.assertTrue(self._account(second).needs_reconnect(_in(0)))

    def test_a_retired_account_is_not_rotated_to(self) -> None:
        # Nothing to sign in with, so choosing it would hand the CLI a dead
        # token and cost a poll to find out.
        self._connect("one@example.com")
        second = self._connect("two@example.com", refresh_expires_in_ms=-1000)
        third = self._connect("three@example.com")

        self._tick(SPENT, FRESH)

        self.assertEqual(
            claude_code_accounts.current_account_id(self.context), third)
        self.assertNotEqual(
            claude_code_accounts.current_account_id(self.context), second)

    def test_a_retired_account_is_not_renewed_again_and_again(self) -> None:
        self._connect("one@example.com")
        second = self._connect("two@example.com", refresh_expires_in_ms=-1000)
        self._idle_since(second, SIX_DAYS_MS)

        with patch("codee_agent_claude_code.oauth.refresh_tokens") as refresh:
            self._tick(FRESH)

        refresh.assert_not_called()

    def test_an_account_with_no_known_window_is_never_retired(self) -> None:
        # Zero means the API never said, which is not the same as expired —
        # retiring on it would tell the user to reconnect something that works.
        account = self._connect("one@example.com", refresh_expires_in_ms=0)
        claude_code_accounts.update_tokens(
            account, "t", "r", _in(LONG_LIFE_MS), self.context)

        with closing(get_db_connection(self.context)) as conn, conn:
            conn.execute("UPDATE claude_code_account SET refresh_expires_at = 0"
                         " WHERE id = ?", (account,))

        self.assertFalse(self._account(account).needs_reconnect(_in(0)))


class ClaudeKeyRestartTest(RotationTestCase):
    """What a fresh executor does with a credentials file it did not write.

    The dangerous case, because a restarting process has written nothing and so
    knows nothing: Claude Code renews Codee's token by itself — Codee hands it
    the refresh token on purpose — so the file on disk is very often newer than
    what Codee last stored.
    """

    def _restart(self) -> ClaudeKeyRotation:
        """A rotation as a freshly started executor has it: nothing signed in."""
        return ClaudeKeyRotation(self.context)

    def test_a_newer_token_for_the_same_account_is_not_overwritten(self) -> None:
        # The bug this guards: overwriting would push Claude Code back onto an
        # older access token and an older refresh token — one Anthropic may
        # already have replaced, leaving neither side able to renew.
        account = self._connect("one@example.com")
        credentials.write_credentials("cli-renewed", "cli-refresh",
                                      _in(LONG_LIFE_MS))

        with patch("codee.lib.claude_key_rotation.fetch_account",
                   return_value="one@example.com"):
            self._tick(FRESH, rotation=self._restart())

        self.assertEqual(self._stored()["accessToken"], "cli-renewed")
        self.assertEqual(self._stored()["refreshToken"], "cli-refresh")

    def test_the_newer_token_is_taken_into_the_account_too(self) -> None:
        # Otherwise the next switch away and back would sign in with the stale
        # pair all over again.
        account = self._connect("one@example.com")
        credentials.write_credentials("cli-renewed", "cli-refresh",
                                      _in(LONG_LIFE_MS))

        with patch("codee.lib.claude_key_rotation.fetch_account",
                   return_value="one@example.com"):
            self._tick(FRESH, rotation=self._restart())

        self.assertEqual(self._account(account).access_token, "cli-renewed")
        self.assertEqual(self._account(account).refresh_token, "cli-refresh")

    def test_a_token_belonging_to_somebody_else_is_still_replaced(self) -> None:
        # Adopting must not become "whatever is in the file wins": a machine
        # signed in as another account has to be put back on rotation's.
        self._connect("one@example.com")
        credentials.write_credentials("someone-elses", "their-refresh",
                                      _in(LONG_LIFE_MS))

        with patch("codee.lib.claude_key_rotation.fetch_account",
                   return_value="someone@else.com"):
            self._tick(FRESH, rotation=self._restart())

        self.assertEqual(self._stored()["accessToken"],
                         "token-for-one@example.com")

    def test_an_adopted_token_near_expiry_is_renewed_before_use(self) -> None:
        # Adopting hands back whatever the file held; if that is minutes from
        # expiring, an agent launched by this very tick would inherit it.
        self._connect("one@example.com")
        credentials.write_credentials("cli-renewed", "cli-refresh", _in(1000))
        renewed = oauth.Tokens("fresher-token", "fresher-refresh",
                               _in(LONG_LIFE_MS))

        with patch("codee.lib.claude_key_rotation.fetch_account",
                   return_value="one@example.com"), \
                patch("codee_agent_claude_code.oauth.refresh_tokens",
                      return_value=renewed):
            self._tick(FRESH, rotation=self._restart())

        self.assertEqual(self._stored()["accessToken"], "fresher-token")

    def test_a_stale_stored_token_is_renewed_rather_than_written_back(self) -> None:
        # Codee was down long enough for its own copy to expire and the file to
        # be emptied. Writing the dead token back would leave the CLI signed
        # out until something else noticed.
        self._connect("one@example.com", expires_in_ms=-1000)
        renewed = oauth.Tokens("fresh-token", "fresh-refresh", _in(LONG_LIFE_MS))

        with patch("codee_agent_claude_code.oauth.refresh_tokens",
                   return_value=renewed):
            self._tick(FRESH, rotation=self._restart())

        self.assertEqual(self._stored()["accessToken"], "fresh-token")

    def test_an_untouched_file_costs_no_round_trip_across_a_restart(self) -> None:
        # The common restart: nothing else ran, the file still holds exactly
        # what Codee stored. It must not cost a lookup to discover that.
        self._connect("one@example.com")
        self._tick(FRESH, rotation=self._restart())

        with patch("codee.lib.claude_key_rotation.fetch_account") as whose:
            self._tick(FRESH, rotation=self._restart())

        whose.assert_not_called()

    def test_rotating_to_another_account_still_overwrites_outright(self) -> None:
        # The switch is deliberate; whoever the file names is being replaced on
        # purpose, and asking who they are would waste a round trip.
        self._connect("one@example.com")
        second = self._connect("two@example.com")
        rotation = self._restart()

        with patch("codee.lib.claude_key_rotation.fetch_account") as whose:
            self._tick(SPENT, FRESH, rotation=rotation)

        whose.assert_not_called()
        self.assertEqual(self._stored()["accessToken"],
                         "token-for-two@example.com")
        self.assertEqual(
            claude_code_accounts.current_account_id(self.context), second)
