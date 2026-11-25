"""Messages other processes ask the executor to run an agent on.

The admin process answers MCP calls but must not run agents itself: account
rotation, the dashboard's Kill and the run log all live in the executor. So a
call is a row here. The caller inserts it and waits; the executor claims it,
runs the agent and writes the outcome back.

A conversation belongs to whoever started it. ``agent_conversations`` is the
record of that, and the only sessions a caller may continue: every other
session on this host (issue runs, people's own) is out of reach.
"""
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from codee_database.database import get_db_connection
from codee_main_context.context import CodeeMainContext
from codee_main_context.logging import get_logger

from codee.lib import runs_db

log = get_logger(__name__)

QUEUED, RUNNING = "queued", "running"
SUCCEEDED, FAILED, CANCELLED = "succeeded", "failed", "cancelled"
KEEP_FINISHED = timedelta(days=1)
WORKER_POLL_SECONDS = 0.5


class RequestError(Exception):
    """A request that can't be accepted; the message is for the caller."""


@dataclass(frozen=True)
class AgentRequest:
    id: str
    owner: str
    message: str
    provider: str
    model: str
    session_id: str
    resume: bool
    status: str
    response: str
    error: str
    job_id: int | None


_COLUMNS = ", ".join(AgentRequest.__dataclass_fields__)


def init(context: CodeeMainContext) -> None:
    with closing(get_db_connection(context)) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agent_requests (
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                message TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                session_id TEXT NOT NULL,
                resume INTEGER NOT NULL,
                status TEXT NOT NULL,
                response TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                job_id INTEGER,
                created_at TEXT NOT NULL
            )""")
        # One turn at a time per conversation, whoever else is asking.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_requests_open"
            " ON agent_requests(session_id)"
            " WHERE status IN ('queued', 'running')")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agent_conversations (
                session_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL
            )""")


def submit(context: CodeeMainContext, owner: str, message: str,
           provider: str = "", model: str = "", session_id: str = "") -> str:
    """Queue a message for the executor; returns the request's id.

    With ``session_id`` the provider and model are the conversation's own; a
    different model may be asked for, a different provider may not.
    """
    init(context)
    now = datetime.now(timezone.utc)
    request_id = str(uuid.uuid4())
    with closing(get_db_connection(context)) as conn, conn:
        conn.execute(
            "DELETE FROM agent_requests WHERE created_at < ?"
            " AND status NOT IN ('queued', 'running')",
            ((now - KEEP_FINISHED).isoformat(),))
        if session_id:
            row = conn.execute(
                "SELECT provider, model FROM agent_conversations"
                " WHERE session_id = ? AND owner = ?",
                (session_id, owner)).fetchone()
            if row is None:
                raise RequestError(
                    f"Unknown session_id {session_id}. Use one a previous "
                    "call returned, or leave it out to start a conversation.")
            if provider and provider != row[0]:
                raise RequestError(
                    f"Session {session_id} runs on {row[0]}; leave provider "
                    "out or start a new conversation.")
            provider, model = row[0], model or row[1]
        try:
            conn.execute(
                "INSERT INTO agent_requests (id, owner, message, provider,"
                " model, session_id, resume, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, owner, message, provider, model,
                 session_id or str(uuid.uuid4()), bool(session_id), QUEUED,
                 now.isoformat()))
        except sqlite3.IntegrityError:
            raise RequestError(f"Session {session_id} is still working on the "
                               "previous message.") from None
    return request_id


def get(context: CodeeMainContext, request_id: str) -> AgentRequest | None:
    with closing(get_db_connection(context)) as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM agent_requests WHERE id = ?",
            (request_id,)).fetchone()
    return _request(row) if row else None


def cancel(context: CodeeMainContext, request_id: str,
           only_queued: bool = False) -> bool:
    """Drop a queued request, or stop a running one's agent.

    False when there was nothing left to cancel.
    """
    with closing(get_db_connection(context)) as conn, conn:
        states = (QUEUED, QUEUED if only_queued else RUNNING)
        done = conn.execute(
            "UPDATE agent_requests SET status = ?, error = 'Cancelled'"
            " WHERE id = ? AND status IN (?, ?)",
            (CANCELLED, request_id, *states)).rowcount
        job = conn.execute("SELECT job_id FROM agent_requests WHERE id = ?",
                           (request_id,)).fetchone()
    if done and job[0] is not None:
        runs_db.kill_job(job[0], main_context=context)
    return bool(done)


def _request(row: tuple) -> AgentRequest:
    values = dict(zip(AgentRequest.__dataclass_fields__, row))
    return AgentRequest(**{**values, "resume": bool(values["resume"])})


