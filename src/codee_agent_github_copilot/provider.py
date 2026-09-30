import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from codee_agent_abstract import acp
from codee_agent_abstract.provider import AbstractCodingAgent, AgentModel, AgentResponse
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
# both, and each model's reasoning effort levels once it is selected. That's
# the only way to ask the CLI what it can run.
ACP_COMMAND = ["copilot", "--acp"]

# Ceiling on what one run may spend. AI credits bill at $0.04 each, so the
# default is the same $20 cap the Claude Code agent puts on a run.
MAX_AI_CREDITS_ENV_VAR = "CODEE_COPILOT_MAX_AI_CREDITS"
COPILOT_DEBUG_ENV_VAR = "COPILOT_DEBUG"
DEFAULT_MAX_AI_CREDITS = 1000
# The CLI rejects anything lower outright, which reads as a broken agent rather
# than a misconfigured cap, so a smaller override is raised to it instead.
MIN_AI_CREDITS = 30

# What to call the value an issue trigger passes when the skill's frontmatter
# carries no ``argument-hint`` to name it.
DEFAULT_ARGUMENT_NAME = "ARGUMENT"


def _max_ai_credits() -> str:
    """The per-run credit cap, read fresh so a changed env applies to the next run."""
    raw = os.environ.get(MAX_AI_CREDITS_ENV_VAR, "").strip()
    if not raw:
        return str(DEFAULT_MAX_AI_CREDITS)
    try:
        return str(max(MIN_AI_CREDITS, int(raw)))
    except ValueError:
        log.warning("%s=%r is not a number, using %d",
                    MAX_AI_CREDITS_ENV_VAR, raw, DEFAULT_MAX_AI_CREDITS)
        return str(DEFAULT_MAX_AI_CREDITS)


class GitHubCopilotAgent(AbstractCodingAgent):
    """Runs the ``copilot`` CLI in a fresh session, under the id the caller supplies."""

    DISPLAY_NAME = "GitHub Copilot"
    CLI_COMMAND = "copilot"
    TIMEOUT_SECONDS = 7200  # 2 hours
    # Copilot's catalog carries only versioned ids -- its one version-free value
    # is `auto`, which picks for cost rather than capability -- so the best model
    # has to be named outright and bumped when a newer Opus lands in the catalog.
    BEST_MODEL = "claude-opus-5"

    def __init__(self, settings: Settings, cwd: Path):
        super().__init__(settings, cwd)
        self._settings = settings

    @classmethod
    def best_model(cls) -> str:
        return cls.BEST_MODEL

    def skill_prompt(self, slug: str, path: Path, argument: str = "",
                     argument_name: str = "") -> str:
        """Point copilot at the skill file instead of sending a slash command.

        `copilot` has no slash command for `.claude/skills`, and every
        issue-triggered skill sets ``disable-model-invocation: true``, so it
        won't pick the skill up on its own either. Naming the file and telling
        it to follow what's inside is the only way in. The path is relative to
        the working directory the run gets, which is where the CLI starts.
        """
        prompt = (f"Read {_relative(path, self._cwd)} and follow its "
                  "instructions exactly.")
        if argument:
            prompt += f" {argument_name or DEFAULT_ARGUMENT_NAME} = {argument}"
        return prompt

    @classmethod
    def list_models(cls) -> list[AgentModel]:
        try:
            return acp.fetch_models(ACP_COMMAND)
        except Exception as exc:
            # The picker falls back to free text, so a CLI that isn't installed
            # or isn't logged in must not break the admin UI.
            log.warning("Could not read the copilot model catalog: %s", exc)
            return []

    def run(self, user_message: str, session_id: str, model: str = "",
            on_session_id: Callable[[str], None] | None = None,
            effort: str = "") -> str:
        # The session is ours to name and the CLI is told to use it, so the
        # answer is known before the run starts.
        if on_session_id:
            on_session_id(session_id)
        prefix = self._settings.github_copilot_prompt_prefix.strip()
        if prefix:
            user_message = f"{prefix}\n\n{user_message}"
        cmd = [
            self.CLI_COMMAND,
            "-p", user_message,
            "--session-id", session_id,
            "--max-ai-credits", _max_ai_credits(),
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
        if effort:
            cmd += ["--reasoning-effort", effort]
        debug_enabled = os.environ.get(
            COPILOT_DEBUG_ENV_VAR, "").strip().lower() == "true"
        if debug_enabled:
            cmd += ["--log-level", "debug"]

        log.info("Running copilot with message: %s", user_message)
        log.debug("cwd=%s cmd=%s", self._cwd, " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.TIMEOUT_SECONDS,
                cwd=self._cwd,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Copilot CLI timed out after 2 hours")

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        log.debug("copilot exited %d (%d bytes stdout, %d bytes stderr)",
                  result.returncode, len(stdout), len(stderr))

        reply, outcome, errors = _parse_events(stdout)

        # Raise on any non-success so callers retry. A run that fails before the
        # session starts (bad model, no auth) exits non-zero with nothing on
        # stdout; one that fails mid-run reports it in the trailing result event.
        if result.returncode != 0:
            raise RuntimeError(
                f"Copilot CLI exited {result.returncode}: "
                f"{_detail(errors, stderr)}"
            )
        if outcome is None:
            # Not the JSONL we know how to read — hand back whatever it printed
            # rather than failing a run that the CLI itself called successful.
            log.warning(
                "copilot produced no result event; returning raw output")
            return AgentResponse(stdout, stderr if debug_enabled else "")
        if outcome.get("exitCode"):
            raise RuntimeError(
                f"Copilot run errored (exit code {outcome['exitCode']}): "
                f"{_detail(errors, stderr)}"
            )
        if not reply:
            raise RuntimeError(
                f"Copilot run produced no response: {_detail(errors, stderr)}"
            )
        return AgentResponse(reply, stderr if debug_enabled else "")


def _relative(path: Path, cwd: Path) -> str:
    """``path`` as the CLI will see it from ``cwd``, or absolute if it's outside."""
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


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
            message = data.get("message") or data.get(
                "errorType") or "unknown error"
            errors.append(str(message))
        elif kind == RESULT:
            outcome = event

    return reply, outcome, errors


def _detail(errors: list[str], stderr: str) -> str:
    """Best available explanation of a failure, trimmed for the log line."""
    return ("; ".join(errors) or stderr.strip() or "no error detail")[:500]
