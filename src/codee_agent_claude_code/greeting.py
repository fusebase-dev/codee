"""A one-word conversation that keeps a subscription counted as in use.

Renewing an account's tokens and asking its usage endpoint both reach
Anthropic, and neither turned out to be enough: subscriptions that only ever
saw those requests still expired while they waited their turn. What a used
account does that those don't is run inference, so an idle one is sent a real
Messages request — the smallest there is, on the cheapest model, as the CLI
itself would send it.
"""
import requests

from codee_agent_claude_code.oauth import API_BASE, OAUTH_BETA, TIMEOUT_SECONDS

MESSAGES_PATH = "/v1/messages"
API_VERSION = "2023-06-01"
# The cheapest model a subscription can run, so a greeting costs next to
# nothing out of the session window it lands in.
MODEL = "claude-haiku-4-5"
# A subscription token is only accepted for Claude Code's own requests, which
# open with this system prompt.
SYSTEM_PROMPT = "You are Claude Code, Anthropic's official CLI for Claude."
GREETING = "Hi"
MAX_TOKENS = 8


class GreetingFailed(Exception):
    """The greeting was not answered, so it did nothing for the account."""


def send_greeting(access_token: str, timeout: int = TIMEOUT_SECONDS) -> None:
    """Say hello as ``access_token``'s owner, and raise if nobody answered."""
    try:
        response = requests.post(
            f"{API_BASE}{MESSAGES_PATH}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "anthropic-beta": OAUTH_BETA,
                "anthropic-version": API_VERSION,
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": GREETING}],
            },
            timeout=timeout,
        )
    except requests.RequestException as error:
        raise GreetingFailed(f"{type(error).__name__}: {error}") from error
    if response.status_code != 200:
        raise GreetingFailed(
            f"HTTP {response.status_code}: {response.text.strip()[:200]}")
