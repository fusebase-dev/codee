import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codee_main_context.context import CodeeMainContext

from codee.lib import runs_db


def _ctx(data_dir: Path) -> CodeeMainContext:
    return CodeeMainContext(data_dir=data_dir)


def _bad_ctx(tmp_path: Path) -> CodeeMainContext:
    # A data_dir that doesn't exist -> sqlite can't open the file -> must be swallowed.
    return _ctx(tmp_path / "missing" / "deeper")


def _db_file(data_dir: Path) -> Path:
    return data_dir / "codee.db"


def test_round_trip_newest_first(tmp_path):
    ctx = _ctx(tmp_path)
    runs_db.record_run("skill-a", "cron", "sid-1", "succeeded",
                       started_at="2026-06-27T10:00:00+00:00", main_context=ctx)
    runs_db.record_run("skill-b", "email", "sid-2", "failed", error="boom",
                       started_at="2026-06-27T11:00:00+00:00", main_context=ctx)

    rows = runs_db.recent_runs(main_context=ctx)
    assert [r["skill_name"] for r in rows] == ["skill-b", "skill-a"]  # newest first
    assert rows[0]["trigger_type"] == "email"
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "boom"
    assert rows[0]["session_id"] == "sid-2"


def test_empty_db_returns_empty_list(tmp_path):
    assert runs_db.recent_runs(main_context=_ctx(tmp_path)) == []


def test_record_run_never_raises_on_bad_path(tmp_path):
    # An unopenable db path must be swallowed (FR-009).
    runs_db.record_run("skill", "cron", "sid", "succeeded",
                       main_context=_bad_ctx(tmp_path))


def test_main_context_is_required():
    # The whole module keys off main_context.data_dir; a missing context must fail
    # loudly at the call site instead of being swallowed as a logging miss.
    for call in (lambda: runs_db.record_run("s", "cron", "sid", "succeeded"),
                 lambda: runs_db.recent_runs(),
                 lambda: runs_db.start_job("sid", "m")):
        try:
            call()
        except TypeError:
            continue
        raise AssertionError("main_context must be a required keyword argument")


# ---------------------------------------------------------------- counts() (US1)
def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_counts_total_and_last_24h(tmp_path):
    ctx = _ctx(tmp_path)
    # Two inside the 24h window, one well outside, one exactly at the boundary (excluded, strict >).
    runs_db.record_run("a", "cron", "s1", "succeeded", started_at=_ago(1), main_context=ctx)
    runs_db.record_run("b", "cron", "s2", "succeeded", started_at=_ago(23), main_context=ctx)
    runs_db.record_run("c", "cron", "s3", "succeeded", started_at=_ago(48), main_context=ctx)
    assert runs_db.counts(ctx) == {"total": 3, "last_24h": 2}


def test_counts_empty_db_zeros(tmp_path):
    assert runs_db.counts(_ctx(tmp_path)) == {"total": 0, "last_24h": 0}


def test_counts_never_raises_on_bad_path(tmp_path):
    assert runs_db.counts(_bad_ctx(tmp_path)) == {"total": 0, "last_24h": 0}


def test_runs_by_hour_buckets(tmp_path):
    ctx = _ctx(tmp_path)
    runs_db.record_run("a", "cron", "s1", "succeeded", started_at=_ago(2), main_context=ctx)
    runs_db.record_run("b", "cron", "s2", "succeeded", started_at=_ago(2.1), main_context=ctx)
    runs_db.record_run("c", "cron", "s3", "succeeded", started_at=_ago(48), main_context=ctx)  # out of window

    hourly = runs_db.runs_by_hour(ctx)
    assert len(hourly) == 24  # always 24 buckets
    # oldest-first: buckets are the last 24 hour labels in ascending time order
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    expected = [(now - timedelta(hours=h)).strftime("%H:00") for h in range(23, -1, -1)]
    assert [h["hour"] for h in hourly] == expected
    assert sum(h["runs"] for h in hourly) == 2  # 48h-old run excluded; the two ~2h-old runs counted


