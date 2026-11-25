"""The OAuth authorization server in front of ``/mcp``.

The SDK's handlers check the protocol (PKCE, redirect URIs, code and client
binding); this supplies storage, the sign-in and the consent page. The sign-in
is the proxy's: the authorize and consent pages stay behind it and read who the
user is from a header it sets. On a public address a request without that
header is refused, so a proxy that lets the pages through fails closed.

Clients are public (PKCE, no secret) and register themselves, so a client's name
proves nothing: the consent page shows where the code will be sent, and asks
every time. Client ID metadata documents are not supported on purpose: they
would have this host fetch any URL a stranger names.
"""
import html
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import anyio
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizeError, RefreshToken,
    RegistrationError, TokenError, construct_redirect_uri)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route, request_response

from codee_plugin_abstract.plugin import PluginHost

from codee_mcp.plugin import DEFAULT_USER_HEADER
from codee_mcp.store import ACCESS_TTL, Store, StoreFull

SCOPE = "mcp"
MCP_PATH = "/mcp"
AUTHORIZE_PATH = "/mcp/oauth/authorize"
CONSENT_PATH = "/mcp/oauth/consent"
TOKEN_PATH = "/mcp/oauth/token"
REGISTER_PATH = "/mcp/oauth/register"
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
LOOPBACK = ("localhost", "127.0.0.1", "::1")
# Schemes a browser would run or open rather than hand to an application.
UNSAFE_SCHEMES = ("javascript", "data", "vbscript", "file", "blob", "about",
                  "view-source", "ws", "wss", "ftp")
LOCAL_USER = "local"
BODY_LIMIT = 64 * 1024

# Set around the SDK's authorize handler, which doesn't pass the request on.
_user: ContextVar[str] = ContextVar("mcp_authorize_user")


class NotSignedIn(Exception):
    pass


@dataclass
class OAuthServer:
    """Storage and policy behind the SDK's handlers."""

    store: Store
    host: PluginHost

    @property
    def issuer(self) -> str:
        return self.host.base_url()

    @property
    def resource(self) -> str:
        return self.issuer + MCP_PATH

    def user(self, request: Request) -> str:
        header = (self.host.settings().get("user_header", "").strip()
                  or DEFAULT_USER_HEADER)
        if name := request.headers.get(header, "").strip().lower():
            return name
        if urlsplit(self.issuer).hostname in LOOPBACK:
            return LOCAL_USER
        raise NotSignedIn(
            f"No {header} header: this page has to be reached through the "
            "sign-in proxy, and the proxy has to send that header.")

    async def get_client(self, client_id: str):
        data = await anyio.to_thread.run_sync(self.store.client, client_id)
        return data and OAuthClientInformationFull.model_validate_json(data)

    async def register_client(self, client: OAuthClientInformationFull):
        for uri in client.redirect_uris or []:
            scheme = uri.scheme.lower()
            if scheme in UNSAFE_SCHEMES or (
                    scheme == "http" and
                    (uri.host or "").strip("[]") not in LOOPBACK):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    f"{uri} must be https, loopback http or an app's scheme")
        client.token_endpoint_auth_method = "none"
        client.client_secret = client.client_secret_expires_at = None
        client.client_name = (client.client_name or "")[:100]
        client.scope = SCOPE
        try:
            await anyio.to_thread.run_sync(
                self.store.add_client, client.client_id,
                client.model_dump_json())
        except StoreFull:
            raise RegistrationError("invalid_client_metadata",
                                    "Too many registered clients") from None

    async def authorize(self, client, params) -> str:
        # A client that sends no resource predates RFC 8707; there is only one.
        if params.resource and params.resource.rstrip("/") not in (
                self.resource, self.issuer):
            raise AuthorizeError("invalid_target",
                                 f"This server protects {self.resource}")
        request_id = await anyio.to_thread.run_sync(
            self.store.add_pending, _user.get(), client.client_id, {
                "state": params.state,
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "explicit": params.redirect_uri_provided_explicitly})
        return (self.issuer + CONSENT_PATH + "?"
                + urlencode({"request": request_id}))

    async def load_authorization_code(self, client, code):
        found = await anyio.to_thread.run_sync(self.store.code, code)
        return found and AuthorizationCode(
            code=code, scopes=[SCOPE], expires_at=found["expires"],
            client_id=found["client_id"],
            code_challenge=found["code_challenge"],
            redirect_uri=found["redirect_uri"],
            redirect_uri_provided_explicitly=found["explicit"],
            resource=self.resource, subject=found["user"])

    async def exchange_authorization_code(self, client, code):
        return self._tokens(await anyio.to_thread.run_sync(
            self.store.exchange_code, code.code))

    async def load_refresh_token(self, client, token):
        found = await anyio.to_thread.run_sync(
            self.store.token, "refresh", token)
        return found and RefreshToken(
            token=token, client_id=found["client_id"], scopes=[SCOPE],
            expires_at=found["expires"], resource=self.resource,
            subject=found["user"])

    async def exchange_refresh_token(self, client, token, scopes):
        return self._tokens(await anyio.to_thread.run_sync(
            self.store.exchange_refresh, token.token))

    @staticmethod
    def _tokens(pair) -> OAuthToken:
        if pair is None:
            raise TokenError("invalid_grant", "already used or expired")
        return OAuthToken(access_token=pair[0], refresh_token=pair[1],
                          expires_in=ACCESS_TTL, scope=SCOPE)

    async def verify_token(self, token: str) -> AccessToken | None:
        found = await anyio.to_thread.run_sync(
            self.store.token, "access", token)
        return found and AccessToken(
            token=token, client_id=found["client_id"], scopes=[SCOPE],
            expires_at=found["expires"], resource=self.resource,
            subject=found["user"])


