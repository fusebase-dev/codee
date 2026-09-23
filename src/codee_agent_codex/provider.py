import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from codee_agent_abstract.provider import AbstractCodingAgent, AgentModel
from codee_main_context.context import Settings
from codee_main_context.logging import get_logger

# Reaching into `codee.lib` from an agent package is the exception rather than
# the rule, and it is here because Codex is the one agent that cannot read
# `.mcp.json` itself: the servers have to be lifted out of that file and handed
# to the CLI per run. What shape the file has is mcp_config's business, and
# duplicating it here would be the worse trade.
from codee.lib.mcp_config import read_mcp_servers


log = get_logger(__name__)

# Events `codex exec --json` streams that carry the pieces we care about.
# Everything else (reasoning, command executions, MCP chatter, token usage) is
# skipped.
THREAD_STARTED = "thread.started"
TURN_COMPLETED = "turn.completed"
TURN_FAILED = "turn.failed"
ITEM_COMPLETED = "item.completed"
ERROR = "error"
AGENT_MESSAGE = "agent_message"

# `codex` has no "list models" command, but its app server answers `model/list`
# with the account's live catalog — ids and display names both. That's the only
# way to ask the CLI what it can run.
APP_SERVER_TIMEOUT_SECONDS = 60
_INITIALIZE_ID = 1
_MODEL_LIST_ID = 2


class CodexAgent(AbstractCodingAgent):
    """Runs the ``codex`` CLI headless, in a fresh thread per run."""

    DISPLAY_NAME = "Codex"
    CLI_COMMAND = "codex"
    TIMEOUT_SECONDS = 7200  # 2 hours
    # Codex's catalog carries only versioned ids and no latest-tier alias, so the
    # best model has to be named outright and bumped when a newer one lands.
    BEST_MODEL = "gpt-6-astra"

    def __init__(self, settings: Settings, cwd: Path):
        super().__init__(settings, cwd)

    @classmethod
    def best_model(cls) -> str:
        return cls.BEST_MODEL

    @classmethod
    def list_models(cls) -> list[AgentModel]:
        try:
            return _fetch_app_server_models()
        except Exception as exc:
            # The picker falls back to free text, so a CLI that isn't installed
            # or isn't logged in must not break the admin UI.
            log.warning("Could not read the codex model catalog: %s", exc)
            return []

    def run(self, user_message: str, session_id: str, model: str = "",
            on_session_id: Callable[[str], None] | None = None) -> str:
        # ``session_id`` is not passed on: Codex mints its own thread id and has
        # no flag to be handed one. It announces that id on its first line of
        # output, which is what ``on_session_id`` carries back — reported while
        # the run is still going, because the dashboard links a session only for
        # as long as it is running.
        log.info("Running codex with message: %s", user_message)
        result = self._exec(user_message, model, on_session_id)

        reply, completed, errors = _parse_events(result.stdout)

        # Raise on any non-success so callers retry. A run that fails before the
        # turn starts (bad model, no auth) exits non-zero with the reason in the
        # stream rather than on stderr; one that fails mid-turn ends in
        # `turn.failed` instead of `turn.completed`.
        if result.returncode != 0:
            raise RuntimeError(
                f"Codex CLI exited {result.returncode}: "
                f"{_detail(errors, result.stderr)}"
            )
        if not completed:
            if not errors and not reply:
                # Not the JSONL we know how to read — hand back whatever it
                # printed rather than failing a run the CLI itself called
                # successful.
                log.warning(
                    "codex produced no turn.completed event; returning raw output")
                return result.stdout
            raise RuntimeError(
                f"Codex run errored: {_detail(errors, result.stderr)}")
        if not reply:
            raise RuntimeError(
                f"Codex run produced no response: {_detail(errors, result.stderr)}")
        return reply

    def continue_conversation(
        self,
        user_message: str,
        session_id: str,
        model: str = "",
        on_session_id: Callable[[str], None] | None = None,
    ) -> str:
        result = self._exec(user_message, model, on_session_id, session_id)
        reply, completed, errors = _parse_events(result.stdout)
        if result.returncode != 0:
            raise RuntimeError(
                f"Codex CLI exited {result.returncode}: "
                f"{_detail(errors, result.stderr)}"
            )
        if not completed:
            raise RuntimeError(
                f"Codex run errored: {_detail(errors, result.stderr)}")
        if not reply:
            raise RuntimeError(
                f"Codex run produced no response: {_detail(errors, result.stderr)}")
        return reply

    def _exec(self, user_message: str, model: str,
              on_session_id: Callable[[str], None] | None = None,
              resume_session_id: str = "",
              ) -> subprocess.CompletedProcess:
        """One headless ``codex exec`` in a thread of its own."""
        cmd = [
            self.CLI_COMMAND, "exec", *
            (["resume"] if resume_session_id else []),
            "--json",
            # Codee's project root is a repository in the normal case but need
            # not be one, and `codex exec` refuses to start outside git.
            "--skip-git-repo-check",
            # Tools, writes and network, with nothing asked of a human who isn't
            # there: the equivalent of claude's bypassPermissions and copilot's
            # --allow-all. Codee's agent is expected to edit the worktrees it
            # was pointed at, so the sandbox would only fail the run.
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        # Codex never sees the skill's frontmatter — the triggers hand it the
        # body alone — so a skill's model only takes effect via this flag.
        if model:
            cmd += ["--model", model]
        cmd += _mcp_overrides(self._cwd)
        # The prompt goes last, behind `--`, so a skill whose slug collides with
        # a subcommand (`codex exec review`) still reaches the model.
        cmd += ["--"]
        if resume_session_id:
            cmd += [resume_session_id]
        cmd += [user_message]

        log.debug("cwd=%s cmd=%s", self._cwd, " ".join(cmd))
        # Streamed rather than collected at the end: the thread id is on the
        # first line, and a caller that only learned it after the process exited
        # would learn it exactly when it stopped being useful.
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self._cwd,
            # The prompt is on the command line; codex would otherwise wait on
            # stdin for more of it and append whatever it inherited.
            stdin=subprocess.DEVNULL,
        )
        out: list[str] = []
        err: list[str] = []
        # Both pipes are drained on their own threads. stderr is small but has
        # to be read as it comes: a full pipe would block codex mid-run.
        readers = [
            threading.Thread(target=_collect, daemon=True,
                             args=(process.stdout, out, on_session_id)),
            threading.Thread(target=_collect, daemon=True,
                             args=(process.stderr, err, None)),
        ]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=self.TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RuntimeError("Codex CLI timed out after 2 hours")
        finally:
            for reader in readers:
                reader.join(timeout=5)

        result = subprocess.CompletedProcess(
            cmd, process.returncode, "".join(out), "".join(err))
        log.debug("codex exited %d (%d bytes stdout, %d bytes stderr)",
                  result.returncode, len(result.stdout), len(result.stderr))
        return result


