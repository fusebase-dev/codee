from sqlite3 import Connection
import sqlite3

from anyio import Path
from codee_main_context.context import CodeeMainContext


def _get_db_path(main_context: CodeeMainContext) -> Path:
    return main_context.data_dir / "codee.db"


def get_db_connection(main_context: CodeeMainContext) -> Connection:
    return sqlite3.connect(_get_db_path(main_context))
