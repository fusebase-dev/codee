import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from codee_agent_abstract.provider import (
    AbstractCodingAgent, AgentModel, PeepEntry)
from codee_agent_abstract.transcript import clip, tail_jsonl, tool_summary
from codee_agent_claude_code.credentials import config_dir
from codee_main_context.context import Settings
from codee_main_context.logging import get_logger


log = get_logger(__name__)

# The `claude` CLI has no command that enumerates its models, so this catalog is
# maintained by hand. It only feeds the admin UI's picker — the editor also takes
# a model id typed by hand, so a model missing here is still usable.
#
# The effort levels are the ones Claude Code's own model table grants each model
# (`--effort` and the skill `effort:` frontmatter take the same values): models
# before the 4.6 generation, and Haiku, have no effort control at all, the 4.6
# generation has no `xhigh`, and the defaults are what Claude Code falls back to.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORTS_WITHOUT_XHIGH = ("low", "medium", "high", "max")
MODELS = [
    AgentModel("claude-opus-5-5", "Claude Opus 5.5", EFFORTS, "medium"),
    AgentModel("claude-opus-5", "Claude Opus 5", EFFORTS, "high"),
    AgentModel("claude-sonnet-5", "Claude Sonnet 5", EFFORTS, "high"),
    AgentModel("claude-fable-5-1", "Claude Fable 5.1", EFFORTS, "high"),
    AgentModel("claude-fable-5", "Claude Fable 5", EFFORTS, "high"),
    AgentModel("claude-opus-4-8", "Claude Opus 4.8", EFFORTS, "high"),
    AgentModel("claude-opus-4-7", "Claude Opus 4.7", EFFORTS, "xhigh"),
    AgentModel("claude-opus-4-6", "Claude Opus 4.6", EFFORTS_WITHOUT_XHIGH),
    AgentModel("claude-sonnet-4-6", "Claude Sonnet 4.6", EFFORTS_WITHOUT_XHIGH),
    AgentModel("claude-haiku-4-5", "Claude Haiku 4.5"),
    # The aliases follow the newest model of their tier, so they get that
    # model's levels but no default: it moves with the next release.
    AgentModel("opus", "Latest Opus (alias)", EFFORTS),
    AgentModel("sonnet", "Latest Sonnet (alias)", EFFORTS),
    AgentModel("haiku", "Latest Haiku (alias)"),
]


class ClaudeCodeAgent(AbstractCodingAgent):
    """Runs the ``claude`` CLI in a fresh session, under the id the caller supplies."""

    DISPLAY_NAME = "Claude Code"
    CLI_COMMAND = "claude"
    MAX_BUDGET_USD = "20.00"
    TIMEOUT_SECONDS = 7200  # 2 hours
    # `--model` takes an alias for the latest model of a tier, so asking for the
    # best one needs no version here and survives the next Opus release.
    BEST_MODEL = "opus"

    def __init__(self, settings: Settings, cwd: Path):
        super().__init__(settings, cwd)

    @classmethod
    def best_model(cls) -> str:
        return cls.BEST_MODEL

    @classmethod
    def list_models(cls) -> list[AgentModel]:
        return list(MODELS)

    @classmethod
    def peep(cls, session_id: str, cwd: Path,
             limit: int = 10) -> list[PeepEntry] | None:
        path = _transcript(session_id, cwd)
        if path is None:
            return []
        entries = [entry for record in tail_jsonl(path)
                   for entry in _entries(record)]
        return entries[-limit:]

    def run(self, user_message: str, session_id: str, model: str = "",
            on_session_id: Callable[[str], None] | None = None,
            effort: str = "") -> str:
        # The session is ours to name and the CLI is told to use it, so the
        # answer is known before the run starts.
        if on_session_id:
            on_session_id(session_id)
        return self._run(user_message, session_id, model, effort, False)

    def continue_conversation(
        self,
        user_message: str,
        session_id: str,
        model: str = "",
        on_session_id: Callable[[str], None] | None = None,
        effort: str = "",
    ) -> str:
        if on_session_id:
            on_session_id(session_id)
        return self._run(user_message, session_id, model, effort, True)

    def _run(self, user_message: str, session_id: str, model: str,
             effort: str, resume: bool) -> str:
        cmd = [
            self.CLI_COMMAND,
            "-p", user_message,
            "--resume" if resume else "--session-id", session_id,
            "--max-budget-usd", self.MAX_BUDGET_USD,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
        ]
        # Skill triggers pass no model: Claude Code reads the skill's `model:`
        # frontmatter itself, and a flag here would override it. Only a caller
        # with no frontmatter to read (workflow inference) names one explicitly.
        if model:
            cmd += ["--model", model]
        # The same holds for the skill's `effort:` frontmatter.
        if effort:
            cmd += ["--effort", effort]

        log.info("Running claude with message: %s", user_message)
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
            raise RuntimeError("Claude CLI timed out after 2 hours")

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        log.debug("claude exited %d (%d bytes stdout, %d bytes stderr)",
                  result.returncode, len(stdout), len(stderr))

        # Raise on any non-success so callers retry. Over-limit exits non-zero;
        # a completed-but-errored run sets is_error in the JSON.
        if result.returncode != 0:
            raise RuntimeError(
                f"Claude CLI exited {result.returncode}: {stderr.strip()[:500]}"
            )

        try:
            response = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout
        if isinstance(response, dict):
            if response.get("is_error"):
                raise RuntimeError(
                    f"Claude run errored ({response.get('subtype', 'unknown')}): "
                    f"{str(response.get('result', ''))[:500]}"
                )
            return response.get("result", stdout)
        return stdout


# A session id names a file, so anything that could step out of the projects
# directory is refused rather than looked up.
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _transcript(session_id: str, cwd: Path) -> Path | None:
    """Where the CLI is writing ``session_id``, or None before it starts.

    The CLI files a session under its cwd with every character that is not a
    letter or a digit turned into a dash. A long cwd is shortened and hashed
    instead, and a symlinked one may be resolved first, so when the expected
    folder has no such file every project folder is tried.
    """
    if not _SESSION_ID.match(session_id or ""):
        return None
    projects = config_dir() / "projects"
    name = f"{session_id}.jsonl"
    expected = projects / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / name
    if expected.is_file():
        return expected
    return next(projects.glob(f"*/{name}"), None)


def _entries(record: dict) -> list[PeepEntry]:
    """The steps one transcript line carries: what the model thought, said or ran.

    Only the model's own turns are read. The CLI saves most thinking blocks
    without their text, and those are skipped rather than shown empty.
    """
    if record.get("type") != "assistant":
        return []
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    timestamp = str(record.get("timestamp") or "")
    entries = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "thinking" and str(block.get("thinking") or "").strip():
            entries.append(PeepEntry("thinking", clip(block["thinking"]),
                                     timestamp))
        elif kind == "text" and str(block.get("text") or "").strip():
            entries.append(PeepEntry("text", clip(block["text"]), timestamp))
        elif kind == "tool_use":
            entries.append(PeepEntry(
                "tool", tool_summary(str(block.get("name") or "tool"),
                                     block.get("input")), timestamp))
    return entries
