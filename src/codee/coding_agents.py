"""Which coding agent implementation backs the agent named in settings.

The mirror image of :mod:`codee.tasks_providers`, and here for the same reason:
the admin UI and the setup wizard both need to go from the stored agent name to
the class that implements it, and neither can reach that through the executor
without starting an agent pool and a poll loop at import.
"""
from pathlib import Path

from codee_agent_abstract.provider import AbstractCodingAgent
from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_agent_codex.provider import CodexAgent
from codee_agent_github_copilot.provider import GitHubCopilotAgent
from codee_main_context.context import CodingAgent, Settings

# Concrete coding agents, keyed by the agent selected in settings.
CODING_AGENTS: dict[CodingAgent, type[AbstractCodingAgent]] = {
    CodingAgent.CLAUDE_CODE: ClaudeCodeAgent,
    CodingAgent.GITHUB_COPILOT: GitHubCopilotAgent,
    CodingAgent.CODEX: CodexAgent,
}


def agent_label(agent: CodingAgent) -> str:
    """How one agent is written for a human, e.g. ``Claude Code``."""
    implementation = CODING_AGENTS.get(agent)
    return (implementation.DISPLAY_NAME if implementation else "") or agent.value


def resolve_agent_code(code: str) -> CodingAgent | None:
    """The agent a skill's ``x-codee-agent`` names, or None when it names none.

    None is also the answer for a value that matches no agent Codee can run.
    Callers treat both the same way — by falling back to the default agent from
    Settings — because a skill that named an agent this build doesn't have is
    better run by the configured one than not run at all.

    Both the stored code (``claude_code``) and the display name (``Claude Code``)
    are accepted: the field is written by hand as often as by the admin UI, and
    a hyphen or a capital in it should not silently drop the skill back onto the
    default agent.
    """
    value = str(code or "").strip()
    if not value:
        return None
    normalized = value.casefold().replace("-", "_").replace(" ", "_")
    for agent in CODING_AGENTS:
        if normalized == agent.value:
            return agent
        if value.casefold() == agent_label(agent).casefold():
            return agent
    return None


def build_coding_agent(settings: Settings, cwd: Path,
                       agent: CodingAgent | None = None) -> AbstractCodingAgent:
    """The agent implementation, defaulting to the one Settings selects.

    ``agent`` is what a skill asked for through ``x-codee-agent``; leaving it
    out is how every caller with no skill in hand gets the default agent.
    """
    selected = agent or settings.coding_agent
    implementation = CODING_AGENTS.get(selected)
    if implementation is None:
        raise ValueError(f"unsupported coding agent: {selected.value}")
    return implementation(settings, cwd)


def installed_coding_agents() -> list[CodingAgent]:
    """The agents whose CLI is on PATH, in the order they are declared.

    What the setup wizard offers when it asks which agent to drive. An empty
    list is a normal answer — Codee can be configured before its agent is
    installed, and the check only looks for the executable anyway.
    """
    return [agent for agent, implementation in CODING_AGENTS.items()
            if implementation.is_installed()]
