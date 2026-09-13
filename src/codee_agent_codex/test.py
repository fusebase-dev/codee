import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codee_agent_codex.provider import (
    CodexAgent, _await_result, _mcp_overrides, _toml)
from codee_main_context.context import Settings

SESSION = "82232f47-df60-4cb3-8c3a-de12074c9205"
# Codex mints this itself and announces it; nothing here hands it one.
THREAD = "01a0997b-f37e-70e1-9c5d-f61c6de133b9"


def _event(kind: str, **fields) -> str:
    return json.dumps({"type": kind, **fields})


def _item(kind: str, **fields) -> str:
    return _event("item.completed", item={"id": "item_0", "type": kind, **fields})


def _stream(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _turn(*lines: str, message: str = "Done, PR is up.") -> str:
    """A whole successful run: thread, turn, a reply, and a clean finish."""
    return _stream(_event("thread.started", thread_id=THREAD),
                   _event("turn.started"),
                   *lines,
                   _item("agent_message", text=message),
                   _event("turn.completed", usage={}))


class _FakeProcess:
    """A `codex exec` whose pipes hand back canned lines, as Popen would.

    The provider streams both pipes on their own threads and waits, so the fake
    has to behave like real file objects (iterable, closeable) rather than mocks.
    """

    def __init__(self, stdout: str, stderr: str, returncode: int, timeout: bool):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self._timeout = timeout
        self.killed = False

    def wait(self, timeout=None):
        if self._timeout:
            raise subprocess.TimeoutExpired("codex", timeout or 0)
        return self.returncode

    def kill(self):
        self.killed = True
        self._timeout = False


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0,
               timeout: bool = False):
    return _FakeProcess(stdout, stderr, returncode, timeout)


class CodexRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("/repo")
        self.agent = CodexAgent(Settings(), self.root)

    def _run(self, completed, model: str = "", message: str = "/do-it CORE-1") -> str:
        self.reported = []
        with patch("subprocess.Popen", return_value=completed) as popen:
            self.captured = popen
            return self.agent.run(message, SESSION, model, self.reported.append)

    def _cmd(self) -> list[str]:
        return self.captured.call_args.args[0]

    def test_returns_the_last_agent_message(self) -> None:
        stdout = _turn(_item("agent_message", text="Looking at it"),
                       _item("command_execution", command="ls"),
                       message="Done, PR is up.\n")

        self.assertEqual(self._run(_completed(stdout)), "Done, PR is up.")

    def test_runs_headless_with_every_permission_granted(self) -> None:
        self._run(_completed(_turn()))

        cmd = self._cmd()
        self.assertEqual(cmd[:2], ["codex", "exec"])
        for flag in ("--json", "--skip-git-repo-check",
                     "--dangerously-bypass-approvals-and-sandbox"):
            self.assertIn(flag, cmd)
        self.assertEqual(self.captured.call_args.kwargs["cwd"], self.root)
        self.assertEqual(self.captured.call_args.kwargs["stdin"],
                         subprocess.DEVNULL)

    def test_the_session_id_is_not_passed_on(self) -> None:
        # Codex has no flag to be handed one; it names its own thread instead.
        self._run(_completed(_turn()))

        self.assertNotIn(SESSION, self._cmd())
        self.assertNotIn("resume", self._cmd())

    def test_the_thread_codex_opened_is_reported_back(self) -> None:
        # What the dashboard links its session viewer to while the run is live.
        self._run(_completed(_turn()))

        self.assertEqual(self.reported, [THREAD])

    def test_a_run_that_never_opened_a_thread_reports_nothing(self) -> None:
        with self.assertRaises(RuntimeError):
            self._run(_completed(stderr="not logged in", returncode=1))

        self.assertEqual(self.reported, [])

    def test_the_prompt_is_fenced_off_from_the_subcommands(self) -> None:
        # `codex exec review` is a subcommand, so a skill called `/review` would
        # never reach the model without the separator.
        self._run(_completed(_turn()), message="/review CORE-1")

        cmd = self._cmd()
        self.assertEqual(cmd[-2:], ["--", "/review CORE-1"])

    def test_the_skill_model_is_passed_on_the_command_line(self) -> None:
        self._run(_completed(_turn()), model="gpt-6-astra")

        cmd = self._cmd()
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-6-astra")

    def test_no_model_leaves_the_agent_on_its_default(self) -> None:
        self._run(_completed(_turn()))

        self.assertNotIn("--model", self._cmd())

    def test_the_best_model_is_a_codex_catalog_id(self) -> None:
        # Codex has no latest-tier alias, so this id is pinned by hand and only
        # a real catalog id will be accepted by the CLI.
        self.assertEqual(CodexAgent.best_model(), "gpt-6-astra")

    def test_a_non_zero_exit_raises_with_the_reason_from_the_stream(self) -> None:
        # Codex reports why it failed in the JSONL, leaving stderr empty.
        stdout = _stream(
            _event("thread.started", thread_id=THREAD),
            _event("error", message="The 'nope' model is not supported."))

        with self.assertRaises(RuntimeError) as raised:
            self._run(_completed(stdout, returncode=1))

        self.assertIn("not supported", str(raised.exception))

    def test_a_turn_that_fails_midway_raises(self) -> None:
        # The CLI can still exit 0 after a failed turn, so the absence of
        # turn.completed is what says the run did not finish.
        stdout = _stream(
            _event("thread.started", thread_id=THREAD),
            _event("turn.started"),
            _item("agent_message", text="Half way there"),
            _event("turn.failed", error={"message": "stream disconnected"}))

        with self.assertRaises(RuntimeError) as raised:
            self._run(_completed(stdout))

        self.assertIn("stream disconnected", str(raised.exception))

    def test_a_turn_that_says_nothing_raises(self) -> None:
        stdout = _stream(_event("thread.started", thread_id=THREAD),
                         _event("turn.started"),
                         _event("turn.completed", usage={}))

        with self.assertRaises(RuntimeError):
            self._run(_completed(stdout))

    def test_output_that_is_not_the_event_stream_is_handed_back(self) -> None:
        # A CLI that called itself successful is not failed on our account.
        self.assertEqual(self._run(_completed("plain text\n")), "plain text\n")

    def test_a_timeout_kills_the_process_and_raises(self) -> None:
        # Left alive it would hold a worker slot for the rest of the process.
        process = _completed(timeout=True)

        with patch("subprocess.Popen", return_value=process):
            with self.assertRaises(RuntimeError):
                self.agent.run("/do-it CORE-1", SESSION)

        self.assertTrue(process.killed)


