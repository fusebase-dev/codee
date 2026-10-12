import json
import os
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from codee_agent_abstract import acp
from codee_agent_abstract.provider import (
    AbstractCodingAgent, AgentModel, ContextUsage, PeepEntry, resolve_command)
from codee_agent_abstract.transcript import clip, token_count, tool_summary
from codee_main_context.context import Settings
from codee_main_context.logging import get_logger

from codee.lib.mcp_config import read_mcp_servers


log = get_logger(__name__)

# `opencode acp` answers with the display names and each model's effort levels
# (OpenCode calls them variants); `opencode models` only lists the ids.
ACP_COMMAND = ["opencode", "acp"]
# How `--model` names a variant: `provider/model#variant`.
VARIANT_SEPARATOR = "#"

# OpenCode keeps every session in one SQLite database under its data directory,
# a row per message, and the models.dev catalog it runs on (each model's
# context limits) in the same file. Peep reads both, read-only.
DATABASE = "opencode.db"
CATALOG_KEY = "models-dev:catalog"
_LAST_ASSISTANT_MESSAGES = (
    "SELECT data FROM session_message WHERE session_id = ? "
    "AND type = 'assistant' ORDER BY seq DESC LIMIT ?")
# A message's tokens stay zero until its call finishes, so the last one that
# has any is the one that says how full the window is.
_LAST_USAGE = (
    "SELECT data FROM session_message WHERE session_id = ? "
    "AND type = 'assistant' "
    "AND coalesce(json_extract(data, '$.tokens.input'), 0) "
    "+ coalesce(json_extract(data, '$.tokens.cache.read'), 0) "
    "+ coalesce(json_extract(data, '$.tokens.cache.write'), 0) > 0 "
    "ORDER BY seq DESC LIMIT 1")
_COMPACTIONS = (
    "SELECT count(*) FROM session_message WHERE session_id = ? "
    "AND type = 'compaction' AND json_extract(data, '$.status') = 'completed'")
_MODEL_LIMIT = (
    "SELECT json_extract(json_extract(value, '$.body'), ?) FROM kv "
    "WHERE key = ?")