class RequestWorker:
    """The executor's side: claims queued requests and runs their agents.

    Has its own ``limit`` slots rather than sharing the tasks': a task's agent
    that messages Codee must not wait for the slot it is itself holding.
    """

    def __init__(self, run_agent: Callable[..., str],
                 context: CodeeMainContext, paused: Callable[[], bool],
                 limit: Callable[[], int]) -> None:
        self.run_agent = run_agent
        self.context = context
        self.paused = paused
        self.limit = limit
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        init(self.context)
        with closing(get_db_connection(self.context)) as conn, conn:
            conn.execute(
                "UPDATE agent_requests SET status = ?, error = ?"
                " WHERE status IN (?, ?)",
                (FAILED, "Codee restarted before this message was answered.",
                 QUEUED, RUNNING))
        threading.Thread(target=self._run_forever, name="agent-requests",
                         daemon=True).start()

    def _run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:
                log.exception("Agent requests failed: %s", exc)
            time.sleep(WORKER_POLL_SECONDS)

    def tick(self) -> None:
        while not self.paused():
            with self._lock:
                if len(self._running) >= self.limit():
                    return
            request = self._claim()
            if request is None:
                return
            with self._lock:
                self._running.add(request.id)
            threading.Thread(target=self._run, args=(request,),
                             name=f"agent-request-{request.id}").start()

    def _claim(self) -> AgentRequest | None:
        with closing(get_db_connection(self.context)) as conn, conn:
            row = conn.execute(
                "UPDATE agent_requests SET status = ? WHERE id = ("
                "SELECT id FROM agent_requests WHERE status = ?"
                " ORDER BY created_at, rowid LIMIT 1)"
                f" RETURNING {_COLUMNS}", (RUNNING, QUEUED)).fetchone()
        return _request(row) if row else None

    def _run(self, request: AgentRequest) -> None:
        started_at = datetime.now(timezone.utc).isoformat()
        session_id = request.session_id

        def on_job(job_id: int | None) -> None:
            self._update(request.id, job_id=job_id)
            current = get(self.context, request.id)
            if job_id is not None and current.status == CANCELLED:
                runs_db.kill_job(job_id, main_context=self.context)

        def on_session(opened: str) -> None:
            nonlocal session_id
            session_id = opened
            self._update(request.id, session_id=opened)
            self._remember(request, opened)

        def record(status: str, **outcome) -> None:
            runs_db.record_run(
                "send_message", "mcp", request.session_id, status,
                started_at=started_at, message=request.message,
                user_message=request.message, main_context=self.context,
                **outcome)

        # The agents' CLIs take the message as an argument; one starting with
        # a dash would be read as an option.
        message = request.message
        if message.startswith("-"):
            message = "\n" + message
        log.info("Message from %s, session %s", request.owner, session_id)
        try:
            response = self.run_agent(
                message, request.session_id, request.model, request.provider,
                label=request.message, resume=request.resume, on_job=on_job,
                on_session=on_session)
        except runs_db.JobKilled as exc:
            record("killed", error=str(exc))
            self._finish(request.id, FAILED, error=str(exc))
        except Exception as exc:
            log.warning("Message from %s failed: %s", request.owner, exc)
            record("failed", error=str(exc)[:500])
            self._finish(request.id, FAILED, error=str(exc)[:500])
        else:
            record("succeeded", response=response)
            self._remember(request, session_id)
            self._finish(request.id, SUCCEEDED, response=str(response))
        finally:
            with self._lock:
                self._running.discard(request.id)

    def _remember(self, request: AgentRequest, session_id: str) -> None:
        # Never re-owned: the first owner of a session keeps it.
        with closing(get_db_connection(self.context)) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO agent_conversations"
                " (session_id, owner, provider, model) VALUES (?, ?, ?, ?)",
                (session_id, request.owner, request.provider, request.model))

    def _update(self, request_id: str, **values) -> None:
        columns = ", ".join(f"{name} = ?" for name in values)
        with closing(get_db_connection(self.context)) as conn, conn:
            conn.execute(f"UPDATE agent_requests SET {columns} WHERE id = ?",
                         (*values.values(), request_id))

    def _finish(self, request_id: str, status: str, response: str = "",
                error: str = "") -> None:
        # A cancelled request stays cancelled.
        with closing(get_db_connection(self.context)) as conn, conn:
            conn.execute(
                "UPDATE agent_requests SET status = ?, response = ?, error = ?"
                " WHERE id = ? AND status = ?",
                (status, response, error, request_id, RUNNING))
