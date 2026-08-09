import json
import subprocess
from pathlib import Path

from codee_agent_abstract.provider import AbstractCodingAgent, AgentModel
from codee_main_context.context import Settings
from codee_main_context.logging import get_logger


log = get_logger(__name__)

# The `claude` CLI has no command that enumerates its models, so this catalog is
# maintained by hand. It only feeds the admin UI's picker — the editor also takes
# a model id typed by hand, so a model missing here is still usable.
MODELS = [
    AgentModel("claude-opus-5", "Claude Opus 5"),
    AgentModel("claude-sonnet-5", "Claude Sonnet 5"),
    AgentModel("claude-fable-5", "Claude Fable 5"),
    AgentModel("claude-opus-4-8", "Claude Opus 4.8"),
    AgentModel("claude-opus-4-7", "Claude Opus 4.7"),
    AgentModel("claude-opus-4-6", "Claude Opus 4.6"),
    AgentModel("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    AgentModel("claude-haiku-4-5", "Claude Haiku 4.5"),
    AgentModel("opus", "Latest Opus (alias)"),
    AgentModel("sonnet", "Latest Sonnet (alias)"),
    AgentModel("haiku", "Latest Haiku (alias)"),
]


class ClaudeCodeAgent(AbstractCodingAgent):
    """Runs the ``claude`` CLI in a fresh session, under the id the caller supplies."""

    MAX_BUDGET_USD = "20.00"
    TIMEOUT_SECONDS = 7200  # 2 hours

    def __init__(self, settings: Settings, cwd: Path):
        super().__init__(settings, cwd)

    @classmethod
    def list_models(cls) -> list[AgentModel]:
        return list(MODELS)

    def run(self, user_message: str, session_id: str, model: str = "") -> str:
        # `model` is deliberately not forwarded as --model: Claude Code reads the
        # skill's `model:` frontmatter itself.
        cmd = [
            "claude",
            "-p", user_message,
            "--session-id", session_id,
            "--max-budget-usd", self.MAX_BUDGET_USD,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
        ]

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
