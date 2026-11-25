"""SQLite storage of the MCP server's OAuth clients, approvals and tokens.

Codes and tokens are stored only as SHA-256 hashes: a copy of the file grants
nothing. Every token hangs off a grant, one approval by one user of one client,
so revoking the grant ends all of them.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

PENDING_TTL = 600
CODE_TTL = 60
ACCESS_TTL = 3600
# Counted from the approval and not extended by refreshing, so someone who lost
# access to the sign-in proxy loses access here too.
GRANT_TTL = 30 * 24 * 3600
# Anyone can register a client; one nobody approved is dropped after this.
UNUSED_CLIENT_TTL = 24 * 3600
MAX_CLIENTS = 1000


class StoreFull(Exception):
    pass


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        new = not self.path.exists()
        if new:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """CREATE TABLE IF NOT EXISTS clients (
                   id TEXT PRIMARY KEY, data TEXT NOT NULL,
                   created REAL NOT NULL);
               CREATE TABLE IF NOT EXISTS pending (
                   id TEXT PRIMARY KEY, user TEXT NOT NULL,
                   client_id TEXT NOT NULL, data TEXT NOT NULL,
                   expires REAL NOT NULL);
               CREATE TABLE IF NOT EXISTS grants (
                   id TEXT PRIMARY KEY, user TEXT NOT NULL,
                   client_id TEXT NOT NULL REFERENCES clients(id)
                       ON DELETE CASCADE,
                   expires REAL NOT NULL);
               CREATE TABLE IF NOT EXISTS codes (
                   hash TEXT PRIMARY KEY,
                   grant_id TEXT NOT NULL REFERENCES grants(id)
                       ON DELETE CASCADE,
                   data TEXT NOT NULL, expires REAL NOT NULL,
                   used INTEGER NOT NULL DEFAULT 0);
               CREATE TABLE IF NOT EXISTS tokens (
                   hash TEXT PRIMARY KEY, kind TEXT NOT NULL,
                   grant_id TEXT NOT NULL REFERENCES grants(id)
                       ON DELETE CASCADE,
                   expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
            """)
        return conn

    def _purge(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        conn.execute("DELETE FROM grants WHERE expires < ?", (now,))
        conn.execute("DELETE FROM tokens WHERE expires < ?", (now,))
        conn.execute("DELETE FROM pending WHERE expires < ?", (now,))
        conn.execute(
            "DELETE FROM clients WHERE created < ? AND id NOT IN"
            " (SELECT client_id FROM grants)", (now - UNUSED_CLIENT_TTL,))

    def add_client(self, client_id: str, data: str) -> None:
        with closing(self._connect()) as conn, conn:
            self._purge(conn)
            if conn.execute("SELECT COUNT(*) FROM clients"
                            ).fetchone()[0] >= MAX_CLIENTS:
                raise StoreFull
            conn.execute("INSERT INTO clients VALUES (?, ?, ?)",
                         (client_id, data, time.time()))

    def client(self, client_id: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT data FROM clients WHERE id = ?",
                               (client_id,)).fetchone()
        return row[0] if row else None

    def add_pending(self, user: str, client_id: str, data: dict) -> str:
        request_id = secrets.token_urlsafe(32)
        with closing(self._connect()) as conn, conn:
            self._purge(conn)
            conn.execute("INSERT INTO pending VALUES (?, ?, ?, ?, ?)",
                         (_hash(request_id), user, client_id, json.dumps(data),
                          time.time() + PENDING_TTL))
        return request_id

    def pending(self, request_id: str, user: str,
                take: bool = False) -> tuple[str, dict] | None:
        """(client id, request) of an approval ``user`` is being asked for."""
        query = " WHERE id = ? AND user = ? AND expires > ?"
        values = (_hash(request_id), user, time.time())
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                ("DELETE FROM pending" + query + " RETURNING client_id, data")
                if take else "SELECT client_id, data FROM pending" + query,
                values).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def add_code(self, user: str, client_id: str, data: dict) -> str:
        """Record the approval and return the authorization code for it."""
        code, grant = secrets.token_urlsafe(32), secrets.token_hex(16)
        now = time.time()
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO grants VALUES (?, ?, ?, ?)",
                         (grant, user, client_id, now + GRANT_TTL))
            conn.execute("INSERT INTO codes VALUES (?, ?, ?, ?, 0)",
                         (_hash(code), grant, json.dumps(data),
                          now + CODE_TTL))
        return code

    def code(self, code: str) -> dict | None:
        """The code's record. A code seen again after use was stolen: its
        grant goes, and with it every token it produced."""
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT g.client_id, g.user, c.data, c.expires, c.used, g.id"
                " FROM codes c JOIN grants g ON g.id = c.grant_id"
                " WHERE c.hash = ?", (_hash(code),)).fetchone()
            if row and row[4]:
                conn.execute("DELETE FROM grants WHERE id = ?", (row[5],))
                return None
        return None if row is None else {
            **json.loads(row[2]), "client_id": row[0], "user": row[1],
            "expires": row[3]}

    def exchange_code(self, code: str) -> tuple[str, str] | None:
        return self._exchange("codes", code)

    def exchange_refresh(self, token: str) -> tuple[str, str] | None:
        return self._exchange("tokens", token)

    def _exchange(self, table: str, value: str) -> tuple[str, str] | None:
        """Use up a code or refresh token: a new (access, refresh) pair."""
        now = time.time()
        with closing(self._connect()) as conn, conn:
            self._purge(conn)
            row = conn.execute(
                f"UPDATE {table} SET used = 1 WHERE hash = ? AND used = 0"
                " AND expires > ? RETURNING grant_id",
                (_hash(value), now)).fetchone()
            if row is None:
                return None
            expires = conn.execute("SELECT expires FROM grants WHERE id = ?",
                                   (row[0],)).fetchone()[0]
            access, refresh = (secrets.token_urlsafe(32) for _ in range(2))
            conn.executemany(
                "INSERT INTO tokens VALUES (?, ?, ?, ?, 0)",
                [(_hash(access), "access", row[0],
                  min(now + ACCESS_TTL, expires)),
                 (_hash(refresh), "refresh", row[0], expires)])
        return access, refresh

    def token(self, kind: str, token: str) -> dict | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT g.client_id, g.user, t.expires, t.used, g.id"
                " FROM tokens t JOIN grants g ON g.id = t.grant_id"
                " WHERE t.hash = ? AND t.kind = ? AND t.expires > ?",
                (_hash(token), kind, time.time())).fetchone()
            # A refresh token used twice means someone else has a copy.
            if row and row[3]:
                conn.execute("DELETE FROM grants WHERE id = ?", (row[4],))
                return None
        return None if row is None else {
            "client_id": row[0], "user": row[1], "expires": int(row[2])}

    def revoke_all(self) -> int:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM pending")
            return conn.execute("DELETE FROM grants").rowcount
