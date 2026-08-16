"""Putting MCP servers into the project's ``.mcp.json``.

Coding agents disagree about how that file is shaped. Claude Code reads its
servers out of ``mcpServers`` and infers a stdio transport from the presence of
``command``; GitHub Copilot reads ``servers`` and wants the transport spelled
out. Neither minds the other's key, so one file carrying both sections is
readable by both agents and the project keeps a single place to look.

This translation lives here rather than in the tasks providers on purpose: a
provider knows which server speaks to its backend, and nothing about which agent
will be told to run it.
"""
import json
import os
from pathlib import Path

from codee_tasks_abstract.provider import McpServer


MCP_FILE = ".mcp.json"

# Top-level section each agent reads its servers from.
CLAUDE_CODE_KEY = "mcpServers"
COPILOT_KEY = "servers"


def mcp_file(root: Path) -> Path:
    return Path(root) / MCP_FILE


def _claude_code_entry(server: McpServer) -> dict:
    entry = {"command": server.command, "args": list(server.args)}
    # Omitted rather than written empty: a server that authenticates some other
    # way (an Azure CLI login, say) has no environment to carry, and an empty
    # `env` in the file reads like one that was meant to be filled in.
    if server.env:
        entry["env"] = dict(server.env)
    return entry


def _copilot_entry(server: McpServer) -> dict:
    # Copilot has no default transport: a server without a type is skipped.
    return {"type": "stdio", **_claude_code_entry(server)}


def _load(path: Path) -> dict:
    """Read the existing config, or start an empty one.

    A file that isn't a JSON object is refused rather than replaced: it was
    written by hand or by another tool, and overwriting it would lose servers
    this project may depend on. The settings page shows the message as-is.
    """
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); "
                         "fix or remove it and try again.") from exc
    if not isinstance(config, dict):
        raise ValueError(f"{path} does not hold a JSON object; "
                         "fix or remove it and try again.")
    return config


def _section(config: dict, key: str, path: Path) -> dict:
    section = config.get(key)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError(f'"{key}" in {path} is not a JSON object; '
                         "fix or remove it and try again.")
    return section


def find_mcp_server(root: Path, name: str) -> dict | None:
    """The server configured under ``name``, or None if there isn't one.

    Only the Claude Code section is consulted: the two are written together, so
    one answers for both. A file that can't be read counts as "not configured"
    rather than raising — this answers a yes/no question the settings page asks
    on every load, and the setup that would fix such a file reports it properly.
    """
    path = mcp_file(root)
    try:
        entry = _section(_load(path), CLAUDE_CODE_KEY, path).get(name)
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None


def write_mcp_server(root: Path, server: McpServer) -> Path:
    """Add or update ``server`` in the project's ``.mcp.json``.

    Only the entry under ``server.name`` is touched, in both agents' sections;
    every other server and every other key in the file survives, so this can be
    run again after the credentials change.
    """
    path = mcp_file(root)
    config = _load(path)
    for key, entry in ((CLAUDE_CODE_KEY, _claude_code_entry(server)),
                       (COPILOT_KEY, _copilot_entry(server))):
        section = _section(config, key, path)
        section[server.name] = entry
        config[key] = section

    # Renamed into place: a coding agent may be starting up and reading this
    # file right now, and a half-written one reads as "no servers configured".
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(config, indent=2) + "\n")
    os.replace(temp, path)
    return path
