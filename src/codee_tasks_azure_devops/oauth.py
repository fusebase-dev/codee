"""Entra ID authorization-code flow for Azure DevOps, with automatic refresh.

Scope note: Azure DevOps publishes exactly one delegated permission through an
Entra ID app registration — ``user_impersonation``. There is no read-only
variant to ask for (the granular ``vso.work`` family only exists in the legacy,
now-deprecated Azure DevOps OAuth app model). Read-only access is therefore
enforced on our side: this package only ever issues reads — GETs and WIQL
queries — and never a create, update, or transition. Narrow it further on the
Azure DevOps side by authorizing with an account that has Readers access.

The flow is confidential-client: the code is exchanged for tokens on the
backend using the app's client secret, so the secret never reaches the browser.
PKCE is layered on top even though a secret is used, which keeps an intercepted
code useless on its own.
"""
import base64
import hashlib
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

import requests
from codee_database import oauth_tokens
from codee_main_context.context import CodeeMainContext, Settings, TasksProvider

# Fixed Entra ID application ID of the Azure DevOps resource. Same value in
# every tenant; it is what makes a token usable against dev.azure.com.
AZURE_DEVOPS_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"
SCOPE = f"{AZURE_DEVOPS_RESOURCE_ID}/user_impersonation offline_access"

PROVIDER = TasksProvider.AZURE_DEVOPS.value

# Path the admin UI serves the callback on. Registered verbatim (behind the
# admin host) as a redirect URI on the Entra app; Entra matches it exactly.
CALLBACK_PATH = "/api/oauth/azure-devops/callback"

# Refresh this far ahead of the stated expiry, so a token can't lapse midway
# through a request that already passed the check.
EXPIRY_MARGIN = timedelta(seconds=120)

# Used when no directory is configured: covers any work/school account, which
# is the only kind Azure DevOps organizations are backed by.
DEFAULT_TENANT = "organizations"

_TIMEOUT = 30


class AzureDevOpsAuthError(RuntimeError):
    """Authorization failed.

    ``terminal`` separates "this refresh token is dead, the user must consent
    again" from "this attempt failed, the next one may not" — an unreachable
    Entra, a 5xx, a throttle, or a client secret that needs correcting in
    Settings. Only a terminal failure justifies discarding a refresh token that
    might still be worth 90 days of unattended operation.
    """

    def __init__(self, message: str, terminal: bool = False):
        super().__init__(message)
        self.terminal = terminal


