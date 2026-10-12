import json
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from codee_agent_abstract import acp
from codee_agent_abstract.provider import (
    AbstractCodingAgent, AgentModel, AgentResponse, PeepEntry)
from codee_agent_abstract.transcript import clip, tail_jsonl, tool_summary
from codee_main_context.context import Settings
from codee_main_context.logging import get_logger


log = get_logger(__name__)

# Events the CLI streams that carry the pieces we care about. Everything else in
# the JSONL stream (tool calls, MCP chatter, usage checkpoints) is skipped.
ASSISTANT_MESSAGE = "assistant.message"
SESSION_ERROR = "session.error"
RESULT = "result"
# The session's events log, which Peep reads, also names each tool call as it
# starts.
TOOL_EXECUTION_START = "tool.execution_start"

# What a session id has to look like before it is put into a path.
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")

# `copilot` has no "list models" command, but its Agent Client Protocol mode
# answers session/new with the account's live catalog — ids and display names
# both, and each model's reasoning effort levels once it is selected. That's
# the only way to ask the CLI what it can run.
ACP_COMMAND = ["copilot", "--acp"]

# `auto` lets Copilot route each request to a model. It takes no reasoning
# effort but a routing tier, which the skill's `effort:` carries instead and
# goes to the CLI as `--auto-tier`. Balance is what runs when none is set.
AUTO_MODEL = "auto"
AUTO_TIERS = ("efficiency", "balance", "intelligence")
DEFAULT_AUTO_TIER = "balance"

# Which models take a long context window, and how long each window is, is only
# in the catalog the SDK's headless server answers `models.list` with: a model
# that offers it carries `billing.tokenPrices.longContext`. ACP doesn't say.
HEADLESS_COMMAND = ["copilot", "--headless", "--stdio", "--no-auto-update"]
DEFAULT_CONTEXT = "default"
LONG_CONTEXT = "long_context"

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
            models = acp.fetch_models(ACP_COMMAND)
        except Exception as exc:
            # The picker falls back to free text, so a CLI that isn't installed
            # or isn't logged in must not break the admin UI.
            log.warning("Could not read the copilot model catalog: %s", exc)
            return []
        try:
            windows = _context_tiers(_headless_models())
        except Exception as exc:
            # Without it the models are still worth offering, just with no
            # choice of context window.
            log.warning("Could not read copilot's context windows: %s", exc)
            windows = {}
        # ACP reports no options for Auto; its tiers come from `--auto-tier`.
        return [AgentModel(model.id, model.name, AUTO_TIERS, DEFAULT_AUTO_TIER,
                           "Tier")
                if model.id == AUTO_MODEL
                else AgentModel(model.id, model.name, model.efforts,
                                model.default_effort,
                                context_tiers=windows.get(model.id, ()))
                for model in models]

    @classmethod
    def peep(cls, session_id: str, cwd: Path,
             limit: int = 10) -> list[PeepEntry] | None:
        # ``cwd`` doesn't matter: Copilot files a session by its id alone.
        path = _events_file(session_id)
        if path is None:
            return []
        entries = [entry for record in tail_jsonl(path)
                   for entry in _events_entries(record)]
        return entries[-limit:]

    def run(self, user_message: str, session_id: str, model: str = "",
            on_session_id: Callable[[str], None] | None = None,
            effort: str = "", context_tier: str = "") -> str:
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
        # Auto's tier is passed even when unset, so a run doesn't pick up
        # whatever tier the machine's own Copilot config has stored.
        if model == AUTO_MODEL:
            cmd += ["--auto-tier", effort or DEFAULT_AUTO_TIER]
        elif effort:
            cmd += ["--reasoning-effort", effort]
        # Also passed when unset, for the same reason as the tier: the CLI
        # otherwise runs on the context tier its own config has stored.
        cmd += ["--context", context_tier or DEFAULT_CONTEXT]
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