class OpenCodeAgent(AbstractCodingAgent):
    """Runs the ``opencode`` CLI non-interactively in a fresh session."""

    DISPLAY_NAME = "OpenCode"
    CLI_COMMAND = "opencode"
    TIMEOUT_SECONDS = 7200  # 2 hours

    def __init__(self, settings: Settings, cwd: Path):
        super().__init__(settings, cwd)

    @classmethod
    def list_models(cls) -> list[AgentModel]:
        try:
            models = acp.fetch_models(ACP_COMMAND)
            if models:
                return models
        except Exception as exc:
            log.debug("Could not read the opencode catalog over ACP: %s", exc)
        try:
            result = subprocess.run(
                resolve_command([cls.CLI_COMMAND, "models"]),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip())
            models = []
            for line in result.stdout.splitlines():
                model = line.strip()
                if model:
                    models.append(AgentModel(model, model))
            return models
        except Exception as exc:
            log.warning("Could not read the opencode model catalog: %s", exc)
            return []

    @classmethod
    def peep(cls, session_id: str, cwd: Path,
             limit: int = 10) -> list[PeepEntry] | None:
        # ``cwd`` doesn't matter: OpenCode files every project's sessions in
        # the one database.
        rows = _query(_LAST_ASSISTANT_MESSAGES, (session_id, limit))
        if rows is None:
            return None
        entries = [entry for (data,) in reversed(rows)
                   for entry in _message_entries(data)]
        return entries[-limit:]

    @classmethod
    def context_usage(cls, session_id: str,
                      cwd: Path) -> ContextUsage | None:
        rows = _query(_LAST_USAGE, (session_id,)) or []
        message = _json_object(rows[0][0]) if rows else {}
        tokens = message.get("tokens")
        used = window = 0
        if isinstance(tokens, dict):
            cache = tokens.get("cache")
            cache = cache if isinstance(cache, dict) else {}
            # The sum OpenCode's own sidebar shows as the context in use.
            used = (sum(token_count(tokens.get(key))
                        for key in ("input", "output", "reasoning"))
                    + token_count(cache.get("read"))
                    + token_count(cache.get("write")))
            model = message.get("model")
            if used and isinstance(model, dict):
                window = _context_window(str(model.get("providerID") or ""),
                                         str(model.get("id") or ""))
        counted = _query(_COMPACTIONS, (session_id,)) or [(0,)]
        compactions = token_count(counted[0][0])
        if not used and not compactions:
            return None
        return ContextUsage(used, window, compactions)

    def skill_prompt(self, slug: str, path: Path, argument: str = "",
                     argument_name: str = "") -> str:
        prompt = f"Read {_relative(path, self._cwd)} and follow its instructions exactly."
        if argument:
            prompt += f" {argument_name or 'ARGUMENT'} = {argument}"
        return prompt

    def run(self, user_message: str, session_id: str, model: str = "",
            on_session_id: Callable[[str], None] | None = None,
            effort: str = "", context_tier: str = "") -> str:
        return self._run(user_message, model, effort, on_session_id)

    def continue_conversation(
        self,
        user_message: str,
        session_id: str,
        model: str = "",
        on_session_id: Callable[[str], None] | None = None,
        effort: str = "",
        context_tier: str = "",
    ) -> str:
        return self._run(user_message, model, effort, on_session_id, session_id)

    def _run(self, user_message: str, model: str, effort: str,
             on_session_id: Callable[[str], None] | None,
             resume_session_id: str = "") -> str:
        cmd = [self.CLI_COMMAND, "run", "--format", "json", "--auto"]
        if resume_session_id:
            cmd += ["--session", resume_session_id]
        model = _with_variant(model, effort)
        if model:
            cmd += ["--model", model]
        cmd += ["--", user_message]

        log.info("Running opencode with message: %s", user_message)
        log.debug("cwd=%s cmd=%s", self._cwd, " ".join(cmd))
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=self._cwd,
            stdin=subprocess.DEVNULL,
            env=_environment(self._cwd),
        )
        out: list[str] = []
        err: list[str] = []
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
            raise RuntimeError("OpenCode CLI timed out after 2 hours")
        finally:
            for reader in readers:
                reader.join(timeout=5)

        result = subprocess.CompletedProcess(
            cmd, process.returncode, "".join(out), "".join(err))

        reply, opened_session, errors, recognized = _parse_events(
            result.stdout)
        detail = "; ".join(errors) or result.stderr.strip()[:500]
        if result.returncode != 0:
            raise RuntimeError(
                f"OpenCode CLI exited {result.returncode}: {detail}")
        if errors:
            raise RuntimeError(f"OpenCode run errored: {detail}")
        if not recognized:
            log.warning(
                "opencode produced no recognized events; returning raw output")
            return result.stdout
        if not reply:
            raise RuntimeError(
                f"OpenCode run produced no response: {detail or 'no detail'}")
        return reply


def _collect(stream, lines: list[str],
             on_session_id: Callable[[str], None] | None) -> None:
    reported = False
    for line in stream:
        lines.append(line)
        if reported or on_session_id is None:
            continue
        _, session_id, _, recognized = _parse_events(line)
        if recognized and session_id:
            reported = True
            try:
                on_session_id(session_id)
            except Exception as exc:
                log.warning("Could not report opencode session %s: %s",
                            session_id, exc)
    stream.close()


def _with_variant(model: str, effort: str) -> str:
    """The ``--model`` value that carries ``effort`` as OpenCode's variant.

    A variant only exists as a suffix of a model id, so an effort with no model
    to hang it on is dropped, and a model id that already names a variant keeps
    the one it names.
    """
    if not effort or not model or VARIANT_SEPARATOR in model:
        if effort and not model:
            log.warning("opencode takes an effort only with a model; "
                        "ignoring effort %r", effort)
        return model
    return f"{model}{VARIANT_SEPARATOR}{effort}"


def _database() -> Path:
    """Where OpenCode keeps its database: ``opencode/`` under XDG_DATA_HOME."""
    configured = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(configured) if configured else Path.home() / ".local" / "share"
    return base / "opencode" / DATABASE


