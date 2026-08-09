import argparse
import subprocess
import sys
import time
from pathlib import Path

from codee_main_context.logging import (
    configure_logging, enable_debug, get_logger)

from codee.init_cli import (
    CONFLICT_PATHS, ensure_working_directories, main as init_main)


SHUTDOWN_TIMEOUT = 5

log = get_logger(__name__)


def _parse_arguments(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Pull out codee-start's own flags; everything else goes to the admin UI."""
    parser = argparse.ArgumentParser(
        prog="codee-start",
        epilog="Any other argument (--port, reflex flags) goes to the admin UI.",
    )
    parser.add_argument(
        "-d", "--debug",
        action="store_true",
        help="log debug messages from the executor and the admin UI",
    )
    parser.add_argument(
        "--debug-all",
        action="store_true",
        help="--debug plus debug output from third-party libraries",
    )
    return parser.parse_known_args(argv)


def _ensure_initialized() -> None:
    destination = Path.cwd()
    if any((destination / path).exists() for path in CONFLICT_PATHS):
        log.info("Codee is already initialized; skipping codee-init")
        # codee-init is skipped, but the runtime directories are still needed
        # here: they may predate this feature, or have been cleaned away.
        ensure_working_directories(destination)
        return

    init_main()


def _stop_processes(processes: list[subprocess.Popen[bytes]]) -> None:
    running = [process for process in processes if process.poll() is None]
    for process in running:
        process.terminate()

    for process in running:
        try:
            process.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> int:
    arguments, admin_arguments = _parse_arguments(sys.argv[1:])
    if arguments.debug or arguments.debug_all:
        # Exports CODEE_DEBUG, which the subprocesses below inherit.
        enable_debug(verbose=arguments.debug_all)
    configure_logging()

    _ensure_initialized()

    commands = [
        [sys.executable, "-m", "codee.executor"],
        [sys.executable, "-m", "codee.admin_cli", *admin_arguments],
    ]
    processes: list[subprocess.Popen[bytes]] = []

    try:
        for command in commands:
            log.debug("starting %s", " ".join(command))
            processes.append(subprocess.Popen(command))

        while True:
            for process in processes:
                return_code = process.poll()
                if return_code is not None:
                    log.debug("pid %s exited with %s",
                              process.pid, return_code)
                    return return_code
            time.sleep(0.2)
    except KeyboardInterrupt:
        return 130
    finally:
        _stop_processes(processes)


if __name__ == "__main__":
    sys.exit(main())