def _headless_models() -> list[dict]:
    """The model catalog Copilot's headless server answers ``models.list`` with.

    The server speaks JSON-RPC framed with ``Content-Length`` headers, and quits
    as soon as its stdin closes, before answering, so it is kept open until the
    reply arrives or ``acp.TIMEOUT_SECONDS`` pass.
    """
    process = subprocess.Popen(
        HEADLESS_COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    try:
        replies: queue.Queue[dict] = queue.Queue()
        threading.Thread(target=_read_framed, args=(process.stdout, replies),
                         daemon=True).start()
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": "models.list", "params": {}}).encode()
        process.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        process.stdin.flush()
        deadline = time.monotonic() + acp.TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("copilot --headless did not answer models.list")
            try:
                reply = replies.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"copilot --headless exited {process.returncode}")
                continue
            if reply.get("id") != 1:
                continue
            if "error" in reply:
                raise RuntimeError(f"models.list errored: {reply['error']}")
            result = reply.get("result")
            models = result.get("models") if isinstance(result, dict) else result
            return [model for model in models or [] if isinstance(model, dict)]
    finally:
        process.kill()


def _read_framed(stream, replies: "queue.Queue[dict]") -> None:
    """Pump ``Content-Length`` framed JSON messages from ``stream`` into a queue."""
    while True:
        length = None
        while True:
            line = stream.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                break
            name, _, value = line.decode("ascii", "replace").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        if length is None:
            continue
        try:
            message = json.loads(stream.read(length))
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            replies.put(message)


def _context_tiers(models: list[dict]) -> dict[str, tuple[tuple[str, str], ...]]:
    """The context windows of each model that offers a long one, by model id.

    The default window is labelled by its prompt budget and the long one by the
    model's whole window, which is how Copilot itself sizes them ("200K" and
    "1M" for Claude Sonnet 5.5).
    """
    tiers = {}
    for model in models:
        prices = (model.get("billing") or {}).get("tokenPrices") or {}
        long_context = prices.get("longContext")
        if not isinstance(long_context, dict):
            continue
        limits = (model.get("capabilities") or {}).get("limits") or {}
        default_size = prices.get("maxPromptTokens") or prices.get("contextMax")
        long_size = (limits.get("max_context_window_tokens")
                     or long_context.get("maxPromptTokens")
                     or long_context.get("contextMax"))
        tiers[str(model.get("id", ""))] = (
            (DEFAULT_CONTEXT, _tokens(default_size) or "Default"),
            (LONG_CONTEXT, _tokens(long_size) or "Long"))
    return tiers


def _tokens(count) -> str:
    """``count`` tokens the way a context window is usually named: 200K, 1M."""
    if not isinstance(count, (int, float)) or count <= 0:
        return ""
    if count >= 1_000_000:
        # Truncated, so a 1,050,000 window still reads as the 1M it is sold as.
        tenths = int(count // 100_000) / 10
        return f"{tenths:g}M"
    return f"{round(count / 1000)}K"


def _events_file(session_id: str) -> Path | None:
    """The events log Copilot is writing ``session_id`` to, or None before it starts.

    Copilot keeps it as ``session-state/<session id>/events.jsonl`` under
    ``COPILOT_HOME``, and the id is the one Codee hands the CLI.
    """
    if not _SESSION_ID.match(session_id or ""):
        return None
    configured = os.environ.get("COPILOT_HOME", "").strip()
    home = Path(configured) if configured else Path.home() / ".copilot"
    path = home / "session-state" / session_id / "events.jsonl"
    return path if path.is_file() else None


def _events_entries(record: dict) -> list[PeepEntry]:
    """The steps one events line carries: what the model thought, said or ran.

    A message's tool requests are left to the ``tool.execution_start`` lines
    that follow, which name the same calls, so each shows once. Tool output is
    left out; the call says what the agent is doing.
    """
    data = record.get("data")
    if not isinstance(data, dict):
        return []
    timestamp = str(record.get("timestamp") or "")
    kind = record.get("type")
    if kind == ASSISTANT_MESSAGE:
        entries = []
        reasoning = str(data.get("reasoningText") or "")
        if reasoning.strip():
            entries.append(PeepEntry("thinking", clip(reasoning), timestamp))
        content = str(data.get("content") or "")
        if content.strip():
            entries.append(PeepEntry("text", clip(content), timestamp))
        return entries
    if kind == TOOL_EXECUTION_START:
        return [PeepEntry("tool", tool_summary(
            str(data.get("toolName") or "tool"), data.get("arguments")),
            timestamp)]
    return []


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
