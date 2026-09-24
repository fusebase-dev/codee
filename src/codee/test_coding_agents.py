import unittest

from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_agent_codex.provider import CodexAgent
from codee_agent_opencode.provider import OpenCodeAgent
from codee_main_context.context import CodingAgent, Settings

from codee.coding_agents import (
    agent_label, build_coding_agent, resolve_agent_code)


class ResolveAgentCodeTest(unittest.TestCase):
    """What a skill may write in ``x-codee-agent`` and still be understood."""

    def test_a_stored_agent_code_resolves(self) -> None:
        self.assertEqual(resolve_agent_code("codex"), CodingAgent.CODEX)
        self.assertEqual(resolve_agent_code("github_copilot"),
                         CodingAgent.GITHUB_COPILOT)
        self.assertEqual(resolve_agent_code("opencode"), CodingAgent.OPENCODE)

    def test_a_hand_written_code_resolves_whatever_its_shape(self) -> None:
        # The field is written by hand as often as by the admin UI.
        for value in ("Claude Code", "claude-code", " claude_code ",
                      "CLAUDE_CODE"):
            with self.subTest(value=value):
                self.assertEqual(resolve_agent_code(value),
                                 CodingAgent.CLAUDE_CODE)

    def test_a_missing_or_unknown_agent_names_none(self) -> None:
        # Both are the caller's cue to fall back to the default agent.
        self.assertIsNone(resolve_agent_code(""))
        self.assertIsNone(resolve_agent_code("   "))
        self.assertIsNone(resolve_agent_code("cursor"))


class BuildCodingAgentTest(unittest.TestCase):
    def test_without_an_agent_the_settings_choose(self) -> None:
        settings = Settings(coding_agent=CodingAgent.CLAUDE_CODE)

        agent = build_coding_agent(settings, cwd=None)

        self.assertIsInstance(agent, ClaudeCodeAgent)

    def test_a_named_agent_overrides_the_settings(self) -> None:
        settings = Settings(coding_agent=CodingAgent.CLAUDE_CODE)

        agent = build_coding_agent(settings, cwd=None, agent=CodingAgent.CODEX)

        self.assertIsInstance(agent, CodexAgent)

    def test_opencode_can_be_built(self) -> None:
        settings = Settings(coding_agent=CodingAgent.OPENCODE)

        agent = build_coding_agent(settings, cwd=None)

        self.assertIsInstance(agent, OpenCodeAgent)


class AgentLabelTest(unittest.TestCase):
    def test_agents_are_labelled_by_their_display_name(self) -> None:
        self.assertEqual(agent_label(CodingAgent.CLAUDE_CODE), "Claude Code")
        self.assertEqual(agent_label(CodingAgent.GITHUB_COPILOT),
                         "GitHub Copilot")
        self.assertEqual(agent_label(CodingAgent.OPENCODE), "OpenCode")


if __name__ == "__main__":
    unittest.main()
