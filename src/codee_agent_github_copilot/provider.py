import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from codee_agent_abstract.provider import AbstractCodingAgent, AgentModel
from codee_main_context.context import Settings
from codee_main_context.logging import get_logger


log = get_logger(__name__)

# Events the CLI streams that carry the pieces we care about. Everything else in
# the JSONL stream (tool calls, MCP chatter, usage checkpoints) is skipped.
ASSISTANT_MESSAGE = "assistant.message"
SESSION_ERROR = "session.error"
RESULT = "result"

# `copilot` has no "list models" command, but its Agent Client Protocol mode
# answers session/new with the account's live catalog — ids and display names
# both. That's the only way to ask the CLI what it can run.
ACP_TIMEOUT_SECONDS = 60
_INITIALIZE_ID = 1
_SESSION_NEW_ID = 2


class GitHubCopilotAgent(AbstractCodingAgent):
    """Runs the ``copilot`` CLI in a fresh session, under the id the caller supplies."""

    # Ceiling on what one run may spend. AI credits bill at $0.04 each, so this
    # is the same $20 cap the Claude Code agent puts on a run. Minimum is 30.
    MAX_AI_CREDITS = "500"
    TIMEOUT_SECONDS = 7200  # 2 hours

    def __init__(self, settings: Settings, cwd: Path):
        super().__init__(settings, cwd)

    @classmethod
    def list_models(cls) -> list[AgentModel]:
        try:
            return _fetch_acp_models()
        except Exception as exc:
            # The picker falls back to free text, so a CLI that isn't installed
            # or isn't logged in must not break the admin UI.
            log.warning("Could not read the copilot model catalog: %s", exc)
            return []

    def run(self, user_message: str, session_id: str, model: str = "") -> str:
        cmd = [
            "copilot",
            "-p", user_message,
            "--session-id", session_id,
            "--max-ai-credits", self.MAX_AI_CREDITS,
            "--output-format", "json",
            # Tools, paths and URLs: the equivalent of claude's bypassPermissions.
            "--allow-all",
            # Nothing is around to answer questions in a headless run.
            "--no-ask-user",
            "--no-color",
        ]
        # Copilot never sees the skill's frontmatter — the triggers hand it the
        # body alone — so a skill's model only takes effect via this flag.
        if model:
            cmd += ["--model", model]

        log.info("Running copilot with message: %s", user_message)
        log.debug("cwd=%s cmd=%s", self._cwd, " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT_SECONDS,
                cwd=self._cwd,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Copilot CLI timed out after 2 hours")

        log.debug("copilot exited %d (%d bytes stdout, %d bytes stderr)",
                  result.returncode, len(result.stdout), len(result.stderr))

        reply, outcome, errors = _parse_events(result.stdout)

        # Raise on any non-success so callers retry. A run that fails before the
        # session starts (bad model, no auth) exits non-zero with nothing on
        # stdout; one that fails mid-run reports it in the trailing result event.
        if result.returncode != 0:
            raise RuntimeError(
                f"Copilot CLI exited {result.returncode}: "
                f"{_detail(errors, result.stderr)}"
            )
        if outcome is None:
            # Not the JSONL we know how to read — hand back whatever it printed
            # rather than failing a run that the CLI itself called successful.
            log.warning("copilot produced no result event; returning raw output")
            return result.stdout
        if outcome.get("exitCode"):
            raise RuntimeError(
                f"Copilot run errored (exit code {outcome['exitCode']}): "
                f"{_detail(errors, result.stderr)}"
            )
        if not reply:
            raise RuntimeError(
                f"Copilot run produced no response: {_detail(errors, result.stderr)}"
            )
        return reply


def _fetch_acp_models() -> list[AgentModel]:
    """Ask ``copilot --acp`` for the account's model catalog.

    Speaks just enough of the Agent Client Protocol to get a session: initialize,
    then session/new, whose result carries ``models.availableModels``. The session
    it opens is a throwaway that no prompt is ever sent to.
    """
    process = subprocess.Popen(
        ["copilot", "--acp"],
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
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
            },
        })
        _send(process, _SESSION_NEW_ID, "session/new", {
            "cwd": str(Path.cwd()),
            "mcpServers": [],
        })

        result = _await_result(process, lines, _SESSION_NEW_ID)
    finally:
        process.kill()

    available = (result.get("models") or {}).get("availableModels") or []
    models = []
    for entry in available:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("modelId", "")).strip()
        if not model_id:
            continue
        name = str(entry.get("name", "")).strip() or model_id
        models.append(AgentModel(model_id, name))
    log.debug("copilot reported %d model(s)", len(models))
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

    Everything else on the wire — the initialize reply, the session/update
    notifications the CLI starts emitting straight away — is skipped.
    """
    deadline = time.monotonic() + ACP_TIMEOUT_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"copilot --acp did not answer within {ACP_TIMEOUT_SECONDS}s")
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            # An immediate exit means the CLI is missing, unauthenticated, or
            # too old for --acp; no point waiting out the whole deadline.
            if process.poll() is not None:
                raise RuntimeError(
                    f"copilot --acp exited {process.returncode}: "
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
            raise RuntimeError(f"copilot --acp errored: {message['error']}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def _parse_events(stdout: str) -> tuple[str, dict | None, list[str]]:
    """Pull the reply, the trailing result event and any errors out of the JSONL.

    Returns ``("", None, [])`` for output that isn't the JSONL stream at all, so
    the caller can tell "no result event" from "the run failed".
    """
    reply = ""
    outcome: dict | None = None
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
        if kind == ASSISTANT_MESSAGE:
            # Keep the last message that actually said something: the ones in
            # between carry only tool requests, and reasoning is a separate field.
            content = (event.get("data") or {}).get("content") or ""
            if content.strip():
                reply = content.strip()
        elif kind == SESSION_ERROR:
            data = event.get("data") or {}
            message = data.get("message") or data.get("errorType") or "unknown error"
            errors.append(str(message))
        elif kind == RESULT:
            outcome = event

    return reply, outcome, errors


def _detail(errors: list[str], stderr: str) -> str:
    """Best available explanation of a failure, trimmed for the log line."""
    return ("; ".join(errors) or stderr.strip() or "no error detail")[:500]