def _collect(stream, lines: list[str],
             on_session_id: Callable[[str], None] | None) -> None:
    """Keep every line, and report the thread id the moment codex names one.

    A callback that raises is logged and dropped: it is bookkeeping on the side
    of a run that is otherwise going fine, and must not take the run down.
    """
    for line in stream:
        lines.append(line)
        if on_session_id is None:
            continue
        thread_id = _started_thread(line)
        if not thread_id:
            continue
        report, on_session_id = on_session_id, None  # one thread per run
        log.debug("codex opened thread %s", thread_id)
        try:
            report(thread_id)
        except Exception as exc:
            log.warning("Could not report codex thread %s: %s", thread_id, exc)
    stream.close()


def _started_thread(line: str) -> str:
    """The thread id in a ``thread.started`` line, or "" for any other line."""
    line = line.strip()
    if not line or THREAD_STARTED not in line:
        return ""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if not isinstance(event, dict) or event.get("type") != THREAD_STARTED:
        return ""
    return str(event.get("thread_id", "")).strip()


def _mcp_overrides(root: Path) -> list[str]:
    """The project's MCP servers, as ``-c`` config overrides for one run.

    Codex reads its servers from ``[mcp_servers]`` in ``config.toml`` and never
    from the project's ``.mcp.json``, so the file Codee writes for Claude Code
    and Copilot is carried onto the command line instead. Per run rather than
    into the user's ``config.toml``: the servers belong to the project Codee is
    driving, and writing them into a machine-wide file would leak the project's
    credentials into every other codex session on the box.
    """
    overrides = []
    for name, server in read_mcp_servers(root).items():
        entry = {"command": server["command"], "args": list(server["args"])}
        if server["env"]:
            entry["env"] = dict(server["env"])
        overrides += ["-c", f"mcp_servers.{name}={_toml(entry)}"]
    return overrides


