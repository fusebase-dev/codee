"""SQLite-backed log of trigger-skill runs (one row per launched Claude session).

Fail-safe by design: recording a run must never break the trigger that called it
(FR-009), and reading never raises on an empty/missing DB (FR-006).
"""
from datetime import datetime, timedelta, timezone

from codee_main_context.context import CodeeMainContext

from codee_database.database import get_db_connection

_COLUMNS = ("id", "skill_name", "trigger_type", "session_id", "status", "error",
            "started_at", "message")


def init(main_context: CodeeMainContext) -> None:
    """Create the runs table + index if absent, and migrate in the message column. Idempotent."""
    with get_db_connection(main_context) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                started_at TEXT NOT NULL,
                message TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC)")
        # Migrate pre-feature DBs lacking the message column (added 003-runs-dashboard).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        if "message" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN message TEXT")
        # In-flight claude runs; a row lives only while its subprocess is running.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS active_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message TEXT,
                started_at TEXT NOT NULL
            )"""
        )


def record_run(skill_name, trigger_type, session_id, status, error=None, started_at=None,
               message=None, *, main_context: CodeeMainContext) -> None:
    """Insert one run row. Never raises to the caller (FR-009)."""
    try:
        init(main_context)
        if started_at is None:
            started_at = datetime.now(timezone.utc).isoformat()
        with get_db_connection(main_context) as conn:
            conn.execute(
                "INSERT INTO runs (skill_name, trigger_type, session_id, status, error,"
                " started_at, message) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (skill_name, trigger_type, session_id,
                 status, error, started_at, message),
            )
    except Exception as exc:  # ponytail: a logging miss must never abort the skill run
        print(f"[runs_db] Failed to record run for {skill_name}: {exc}")


def recent_runs(limit: int = 100, *, main_context: CodeeMainContext) -> list[dict]:
    """Most recent runs newest-first as dicts; [] on empty/missing DB (FR-006)."""
    try:
        init(main_context)
        with get_db_connection(main_context) as conn:
            rows = conn.execute(
                "SELECT id, skill_name, trigger_type, session_id, status, error,"
                " started_at, message"
                " FROM runs ORDER BY started_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(zip(_COLUMNS, row)) for row in rows]
    except Exception as exc:
        print(f"[runs_db] Failed to read recent runs: {exc}")
        return []


def counts(main_context: CodeeMainContext) -> dict:
    """Total runs and runs in the trailing 24h (UTC). Zeros on empty/missing DB; never raises."""
    try:
        init(main_context)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with get_db_connection(main_context) as conn:
            total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            last_24h = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE started_at > ?", (cutoff,)
            ).fetchone()[0]
        return {"total": total, "last_24h": last_24h}
    except Exception as exc:
        print(f"[runs_db] Failed to read counts: {exc}")
        return {"total": 0, "last_24h": 0}


def clear_active_jobs(main_context: CodeeMainContext) -> None:
    """Drop all in-flight job rows. Called on cron_jira startup to purge stale rows."""
    try:
        init(main_context)
        with get_db_connection(main_context) as conn:
            conn.execute("DELETE FROM active_jobs")
    except Exception as exc:  # ponytail: never abort startup over a bookkeeping wipe
        print(f"[runs_db] Failed to clear active jobs: {exc}")


def start_job(session_id, message, started_at=None, *,
              main_context: CodeeMainContext) -> int | None:
    """Mark a claude run as in-flight. Returns its job id (or None if logging failed)."""
    try:
        init(main_context)
        if started_at is None:
            started_at = datetime.now(timezone.utc).isoformat()
        with get_db_connection(main_context) as conn:
            cur = conn.execute(
                "INSERT INTO active_jobs (session_id, message, started_at) VALUES (?, ?, ?)",
                (session_id, message, started_at),
            )
            return cur.lastrowid
    except Exception as exc:  # ponytail: a logging miss must never abort the run
        print(f"[runs_db] Failed to start job {session_id}: {exc}")
        return None


def finish_job(job_id, main_context: CodeeMainContext) -> None:
    """Remove an in-flight job row once its subprocess returns. No-op on None."""
    if job_id is None:
        return
    try:
        with get_db_connection(main_context) as conn:
            conn.execute("DELETE FROM active_jobs WHERE id = ?", (job_id,))
    except Exception as exc:
        print(f"[runs_db] Failed to finish job {job_id}: {exc}")


def active_jobs(main_context: CodeeMainContext) -> list[dict]:
    """In-flight jobs, youngest-first, each with elapsed seconds. [] on empty/missing DB."""
    try:
        init(main_context)
        with get_db_connection(main_context) as conn:
            rows = conn.execute(
                "SELECT id, session_id, message, started_at FROM active_jobs"
            ).fetchall()
    except Exception as exc:
        print(f"[runs_db] Failed to read active jobs: {exc}")
        return []
    now = datetime.now(timezone.utc)
    jobs = []
    for job_id, session_id, message, started_at in rows:
        try:
            elapsed = int(
                (now - datetime.fromisoformat(started_at)).total_seconds())
        except (ValueError, TypeError):
            elapsed = 0  # ponytail: bad timestamp -> show 0, don't drop the row
        jobs.append({"id": job_id, "session_id": session_id, "message": message,
                     "started_at": started_at, "elapsed": max(elapsed, 0)})
    jobs.sort(key=lambda j: j["elapsed"])
    return jobs


def fmt_elapsed(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


def runs_by_hour(main_context: CodeeMainContext) -> list[dict]:
    """Run counts bucketed by hour over the trailing 24h (UTC), oldest-first.

    Always returns 24 buckets (so empty hours show as 0). Never raises.
    """
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    buckets = [now - timedelta(hours=h) for h in range(23, -1, -1)]
    counts = {b: 0 for b in buckets}
    try:
        init(main_context)
        with get_db_connection(main_context) as conn:
            rows = conn.execute(
                "SELECT started_at FROM runs WHERE started_at >= ?", (buckets[0].isoformat(
                ),)
            ).fetchall()
        for (ts,) in rows:
            try:
                hour = datetime.fromisoformat(ts).astimezone(timezone.utc).replace(
                    minute=0, second=0, microsecond=0)
            except (ValueError, TypeError):
                continue  # ponytail: skip a malformed timestamp, don't drop the whole chart
            if hour in counts:
                counts[hour] += 1
    except Exception as exc:
        print(f"[runs_db] Failed to bucket runs by hour: {exc}")
    return [{"hour": b.strftime("%H:00"), "runs": counts[b]} for b in buckets]
