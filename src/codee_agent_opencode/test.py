import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codee_agent_abstract.provider import AgentModel
from codee_agent_opencode.provider import OpenCodeAgent, _environment
from codee_main_context.context import Settings


SESSION = "ses_01k123"


def _event(kind: str, **fields) -> str:
    return json.dumps({"type": kind, "sessionID": SESSION, **fields})


class _FakeProcess:
    def __init__(self, stdout: str, stderr: str, returncode: int,
                 timeout: bool = False):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.timeout = timeout

    def wait(self, timeout=None):
        if self.timeout:
            raise subprocess.TimeoutExpired("opencode", timeout or 0)
        return self.returncode

    def kill(self):
        self.timeout = False


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0,
               timeout: bool = False) -> _FakeProcess:
    return _FakeProcess(stdout, stderr, returncode, timeout)


class OpenCodeRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("/repo")
        self.agent = OpenCodeAgent(Settings(), self.root)

    def _run(self, result: Mock, model: str = "", effort: str = "") -> str:
        self.reported = []
        with patch("subprocess.Popen", return_value=result) as run:
            self.captured = run
            return self.agent.run("/do-it CORE-1", "unused", model,
                                  self.reported.append, effort)

    def _model_flag(self) -> str:
        cmd = self.captured.call_args.args[0]
        return cmd[cmd.index("--model") + 1]

    def test_the_skill_effort_is_the_models_variant(self) -> None:
        self._run(_completed(_event(
            "text", part={"type": "text", "text": "Done."})),
            "anthropic/claude-opus-5-5", "max")

        self.assertEqual(self._model_flag(), "anthropic/claude-opus-5-5#max")

    def test_a_model_naming_its_own_variant_keeps_it(self) -> None:
        self._run(_completed(_event(
            "text", part={"type": "text", "text": "Done."})),
            "anthropic/claude-opus-5-5#low", "max")

        self.assertEqual(self._model_flag(), "anthropic/claude-opus-5-5#low")

    def test_an_effort_without_a_model_is_dropped(self) -> None:
        # A variant only exists as a suffix of a model id.
        self._run(_completed(_event(
            "text", part={"type": "text", "text": "Done."})), "", "high")

        self.assertNotIn("--model", self.captured.call_args.args[0])

    def test_returns_text_and_reports_the_opened_session(self) -> None:
        stdout = "\n".join([
            _event("step_start", part={"type": "step-start"}),
            _event("text", part={"type": "text", "text": "Done."}),
        ])

        self.assertEqual(self._run(_completed(stdout)), "Done.")
        self.assertEqual(self.reported, [SESSION])

    def test_runs_headless_with_permissions_and_model(self) -> None:
        self._run(_completed(_event(
            "text", part={"type": "text", "text": "Done."})),
            "anthropic/claude-opus-4-1")

        cmd = self.captured.call_args.args[0]
        self.assertEqual(cmd[:2], ["opencode", "run"])
        self.assertIn("--auto", cmd)
        self.assertEqual(cmd[cmd.index("--format") + 1], "json")
        self.assertEqual(cmd[cmd.index("--model") + 1],
                         "anthropic/claude-opus-4-1")
        self.assertEqual(cmd[-2:], ["--", "/do-it CORE-1"])
        self.assertEqual(self.captured.call_args.kwargs["cwd"], self.root)
        self.assertEqual(self.captured.call_args.kwargs["stdin"],
                         subprocess.DEVNULL)

    def test_project_mcp_servers_are_translated_into_inline_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".mcp.json").write_text(json.dumps({
                "mcpServers": {
                    "jira": {
                        "command": "uvx",
                        "args": ["mcp-atlassian"],
                        "env": {"JIRA_URL": "https://acme.example"},
                    },
                },
            }))
            with patch.dict("os.environ", {
                    "OPENCODE_CONFIG_CONTENT": '{"model":"openai/gpt-5"}'},
                    clear=True):
                config = json.loads(_environment(root)[
                    "OPENCODE_CONFIG_CONTENT"])

        self.assertEqual(config["model"], "openai/gpt-5")
        self.assertEqual(config["mcp"]["jira"], {
            "type": "local",
            "command": ["uvx", "mcp-atlassian"],
            "enabled": True,
            "environment": {"JIRA_URL": "https://acme.example"},
        })

    def test_continuing_resumes_the_opencode_session(self) -> None:
        result = _completed(_event(
            "text", part={"type": "text", "text": "Continued."}))
        with patch("subprocess.Popen", return_value=result) as run:
            response = self.agent.continue_conversation("And now?", SESSION)

        self.assertEqual(response, "Continued.")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--session") + 1], SESSION)

    def test_cli_and_stream_errors_raise(self) -> None:
        error = _event("error", error={"data": {"message": "bad model"}})
        with self.assertRaisesRegex(RuntimeError, "bad model"):
            self._run(_completed(error, returncode=1))
        with self.assertRaisesRegex(RuntimeError, "bad model"):
            self._run(_completed(error))

    def test_plain_successful_output_is_returned(self) -> None:
        self.assertEqual(self._run(_completed(
            "plain output\n")), "plain output\n")

    def test_timeout_raises(self) -> None:
        with patch("subprocess.Popen", return_value=_completed(timeout=True)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                self.agent.run("do it", "unused")

    def test_skill_prompt_names_the_skill_file(self) -> None:
        prompt = self.agent.skill_prompt(
            "task-developer", self.root / ".claude/skills/task-developer/SKILL.md",
            "CORE-1", "TASK_ID")

        self.assertEqual(
            prompt,
            "Read .claude/skills/task-developer/SKILL.md and follow its "
            "instructions exactly. TASK_ID = CORE-1",
        )

    def test_the_catalog_is_read_over_acp(self) -> None:
        catalog = [AgentModel("anthropic/claude-opus-5-5",
                              "anthropic/Claude Opus 5.5", ("low", "max"))]

        with patch("codee_agent_opencode.provider.acp.fetch_models",
                   return_value=catalog) as fetch, \
                patch("subprocess.run") as models_command:
            self.assertEqual(OpenCodeAgent.list_models(), catalog)

        fetch.assert_called_once_with(["opencode", "acp"])
        models_command.assert_not_called()

    def test_models_come_from_the_cli_when_acp_cannot_answer(self) -> None:
        result = Mock(returncode=0, stderr="", stdout=(
            "anthropic/claude-opus-4-1\nopenai/gpt-5\n"))
        with patch("codee_agent_opencode.provider.acp.fetch_models",
                   side_effect=RuntimeError("opencode acp exited 1")), \
                patch("subprocess.run", return_value=result):
            models = OpenCodeAgent.list_models()

        self.assertEqual([model.id for model in models], [
            "anthropic/claude-opus-4-1", "openai/gpt-5"])


if __name__ == "__main__":
    unittest.main()
