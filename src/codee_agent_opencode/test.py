import io
import json
import os
import sqlite3
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

    def test_the_models_command_runs_the_cli_on_path(self) -> None:
        # On Windows the CLI is an npm .cmd shim that a bare name never finds.
        shim = r"C:\Users\me\AppData\Roaming\npm\opencode.CMD"
        result = Mock(returncode=0, stderr="", stdout="openai/gpt-5\n")
        with patch("codee_agent_opencode.provider.acp.fetch_models",
                   return_value=[]), \
                patch("codee_agent_abstract.provider.shutil.which",
                      return_value=shim), \
                patch("subprocess.run", return_value=result) as run:
            OpenCodeAgent.list_models()

        self.assertEqual(run.call_args.args[0], [shim, "models"])


if __name__ == "__main__":
    unittest.main()


class OpenCodeSessionTest(unittest.TestCase):
    """Peep and context usage, read from OpenCode's SQLite database."""

    SESSION = "ses_f0d0cbe84ffeNWiy62O86QX1MO"

    def _database(self, data_home: str, *messages: tuple[str, dict],
                  catalog: dict | None = None) -> None:
        folder = Path(data_home) / "opencode"
        folder.mkdir()
        connection = sqlite3.connect(folder / "opencode.db")
        connection.execute(
            "CREATE TABLE session_message (id text PRIMARY KEY, "
            "session_id text NOT NULL, type text NOT NULL, "
            "seq integer NOT NULL, time_created integer NOT NULL, "
            "time_updated integer NOT NULL, data text NOT NULL)")
        connection.execute(
            "CREATE TABLE kv (key text PRIMARY KEY, value text NOT NULL, "
            "time_created integer NOT NULL, time_updated integer NOT NULL)")
        for seq, (kind, data) in enumerate(messages):
            connection.execute(
                "INSERT INTO session_message VALUES (?, ?, ?, ?, 0, 0, ?)",
                (f"msg_{seq}", self.SESSION, kind, seq, json.dumps(data)))
        connection.execute(
            "INSERT INTO session_message VALUES ('other', 'ses_other', "
            "'assistant', 0, 0, 0, ?)",
            (json.dumps({"content": [{"type": "text", "text": "Not mine"}]}),))
        if catalog is not None:
            connection.execute(
                "INSERT INTO kv VALUES ('models-dev:catalog', ?, 0, 0)",
                (json.dumps({"updatedAt": 0, "body": json.dumps(catalog)}),))
        connection.commit()
        connection.close()

    def _assistant(self, *content: dict, tokens: dict | None = None,
                   created: int = 1790782423879) -> tuple[str, dict]:
        return ("assistant", {
            "time": {"created": created},
            "model": {"id": "gpt-5-mini", "providerID": "github-copilot"},
            "content": list(content),
            "tokens": tokens or {"input": 0, "output": 0, "reasoning": 0,
                                 "cache": {"read": 0, "write": 0}}})

    def test_reads_the_last_steps_of_the_session(self) -> None:
        with tempfile.TemporaryDirectory() as data_home:
            self._database(
                data_home,
                ("user", {"text": "/nightly"}),
                self._assistant(
                    {"type": "reasoning", "text": "Reading the logs first.",
                     "time": {"created": 1790782427293}},
                    {"type": "tool", "name": "read", "state": {
                        "status": "completed",
                        "input": {"filePath": "/repo/app.log"},
                        "output": "secret output"}}),
                ("compaction", {"status": "completed", "summary": "..."}),
                self._assistant({"type": "text", "text": "Found it."}))
            with patch.dict(os.environ, {"XDG_DATA_HOME": data_home}):
                steps = OpenCodeAgent.peep(self.SESSION, Path("/repo"))

        self.assertEqual(
            [(step.kind, step.text) for step in steps],
            [("thinking", "Reading the logs first."),
             ("tool", "read: /repo/app.log"),
             ("text", "Found it.")])
        self.assertEqual(steps[0].timestamp, "2026-09-30T15:33:47.293Z")
        self.assertEqual(steps[1].timestamp, "2026-09-30T15:33:43.879Z")

    def test_reads_the_last_finished_calls_tokens_and_counts_compactions(
            self) -> None:
        with tempfile.TemporaryDirectory() as data_home:
            self._database(
                data_home,
                self._assistant(tokens={"input": 90000, "output": 10,
                                        "reasoning": 0,
                                        "cache": {"read": 0, "write": 0}}),
                ("compaction", {"status": "completed", "reason": "auto"}),
                ("compaction", {"status": "running"}),
                self._assistant(tokens={"input": 600, "output": 100,
                                        "reasoning": 50,
                                        "cache": {"read": 30000,
                                                  "write": 250}}),
                # Still streaming: its tokens aren't in yet.
                self._assistant({"type": "text", "text": "Working"}),
                catalog={"github-copilot": {"models": {"gpt-5-mini": {
                    "limit": {"context": 264000, "input": 128000,
                              "output": 64000}}}}})
            with patch.dict(os.environ, {"XDG_DATA_HOME": data_home}):
                usage = OpenCodeAgent.context_usage(self.SESSION, Path("/"))

        self.assertEqual((usage.tokens, usage.window, usage.compactions),
                         (31000, 128000, 1))

    def test_a_model_the_catalog_doesnt_list_has_no_window(self) -> None:
        with tempfile.TemporaryDirectory() as data_home:
            self._database(
                data_home,
                self._assistant(tokens={"input": 500, "output": 0,
                                        "reasoning": 0,
                                        "cache": {"read": 0, "write": 0}}),
                catalog={"anthropic": {"models": {}}})
            with patch.dict(os.environ, {"XDG_DATA_HOME": data_home}):
                usage = OpenCodeAgent.context_usage(self.SESSION, Path("/"))

        self.assertEqual((usage.tokens, usage.window), (500, 0))

    def test_no_database_yet_means_nothing_to_show(self) -> None:
        with tempfile.TemporaryDirectory() as data_home:
            with patch.dict(os.environ, {"XDG_DATA_HOME": data_home}):
                self.assertEqual(
                    OpenCodeAgent.peep(self.SESSION, Path("/")), [])
                self.assertIsNone(
                    OpenCodeAgent.context_usage(self.SESSION, Path("/")))

    def test_an_opencode_before_2_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as data_home:
            folder = Path(data_home) / "opencode"
            folder.mkdir()
            sqlite3.connect(folder / "opencode.db").close()
            with patch.dict(os.environ, {"XDG_DATA_HOME": data_home}):
                self.assertIsNone(
                    OpenCodeAgent.peep(self.SESSION, Path("/")))
