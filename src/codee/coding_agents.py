"""Which coding agent implementation backs the agent named in settings.

The mirror image of :mod:`codee.tasks_providers`, and here for the same reason:
the admin UI and the setup wizard both need to go from the stored agent name to
the class that implements it, and neither can reach that through the executor
without starting an agent pool and a poll loop at import.
"""
from pathlib import Path

from codee_agent_abstract.provider import AbstractCodingAgent
from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_agent_github_copilot.provider import GitHubCopilotAgent
from codee_main_context.context import CodingAgent, Settings

# Concrete coding agents, keyed by the agent selected in settings.
CODING_AGENTS: dict[CodingAgent, type[AbstractCodingAgent]] = {
    CodingAgent.CLAUDE_CODE: ClaudeCodeAgent,
    CodingAgent.GITHUB_COPILOT: GitHubCopilotAgent,
}


def build_coding_agent(settings: Settings, cwd: Path) -> AbstractCodingAgent:
    agent = CODING_AGENTS.get(settings.coding_agent)
    if agent is None:
        raise ValueError(
            f"unsupported coding agent: {settings.coding_agent.value}")
    return agent(settings, cwd)


def installed_coding_agents() -> list[CodingAgent]:
    """The agents whose CLI is on PATH, in the order they are declared.

    What the setup wizard offers when it asks which agent to drive. An empty
    list is a normal answer — Codee can be configured before its agent is
    installed, and the check only looks for the executable anyway.
    """
    return [agent for agent, implementation in CODING_AGENTS.items()
            if implementation.is_installed()]
