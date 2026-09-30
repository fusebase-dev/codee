"""Reading a model catalog out of an Agent Client Protocol server.

Copilot (``copilot --acp``) and OpenCode (``opencode acp``) both speak ACP, and
it is the only way either CLI says which models it runs and which reasoning
effort levels each of them takes. The effort levels are a session config option
in the ``thought_level`` category whose choices belong to the session's current
model, so the catalog is read by switching a throwaway session through every
model in turn. No prompt is ever sent.
"""
import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from codee_agent_abstract.provider import AgentModel
from codee_main_context.logging import get_logger


log = get_logger(__name__)

TIMEOUT_SECONDS = 60
MODEL_CATEGORY = "model"
EFFORT_CATEGORY = "thought_level"
# OpenCode lists "default" among the effort choices. It means "leave it to the
# model", which is what an unset effort already says.
DEFAULT_EFFORT_VALUE = "default"

_INITIALIZE_ID = 1
_SESSION_NEW_ID = 2
_FIRST_SET_ID = 100


def fetch_models(command: list[str]) -> list[AgentModel]:
    """Ask the ACP server ``command`` starts for its models and their efforts.

    Raises when the server can't be started or doesn't answer; callers turn
    that into an empty catalog, since the picker takes a typed model id too.
    """
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=Path.cwd(),
    )
    label = " ".join(command)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        lines: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(
            target=drain, args=(process.stdout, lines), daemon=True)
        reader.start()

        send(process, _INITIALIZE_ID, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
            },
        })
        send(process, _SESSION_NEW_ID, "session/new", {
            "cwd": str(Path.cwd()),
            "mcpServers": [],
        })
        session = await_result(process, lines, _SESSION_NEW_ID, label, deadline)
        models = _catalog(session)

        option = _config_option(session, MODEL_CATEGORY)
        session_id = session.get("sessionId")
        if option is None or not session_id:
            # An older CLI with no config options: the ids are still worth
            # offering, just without effort levels.
            return models
        for index, model in enumerate(models):
            request_id = _FIRST_SET_ID + index
            send(process, request_id, "session/set_config_option", {
                "sessionId": session_id,
                "configId": option["id"],
                "value": model.id,
            })
            try:
                result = await_result(process, lines, request_id, label,
                                      deadline)
            except RuntimeError as exc:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise
                # One model the server won't switch to leaves that model
                # without efforts, not the whole catalog without them.
                log.debug("%s could not switch to %s: %s", label, model.id, exc)
                continue
            models[index] = _with_efforts(model, result)
    finally:
        process.kill()

    log.debug("%s reported %d model(s)", label, len(models))
    return models


def _catalog(session: dict) -> list[AgentModel]:
    """The models ``session/new`` offers, deduplicated in the order given.

    Copilot answers with ``models.availableModels`` as well as the config
    option; OpenCode only with the option. Either names the same models.
    """
    entries: list[tuple[str, str]] = []
    available = (session.get("models") or {}).get("availableModels")
    if available:
        entries = [(str(entry.get("modelId", "")), str(entry.get("name", "")))
                   for entry in available if isinstance(entry, dict)]
    else:
        option = _config_option(session, MODEL_CATEGORY) or {}
        entries = [(str(entry.get("value", "")), str(entry.get("name", "")))
                   for entry in option.get("options") or []
                   if isinstance(entry, dict)]

    models: dict[str, AgentModel] = {}
    for model_id, name in entries:
        model_id = model_id.strip()
        if model_id and model_id not in models:
            models[model_id] = AgentModel(model_id, name.strip() or model_id)
    return list(models.values())


def _with_efforts(model: AgentModel, result: dict) -> AgentModel:
    """``model`` with the effort levels the server offers once it is selected."""
    option = _config_option(result, EFFORT_CATEGORY)
    if option is None:
        return model
    efforts = tuple(
        value for value in (str(entry.get("value", "")).strip()
                            for entry in option.get("options") or []
                            if isinstance(entry, dict))
        if value and value != DEFAULT_EFFORT_VALUE)
    default = str(option.get("currentValue", "") or "").strip()
    if default not in efforts:
        default = ""
    return AgentModel(model.id, model.name, efforts, default)


def _config_option(result: dict, category: str) -> dict | None:
    for option in result.get("configOptions") or []:
        if isinstance(option, dict) and option.get("category") == category:
            return option
    return None


def send(process: subprocess.Popen, request_id: int, method: str,
         params: dict) -> None:
    request = {"jsonrpc": "2.0", "id": request_id,
               "method": method, "params": params}
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()


def drain(stream, lines: "queue.Queue[str]") -> None:
    """Pump the CLI's stdout into a queue so the read can be given a deadline."""
    for line in stream:
        lines.put(line)


def await_result(process: subprocess.Popen, lines: "queue.Queue[str]",
                 request_id: int, label: str = "acp",
                 deadline: float | None = None) -> dict:
    """Read until the response to ``request_id`` arrives, or the deadline passes.

    Everything else on the wire — earlier replies, the session/update
    notifications the CLI starts emitting straight away — is skipped.
    """
    if deadline is None:
        deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{label} did not answer within {TIMEOUT_SECONDS}s")
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            # An immediate exit means the CLI is missing, unauthenticated, or
            # too old for ACP; no point waiting out the whole deadline.
            if process.poll() is not None:
                raise RuntimeError(
                    f"{label} exited {process.returncode}: "
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
            raise RuntimeError(f"{label} errored: {message['error']}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}