def _toml(value) -> str:
    """The TOML literal for a value ``-c`` will accept.

    Only what an MCP server entry is made of: strings, lists of them, and tables
    of them. TOML basic strings escape the way JSON strings do, so the quoting
    is borrowed; inline tables are the one place the two syntaxes differ.
    """
    if isinstance(value, dict):
        fields = ", ".join(f"{json.dumps(str(key))} = {_toml(item)}"
                           for key, item in value.items())
        return f"{{{fields}}}"
    if isinstance(value, (list, tuple)):
        return f"[{', '.join(_toml(item) for item in value)}]"
    return json.dumps(str(value))


def _fetch_app_server_models() -> list[AgentModel]:
    """Ask ``codex app-server`` for the account's model catalog.

    Speaks just enough of the app server protocol to get an answer: initialize,
    then ``model/list``. No thread is started and no prompt is ever sent.
    """
    process = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=Path.cwd(),
    )
    try:
        lines: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(
            target=_drain, args=(process.stdout, lines), daemon=True)
        reader.start()

        _send(process, _INITIALIZE_ID, "initialize", {
            "clientInfo": {"name": "codee", "title": "Codee", "version": "1"},
        })
        _send(process, _MODEL_LIST_ID, "model/list", {})

        result = _await_result(process, lines, _MODEL_LIST_ID)
    finally:
        process.kill()

    models = []
    for entry in result.get("data") or []:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id", "")).strip()
        if not model_id:
            continue
        name = str(entry.get("displayName", "")).strip() or model_id
        models.append(AgentModel(model_id, name))
    log.debug("codex reported %d model(s)", len(models))
    return models


def _send(process: subprocess.Popen, request_id: int, method: str, params: dict) -> None:
    request = {"jsonrpc": "2.0", "id": request_id,
               "method": method, "params": params}
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()


def _drain(stream, lines: "queue.Queue[str]") -> None:
    """Pump the CLI's stdout into a queue so the read can be given a deadline."""
    for line in stream:
        lines.put(line)


def _await_result(
    process: subprocess.Popen, lines: "queue.Queue[str]", request_id: int
) -> dict:
    """Read until the response to ``request_id`` arrives, or the deadline passes.

    Everything else on the wire — the initialize reply, the config warnings and
    status notifications the server emits straight away — is skipped.
    """
    deadline = time.monotonic() + APP_SERVER_TIMEOUT_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"codex app-server did not answer within "
                f"{APP_SERVER_TIMEOUT_SECONDS}s")
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            # An immediate exit means the CLI is missing, unauthenticated, or
            # too old for the app server; no point waiting out the deadline.
            if process.poll() is not None:
                raise RuntimeError(
                    f"codex app-server exited {process.returncode}: "
                    f"{(process.stderr.read() or '').strip()[:300] or 'no output'}"
                )
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if "error" in message:
            raise RuntimeError(f"codex app-server errored: {message['error']}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def _parse_events(stdout: str) -> tuple[str, bool, list[str]]:
    """Pull the reply, the outcome and any errors out of the JSONL.

    ``completed`` is whether the turn actually finished: a run that dies partway
    ends in ``turn.failed``, and one that never starts ends after neither.
    """
    reply = ""
    completed = False
    errors: list[str] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        kind = event.get("type")
        if kind == ITEM_COMPLETED:
            item = event.get("item") or {}
            if item.get("type") == AGENT_MESSAGE:
                # Keep the last message that actually said something: the ones
                # in between narrate tool calls that follow them.
                text = str(item.get("text") or "")
                if text.strip():
                    reply = text.strip()
            elif item.get("type") == ERROR:
                errors.append(str(item.get("message") or "unknown error"))
        elif kind == TURN_COMPLETED:
            completed = True
        elif kind == TURN_FAILED:
            errors.append(str((event.get("error") or {}).get("message")
                              or "unknown error"))
        elif kind == ERROR:
            errors.append(str(event.get("message") or "unknown error"))

    return reply, completed, errors


def _detail(errors: list[str], stderr: str) -> str:
    """Best available explanation of a failure, trimmed for the log line."""
    return ("; ".join(errors) or stderr.strip() or "no error detail")[:500]
