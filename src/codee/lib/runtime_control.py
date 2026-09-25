"""Persistent controls shared by the admin UI and executor process."""
from pathlib import Path

from codee_main_context.context import CodeeMainContext

PAUSED_FILE = "paused"


def is_paused(context: CodeeMainContext) -> bool:
    """Return whether new agent work is paused."""
    return (context.data_dir / PAUSED_FILE).exists()


def set_paused(context: CodeeMainContext, paused: bool) -> None:
    """Persist whether new agent work may start."""
    path = context.data_dir / PAUSED_FILE
    if paused:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    else:
        path.unlink(missing_ok=True)