@dataclass(frozen=True)
class OAuthConfig:
    """The Entra app registration, as captured in settings.json.

    There is no project here: queries run across the whole organization. A
    project would only ever have been a filter — the access Entra grants covers
    the organization however it is set.
    """

    organization_url: str = ""
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""

    @classmethod
    def from_settings(cls, settings: Settings) -> "OAuthConfig":
        creds = settings.credentials.get(PROVIDER, {})
        return cls(
            organization_url=(creds.get("organization_url") or "").strip().rstrip("/"),
            tenant_id=(creds.get("tenant_id") or "").strip(),
            client_id=(creds.get("client_id") or "").strip(),
            client_secret=(creds.get("client_secret") or "").strip(),
        )

    def is_complete(self) -> bool:
        """Whether we have everything needed to run the flow and query tasks."""
        return bool(self.organization_url and self.client_id
                    and self.client_secret)

    @property
    def organization(self) -> str:
        """The organization's bare name, as the Azure DevOps tooling takes it.

        ``https://dev.azure.com/contoso`` and the legacy
        ``https://contoso.visualstudio.com`` both name ``contoso``. Which form
        the user configured is their business, so a caller that needs the name
        rather than the URL doesn't have to care which one it got.
        """
        parsed = urlparse(self.organization_url)
        path = parsed.path.strip("/")
        if path:
            return path.split("/")[0]
        # No path: an ``<org>.visualstudio.com`` host, or one typed bare.
        return (parsed.netloc or parsed.path).split(".")[0]

    @property
    def tenant(self) -> str:
        return self.tenant_id or DEFAULT_TENANT

    @property
    def authorize_endpoint(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"


def new_state() -> str:
    """Opaque value tying the callback back to the request that started it."""
    return secrets.token_urlsafe(32)


def new_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def code_challenge_for(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorization_url(
    config: OAuthConfig,
    redirect_uri: str,
    state: str,
    code_verifier: str,
) -> str:
    """The Entra URL to send the browser to for consent."""
    query = urlencode({
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SCOPE,
        "state": state,
        "code_challenge": code_challenge_for(code_verifier),
        "code_challenge_method": "S256",
        # Force account selection: the admin may well be signed into a personal
        # account that has no access to the organization.
        "prompt": "select_account",
    })
    return f"{config.authorize_endpoint}?{query}"


def exchange_code(
    config: OAuthConfig,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> dict:
    """Trade an authorization code for access + refresh tokens."""
    return _post_token(config, {
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "scope": SCOPE,
    })


def refresh_access_token(config: OAuthConfig, refresh_token: str) -> dict:
    """Trade a refresh token for a fresh access token (and usually a new refresh token)."""
    return _post_token(config, {
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": SCOPE,
    })


def _post_token(config: OAuthConfig, data: dict) -> dict:
    """POST to the token endpoint and normalize the response.

    Returns ``{access_token, refresh_token, expires_at, scope}`` with
    ``expires_at`` as a UTC ISO timestamp, so callers never have to reason
    about the relative ``expires_in`` they were handed.
    """
    try:
        response = requests.post(
            config.token_endpoint,
            data=data,
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AzureDevOpsAuthError(f"Could not reach Entra ID: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code >= 400 or "access_token" not in payload:
        raise AzureDevOpsAuthError(
            _describe_token_error(response, payload),
            terminal=_is_terminal_token_error(response, payload))

    expires_in = payload.get("expires_in")
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = 3600  # ponytail: no expiry given -> assume the documented default
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(seconds=seconds)).isoformat(),
        "scope": payload.get("scope", ""),
    }


def _is_terminal_token_error(response, payload: dict) -> bool:
    """Whether the stored refresh token can never be redeemed again.

    Only ``invalid_grant`` says that — the grant itself is revoked, expired, or
    consent was withdrawn. Everything else is about this attempt: 5xx and 429
    are Entra having a moment, and ``invalid_client`` means the secret in
    Settings needs fixing, which leaves the refresh token perfectly good.
    """
    if response.status_code >= 500 or response.status_code == 429:
        return False
    return payload.get("error") == "invalid_grant"


def _describe_token_error(response, payload: dict) -> str:
    """Entra's error_description is multi-line with correlation IDs; keep line one."""
    description = str(payload.get("error_description") or "").strip()
    if description:
        return description.splitlines()[0]
    error = payload.get("error")
    return str(error) if error else f"Entra ID returned HTTP {response.status_code}"


def fetch_account(access_token: str) -> str:
    """Best-effort display name of the account that authorized, for the UI.

    A failure here says nothing about the token's usefulness for work items, so
    it degrades to an empty label instead of failing the connection.
    """
    try:
        response = requests.get(
            "https://app.vssps.visualstudio.com/_apis/profile/profiles/me",
            params={"api-version": "7.1"},
            headers={"Authorization": f"Bearer {access_token}",
                     "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        profile = response.json()
    except (requests.RequestException, ValueError):
        return ""
    return str(profile.get("emailAddress") or profile.get("displayName") or "")


def is_expired(expires_at: str | None, now: datetime | None = None) -> bool:
    """Whether a stored expiry has passed, or is close enough to count as passed."""
    if not expires_at:
        return True  # ponytail: unknown expiry -> refresh rather than send a dud token
    try:
        deadline = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline - EXPIRY_MARGIN <= (now or datetime.now(timezone.utc))


class AzureDevOpsAuth:
    """Reads the stored tokens and keeps the access token current.

    One instance per provider instance; the lock keeps the executor's worker
    threads from firing off concurrent refreshes for the same token.
    """

    def __init__(self, config: OAuthConfig, main_context: CodeeMainContext):
        self._config = config
        self._context = main_context
        self._lock = threading.Lock()

    def connection(self) -> dict | None:
        """Stored token row, or None when Azure DevOps was never connected."""
        return oauth_tokens.load_tokens(PROVIDER, main_context=self._context)

    def is_connected(self) -> bool:
        return self.connection() is not None

    def disconnect(self) -> None:
        oauth_tokens.delete_tokens(PROVIDER, main_context=self._context)

    def access_token(self) -> str:
        """A usable access token, refreshing first if the stored one is stale.

        Raises AzureDevOpsAuthError when the user has to reconnect. In that case
        the dead tokens are dropped, so the admin UI reports "not connected"
        instead of showing a connection that can no longer fetch anything.
        """
        with self._lock:
            tokens = self.connection()
            if tokens is None:
                raise AzureDevOpsAuthError(
                    "Azure DevOps is not connected. Connect it in Settings.",
                    terminal=True)
            if not is_expired(tokens["expires_at"]):
                return tokens["access_token"]

            refresh_token = tokens.get("refresh_token")
            if not refresh_token:
                self.disconnect()
                raise AzureDevOpsAuthError(
                    "The Azure DevOps access token expired and no refresh token "
                    "was stored. Reconnect in Settings.", terminal=True)

            try:
                fresh = refresh_access_token(self._config, refresh_token)
            except AzureDevOpsAuthError as exc:
                if not exc.terminal:
                    # Entra was unreachable, throttled, or misconfigured. The
                    # refresh token is untouched and the next poll retries it —
                    # an outage must not cost the user a manual reconsent.
                    raise
                # The grant itself is dead (revoked, or aged past its 90-day
                # window). Retrying it every poll would only hammer Entra.
                self.disconnect()
                raise AzureDevOpsAuthError(
                    f"Azure DevOps authorization expired ({exc}). "
                    "Reconnect in Settings.", terminal=True) from exc

            self.store(fresh, account=tokens.get("account") or "",
                       fallback_refresh_token=refresh_token)
            return fresh["access_token"]

    def store(
        self,
        tokens: dict,
        account: str = "",
        fallback_refresh_token: str | None = None,
    ) -> None:
        """Persist a token response. Keeps the previous refresh token if none came back."""
        oauth_tokens.save_tokens(
            PROVIDER,
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token") or fallback_refresh_token,
            expires_at=tokens.get("expires_at"),
            scope=tokens.get("scope", ""),
            account=account,
            main_context=self._context,
        )
