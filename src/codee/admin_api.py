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
from starlette.responses import RedirectResponse
from starlette.routing import Route

from codee.admin_service import AdminService
from codee_tasks_azure_devops.oauth import CALLBACK_PATH

SETTINGS_PATH = "/settings"

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


api_app = Starlette(routes=[
    Route(CALLBACK_PATH, azure_devops_callback, methods=["GET"]),
])
