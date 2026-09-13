"""SQLite storage for the Claude accounts Codee rotates Claude Code between.

An account here is a completed sign-in: an access token, the refresh token that
keeps it alive, and the email it was granted by. All three are long-lived
credentials, which is why none of this is in ``settings.json`` — that file is
rewritten by the admin UI on every save and is easy to hand-edit or copy
around. Same reasoning as :mod:`codee_database.oauth_tokens`, and the same
consequence: which account is current lives here too, so a settings save cannot
put a spent account back into use.

Like the OAuth token store and unlike :mod:`codee.lib.runs_db`, these calls
raise rather than swallow errors. A lost write here would have the next check
rotate away from an account that was already rotated to, and a read that
quietly answered "none" would silently restart the list from the top.
"""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from codee_main_context.context import CodeeMainContext

from codee_database.database import get_db_connection

_ACCOUNT_COLUMNS = ("id", "label", "access_token", "refresh_token",
                    "expires_at", "refresh_expires_at", "refreshed_at",
                    "scopes", "subscription_type", "position", "connected_at")
# Columns added after the table shipped, with the type and default to create
# them with. A Codee that already has accounts must keep them.
_ADDED_COLUMNS = {
    "refresh_expires_at": "INTEGER NOT NULL DEFAULT 0",
    "refreshed_at": "INTEGER NOT NULL DEFAULT 0",
}
# The current-account pointer is one row, pinned to this id. There is exactly
# one "the account in use", so the alternative would be a table that has to be
# emptied before every write.
_CURRENT_ROW = 1


@dataclass(frozen=True)
class Account:
    """One connected Claude account, as everything outside this module sees it."""

    id: int
    # The email the sign-in was granted by, for the settings page to show. Only
    # ever empty for an account whose profile could not be read at connect time.
    label: str
    access_token: str
    refresh_token: str
    # Milliseconds since the epoch, matching the CLI's credentials file.
    expires_at: int
    # When the refresh token runs out. This is what ends an account: past it
    # nothing can be renewed and the user has to sign in again. Zero means the
    # API never said, which is not the same as expired.
    refresh_expires_at: int = 0
    # When it was last minted or renewed, in milliseconds. What decides whether
    # an account sitting out of rotation is due a renewal to keep its refresh
    # token alive.
    refreshed_at: int = 0
    scopes: str = ""
    subscription_type: str = ""
    position: int = 0
    connected_at: str = ""

    def needs_reconnect(self, now_ms: int) -> bool:
        """Whether this account can no longer renew itself.

        Only ever true for an account whose refresh window we actually know and
        which is past it. An account the API never gave a window for is left
        alone: retiring it on a guess would tell the user to reconnect
        something that works.
        """
        return bool(self.refresh_expires_at) and self.refresh_expires_at <= now_ms


