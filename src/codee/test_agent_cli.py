import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codee import agent_cli


class AgentCliTest(unittest.TestCase):
    def _init(self, argv: list[str]) -> int:
        with patch("sys.argv", ["codee-agent", *argv]):
            return agent_cli.main()

    def test_init_scaffolds_then_runs_the_wizard(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                with patch.object(agent_cli.setup_wizard, "run",
                                  return_value=0) as wizard, \
                        patch("sys.stdin.isatty", return_value=True):
                    self.assertEqual(self._init(["init"]), 0)
                self.assertTrue(Path("AGENTS.md").is_file())
                self.assertTrue(Path("pyproject.toml").is_file())
                self.assertEqual(wizard.call_args.args[1],
                                 agent_cli.DEFAULT_ADMIN_PORT)
            finally:
                os.chdir(original_directory)

    def test_scaffold_only_skips_every_question(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                with patch.object(agent_cli.setup_wizard, "run") as wizard, \
                        patch("sys.stdin.isatty", return_value=True):
                    self.assertEqual(self._init(["init", "--scaffold-only"]), 0)
                self.assertTrue(Path("AGENTS.md").is_file())
                wizard.assert_not_called()
            finally:
                os.chdir(original_directory)

    def test_a_pipe_still_scaffolds_but_asks_nothing(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                with patch.object(agent_cli.setup_wizard, "run") as wizard, \
                        patch("sys.stdin.isatty", return_value=False):
                    self.assertEqual(self._init(["init"]), 0)
                self.assertTrue(Path(".claude/skills").is_dir())
                wizard.assert_not_called()
            finally:
                os.chdir(original_directory)

    def test_declining_the_overwrite_prompt_creates_nothing(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                Path("AGENTS.md").write_text("custom")
                with patch("builtins.input", return_value="n"), \
                        patch.object(agent_cli.setup_wizard, "run") as wizard:
                    self.assertEqual(self._init(["init"]), 1)
                self.assertEqual(Path("AGENTS.md").read_text(), "custom")
                self.assertFalse(Path("pyproject.toml").exists())
                wizard.assert_not_called()
            finally:
                os.chdir(original_directory)

    def test_the_port_is_passed_through_to_the_browser_handoff(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                with patch.object(agent_cli.setup_wizard, "run",
                                  return_value=0) as wizard, \
                        patch("sys.stdin.isatty", return_value=True):
                    self._init(["init", "--port", "9000"])
                self.assertEqual(wizard.call_args.args[1], 9000)
            finally:
                os.chdir(original_directory)

    def test_interrupting_the_wizard_is_not_a_crash(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                with patch.object(agent_cli.setup_wizard, "run",
                                  side_effect=KeyboardInterrupt), \
                        patch("sys.stdin.isatty", return_value=True):
                    self.assertEqual(self._init(["init"]), 130)
            finally:
                os.chdir(original_directory)


if __name__ == "__main__":
    unittest.main()
