"""Signing in to a Claude account, and asking the API about the result.

Codee connects an account the same way ``claude /login`` does: the OAuth flow
below, against Anthropic's own Claude Code client. That matters beyond
convenience — a token minted by ``claude setup-token`` carries ``user:inference``
and nothing else, so it cannot be asked whose account it is or how much of its
allowance is left, which are exactly the two questions rotation is built on.
A session token from this flow carries both scopes.

Everything that talks to that API lives here: the sign-in itself, the refresh
that keeps a connected account usable, and the plain GET the account and usage
lookups are built from.
"""
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import requests

API_BASE = "https://api.anthropic.com"
# These endpoints are part of the OAuth surface rather than the Messages API,
# and refuse a request that doesn't opt into it.
OAUTH_BETA = "oauth-2025-04-20"
TIMEOUT_SECONDS = 30


class OAuthApiError(Exception):
    """The request could not be made, or came back as something unreadable.

    Not the same as a request that was answered with a refusal: a 401 is an
    answer, and each caller reads it its own way.
    """


class OAuthRefused(OAuthApiError):
    """The API answered, and the answer was no.

    Split out because the two failures mean opposite things to a caller holding
    a refresh token. A request that never arrived says nothing about the
    credential and is worth retrying; a refusal says the credential is finished,
    and retrying it every five minutes for the rest of the process is how an
    account that needs reconnecting stays silently broken instead.
    """


@dataclass(frozen=True)
class Response:
    """One answer from the OAuth API: its status, and the body when there is one."""

    status_code: int
    payload: dict
    text: str

    @property
    def ok(self) -> bool:
        return self.status_code < 400


def get(path: str, access_token: str,
        timeout: int = TIMEOUT_SECONDS) -> Response:
    """GET one OAuth endpoint as ``access_token``'s owner.

    Raises :class:`OAuthApiError` when there is no answer to read at all — a
    network failure, or a success whose body is not a JSON object. A refusal
    comes back as a Response for the caller to interpret, because what a 401
    means depends on what was being asked.
    """
    try:
        response = requests.get(
            f"{API_BASE}{path}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "anthropic-beta": OAUTH_BETA,
                "Accept": "application/json",
            },
            timeout=timeout,
        )
    except requests.RequestException as error:
        raise OAuthApiError(f"{type(error).__name__}: {error}") from error

    text = response.text.strip()
    if response.status_code >= 400:
        return Response(response.status_code, {}, text)

    try:
        payload = response.json()
    except ValueError as error:
        raise OAuthApiError(f"unreadable response: {error}") from error
    if not isinstance(payload, dict):
        raise OAuthApiError("unreadable response: not an object")
    return Response(response.status_code, payload, text)


# The sign-in flow, as the `claude` CLI runs it for `/login`. Codee drives the
# same flow against the same OAuth client so that a connected account yields
# exactly the credential the CLI would have written for itself — a real session
# token with the scopes below, rather than the inference-only token
# `claude setup-token` mints, which cannot be asked whose account it is and
# cannot be asked how much allowance it has left.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# The redirect for the copy-the-code flow: Anthropic prints the authorization
# code on this page instead of sending it to a server. Codee has no callback
# URL Anthropic would accept and the machine running it may have no browser at
# all, so this is the only flow that can work here.
MANUAL_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = ("user:profile", "user:inference", "user:sessions:claude_code",
          "user:mcp_servers")


@dataclass(frozen=True)
class Tokens:
    """One connected account's credentials, as the token endpoint returns them."""

    access_token: str
    refresh_token: str
    # Milliseconds since the epoch, matching how the CLI's credentials file
    # writes it — the caller's only job is to pass it through.
    expires_at: int
    # When the refresh token itself runs out, in milliseconds too. This is the
    # one that ends an account rather than merely ageing its token: past it,
    # nothing can be renewed and the account has to be connected again. Zero
    # when the response did not say, which reads as "unknown", never as "now".
    refresh_expires_at: int = 0
    scopes: tuple[str, ...] = ()
    subscription_type: str = ""


