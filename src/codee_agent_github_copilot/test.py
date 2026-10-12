import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codee_agent_abstract.provider import AgentModel
from codee_agent_github_copilot.provider import (
    COPILOT_DEBUG_ENV_VAR, MAX_AI_CREDITS_ENV_VAR, GitHubCopilotAgent,
    _context_tiers)
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

    def _run(self, completed, model: str = "", effort: str = "",
             context_tier: str = "") -> str:
        with patch("subprocess.run", return_value=completed) as run:
            self.captured = run
            return self.agent.run("/do-it CORE-1", SESSION, model,
                                  effort=effort, context_tier=context_tier)

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

    def test_the_skill_effort_is_passed_on_the_command_line(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))

        self._run(_completed(stdout), "gpt-6-sol", "none")

        cmd = self.captured.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--reasoning-effort") + 1], "none")

    def test_no_effort_leaves_the_model_on_its_default(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))

        self._run(_completed(stdout), "gpt-6-sol")

        self.assertNotIn("--reasoning-effort", self.captured.call_args.args[0])

    def test_auto_takes_the_skill_effort_as_its_tier(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))

        self._run(_completed(stdout), "auto", "intelligence")

        cmd = self.captured.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--auto-tier") + 1], "intelligence")
        self.assertNotIn("--reasoning-effort", cmd)

    def test_auto_without_a_tier_runs_on_balance(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))

        self._run(_completed(stdout), "auto")

        cmd = self.captured.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--auto-tier") + 1], "balance")

    def test_other_models_get_no_tier(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))

        self._run(_completed(stdout), "gpt-6-sol", "high")

        self.assertNotIn("--auto-tier", self.captured.call_args.args[0])

    def test_the_skill_context_tier_is_passed_on_the_command_line(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))

        self._run(_completed(stdout), "claude-opus-5.5",
                  context_tier="long_context")

        cmd = self.captured.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--context") + 1], "long_context")

    def test_no_context_tier_pins_the_default_window(self) -> None:
        stdout = _stream(_event("assistant.message", {"content": "ok"}),
                         _event("result", exitCode=0))

        self._run(_completed(stdout), "claude-opus-5.5")

        cmd = self.captured.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--context") + 1], "default")

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
    def test_the_catalog_is_read_from_copilots_acp_mode(self) -> None:
        catalog = [AgentModel("claude-opus-5", "Claude Opus 5",
                              ("low", "high"), "high")]

        with patch("codee_agent_github_copilot.provider.acp.fetch_models",
                   return_value=catalog) as fetch, \
                patch("codee_agent_github_copilot.provider._headless_models",
                      return_value=[]):
            self.assertEqual(GitHubCopilotAgent.list_models(), catalog)

        fetch.assert_called_once_with(["copilot", "--acp"])

    def test_auto_offers_its_routing_tiers(self) -> None:
        catalog = [AgentModel("auto", "Auto"),
                   AgentModel("claude-opus-5", "Claude Opus 5")]

        with patch("codee_agent_github_copilot.provider.acp.fetch_models",
                   return_value=catalog), \
                patch("codee_agent_github_copilot.provider._headless_models",
                      return_value=[]):
            models = GitHubCopilotAgent.list_models()

        self.assertEqual(models[0], AgentModel(
            "auto", "Auto", ("efficiency", "balance", "intelligence"),
            "balance", "Tier"))
        self.assertEqual(models[1], catalog[1])

    def test_models_with_a_long_context_offer_both_windows(self) -> None:
        catalog = [AgentModel("claude-opus-5.5", "Claude Opus 5.5",
                              ("low", "high"), "high"),
                   AgentModel("claude-haiku-4.5", "Claude Haiku 4.5")]
        headless = [
            {"id": "claude-opus-5.5",
             "capabilities": {"limits": {"max_context_window_tokens": 1000000}},
             "billing": {"tokenPrices": {"maxPromptTokens": 200000,
                                         "longContext": {"inputPrice": 400}}}},
            {"id": "claude-haiku-4.5",
             "capabilities": {"limits": {"max_context_window_tokens": 200000}},
             "billing": {"tokenPrices": {}}},
        ]

        with patch("codee_agent_github_copilot.provider.acp.fetch_models",
                   return_value=catalog), \
                patch("codee_agent_github_copilot.provider._headless_models",
                      return_value=headless):
            opus, haiku = GitHubCopilotAgent.list_models()

        self.assertEqual(opus.context_tiers,
                         (("default", "200K"), ("long_context", "1M")))
        self.assertEqual(opus.efforts, ("low", "high"))
        self.assertEqual(haiku.context_tiers, ())

    def test_a_window_a_little_over_a_million_still_reads_as_1m(self) -> None:
        tiers = _context_tiers([{
            "id": "gpt-6.1-sol",
            "capabilities": {"limits": {"max_context_window_tokens": 1050000}},
            "billing": {"tokenPrices": {"maxPromptTokens": 272000,
                                        "longContext": {}}}}])

        self.assertEqual(tiers["gpt-6.1-sol"],
                         (("default", "272K"), ("long_context", "1M")))

    def test_no_context_windows_still_lists_the_models(self) -> None:
        catalog = [AgentModel("claude-opus-5.5", "Claude Opus 5.5")]

        with patch("codee_agent_github_copilot.provider.acp.fetch_models",
                   return_value=catalog), \
                patch("codee_agent_github_copilot.provider._headless_models",
                      side_effect=RuntimeError("no answer")):
            self.assertEqual(GitHubCopilotAgent.list_models(), catalog)

    def test_an_unavailable_cli_yields_no_models_rather_than_raising(self) -> None:
        with patch("codee_agent_abstract.acp.subprocess.Popen",
                   side_effect=FileNotFoundError("copilot")):
            self.assertEqual(GitHubCopilotAgent.list_models(), [])