class CodexMcpTest(unittest.TestCase):
    """Codex reads `[mcp_servers]` from its own config, never the project file."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def _write(self, config: dict) -> None:
        (self.root / ".mcp.json").write_text(json.dumps(config))

    def test_each_project_server_becomes_a_config_override(self) -> None:
        self._write({"mcpServers": {"ado": {
            "command": "npx", "args": ["-y", "@azure/mcp"],
            "env": {"ADO_PAT": "secret"}}}})

        self.assertEqual(_mcp_overrides(self.root), [
            "-c",
            'mcp_servers.ado={"command" = "npx", "args" = ["-y", "@azure/mcp"], '
            '"env" = {"ADO_PAT" = "secret"}}',
        ])

    def test_a_server_with_no_environment_carries_none(self) -> None:
        self._write({"mcpServers": {"ado": {"command": "npx", "args": []}}})

        self.assertEqual(_mcp_overrides(self.root),
                         ["-c", 'mcp_servers.ado={"command" = "npx", "args" = []}'])

    def test_a_project_without_the_file_passes_no_overrides(self) -> None:
        self.assertEqual(_mcp_overrides(self.root), [])

    def test_a_broken_file_passes_no_overrides_rather_than_failing_the_run(self) -> None:
        (self.root / ".mcp.json").write_text("{not json")

        self.assertEqual(_mcp_overrides(self.root), [])

    def test_quotes_and_backslashes_in_a_value_survive(self) -> None:
        # The override is parsed as TOML, so a Windows path or a quoted argument
        # has to come out escaped rather than ending the string early.
        self.assertEqual(_toml({"a": 'say "hi"\\now'}),
                         '{"a" = "say \\"hi\\"\\\\now"}')


class CodexModelsTest(unittest.TestCase):
    def test_the_catalog_comes_back_as_ids_and_display_names(self) -> None:
        result = {"data": [{"id": "gpt-6-astra", "displayName": "GPT-6-Astra"},
                           {"id": "gpt-5.5", "displayName": "GPT-5.5"}]}

        with patch("codee_agent_codex.provider.subprocess.Popen"), \
                patch("codee_agent_codex.provider._send"), \
                patch("codee_agent_codex.provider._await_result",
                      return_value=result):
            models = CodexAgent.list_models()

        self.assertEqual([(model.id, model.name) for model in models],
                         [("gpt-6-astra", "GPT-6-Astra"), ("gpt-5.5", "GPT-5.5")])

    def test_a_cli_that_cannot_be_asked_yields_an_empty_list(self) -> None:
        # The skill editor falls back to a hand-typed id, so a missing or
        # signed-out CLI must not break the page.
        with patch("codee_agent_codex.provider.subprocess.Popen",
                   side_effect=FileNotFoundError("codex")):
            self.assertEqual(CodexAgent.list_models(), [])

    def test_an_app_server_that_exits_is_reported_rather_than_waited_out(self) -> None:
        process = Mock()
        process.poll.return_value = 1
        process.returncode = 1
        process.stderr.read.return_value = "not logged in"

        with self.assertRaises(RuntimeError) as raised:
            _await_result(process, _EmptyQueue(), 2)

        self.assertIn("not logged in", str(raised.exception))


class _EmptyQueue:
    """A queue that never has a line, so the exit check is what answers."""

    def get(self, timeout: float = 0):
        raise __import__("queue").Empty


if __name__ == "__main__":
    unittest.main()