def test_runs_by_hour_empty_and_bad_path(tmp_path):
    bad = _bad_ctx(tmp_path)
    assert all(h["runs"] == 0 for h in runs_db.runs_by_hour(bad))
    assert len(runs_db.runs_by_hour(bad)) == 24


# ---------------------------------------------------------------- message column (US2)
def test_message_round_trip(tmp_path):
    ctx = _ctx(tmp_path)
    runs_db.record_run("a", "cron", "s1", "succeeded", message="hello", main_context=ctx)
    runs_db.record_run("b", "email", "s2", "succeeded", message="", main_context=ctx)
    runs_db.record_run("c", "aws-sqs", "s3", "succeeded", main_context=ctx)  # message defaults None
    by_skill = {r["skill_name"]: r["message"] for r in runs_db.recent_runs(main_context=ctx)}
    assert by_skill == {"a": "hello", "b": "", "c": None}


# ---------------------------------------------------------------- active_jobs
def test_active_job_lifecycle(tmp_path):
    ctx = _ctx(tmp_path)
    jid = runs_db.start_job("sid-1", "/task-developer NIM-1", main_context=ctx)
    jobs = runs_db.active_jobs(ctx)
    assert len(jobs) == 1
    assert jobs[0]["session_id"] == "sid-1"
    assert jobs[0]["message"] == "/task-developer NIM-1"
    assert jobs[0]["elapsed"] >= 0

    runs_db.finish_job(jid, main_context=ctx)
    assert runs_db.active_jobs(ctx) == []


def test_active_jobs_elapsed_and_order(tmp_path):
    ctx = _ctx(tmp_path)
    runs_db.start_job("young", "b", started_at=_ago(0.01), main_context=ctx)
    runs_db.start_job("old", "a", started_at=_ago(1), main_context=ctx)
    jobs = runs_db.active_jobs(ctx)
    assert [j["session_id"] for j in jobs] == ["young", "old"]  # youngest first
    assert jobs[1]["elapsed"] >= 3500  # ~1h old


def test_clear_active_jobs_purges_stale(tmp_path):
    ctx = _ctx(tmp_path)
    runs_db.start_job("s1", "m1", main_context=ctx)
    runs_db.start_job("s2", "m2", main_context=ctx)
    runs_db.clear_active_jobs(ctx)
    assert runs_db.active_jobs(ctx) == []


def test_active_jobs_never_raise_on_bad_path(tmp_path):
    bad = _bad_ctx(tmp_path)
    assert runs_db.active_jobs(bad) == []
    assert runs_db.start_job("s", "m", main_context=bad) is None
    runs_db.finish_job(None, main_context=bad)  # no-op, no raise
    runs_db.clear_active_jobs(bad)


def test_fmt_elapsed():
    assert runs_db.fmt_elapsed(45) == "45s"
    assert runs_db.fmt_elapsed(125) == "2m 5s"
    assert runs_db.fmt_elapsed(3700) == "1h 1m"


def test_migration_adds_message_column_without_data_loss(tmp_path):
    ctx = _ctx(tmp_path)
    # Build an old-schema DB (spec 001, no message column) with one row.
    with sqlite3.connect(_db_file(tmp_path)) as conn:
        conn.execute(
            """CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name TEXT NOT NULL, trigger_type TEXT NOT NULL,
                session_id TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT, started_at TEXT NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO runs (skill_name, trigger_type, session_id, status, started_at)"
            " VALUES ('old', 'cron', 'sid-old', 'succeeded', '2026-06-27T10:00:00+00:00')"
        )

    runs_db.init(ctx)  # must not raise; must add the column
    rows = runs_db.recent_runs(main_context=ctx)
    assert len(rows) == 1
    assert rows[0]["skill_name"] == "old"
    assert rows[0]["message"] is None  # pre-feature row reads as NULL

    # New writes carry message; the upgraded DB round-trips it.
    runs_db.record_run("new", "cron", "sid-new", "succeeded", message="m", main_context=ctx)
    assert runs_db.recent_runs(main_context=ctx)[0]["message"] == "m"