class GitHubCopilotPeepTest(unittest.TestCase):
    def _write_events(self, home: str, *records: dict) -> None:
        folder = Path(home) / "session-state" / SESSION
        folder.mkdir(parents=True)
        (folder / "events.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8")

    def test_reads_the_last_steps_from_the_sessions_events(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self._write_events(
                home,
                {"type": "session.start", "data": {"sessionId": SESSION}},
                {"type": "user.message", "data": {"content": "Do the task"}},
                {"type": "assistant.message",
                 "timestamp": "2026-10-01T10:00:00Z",
                 "data": {"content": "",
                          "reasoningText": "Reading the logs first.",
                          "toolRequests": [{"name": "bash", "arguments": {
                              "command": "tail app.log"}}]}},
                {"type": "tool.execution_start",
                 "timestamp": "2026-10-01T10:00:01Z",
                 "data": {"toolName": "bash",
                          "arguments": {"command": "tail app.log"}}},
                {"type": "tool.execution_complete",
                 "data": {"result": {"content": "secret output"}}},
                {"type": "assistant.message",
                 "data": {"content": "Found it.", "toolRequests": []}},
            )
            with patch.dict(os.environ, {"COPILOT_HOME": home}):
                steps = GitHubCopilotAgent.peep(SESSION, Path("/repo"))

        self.assertEqual(
            [(step.kind, step.text) for step in steps],
            [("thinking", "Reading the logs first."),
             ("tool", "bash: tail app.log"),
             ("text", "Found it.")])
        self.assertEqual(steps[0].timestamp, "2026-10-01T10:00:00Z")
        self.assertEqual(steps[1].timestamp, "2026-10-01T10:00:01Z")

    def test_keeps_only_the_last_steps(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self._write_events(home, *[
                {"type": "assistant.message", "data": {"content": f"Step {n}"}}
                for n in range(5)])
            with patch.dict(os.environ, {"COPILOT_HOME": home}):
                steps = GitHubCopilotAgent.peep(SESSION, Path("/repo"), 2)

        self.assertEqual([step.text for step in steps], ["Step 3", "Step 4"])

    def test_a_session_not_started_yet_has_no_steps(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"COPILOT_HOME": home}):
                self.assertEqual(
                    GitHubCopilotAgent.peep(SESSION, Path("/repo")), [])

    def test_a_session_id_that_is_not_a_plain_name_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"COPILOT_HOME": home}):
                self.assertEqual(
                    GitHubCopilotAgent.peep("../outside", Path("/repo")), [])


if __name__ == "__main__":
    unittest.main()


class GitHubCopilotContextUsageTest(unittest.TestCase):
    def _usage_record(self, input_tokens: int, limit: int, **fields) -> dict:
        return {"type": "session.usage_record", **fields, "data": {"usage": {
            "model": "claude-opus-5", "inputTokens": input_tokens,
            "cacheReadTokens": input_tokens - 100, "outputTokens": 500,
            "maxPromptTokens": limit}}}

    def _usage_of(self, *records: dict) -> object:
        with tempfile.TemporaryDirectory() as home:
            folder = Path(home) / "session-state" / SESSION
            folder.mkdir(parents=True)
            (folder / "events.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8")
            with patch.dict(os.environ, {"COPILOT_HOME": home}):
                return GitHubCopilotAgent.context_usage(SESSION, Path("/repo"))

    def test_reads_the_last_calls_tokens_and_counts_the_compactions(
            self) -> None:
        usage = self._usage_of(
            self._usage_record(120000, 128000),
            {"type": "session.compaction_complete",
             "data": {"success": True, "preCompactionTokens": 120500,
                      "postCompactionTokens": 9000, "tokenLimit": 128000}},
            {"type": "session.compaction_complete",
             "data": {"success": False, "error": "timeout"}},
            self._usage_record(30000, 128000),
            self._usage_record(90000, 64000, agentId="explore-1"),
            {"type": "session.compaction_complete", "agentId": "explore-1",
             "data": {"success": True, "postCompactionTokens": 1}})

        self.assertEqual((usage.tokens, usage.window, usage.compactions),
                         (30500, 128000, 1))

    def test_a_compaction_leaves_what_it_kept_until_the_next_call(
            self) -> None:
        usage = self._usage_of(
            self._usage_record(120000, 128000),
            {"type": "session.compaction_complete",
             "data": {"success": True, "postCompactionTokens": 9000,
                      "tokenLimit": 128000}})

        self.assertEqual((usage.tokens, usage.window, usage.compactions),
                         (9000, 128000, 1))

    def test_a_cli_that_logs_no_usage_says_nothing(self) -> None:
        self.assertIsNone(self._usage_of(
            {"type": "assistant.message", "data": {"content": "Hi"}}))