def oauth_routes(server: OAuthServer) -> list[Route]:
    authorize_handler = AuthorizationHandler(server)
    token_handler = TokenHandler(server, ClientAuthenticator(server))
    register_handler = RegistrationHandler(server, ClientRegistrationOptions(
        enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]))

    async def authorize(request: Request):
        try:
            user = server.user(request)
        except NotSignedIn as exc:
            return _page("Not signed in", f"<p>{html.escape(str(exc))}</p>",
                         403)
        reset = _user.set(user)
        try:
            return await authorize_handler.handle(request)
        finally:
            _user.reset(reset)

    async def consent(request: Request):
        try:
            user = server.user(request)
        except NotSignedIn as exc:
            return _page("Not signed in", f"<p>{html.escape(str(exc))}</p>",
                         403)
        post = request.method == "POST"
        if post:
            # The request id is already unguessable to another site; this
            # keeps it so if one ever leaks.
            if request.headers.get("origin") != _origin(server.issuer):
                return _page("Refused", "<p>Cross-site request.</p>", 403)
            form = await request.form()
        else:
            form = request.query_params
        request_id = str(form.get("request") or "")
        found = await anyio.to_thread.run_sync(
            server.store.pending, request_id, user, post)
        client = found and await server.get_client(found[0])
        if not found or not client:
            return _page("Link expired", "<p>This request was already answered "
                         "or has expired. Connect again from the client.</p>",
                         400)
        client_id, asked = found
        if not post:
            return _consent_page(client, asked["redirect_uri"], user,
                                 request_id)
        answer = {"state": asked["state"], "iss": server.issuer}
        if form.get("answer") == "allow":
            answer["code"] = await anyio.to_thread.run_sync(
                server.store.add_code, user, client_id, asked)
        else:
            answer["error"] = "access_denied"
        return RedirectResponse(
            construct_redirect_uri(asked["redirect_uri"], **answer),
            status_code=303, headers={"Cache-Control": "no-store"})

    async def as_metadata(request: Request):
        issuer = server.issuer
        return JSONResponse({
            "issuer": issuer,
            "authorization_endpoint": issuer + AUTHORIZE_PATH,
            "token_endpoint": issuer + TOKEN_PATH,
            "registration_endpoint": issuer + REGISTER_PATH,
            "scopes_supported": [SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
        })

    async def resource_metadata(request: Request):
        return JSONResponse({
            "resource": server.resource,
            "authorization_servers": [server.issuer],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "Codee",
        })

    def limited(handler):
        return RequestBodyLimitMiddleware(request_response(handler),
                                          BODY_LIMIT)

    return [
        Route(AUTHORIZE_PATH, authorize, methods=["GET"]),
        Route(CONSENT_PATH, limited(consent), methods=["GET", "POST"]),
        Route(TOKEN_PATH, limited(token_handler.handle), methods=["POST"]),
        Route(REGISTER_PATH, limited(register_handler.handle),
              methods=["POST"]),
        Route(AS_METADATA_PATH, as_metadata, methods=["GET"]),
        Route(RESOURCE_METADATA_PATH, resource_metadata, methods=["GET"]),
        Route(RESOURCE_METADATA_PATH + MCP_PATH, resource_metadata,
              methods=["GET"]),
    ]


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _consent_page(client, redirect_uri: str, user: str,
                  request_id: str) -> HTMLResponse:
    target = urlsplit(redirect_uri)
    where = (target.netloc if target.scheme in ("http", "https")
             else f"{target.scheme}: application on your computer")
    if target.hostname in LOOPBACK:
        where = f"an application on your computer ({target.netloc})"
    name = html.escape(client.client_name or "An unnamed client")
    return _page("Allow access to Codee?", f"""
<p><b>{name}</b> wants to send messages to Codee as
<b>{html.escape(user)}</b>.</p>
<p>Access will be sent to: <b>{html.escape(where)}</b></p>
<p>Codee's agents run with every permission on their host, so this is as good
as giving it a shell there. The name above is whatever the client calls itself.
Allow it only if you started this just now.</p>
<form method="post" action="{CONSENT_PATH}">
<input type="hidden" name="request" value="{html.escape(request_id)}">
<button name="answer" value="deny">Deny</button>
<button name="answer" value="allow">Allow</button>
</form>""")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>{title}</title>
<style>:root{{color-scheme:light dark}}body{{font:16px/1.5 system-ui;
max-width:32rem;margin:10vh auto;padding:0 1rem;overflow-wrap:anywhere}}
button{{font:inherit;padding:.5rem 1.5rem;margin-right:.5rem}}</style>
<h1>{title}</h1>{body}</html>""", status, headers={
        # No form-action: browsers apply it to the redirect back to the client.
        "Content-Security-Policy": "default-src 'none'; style-src "
        "'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
        "X-Frame-Options": "DENY", "Cache-Control": "no-store",
        # Not no-referrer, which would make the form's Origin "null".
        "Referrer-Policy": "same-origin"})
