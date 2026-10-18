"""Console-script launcher for the Reflex admin UI."""
import argparse
import os
import sys
from pathlib import Path

from codee.admin_service import DEFAULT_ADMIN_PORT, public_base_url
from codee_main_context.context import data_dir, project_root
from codee_main_context.logging import (
    configure_logging, enable_debug, get_logger, verbose_debug_enabled)


log = get_logger(__name__)


RXCONFIG = """import reflex as rx

config = rx.Config(
    app_name="codee_admin",
    telemetry_enabled=False,
    show_built_with_reflex=False,
    plugins=[
        rx.plugins.RadixThemesPlugin(
            theme=rx.theme(
                accent_color="indigo",
                gray_color="slate",
                radius="small",
                appearance="inherit",
            )
        ),
    ],
    disable_plugins=[rx.plugins.SitemapPlugin],
)
"""


def _api_url(port: int) -> str:
    """Origin the browser uses for the backend websocket and uploads.

    Reflex bakes this into the compiled frontend. The browser swaps the host
    ``localhost`` for whatever host the page was opened on, so the default
    works from any address on the same port. Behind a reverse proxy that
    changes the port or path, set the public origin instead.
    """
    return public_base_url() or f"http://localhost:{port}"


def _runtime_directory(root: Path) -> Path:
    runtime = data_dir(root) / "reflex"
    runtime.mkdir(parents=True, exist_ok=True)
    config_path = runtime / "rxconfig.py"
    if not config_path.exists() or config_path.read_text() != RXCONFIG:
        config_path.write_text(RXCONFIG)
    return runtime


def main() -> int:
    parser = argparse.ArgumentParser(prog="codee-admin")
    parser.add_argument("--port", "--server.port",
                        dest="port", type=int, default=DEFAULT_ADMIN_PORT)
    parser.add_argument("-d", "--debug", action="store_true",
                        help="log debug messages")
    arguments, reflex_arguments = parser.parse_known_args()

    if arguments.debug:
        enable_debug()
    configure_logging()

    root = project_root().resolve()
    codee_data = data_dir(root).resolve()
    os.environ["CODEE_PROJECT_ROOT"] = str(root)
    os.environ["CODEE_DATA_DIR"] = str(codee_data)
    os.environ["REFLEX_API_URL"] = _api_url(arguments.port)
    os.chdir(_runtime_directory(root))

    from reflex.reflex import cli

    sys.argv = [
        "reflex",
        "run",
        "--env",
        "prod",
        "--single-port",
        "--frontend-port",
        str(arguments.port),
        # Reflex keeps its own logger; only CODEE_DEBUG=all turns it up, since
        # its debug output is far noisier than ours.
        *(["--loglevel", "debug"] if verbose_debug_enabled() else []),
        *reflex_arguments,
    ]
    log.debug("root=%s data=%s reflex argv=%s", root, codee_data, sys.argv)
    cli()
    return 0


if __name__ == "__main__":
    sys.exit(main())
