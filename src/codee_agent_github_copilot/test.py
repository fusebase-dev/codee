import json
import os
import queue
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codee_agent_github_copilot.provider import (
    COPILOT_DEBUG_ENV_VAR, MAX_AI_CREDITS_ENV_VAR, GitHubCopilotAgent,
    _await_result)
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


class CopilotSkillPromptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = GitHubCopilotAgent(Settings(), Path("/repo"))

    def _prompt(self, path: Path, argument: str = "", argument_name: str = "") -> str:
        return self.agent.skill_prompt("story-code-reviewer", path, argument,
                                       argument_name)

    def test_names_the_skill_file_instead_of_a_slash_command(self) -> None:
        prompt = self._prompt(
            Path("/repo/.claude/skills/story-code-reviewer/SKILL.md"),
            "90939", "STORY_ID")

        self.assertEqual(
            prompt,
            "Read .claude/skills/story-code-reviewer/SKILL.md and follow its "
            "instructions exactly. STORY_ID = 90939",
        )

    def test_a_skill_that_names_no_argument_gets_a_generic_label(self) -> None:
        prompt = self._prompt(
            Path("/repo/.claude/skills/story-code-reviewer/SKILL.md"), "90939")

        self.assertTrue(prompt.endswith("ARGUMENT = 90939"), prompt)

    def test_no_argument_leaves_the_instruction_alone(self) -> None:
        prompt = self._prompt(
            Path("/repo/.claude/skills/story-code-reviewer/SKILL.md"))

        self.assertEqual(
            prompt,
            "Read .claude/skills/story-code-reviewer/SKILL.md and follow its "
            "instructions exactly.",
        )

    def test_a_skill_outside_the_working_directory_keeps_its_full_path(self) -> None:
        prompt = self._prompt(
            Path("/elsewhere/skills/reviewer/SKILL.md"), "7", "ID")

        self.assertEqual(
            prompt,
            "Read /elsewhere/skills/reviewer/SKILL.md and follow its "
            "instructions exactly. ID = 7",
        )


class CopilotRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = GitHubCopilotAgent(Settings(), Path("/repo"))

    def _run(self, completed, model: str = "") -> str:
        with patch("subprocess.run", return_value=completed) as run:
            self.captured = run
            return self.agent.run("/do-it CORE-1", SESSION, model)

    def test_returns_the_last_assistant_message(self) -> None:
        stdout = _stream(
            _event("assistant.message", {
                   "content": "Looking at it", "toolRequests": [{}]}),
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
        self.assertEqual(self.captured.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(self.captured.call_args.kwargs["errors"], "replace")

    def test_prepends_the_configured_prompt_prefix(self) -> None:
        self.agent = GitHubCopilotAgent(
            Settings(github_copilot_prompt_prefix="Follow repository policy."),
            Path("/repo"),
        )
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))

        self._run(_completed(stdout))

        cmd = self.captured.call_args.args[0]
        self.assertEqual(
            cmd[cmd.index("-p") + 1],
            "Follow repository policy.\n\n/do-it CORE-1",
        )

    def test_continuing_reuses_the_same_session_id(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))
        with patch("subprocess.run", return_value=_completed(stdout)) as run:
            self.agent.continue_conversation("And now?", SESSION)

        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--session-id") + 1], SESSION)

    def test_missing_captured_streams_do_not_raise_type_error(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["copilot"], returncode=0, stdout=None, stderr=None)

        self.assertEqual(self._run(completed), "")

    def test_the_skill_model_is_passed_on_the_command_line(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))

        self._run(_completed(stdout), model="claude-opus-5")

        cmd = self.captured.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-5")

    def test_no_model_leaves_the_agent_on_its_default(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))

        self._run(_completed(stdout))

        self.assertNotIn("--model", self.captured.call_args.args[0])

    def test_debug_env_adds_log_level_and_preserves_stderr(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))
        with patch.dict(os.environ, {COPILOT_DEBUG_ENV_VAR: "true"}):
            response = self._run(_completed(stdout, stderr="debug line\n"))

        cmd = self.captured.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--log-level") + 1], "debug")
        self.assertEqual(response, "ok")
        self.assertEqual(response.debug_logs, "debug line\n")

    def test_debug_env_must_be_true(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))
        with patch.dict(os.environ, {COPILOT_DEBUG_ENV_VAR: "1"}):
            response = self._run(_completed(stdout, stderr="not retained"))

        self.assertNotIn("--log-level", self.captured.call_args.args[0])
        self.assertEqual(response.debug_logs, "")

    def test_the_best_model_is_an_anthropic_catalog_id(self) -> None:
        # Copilot has no latest-tier alias, so this id is pinned by hand and only
        # a real catalog id will be accepted by the CLI.
        self.assertEqual(GitHubCopilotAgent.best_model(), "claude-opus-5")

    def test_a_non_zero_exit_raises_with_the_stderr_reason(self) -> None:
        completed = _completed(
            stderr='Error: Model "nope" from --model flag is not available.',
            returncode=1)

        with self.assertRaises(RuntimeError) as caught:
            self._run(completed)

        self.assertIn("is not available", str(caught.exception))

    def test_a_failed_run_raises_with_the_session_error(self) -> None:
        stdout = _stream(
            _event("session.error", {
                   "errorType": "quota", "message": "quota exceeded"}),
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

    def test_raw_output_keeps_debug_logs(self) -> None:
        with patch.dict(os.environ, {COPILOT_DEBUG_ENV_VAR: "TRUE"}):
            response = self._run(_completed("plain text reply\n", "debug\n"))

        self.assertEqual(response, "plain text reply\n")
        self.assertEqual(response.debug_logs, "debug\n")

    def test_a_timeout_raises(self) -> None:
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="copilot", timeout=7200)):
            with self.assertRaises(RuntimeError):
                self.agent.run("/do-it CORE-1", SESSION)


