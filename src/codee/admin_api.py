"""Plain HTTP endpoints served alongside the Reflex pages.

OAuth callbacks can't be Reflex pages: the authorization code would have to
travel through the browser and into a page's ``on_load`` before anything could
be done with it. Handling them as backend routes keeps the code and the client
secret server-side, and lets the browser be bounced straight back to /settings.

Reflex mounts its own ASGI app underneath this Starlette app (see
``api_transformer`` in :mod:`codee.admin`), so these routes are matched first
and everything else falls through to the UI.
"""
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from codee.admin_service import PLUGIN_CALLBACK_PATH, AdminService
from codee_main_context.logging import get_logger
from codee_plugin_abstract.plugin import AbstractCodeePlugin, PluginHost
from codee_tasks_azure_devops.oauth import CALLBACK_PATH

log = get_logger(__name__)

SETTINGS_PATH = "/settings"
PLUGIN_HTTP_PATH = "/p/{name}/{path:path}"
PLUGINS_PATH = "/plugins"

# Its own service instance: this route runs outside any Reflex session, and
# every handler re-reads settings from disk anyway.
_service = AdminService()


def _back_to_settings(connected: bool, message: str) -> RedirectResponse:
    """Return to the settings page carrying the outcome for the UI to toast."""
    query = urlencode(
        {"azure": "connected" if connected else "error", "message": message})
    # 303: the callback is a GET, and the browser should land on /settings as a
    # fresh GET rather than replaying anything.
    return RedirectResponse(f"{SETTINGS_PATH}?{query}", status_code=303)


def azure_devops_callback(request: Request) -> RedirectResponse:
    """Entra ID redirects here with ?code&state, or with ?error on refusal.

    Defined as a sync handler on purpose: the token exchange is a blocking HTTP
    call, so Starlette runs it in a worker thread instead of stalling the event
    loop that serves the rest of the admin UI.
    """
    params = request.query_params
    if params.get("error"):
        description = params.get("error_description") or params["error"]
        return _back_to_settings(False, description.splitlines()[0])

    code, state = params.get("code"), params.get("state")
    if not code or not state:
        return _back_to_settings(
            False, "Azure DevOps did not return an authorization code.")

    connected, message = _service.complete_azure_authorization(code, state)
    return _back_to_settings(connected, message)


def plugin_sign_in_callback(request: Request) -> RedirectResponse:
    """A plugin's OAuth provider redirects here; back to /plugins after.

    Sync for the same reason as the Azure DevOps callback: the plugin's code
    exchange is a blocking call.
    """
    name = request.path_params["name"]
    params = request.query_params
    if params.get("error"):
        description = params.get("error_description") or params["error"]
        connected, message = False, description.splitlines()[0]
    elif not params.get("code") or not params.get("state"):
        connected, message = False, "No authorization code came back."
    else:
        connected, message = _service.complete_plugin_sign_in(
            name, params["code"], params["state"])
    query = urlencode({"sign_in": "ok" if connected else "error",
                       "message": message})
    return RedirectResponse(f"{PLUGINS_PATH}?{query}", status_code=303)


def plugin_http(request: Request) -> Response:
    """``/p/<name>/<path>``: whatever the plugin serves there.

    Sync so a plugin can make blocking calls (a storage read) without stalling
    the event loop that serves the rest of the admin UI.
    """
    target = _service.plugin_http_target(request.path_params["name"])
    if target is None:
        return PlainTextResponse("Not found", status_code=404)
    plugin, settings = target
    try:
        response = plugin.serve_http(
            request, request.path_params["path"], settings)
    except Exception as exc:
        # One plugin's bug costs that request, never the admin UI.
        log.exception("Plugin %s failed to serve %s: %s",
                      plugin.NAME, request.url.path, exc)
        return PlainTextResponse("Plugin error", status_code=502)
    if response is None:
        return PlainTextResponse("Not found", status_code=404)
    return response


class _WhileEnabled:
    """A plugin's route, answering 404 while the plugin is off."""

    def __init__(self, name: str, app: ASGIApp) -> None:
        self.name = name
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive,
                       send: Send) -> None:
        app = self.app
        if _service.plugin_http_target(self.name) is None:
            app = PlainTextResponse("Not found", status_code=404)
        await app(scope, receive, send)


def _plugin_routes(plugin: AbstractCodeePlugin) -> list[Route]:
    host = PluginHost(
        base_url=_service.admin_base_url,
        settings=lambda: (_service.plugin_http_target(plugin.NAME)
                          or (plugin, {}))[1],
        context=_service.context)
    try:
        routes = plugin.http_routes(host)
    except Exception as exc:
        # One plugin's bug costs that plugin its routes, never the admin UI.
        log.exception("Plugin %s has no routes: %s", plugin.NAME, exc)
        return []
    # Every method is taken here and refused by the plugin's own route: one
    # Codee didn't match would fall through to the UI, and a proxy rule that
    # opens the path would open that with it.
    return [Route(route.path, _WhileEnabled(plugin.NAME, route.handle))
            for route in routes]


api_app = Starlette(routes=[
    Route(CALLBACK_PATH, azure_devops_callback, methods=["GET"]),
    Route(PLUGIN_CALLBACK_PATH, plugin_sign_in_callback, methods=["GET"]),
    Route(PLUGIN_HTTP_PATH, plugin_http, methods=["GET", "HEAD"]),
    *(route for plugin in _service.plugins.plugins
      for route in _plugin_routes(plugin)),
])
