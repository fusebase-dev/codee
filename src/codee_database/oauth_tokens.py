"""SQLite storage for OAuth tokens, keyed by tasks provider.

Tokens are kept out of ``settings.json`` because that file is written by the
admin UI on every save and is easy to hand-edit or copy around; a refresh token
is a long-lived credential and belongs next to the rest of the runtime state.

Unlike :mod:`codee.lib.runs_db` these calls raise rather than swallow errors:
losing a token write silently would leave the UI claiming a connection that
doesn't exist, and a read that quietly returns ``None`` would look like "never
connected" and send the user through the whole consent flow again.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone

from codee_main_context.context import CodeeMainContext

from codee_database.database import get_db_connection

# An authorization redirect that hasn't come back within this window is treated
# as abandoned, so a stale row can't be replayed later.
PENDING_TTL = timedelta(minutes=10)

_TOKEN_COLUMNS = ("provider", "access_token", "refresh_token", "expires_at",
                  "scope", "account", "updated_at")
_PENDING_COLUMNS = ("state", "provider", "code_verifier",
                    "redirect_uri", "created_at")


def init(main_context: CodeeMainContext) -> None:
    """Create the token and pending-authorization tables if absent. Idempotent."""
    with closing(get_db_connection(main_context)) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS oauth_tokens (
                provider TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                expires_at TEXT,
                scope TEXT,
                account TEXT,
                updated_at TEXT NOT NULL
            )"""
        )
        # One row per authorization in flight: the CSRF state we handed to the
        # identity provider, plus the PKCE verifier the callback has to send back.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS oauth_pending (
                state TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                code_verifier TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )


def save_tokens(
    provider: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: str | None,
    scope: str = "",
    account: str = "",
    main_context: CodeeMainContext = None,
) -> None:
    """Store (or replace) the tokens for a provider."""
    init(main_context)
    with closing(get_db_connection(main_context)) as conn, conn:
        conn.execute(
            "INSERT INTO oauth_tokens (provider, access_token, refresh_token,"
            " expires_at, scope, account, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(provider) DO UPDATE SET"
            " access_token = excluded.access_token,"
            " refresh_token = excluded.refresh_token,"
            " expires_at = excluded.expires_at,"
            " scope = excluded.scope,"
            " account = excluded.account,"
            " updated_at = excluded.updated_at",
            (provider, access_token, refresh_token, expires_at, scope, account,
             datetime.now(timezone.utc).isoformat()),
        )


def load_tokens(provider: str, main_context: CodeeMainContext = None) -> dict | None:
    """Return the stored tokens for a provider, or None if it was never connected."""
    init(main_context)
    with closing(get_db_connection(main_context)) as conn:
        row = conn.execute(
            "SELECT provider, access_token, refresh_token, expires_at, scope,"
            " account, updated_at FROM oauth_tokens WHERE provider = ?",
            (provider,),
        ).fetchone()
    return dict(zip(_TOKEN_COLUMNS, row)) if row else None


def delete_tokens(provider: str, main_context: CodeeMainContext = None) -> None:
    """Forget a provider's tokens, so the UI reports it as disconnected."""
    init(main_context)
    with closing(get_db_connection(main_context)) as conn, conn:
        conn.execute("DELETE FROM oauth_tokens WHERE provider = ?", (provider,))


def create_pending(
    provider: str,
    state: str,
    code_verifier: str,
    redirect_uri: str,
    main_context: CodeeMainContext = None,
) -> None:
    """Record an authorization we're about to send the browser off to."""
    init(main_context)
    with closing(get_db_connection(main_context)) as conn, conn:
        _purge_expired_pending(conn)
        conn.execute(
            "INSERT OR REPLACE INTO oauth_pending (state, provider, code_verifier,"
            " redirect_uri, created_at) VALUES (?, ?, ?, ?, ?)",
            (state, provider, code_verifier, redirect_uri,
             datetime.now(timezone.utc).isoformat()),
        )


def consume_pending(
    provider: str,
    state: str,
    main_context: CodeeMainContext = None,
) -> dict | None:
    """Take the pending authorization matching ``state``, removing it.

    Returns None when the state is unknown, belongs to another provider, or has
    expired — all of which mean the callback must be rejected. Single-use by
    construction: the row is deleted whether or not the exchange later succeeds,
    so a replayed callback finds nothing.
    """
    init(main_context)
    with closing(get_db_connection(main_context)) as conn, conn:
        _purge_expired_pending(conn)
        row = conn.execute(
            "SELECT state, provider, code_verifier, redirect_uri, created_at"
            " FROM oauth_pending WHERE state = ? AND provider = ?",
            (state, provider),
        ).fetchone()
        conn.execute("DELETE FROM oauth_pending WHERE state = ?", (state,))
    return dict(zip(_PENDING_COLUMNS, row)) if row else None


def _purge_expired_pending(conn) -> None:
    cutoff = (datetime.now(timezone.utc) - PENDING_TTL).isoformat()
    conn.execute("DELETE FROM oauth_pending WHERE created_at < ?", (cutoff,))
