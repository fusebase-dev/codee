import json
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from codee_agent_abstract.provider import AbstractCodingAgent, AgentModel
from codee_main_context.context import Settings
from codee_main_context.logging import get_logger

from codee.lib.mcp_config import read_mcp_servers


log = get_logger(__name__)


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
            result = subprocess.run(
                [cls.CLI_COMMAND, "models"],
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

    def skill_prompt(self, slug: str, path: Path, argument: str = "",
                     argument_name: str = "") -> str:
        prompt = f"Read {_relative(path, self._cwd)} and follow its instructions exactly."
        if argument:
            prompt += f" {argument_name or 'ARGUMENT'} = {argument}"
        return prompt

    def run(self, user_message: str, session_id: str, model: str = "",
            on_session_id: Callable[[str], None] | None = None) -> str:
        return self._run(user_message, model, on_session_id)

    def continue_conversation(
        self,
        user_message: str,
        session_id: str,
        model: str = "",
        on_session_id: Callable[[str], None] | None = None,
    ) -> str:
        return self._run(user_message, model, on_session_id, session_id)

    def _run(self, user_message: str, model: str,
             on_session_id: Callable[[str], None] | None,
             resume_session_id: str = "") -> str:
        cmd = [self.CLI_COMMAND, "run", "--format", "json", "--auto"]
        if resume_session_id:
            cmd += ["--session", resume_session_id]
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
