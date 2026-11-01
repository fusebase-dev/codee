"""Signing in to Microsoft Graph as a user, and keeping the token fresh.

Delegated access: the plugin reads exactly what the signed-in account can
read, so the account must be a member of every team whose channels a skill
watches. ``ChannelMessage.Read.All`` needs an admin's consent in the tenant.

Tokens live in ``<data_dir>/token.json``, written atomically because the admin
(signing in) and the executor (refreshing) both write it.
"""
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

from codee_plugin_abstract.plugin import SignInError

SCOPES = ("offline_access User.Read User.ReadBasic.All "
          "ChannelMessage.Read.All Team.ReadBasic.All Channel.ReadBasic.All")
GRAPH = "https://graph.microsoft.com/v1.0"
# Any work or school account; set the tenant ID to pin one directory.
DEFAULT_TENANT = "organizations"
# Refresh this long before expiry, so a token can't lapse mid-tick.
EXPIRY_MARGIN = timedelta(minutes=2)
TIMEOUT = 30


class SignedOut(RuntimeError):
    """No usable token: someone has to sign in on the Plugins page again."""


def _authority(settings: dict[str, str]) -> str:
    tenant = settings.get("tenant_id", "").strip() or DEFAULT_TENANT
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


def _client(settings: dict[str, str]) -> tuple[str, str]:
    client_id = settings.get("client_id", "").strip()
    secret = settings.get("client_secret", "").strip()
    if not client_id or not secret:
        raise SignInError("Fill in the client ID and client secret, then "
                          "sign in.")
    return client_id, secret


def authorization_url(settings: dict[str, str], redirect_uri: str,
                      state: str, code_verifier: str) -> str:
    client_id, _ = _client(settings)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
    return f"{_authority(settings)}/authorize?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })


class TokenStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "token.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, tokens: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(tokens, indent=2))
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def account(self) -> dict:
        """The signed-in user: ``id``, ``name`` and ``email``; {} if none."""
        return self.load().get("account") or {}

    def complete(self, settings: dict[str, str], redirect_uri: str, code: str,
                 code_verifier: str) -> dict:
        """Exchange an authorization code; store and return the account."""
        tokens = self._request(settings, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        })
        me = requests.get(f"{GRAPH}/me", timeout=TIMEOUT, headers={
            "Authorization": f"Bearer {tokens['access_token']}"})
        if not me.ok:
            raise SignInError(f"Signed in, but /me failed: {me.status_code}")
        profile = me.json()
        account = {"id": profile.get("id", ""),
                   "name": profile.get("displayName", ""),
                   "email": profile.get("mail")
                   or profile.get("userPrincipalName", "")}
        self.save({**self._stamped(tokens), "account": account})
        return account

    def access_token(self, settings: dict[str, str]) -> str:
        """A valid access token, refreshed when it is about to expire."""
        stored = self.load()
        if not stored.get("refresh_token"):
            raise SignedOut("Teams is not signed in; sign in on the Plugins "
                            "page.")
        expires = datetime.fromisoformat(stored["expires_at"])
        if expires - EXPIRY_MARGIN > datetime.now(timezone.utc):
            return stored["access_token"]
        try:
            tokens = self._request(settings, {
                "grant_type": "refresh_token",
                "refresh_token": stored["refresh_token"],
            })
        except SignInError as exc:
            raise SignedOut(f"Teams sign-in expired ({exc}); sign in again "
                            "on the Plugins page.") from exc
        # A refresh may or may not hand out a new refresh token.
        tokens.setdefault("refresh_token", stored["refresh_token"])
        self.save({**self._stamped(tokens), "account": stored.get("account")})
        return tokens["access_token"]

    def _request(self, settings: dict[str, str], form: dict) -> dict:
        client_id, secret = _client(settings)
        response = requests.post(f"{_authority(settings)}/token", timeout=TIMEOUT,
                                 data={**form, "client_id": client_id,
                                       "client_secret": secret,
                                       "scope": SCOPES})
        body = response.json() if response.content else {}
        if not response.ok or "access_token" not in body:
            description = (body.get("error_description") or body.get("error")
                           or f"HTTP {response.status_code}")
            raise SignInError(description.splitlines()[0])
        return body

    @staticmethod
    def _stamped(tokens: dict) -> dict:
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(tokens.get("expires_in", 3600)))
        return {"access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", ""),
                "expires_at": expires_at.isoformat()}