@dataclass(frozen=True)
class Authorization:
    """A sign-in waiting for the user to come back with a code.

    The verifier and the state have to survive from the moment the URL is
    opened to the moment the code is pasted, and neither may be shown: the
    verifier is what proves the code was redeemed by whoever asked for it.
    """

    url: str
    state: str
    code_verifier: str


def start_authorization() -> Authorization:
    """Build the URL to open, and the PKCE secrets its code will be redeemed with."""
    code_verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    query = urlencode({
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": MANUAL_REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    return Authorization(f"{AUTHORIZE_URL}?{query}", state, code_verifier)


def split_code(pasted: str) -> tuple[str, str]:
    """The code and the state out of what the user pasted back.

    Anthropic prints them joined as ``code#state``, and that whole string is
    what gets copied — so accept it as it comes, and accept a bare code too for
    anyone who copied only the first half.
    """
    pasted = str(pasted or "").strip()
    code, _, state = pasted.partition("#")
    return code.strip(), state.strip()


def exchange_code(code: str, state: str, code_verifier: str,
                  timeout: int = TIMEOUT_SECONDS) -> Tokens:
    """Redeem an authorization code for one account's tokens."""
    return _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": MANUAL_REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
        "state": state,
    }, timeout)


def refresh_tokens(refresh_token: str,
                   timeout: int = TIMEOUT_SECONDS) -> Tokens:
    """Trade a refresh token for a fresh access token.

    A session access token is good for hours, not months, so this is what keeps
    a connected account usable — and what has to happen before an account is
    handed to the CLI after sitting unused while another one was in rotation.
    """
    return _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "scope": " ".join(SCOPES),
    }, timeout)


def _post_token(payload: dict, timeout: int) -> Tokens:
    """POST to the token endpoint and read the tokens out of the answer."""
    try:
        response = requests.post(
            TOKEN_URL, json=payload,
            headers={"Content-Type": "application/json"}, timeout=timeout)
    except requests.RequestException as error:
        raise OAuthApiError(f"{type(error).__name__}: {error}") from error

    if response.status_code == 401:
        # The one failure worth naming: it is what a code that was mistyped,
        # reused or left to expire comes back as, and "401" alone would send
        # the user looking for a problem with their account.
        raise OAuthRefused(
            "the authorization code was refused — it can only be used once, "
            "and expires within a few minutes of being shown")
    if 400 <= response.status_code < 500:
        # Refused rather than unreachable: the credential is finished, and the
        # caller has to say so rather than keep retrying it.
        raise OAuthRefused(
            f"HTTP {response.status_code}: {response.text.strip()[:200]}")
    if response.status_code != 200:
        raise OAuthApiError(
            f"HTTP {response.status_code}: {response.text.strip()[:200]}")

    try:
        data = response.json()
    except ValueError as error:
        raise OAuthApiError(f"unreadable response: {error}") from error
    if not isinstance(data, dict):
        raise OAuthApiError("unreadable response: not an object")

    access_token = str(data.get("access_token") or "")
    if not access_token:
        raise OAuthApiError("the response carried no access token")
    return Tokens(
        access_token=access_token,
        refresh_token=str(data.get("refresh_token") or ""),
        expires_at=_expires_at(data.get("expires_in")),
        refresh_expires_at=_expires_at(data.get("refresh_token_expires_in")),
        scopes=tuple(str(data.get("scope") or "").split()),
        subscription_type=str(data.get("subscription_type") or ""),
    )


def _expires_at(expires_in: object) -> int:
    """``expires_in`` seconds turned into the millisecond stamp the CLI stores.

    Zero when the response named no lifetime. For the access token that reads
    as "already expired", so the next use renews rather than trusting a token
    of unknown age; for the refresh token it reads as "unknown", which is the
    only safe answer — treating it as expired would retire a working account.
    """
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
        return 0
    return int((time.time() + float(expires_in)) * 1000)
