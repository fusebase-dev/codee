"""How much of a Claude Code subscription key's allowance is already spent.

A subscription is metered in two windows — a rolling session ("5-hour") one and
a weekly one — and the ``claude`` CLI exits non-zero once either is used up.
Waiting for that exit is too late to be useful: the run that hit the limit has
already been lost. Asking the account's own usage endpoint instead is what lets
the executor move to the next key before a task is handed to a key that cannot
run it.

There is no CLI command for this, so the OAuth endpoint the CLI itself reports
usage from is called directly.
"""
from dataclasses import dataclass

from codee_agent_claude_code import oauth
from codee_agent_claude_code.oauth import OAUTH_BETA, TIMEOUT_SECONDS
from codee_main_context.logging import get_logger

log = get_logger(__name__)

USAGE_PATH = "/api/oauth/usage"

# The two windows a subscription is metered in, as the response names them.
# Anything else it reports — per-model weeklies, extra-usage credits — is
# deliberately ignored: those narrow a single model or cost money rather than
# stopping the key, and rotating on one would burn through the list for a limit
# the agent never actually hit.
SESSION_WINDOW = "five_hour"
WEEKLY_WINDOW = "seven_day"
WINDOWS = (SESSION_WINDOW, WEEKLY_WINDOW)

# What counts as spent. The endpoint reports a percentage, and the CLI starts
# refusing at 100.
LIMIT_PERCENT = 100.0


class UsageUnavailable(Exception):
    """The account's usage could not be read, so nothing can be said about it.

    A network blip or a 500 from the endpoint. Distinct from a key that is out
    of allowance, because the answer for the caller is the opposite one: keep
    using the key and ask again next time, rather than rotate away from it.
    """


@dataclass(frozen=True)
class Usage:
    """What one access key has left, as far as rotation is concerned."""

    # True when the key cannot run anything right now: a window is used up, the
    # account is locked, or the endpoint rejected the key outright.
    limited: bool
    # Why, in a few words, for the log line that records a rotation.
    reason: str = ""
    # Percent used of each window this endpoint reported, keyed by window name.
    windows: dict[str, float] = None  # type: ignore[assignment]
    # When each window next resets, as the endpoint words it, keyed the same
    # way. Only for showing a human when their allowance comes back — nothing
    # decides anything on it, so a window that reports no reset is simply
    # absent rather than a problem.
    resets_at: dict[str, str] = None  # type: ignore[assignment]

    def describe(self) -> str:
        """The windows as a log fragment, e.g. ``session 100%, weekly 70%``."""
        labels = {SESSION_WINDOW: "session", WEEKLY_WINDOW: "weekly"}
        return ", ".join(f"{labels.get(window, window)} {percent:g}%"
                         for window, percent in (self.windows or {}).items())


def fetch_usage(access_token: str, timeout: int = TIMEOUT_SECONDS) -> Usage:
    """Ask Anthropic how much of ``access_token``'s allowance is left.

    Raises :class:`UsageUnavailable` when the question could not be answered.
    A key the endpoint *rejects* is not that case: an expired or revoked key is
    as unusable as an exhausted one, and reported as limited so the executor
    rotates off it.
    """
    try:
        response = oauth.get(USAGE_PATH, access_token, timeout)
    except oauth.OAuthApiError as error:
        raise UsageUnavailable(str(error)) from error

    if response.status_code in (401, 403):
        return Usage(limited=True, reason="the key was rejected "
                                          f"(HTTP {response.status_code})",
                     windows={}, resets_at={})
    if not response.ok:
        raise UsageUnavailable(
            f"HTTP {response.status_code}: {response.text[:200]}")
    return read_usage(response.payload)


def read_usage(payload: dict) -> Usage:
    """Turn one usage response into the verdict rotation needs.

    Split out from the request so the shape of the response can be tested
    without one, and because the shape is the part that moves: the endpoint has
    grown fields over time, and a window it stops reporting must read as "not
    limited" rather than as an error that pins the executor to a spent key.
    """
    windows: dict[str, float] = {}
    resets_at: dict[str, str] = {}
    reasons: list[str] = []
    for window in WINDOWS:
        section = payload.get(window)
        if not isinstance(section, dict):
            continue
        percent = _percent(section.get("utilization"))
        if percent is not None:
            windows[window] = percent
            if percent >= LIMIT_PERCENT:
                reasons.append(f"{window} at {percent:g}%")
        resets = section.get("resets_at")
        if isinstance(resets, str) and resets:
            resets_at[window] = resets
        # Set while an account is suspended or over a spend cap, which stops
        # the key just as firmly as a full window does.
        locked = section.get("locked_reason")
        if locked:
            reasons.append(f"{window} locked ({locked})")
    return Usage(limited=bool(reasons), reason="; ".join(reasons),
                 windows=windows, resets_at=resets_at)


def _percent(value: object) -> float | None:
    """One utilization figure as a number, or None when it isn't one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
