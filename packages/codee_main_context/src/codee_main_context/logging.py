"""Process-wide logging for Codee's entry points.

Codee runs as several processes -- ``codee-start`` launches the executor and
the admin UI as subprocesses -- so debug mode is carried in the environment:
``CODEE_DEBUG=1`` switches this package to ``DEBUG`` level, and because
subprocesses inherit the environment, one flag on the launcher turns on debug
logging everywhere.

Logging in any module::

    from codee_main_context.logging import get_logger

    log = get_logger(__name__)
    log.debug("fetched %d task(s) from %s", len(tasks), provider.describe())

Only entry points (``main()`` functions) call :func:`configure_logging`, and
they call it once, before doing any work. Everything else just asks for a
logger and logs; the handler is the entry point's business.
"""
import logging
import os
import sys
from typing import TextIO

# Set to "1"/"true"/"yes"/"on" for Codee debug output, or "all" to also
# un-mute the chatty third-party libraries listed below.
DEBUG_ENV_VAR = "CODEE_DEBUG"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_VERBOSE = "all"

# Libraries whose DEBUG output would bury ours (a single boto3 call emits
# dozens of lines). Held at INFO unless CODEE_DEBUG=all asks for everything.
_NOISY_LOGGERS = (
    "asyncio",
    "boto3",
    "botocore",
    "httpcore",
    "httpx",
    "reflex",
    "s3transfer",
    "urllib3",
    "watchdog",
    "watchfiles",
)

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DEBUG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s:%(lineno)d %(funcName)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def _flag() -> str:
    return os.environ.get(DEBUG_ENV_VAR, "").strip().lower()


def debug_enabled() -> bool:
    """True when ``CODEE_DEBUG`` asks for debug output."""
    return _flag() in _TRUTHY or _flag() == _VERBOSE


def verbose_debug_enabled() -> bool:
    """True for ``CODEE_DEBUG=all``: our debug output plus the noisy libraries'."""
    return _flag() == _VERBOSE


def enable_debug(*, verbose: bool = False) -> None:
    """Turn debug on for this process and every subprocess it goes on to spawn.

    Writing to ``os.environ`` rather than to a module global is what makes
    ``codee-start --debug`` reach the executor and admin processes, which are
    separate interpreters that inherit the environment.
    """
    os.environ[DEBUG_ENV_VAR] = _VERBOSE if verbose else "1"


def configure_logging(debug: bool | None = None, *,
                      stream: TextIO | None = None) -> None:
    """Install Codee's log handler. Call once, from an entry point.

    ``debug=None`` (the default) takes the level from ``CODEE_DEBUG``; passing
    ``debug=True`` also exports the flag so subprocesses inherit it. Safe to
    call more than once -- the previous handler is replaced, not stacked.
    """
    if debug:
        enable_debug()
    if debug is None:
        debug = debug_enabled()

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format=_DEBUG_FORMAT if debug else _FORMAT,
        datefmt=_DATE_FORMAT,
        # stdout, not the logging default of stderr: the code around these logs
        # still prints to stdout, and splitting the two streams would scramble
        # their order as soon as the output is piped to a file.
        stream=stream or sys.stdout,
        force=True,
    )

    if not verbose_debug_enabled():
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Logger for a module; pass ``__name__``.

    A thin wrapper over ``logging.getLogger`` so modules have one import for
    logging and never touch handler configuration by accident.
    """
    return logging.getLogger(name)
