"""Whose Claude account one access key belongs to.

A list of masked keys says nothing about which subscription each one is — and
that is the only thing anyone needs to know when looking at them, because the
whole point of the list is that each key is a different account. The keys carry
no readable identity of their own, so the account has to be asked for.

Asked once. The answer cannot change: a key belongs to the account it was
issued for, for as long as it exists.
"""
from codee_agent_claude_code import oauth
from codee_main_context.logging import get_logger

log = get_logger(__name__)

PROFILE_PATH = "/api/oauth/profile"


class AccountUnavailable(Exception):
    """The account behind a key could not be read, so there is nothing to cache.

    Covers both a question that could not be asked (a network failure) and a
    key the API refused. Neither is recorded: a label that is missing is drawn
    as missing and asked for again, which is the behaviour that gets a key
    added while the machine was offline labelled on the next visit.
    """


def fetch_account(access_token: str) -> str:
    """The account label for one key, e.g. ``name@example.com``.

    Raises :class:`AccountUnavailable` when there is no answer to show.
    """
    try:
        response = oauth.get(PROFILE_PATH, access_token)
    except oauth.OAuthApiError as error:
        raise AccountUnavailable(str(error)) from error

    # The body is carried through on a refusal as well as on a failure: a key
    # rejected for want of a scope and one that has been revoked both come back
    # as 401, and only the body says which — so it has to reach the log line
    # and the row's tooltip, or there is nothing to act on.
    if not response.ok:
        detail = f": {response.text[:200]}" if response.text else ""
        raise AccountUnavailable(
            f"the key was rejected (HTTP {response.status_code}){detail}"
            if response.status_code in (401, 403)
            else f"HTTP {response.status_code}{detail}")

    label = read_account(response.payload)
    if not label:
        raise AccountUnavailable("the profile named no account")
    return label


def read_account(payload: dict) -> str:
    """The label to show for one profile response, or ``""`` if it names none.

    The email first, because that is what tells two subscriptions of the same
    person apart, and what they are signed in as. The names and the
    organization are fallbacks for a response that carries no email — better a
    label that is merely vague than a row that looks like a failed lookup.
    """
    account = payload.get("account")
    account = account if isinstance(account, dict) else {}
    organization = payload.get("organization")
    organization = organization if isinstance(organization, dict) else {}
    for candidate in (account.get("email"), account.get("display_name"),
                      account.get("full_name"), organization.get("name")):
        label = str(candidate or "").strip()
        if label:
            return label
    return ""