def init(main_context: CodeeMainContext) -> None:
    """Create the account and current-account tables if absent. Idempotent."""
    with closing(get_db_connection(main_context)) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS claude_code_account (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL DEFAULT '',
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL DEFAULT 0,
                scopes TEXT NOT NULL DEFAULT '',
                subscription_type TEXT NOT NULL DEFAULT '',
                refresh_expires_at INTEGER NOT NULL DEFAULT 0,
                refreshed_at INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                connected_at TEXT NOT NULL
            )"""
        )
        existing = {row[1] for row
                    in conn.execute("PRAGMA table_info(claude_code_account)")}
        for column, definition in _ADDED_COLUMNS.items():
            if column not in existing:
                conn.execute("ALTER TABLE claude_code_account"
                             f" ADD COLUMN {column} {definition}")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS claude_code_current_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                account_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )


def accounts(main_context: CodeeMainContext) -> list[Account]:
    """Every connected account, in the order they are rotated through.

    That order is the order they were connected, which is also the order the
    settings page lists them in — so what a user reads top to bottom is what
    rotation will work through.
    """
    init(main_context)
    with closing(get_db_connection(main_context)) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_ACCOUNT_COLUMNS)} FROM claude_code_account"
            " ORDER BY position, id"
        ).fetchall()
    return [Account(**dict(zip(_ACCOUNT_COLUMNS, row))) for row in rows]


def add_account(label: str, access_token: str, refresh_token: str,
                expires_at: int, scopes: str, subscription_type: str,
                main_context: CodeeMainContext,
                refresh_expires_at: int = 0) -> int:
    """Record a completed sign-in, at the end of the rotation order.

    Returns the new account's id. Connecting the same account twice makes two
    rows: they are two sign-ins, each with tokens of its own, and telling the
    user "already connected" would be a lie about a credential that does now
    exist. The list shows the email, so a duplicate is visible.
    """
    init(main_context)
    with closing(get_db_connection(main_context)) as conn, conn:
        last = conn.execute(
            "SELECT COALESCE(MAX(position), -1) FROM claude_code_account"
        ).fetchone()[0]
        now = datetime.now(timezone.utc)
        cursor = conn.execute(
            "INSERT INTO claude_code_account (label, access_token,"
            " refresh_token, expires_at, refresh_expires_at, refreshed_at,"
            " scopes, subscription_type, position, connected_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (label, access_token, refresh_token, expires_at,
             refresh_expires_at, int(now.timestamp() * 1000), scopes,
             subscription_type, last + 1, now.isoformat()),
        )
    return int(cursor.lastrowid)


def update_tokens(account_id: int, access_token: str, refresh_token: str,
                  expires_at: int, main_context: CodeeMainContext,
                  refresh_expires_at: int | None = None) -> None:
    """Store the tokens a refresh returned, keeping the account's identity.

    The refresh token is written too: Anthropic may hand back a new one, and an
    account left holding the spent one would be unusable the next time its turn
    came round. ``refresh_expires_at`` is left as it was when the caller has
    nothing newer to say — a renewal that reported no window must not erase the
    one already known.
    """
    init(main_context)
    stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
    with closing(get_db_connection(main_context)) as conn, conn:
        conn.execute(
            "UPDATE claude_code_account SET access_token = ?,"
            " refresh_token = ?, expires_at = ?, refreshed_at = ?"
            " WHERE id = ?",
            (access_token, refresh_token, expires_at, stamp, account_id),
        )
        if refresh_expires_at:
            conn.execute("UPDATE claude_code_account"
                         " SET refresh_expires_at = ? WHERE id = ?",
                         (refresh_expires_at, account_id))


def retire_account(account_id: int, main_context: CodeeMainContext) -> None:
    """Mark an account as no longer renewable, after a refusal said so.

    Stamped with the moment it was refused, so it reads as expired to
    :meth:`Account.needs_reconnect` without anything having to remember the
    refusal. The tokens are left in place: they are what the user is being
    asked to replace, and deleting the row would take the account off the
    settings page instead of telling them to sign in again.
    """
    init(main_context)
    with closing(get_db_connection(main_context)) as conn, conn:
        conn.execute(
            "UPDATE claude_code_account SET refresh_expires_at = ?"
            " WHERE id = ?",
            (int(datetime.now(timezone.utc).timestamp() * 1000), account_id),
        )


def set_label(account_id: int, label: str,
              main_context: CodeeMainContext) -> None:
    """Name an account, for one whose profile could only be read later."""
    init(main_context)
    with closing(get_db_connection(main_context)) as conn, conn:
        conn.execute("UPDATE claude_code_account SET label = ? WHERE id = ?",
                     (label, account_id))


def remove_account(account_id: int, main_context: CodeeMainContext) -> None:
    """Forget an account, and the pointer to it if it was the one in use."""
    init(main_context)
    with closing(get_db_connection(main_context)) as conn, conn:
        conn.execute("DELETE FROM claude_code_account WHERE id = ?",
                     (account_id,))
        conn.execute(
            "DELETE FROM claude_code_current_account WHERE account_id = ?",
            (account_id,))


def current_account_id(main_context: CodeeMainContext) -> int:
    """The account in use, or ``0`` when none has been chosen yet.

    Zero is the normal answer before the first rotation check runs, and is what
    tells the caller to start the list from its first account.
    """
    init(main_context)
    with closing(get_db_connection(main_context)) as conn:
        row = conn.execute(
            "SELECT account_id FROM claude_code_current_account WHERE id = ?",
            (_CURRENT_ROW,),
        ).fetchone()
    return int(row[0]) if row else 0


def set_current_account(account_id: int,
                        main_context: CodeeMainContext) -> None:
    """Record ``account_id`` as the one in use, replacing whatever was there."""
    init(main_context)
    with closing(get_db_connection(main_context)) as conn, conn:
        conn.execute(
            "INSERT INTO claude_code_current_account (id, account_id, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " account_id = excluded.account_id,"
            " updated_at = excluded.updated_at",
            (_CURRENT_ROW, account_id, datetime.now(timezone.utc).isoformat()),
        )


def clear_current_account(main_context: CodeeMainContext) -> None:
    """Forget which account is in use, so the next check starts from the top."""
    init(main_context)
    with closing(get_db_connection(main_context)) as conn, conn:
        conn.execute("DELETE FROM claude_code_current_account WHERE id = ?",
                     (_CURRENT_ROW,))
