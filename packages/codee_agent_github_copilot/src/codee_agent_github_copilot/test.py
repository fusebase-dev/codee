import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from codee_agent_github_copilot.provider import GitHubCopilotAgent
from codee_main_context.context import Settings

SESSION = "82232f47-df60-4cb3-8c3a-de12074c9205"


def _event(kind: str, data: dict | None = None, **extra) -> str:
    event = {"type": kind, **extra}
    if data is not None:
        event["data"] = data
    return json.dumps(event)


def _stream(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["copilot"], returncode=returncode, stdout=stdout, stderr=stderr)


class CopilotRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = GitHubCopilotAgent(Settings(), Path("/repo"))

    def _run(self, completed) -> str:
        with patch("subprocess.run", return_value=completed) as run:
            self.captured = run
            return self.agent.run("/do-it CORE-1", SESSION)

    def test_returns_the_last_assistant_message(self) -> None:
        stdout = _stream(
            _event("assistant.message", {"content": "Looking at it", "toolRequests": [{}]}),
            _event("tool.execution_complete", {}),
            _event("assistant.message", {"content": "Done, PR is up.\n"}),
            _event("result", exitCode=0, sessionId=SESSION),
        )

        self.assertEqual(self._run(_completed(stdout)), "Done, PR is up.")

    def test_ignores_trailing_messages_that_only_call_tools(self) -> None:
        stdout = _stream(
            _event("assistant.message", {"content": "The answer is 42."}),
            _event("assistant.message", {"content": "", "toolRequests": [{}]}),
            _event("result", exitCode=0),
        )

        self.assertEqual(self._run(_completed(stdout)), "The answer is 42.")

    def test_passes_the_session_id_and_runs_headless(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))

        self._run(_completed(stdout))

        cmd = self.captured.call_args.args[0]
        self.assertEqual(cmd[:2], ["copilot", "-p"])
        self.assertEqual(cmd[2], "/do-it CORE-1")
        self.assertIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--session-id") + 1], SESSION)
        for flag in ("--allow-all", "--no-ask-user", "--output-format"):
            self.assertIn(flag, cmd)
        self.assertEqual(self.captured.call_args.kwargs["cwd"], Path("/repo"))

    def test_a_non_zero_exit_raises_with_the_stderr_reason(self) -> None:
        completed = _completed(
            stderr='Error: Model "nope" from --model flag is not available.',
            returncode=1)

        with self.assertRaises(RuntimeError) as caught:
            self._run(completed)

        self.assertIn("is not available", str(caught.exception))

    def test_a_failed_run_raises_with_the_session_error(self) -> None:
        stdout = _stream(
            _event("session.error", {"errorType": "quota", "message": "quota exceeded"}),
            _event("result", exitCode=1),
        )

        with self.assertRaises(RuntimeError) as caught:
            self._run(_completed(stdout))

        self.assertIn("quota exceeded", str(caught.exception))

    def test_a_run_with_no_response_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._run(_completed(_stream(_event("result", exitCode=0))))

    def test_output_that_is_not_the_event_stream_is_passed_through(self) -> None:
        self.assertEqual(self._run(_completed("plain text reply\n")),
                         "plain text reply\n")

    def test_a_timeout_raises(self) -> None:
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="copilot", timeout=7200)):
            with self.assertRaises(RuntimeError):
                self.agent.run("/do-it CORE-1", SESSION)


if __name__ == "__main__":
    unittest.main()