class CopilotCreditCapTest(unittest.TestCase):
    def _credits(self, value: str | None) -> str:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))
        with patch.dict(os.environ, {}, clear=False):
            if value is None:
                os.environ.pop(MAX_AI_CREDITS_ENV_VAR, None)
            else:
                os.environ[MAX_AI_CREDITS_ENV_VAR] = value
            with patch("subprocess.run", return_value=_completed(stdout)) as run:
                GitHubCopilotAgent(Settings(), Path(
                    "/repo")).run("/do-it", SESSION)
        cmd = run.call_args.args[0]
        return cmd[cmd.index("--max-ai-credits") + 1]

    def test_an_unset_env_var_keeps_the_default_cap(self) -> None:
        self.assertEqual(self._credits(None), "1000")

    def test_the_env_var_sets_the_cap(self) -> None:
        self.assertEqual(self._credits("250"), "250")

    def test_a_blank_env_var_keeps_the_default_cap(self) -> None:
        self.assertEqual(self._credits("  "), "1000")

    def test_a_cap_under_the_cli_minimum_is_raised_to_it(self) -> None:
        self.assertEqual(self._credits("5"), "30")

    def test_a_non_numeric_cap_falls_back_to_the_default(self) -> None:
        self.assertEqual(self._credits("lots"), "1000")


class CopilotModelCatalogTest(unittest.TestCase):
    def _queue(self, *lines: str) -> "queue.Queue[str]":
        lines_queue: queue.Queue[str] = queue.Queue()
        for line in lines:
            lines_queue.put(line)
        return lines_queue

    def test_reads_the_session_new_result_past_other_traffic(self) -> None:
        lines = self._queue(
            json.dumps({"jsonrpc": "2.0", "id": 1,
                       "result": {"protocolVersion": 1}}),
            json.dumps(
                {"jsonrpc": "2.0", "method": "session/update", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2,
                       "result": {"sessionId": "s1"}}),
        )

        result = _await_result(Mock(poll=Mock(return_value=None)), lines, 2)

        self.assertEqual(result["sessionId"], "s1")

    def test_an_early_exit_raises_instead_of_waiting_out_the_deadline(self) -> None:
        process = Mock(poll=Mock(return_value=1), returncode=1,
                       stderr=Mock(read=Mock(return_value="not logged in")))

        with self.assertRaises(RuntimeError) as caught:
            _await_result(process, self._queue(), 2)

        self.assertIn("not logged in", str(caught.exception))

    def test_a_catalog_becomes_id_and_name_pairs(self) -> None:
        result = {"models": {"availableModels": [
            {"modelId": "claude-opus-5", "name": "Claude Opus 5"},
            # no display name: falls back to the id
            {"modelId": "gpt-5.4"},
            {"name": "nameless"},            # no id at all: unusable, skipped
        ]}}

        with patch("codee_agent_github_copilot.provider.subprocess.Popen") as popen, \
                patch("codee_agent_github_copilot.provider._send"), \
                patch("codee_agent_github_copilot.provider._await_result",
                      return_value=result):
            models = GitHubCopilotAgent.list_models()

        self.assertEqual([(m.id, m.name) for m in models],
                         [("claude-opus-5", "Claude Opus 5"), ("gpt-5.4", "gpt-5.4")])
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")

    def test_an_unavailable_cli_yields_no_models_rather_than_raising(self) -> None:
        with patch("codee_agent_github_copilot.provider.subprocess.Popen",
                   side_effect=FileNotFoundError("copilot")):
            self.assertEqual(GitHubCopilotAgent.list_models(), [])


if __name__ == "__main__":
    unittest.main()
