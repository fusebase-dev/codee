import json
import subprocess
from pathlib import Path

from codee_agent_abstract.provider import AbstractCodingAgent
from codee_main_context.context import Settings
from codee_main_context.logging import get_logger


log = get_logger(__name__)


class ClaudeCodeAgent(AbstractCodingAgent):
    """Runs the ``claude`` CLI, resuming a session when one already exists on disk."""

    # Where the claude CLI stores its per-session transcripts.
    SESSIONS_DIR = Path.home() / ".claude" / "projects" / \
        "-home-ubuntu-claude-coder"

    MAX_BUDGET_USD = "20.00"
    TIMEOUT_SECONDS = 7200  # 2 hours

    def __init__(self, settings: Settings, cwd: Path):
        super().__init__(settings, cwd)

    def session_exists(self, session_id: str) -> bool:
        return (self.SESSIONS_DIR / f"{session_id}.jsonl").exists()

    def run(self, user_message: str, session_id: str) -> str:
        cmd = [
            "claude",
            "-p", user_message,
            "--max-budget-usd", self.MAX_BUDGET_USD,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
        ]

        # Resume an existing session, otherwise start one with a fixed id.
        if self.session_exists(session_id):
            cmd.extend(["--resume", session_id])
            log.debug("resuming session %s from %s",
                      session_id, self.SESSIONS_DIR)
        else:
            cmd.extend(["--session-id", session_id])
            log.debug("no transcript for %s in %s; starting a new session",
                      session_id, self.SESSIONS_DIR)

        log.info("Running claude with message: %s", user_message)
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
            raise RuntimeError("Claude CLI timed out after 2 hours")

        log.debug("claude exited %d (%d bytes stdout, %d bytes stderr)",
                  result.returncode, len(result.stdout), len(result.stderr))

        # Raise on any non-success so callers retry. Over-limit exits non-zero;
        # a completed-but-errored run sets is_error in the JSON.
        if result.returncode != 0:
            raise RuntimeError(
                f"Claude CLI exited {result.returncode}: {result.stderr.strip()[:500]}"
            )

        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout
        if isinstance(response, dict):
            if response.get("is_error"):
                raise RuntimeError(
                    f"Claude run errored ({response.get('subtype', 'unknown')}): "
                    f"{str(response.get('result', ''))[:500]}"
                )
            return response.get("result", result.stdout)
        return result.stdout