def _query(sql: str, parameters: tuple) -> list[tuple] | None:
    """The rows ``sql`` reads from OpenCode's database, opened read-only.

    [] when there is no database yet or it can't be read just now. None when
    it has no ``session_message`` table: an OpenCode older than 2.0, which
    keeps its sessions another way.
    """
    path = _database()
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True,
                                     timeout=2)
    except sqlite3.Error:
        return []
    try:
        return connection.execute(sql, parameters).fetchall()
    except sqlite3.OperationalError as exc:
        return None if "no such table: session_message" in str(exc) else []
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _json_object(data: object) -> dict:
    """``data`` parsed as a JSON object, {} when it is anything else."""
    try:
        value = json.loads(data) if isinstance(data, str) else None
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _iso(milliseconds: object) -> str:
    """An epoch time in milliseconds as ISO UTC, "" when there is none."""
    if not token_count(milliseconds):
        return ""
    moment = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _message_entries(data: str) -> list[PeepEntry]:
    """The steps one assistant message carries: what the model thought, said or ran.

    Tool output is left out; the call says what the agent is doing.
    """
    message = _json_object(data)
    time = message.get("time")
    created = time.get("created") if isinstance(time, dict) else None
    content = message.get("content")
    entries = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        time = block.get("time")
        timestamp = _iso((time.get("created") if isinstance(time, dict)
                          else None) or created)
        kind = block.get("type")
        text = str(block.get("text") or "")
        if kind == "reasoning" and text.strip():
            entries.append(PeepEntry("thinking", clip(text), timestamp))
        elif kind == "text" and text.strip():
            entries.append(PeepEntry("text", clip(text), timestamp))
        elif kind == "tool":
            state = block.get("state")
            entries.append(PeepEntry("tool", tool_summary(
                str(block.get("name") or "tool"),
                state.get("input") if isinstance(state, dict) else None),
                timestamp))
    return entries


def _context_window(provider: str, model: str) -> int:
    """How many tokens ``model`` takes in, from OpenCode's models.dev catalog.

    Its input limit where the catalog gives one (that is what OpenCode
    compacts against), else its whole context. 0 for a model the catalog
    doesn't list, such as one configured by hand.
    """
    if not provider or not model or '"' in provider + model:
        return 0
    rows = _query(_MODEL_LIMIT,
                  (f'$."{provider}".models."{model}".limit', CATALOG_KEY))
    limit = _json_object(rows[0][0]) if rows else {}
    return token_count(limit.get("input")) or token_count(limit.get("context"))


def _relative(path: Path, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


def _environment(root: Path) -> dict[str, str]:
    """Process environment with the project's MCP servers translated for OpenCode."""
    environment = os.environ.copy()
    raw = environment.get("OPENCODE_CONFIG_CONTENT", "").strip()
    try:
        config = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "OPENCODE_CONFIG_CONTENT is not valid JSON") from exc
    if not isinstance(config, dict):
        raise RuntimeError("OPENCODE_CONFIG_CONTENT must hold a JSON object")

    servers = read_mcp_servers(root)
    if servers:
        mcp = config.get("mcp") or {}
        if not isinstance(mcp, dict):
            raise RuntimeError(
                "OPENCODE_CONFIG_CONTENT.mcp must hold a JSON object")
        for name, server in servers.items():
            entry = {
                "type": "local",
                "command": [server["command"], *server["args"]],
                "enabled": True,
            }
            if server["env"]:
                entry["environment"] = server["env"]
            mcp[name] = entry
        config["mcp"] = mcp
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return environment


def _parse_events(output: str) -> tuple[str, str, list[str], bool]:
    replies: list[str] = []
    errors: list[str] = []
    session_id = ""
    recognized = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type not in {"text", "error", "tool_use", "step_start",
                              "step_finish", "reasoning"}:
            continue
        recognized = True
        session_id = session_id or str(event.get("sessionID", "")).strip()
        if event_type == "text":
            part = event.get("part") or {}
            text = str(part.get("text", "")).strip(
            ) if isinstance(part, dict) else ""
            if text:
                replies.append(text)
        elif event_type == "error":
            error = event.get("error")
            if isinstance(error, dict):
                data = error.get("data") or {}
                error = data.get("message") if isinstance(
                    data, dict) else error
            if error:
                errors.append(str(error))
    return "\n\n".join(replies), session_id, errors, recognized
