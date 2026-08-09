import json
import subprocess
from pathlib import Path

from codee_agent_abstract.provider import AbstractCodingAgent
from codee_main_context.context import Settings
from codee_main_context.logging import get_logger


log = get_logger(__name__)

# Events the CLI streams that carry the pieces we care about. Everything else in
# the JSONL stream (tool calls, MCP chatter, usage checkpoints) is skipped.
ASSISTANT_MESSAGE = "assistant.message"
SESSION_ERROR = "session.error"
RESULT = "result"


class GitHubCopilotAgent(AbstractCodingAgent):
    """Runs the ``copilot`` CLI in a fresh session, under the id the caller supplies."""

    # Ceiling on what one run may spend. AI credits bill at $0.04 each, so this
    # is the same $20 cap the Claude Code agent puts on a run. Minimum is 30.
    MAX_AI_CREDITS = "500"
    TIMEOUT_SECONDS = 7200  # 2 hours

    def __init__(self, settings: Settings, cwd: Path):
        super().__init__(settings, cwd)

    def run(self, user_message: str, session_id: str) -> str:
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
