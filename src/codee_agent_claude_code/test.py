import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_main_context.context import Settings

SESSION = "82232f47-df60-4cb3-8c3a-de12074c9205"


def _completed(result: str = "ok", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stderr=stderr,
        stdout=json.dumps({"result": result}))


class ClaudeCodeRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = ClaudeCodeAgent(Settings(), Path("/repo"))

    def _run(self, model: str = "") -> list[str]:
        with patch("subprocess.run", return_value=_completed()) as run:
            self.agent.run("/do-it CORE-1", SESSION, model)
        return run.call_args.args[0]

    def test_a_skill_run_leaves_the_model_to_the_frontmatter(self) -> None:
        # Claude Code reads the skill's `model:` itself; a flag would override it.
        self.assertNotIn("--model", self._run())

    def test_an_explicit_model_is_passed_on_the_command_line(self) -> None:
        cmd = self._run(model="opus")

        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")

    def test_the_best_model_is_a_version_free_alias(self) -> None:
        # The alias tracks the newest Opus, so no release needs an edit here.
        self.assertEqual(ClaudeCodeAgent.best_model(), "opus")
