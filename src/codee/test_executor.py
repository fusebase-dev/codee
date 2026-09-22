import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_agent_codex.provider import CodexAgent
from codee_agent_github_copilot.provider import GitHubCopilotAgent
from codee_main_context.context import (
    CodingAgent, Settings, TasksProvider, save_settings)
from codee_tasks_azure_devops.provider import AzureDevOpsTasksProvider
from codee_tasks_jira.provider import JiraTasksProvider

from codee import executor, tasks_providers
from codee.lib import runs_db


class RefreshConfigTest(unittest.TestCase):
    """The executor re-reads settings.json each poll, so edits need no restart."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.data_dir = Path(self._temporary.name)
        # The Azure DevOps provider resolves its token store from the ambient
        # data dir at construction, so point that at the temp dir too.
        environment = patch.dict(os.environ,
                                 {"CODEE_DATA_DIR": str(self.data_dir)})
        environment.start()
        self.addCleanup(environment.stop)

        original_data_dir = executor.context.data_dir
        original_settings = executor.context.settings
        original_provider = executor.tasks_provider
        original_agent = executor.coding_agent

        def restore() -> None:
            executor.context.data_dir = original_data_dir
            executor.context.settings = original_settings
            executor.tasks_provider = original_provider
            executor.coding_agent = original_agent

        self.addCleanup(restore)

        executor.context.data_dir = self.data_dir
        executor.context.settings = Settings()
        executor.tasks_provider = executor.build_tasks_provider(Settings())

    def _save(self, **overrides) -> Settings:
        settings = Settings(**overrides)
        save_settings(self.data_dir, settings)
        return settings

    def test_switching_provider_rebuilds_it(self) -> None:
        self._save(
            tasks_provider=TasksProvider.AZURE_DEVOPS,
            credentials={TasksProvider.AZURE_DEVOPS.value: {
                "organization_url": "https://dev.azure.com/acme",
                "tenant_id": "tenant-1",
                "client_id": "client-1",
                "client_secret": "secret-1",
            }},
        )

        executor._refresh_config()

        self.assertIsInstance(executor.tasks_provider, AzureDevOpsTasksProvider)
        self.assertEqual(executor.context.settings.tasks_provider,
                         TasksProvider.AZURE_DEVOPS)

    def test_new_credentials_reach_the_provider(self) -> None:
        self._save(credentials={TasksProvider.JIRA.value: {
            "base_url": "https://acme.atlassian.net",
            "account_email": "bot@acme.test",
            "api_token": "rotated-token",
            "project": "NIM",
        }})

        executor._refresh_config()

        provider = executor.tasks_provider
        self.assertIsInstance(provider, JiraTasksProvider)
        self.assertTrue(provider.is_configured())
        self.assertIn("NIM", provider.describe())

    def test_a_new_task_filter_reaches_the_provider(self) -> None:
        # The filter is captured at construction like the credentials are, so
        # without a rebuild the poll keeps running the query it started with.
        self._save(credentials={TasksProvider.JIRA.value: {
            "base_url": "https://acme.atlassian.net",
            "account_email": "bot@acme.test",
            "api_token": "token",
            "project": "NIM",
        }}, task_filters={TasksProvider.JIRA.value: 'labels = "codee"'})

        executor._refresh_config()

        self.assertIn('AND (labels = "codee") ',
                      executor.tasks_provider._build_jql(
                          executor.tasks_provider._work_items[0], ["Ready"]))

    def test_unchanged_settings_keep_the_live_provider(self) -> None:
        self._save()
        executor._refresh_config()
        provider = executor.tasks_provider

        executor._refresh_config()

        self.assertIs(executor.tasks_provider, provider)

    def test_broken_provider_settings_keep_polling_with_the_old_one(self) -> None:
        self._save(credentials={TasksProvider.JIRA.value: {
            "base_url": "https://acme.atlassian.net",
            "account_email": "bot@acme.test",
            "api_token": "token",
            "project": "NIM",
        }})
        executor._refresh_config()
        provider = executor.tasks_provider

        self._save(
            tasks_provider=TasksProvider.AZURE_DEVOPS,
            credentials={TasksProvider.AZURE_DEVOPS.value: {
                "organization_url": "https://dev.azure.com/acme"}},
        )
        with patch.dict(tasks_providers.TASKS_PROVIDERS,
                        {TasksProvider.AZURE_DEVOPS: _Exploding}):
            executor._refresh_config()

        self.assertIs(executor.tasks_provider, provider)

    def test_switching_coding_agent_rebuilds_it(self) -> None:
        self._save(coding_agent=CodingAgent.CLAUDE_CODE)
        executor._refresh_config()

        self._save(coding_agent=CodingAgent.GITHUB_COPILOT)
        executor._refresh_config()

        self.assertIsInstance(executor.coding_agent, GitHubCopilotAgent)
        self.assertEqual(executor.context.settings.coding_agent,
                         CodingAgent.GITHUB_COPILOT)

    def test_broken_coding_agent_settings_keep_polling_with_the_old_one(self) -> None:
        self._save(coding_agent=CodingAgent.CLAUDE_CODE)
        executor._refresh_config()
        agent = executor.coding_agent

        # An agent that can't be built must not take down the poll loop.
        self._save(coding_agent=CodingAgent.GITHUB_COPILOT)
        with patch.dict(executor._CODING_AGENTS,
                        {CodingAgent.GITHUB_COPILOT: _Exploding}):
            executor._refresh_config()

        self.assertIs(executor.coding_agent, agent)


class AgentForSkillTest(unittest.TestCase):
    """``x-codee-agent`` picks the agent; everything else gets the default one."""

    def setUp(self) -> None:
        original_settings = executor.context.settings
        original_agent = executor.coding_agent
        self.addCleanup(
            lambda: setattr(executor.context, "settings", original_settings))
        self.addCleanup(lambda: setattr(executor, "coding_agent",
                                        original_agent))
        executor.context.settings = Settings(
            coding_agent=CodingAgent.CLAUDE_CODE)
        executor.coding_agent = executor._build_coding_agent(
            executor.context.settings)

    def test_a_skill_that_names_no_agent_runs_on_the_default_one(self) -> None:
        self.assertIs(executor._agent_for_skill(""), executor.coding_agent)

    def test_a_skill_that_names_the_default_agent_reuses_it(self) -> None:
        self.assertIs(executor._agent_for_skill("claude_code"),
                      executor.coding_agent)

    def test_a_skill_that_names_another_agent_gets_that_one(self) -> None:
        self.assertIsInstance(executor._agent_for_skill("codex"), CodexAgent)

    def test_an_agent_codee_cannot_run_falls_back_to_the_default(self) -> None:
        # A typo in frontmatter must not fail the poll forever; the work still
        # gets done by the configured agent.
        self.assertIs(executor._agent_for_skill("cursor"),
                      executor.coding_agent)


class SkillPromptTest(unittest.TestCase):
    """The invocation a skill gets is phrased by the agent that will run it."""

    SKILL = Path("/repo/.claude/skills/story-code-reviewer/SKILL.md")

    def test_claude_code_gets_the_slash_command(self) -> None:
        agent = ClaudeCodeAgent(Settings(), Path("/repo"))

        self.assertEqual(
            agent.skill_prompt("story-code-reviewer", self.SKILL, "90939",
                               "STORY_ID"),
            "/story-code-reviewer 90939",
        )

    def test_copilot_gets_told_to_read_the_skill_file(self) -> None:
        # `copilot` resolves no slash command and the skill hides itself with
        # disable-model-invocation, so the path has to be spelled out.
        agent = GitHubCopilotAgent(Settings(), Path("/repo"))

        self.assertEqual(
            agent.skill_prompt("story-code-reviewer", self.SKILL, "90939",
                               "STORY_ID"),
            "Read .claude/skills/story-code-reviewer/SKILL.md and follow its "
            "instructions exactly. STORY_ID = 90939",
        )


class RunAgentSelectionTest(unittest.TestCase):
    """The agent a run lands on is the one the triggering skill asked for."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        original_data_dir = executor.context.data_dir
        self.addCleanup(
            lambda: setattr(executor.context, "data_dir", original_data_dir))
        executor.context.data_dir = Path(self._temporary.name)

    def test_the_skills_agent_and_model_reach_the_agent(self) -> None:
        agent = Mock()
        agent.run.return_value = "done"
        agent.describe.return_value = "CodexAgent"

        with patch.object(executor, "_agent_for_skill",
                          return_value=agent) as chosen:
            reply = executor._run_agent("/nightly", "sid-1", "gpt-6-astra",
                                        "codex")

        self.assertEqual(reply, "done")
        chosen.assert_called_once_with("codex")
        self.assertEqual(agent.run.call_args.args[:3],
                         ("/nightly", "sid-1", "gpt-6-astra"))


class RunTaskLoggingTest(unittest.TestCase):
    """Issue-triggered coding runs land in the runs table like the other triggers."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        original_data_dir = executor.context.data_dir
        self.addCleanup(
            lambda: setattr(executor.context, "data_dir", original_data_dir))
        executor.context.data_dir = Path(self._temporary.name)

    def _runs(self) -> list[dict]:
        return runs_db.recent_runs(main_context=executor.context)

    def test_successful_run_is_recorded(self) -> None:
        with patch.object(executor, "_run_agent", return_value="done"):
            executor._run_task("NIM-1", "/story-developer NIM-1",
                               "sid-1", "story-developer")

        run, = self._runs()
        self.assertEqual(run["skill_name"], "story-developer")
        self.assertEqual(run["trigger_type"], "issue")
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["session_id"], "sid-1")
        self.assertEqual(run["message"], "/story-developer NIM-1")
        self.assertEqual(run["user_message"], "/story-developer NIM-1")
        self.assertEqual(run["response"], "done")

    def test_failed_run_is_recorded_with_the_error(self) -> None:
        with patch.object(executor, "_run_agent", side_effect=RuntimeError("over limit")):
            executor._run_task("NIM-2", "/story-developer NIM-2",
                               "sid-2", "story-developer")

        run, = self._runs()
        self.assertEqual(run["status"], "failed")
        self.assertIn("over limit", run["error"])

    def test_bookkeeping_failure_does_not_sink_a_finished_run(self) -> None:
        # finish_job runs in _run_agent's finally; if it raises (a stale call
        # signature, an unwritable db) it must not replace the agent's reply,
        # which would mark a completed run "failed" and leave the cron slot due.
        with patch.object(executor.coding_agent, "run", return_value="done"), \
                patch.object(runs_db, "finish_job",
                             side_effect=TypeError("missing 1 required positional argument")):
            self.assertEqual(executor._run_agent("/story-developer NIM-4", "sid-4"),
                             "done")

    def test_the_run_log_shows_the_command_not_the_agents_wording(self) -> None:
        # A Copilot run is prompted with the skill's file path, but the run log
        # is still the slash command, so both agents' runs read the same.
        prompt = ("Read .claude/skills/story-developer/SKILL.md and follow its "
                  "instructions exactly. STORY_ID = 4124")

        with patch.object(executor, "_run_agent", return_value="done") as run:
            executor._run_task("NIM-5", prompt, "sid-5", "story-developer",
                               label="/story-developer 4124")

        run_record, = self._runs()
        self.assertEqual(run_record["message"], "/story-developer 4124")
        self.assertEqual(run_record["user_message"], prompt)
        # The agent still gets the wording it can act on.
        self.assertEqual(run.call_args.args[0], prompt)

    def test_a_failed_run_is_logged_under_the_command_too(self) -> None:
        with patch.object(executor, "_run_agent", side_effect=RuntimeError("boom")):
            executor._run_task("NIM-6", "Read .claude/skills/x/SKILL.md ...",
                               "sid-6", "story-developer",
                               label="/story-developer 4124")

        run_record, = self._runs()
        self.assertEqual(run_record["message"], "/story-developer 4124")

    def test_the_live_job_row_carries_the_command(self) -> None:
        with patch.object(executor.coding_agent, "run", return_value="done"), \
                patch.object(runs_db, "start_job",
                             return_value="job-1") as started:
            executor._run_agent("Read .claude/skills/x/SKILL.md ...", "sid-7",
                                label="/story-developer 4124")

        self.assertEqual(started.call_args.args[1], "/story-developer 4124")

    def test_a_run_with_no_label_is_shown_as_its_prompt(self) -> None:
        # Cron, email and SQS hand over a skill body and have no command to show.
        with patch.object(executor, "_run_agent", return_value="done"):
            executor._run_task("NIM-7", "Do the nightly sweep", "sid-8",
                               "nightly")

        run_record, = self._runs()
        self.assertEqual(run_record["message"], "Do the nightly sweep")

    def test_counts_include_issue_runs(self) -> None:
        with patch.object(executor, "_run_agent", return_value="done"):
            executor._run_task("NIM-3", "/story-developer NIM-3",
                               "sid-3", "story-developer")

        counts = runs_db.counts(executor.context)
        self.assertEqual(counts, {"total": 1, "last_24h": 1})


class MaxParallelAgentsTest(unittest.TestCase):
    """The cap is read per launch, so a settings edit needs no restart."""

    def setUp(self) -> None:
        original_settings = executor.context.settings
        self.addCleanup(
            lambda: setattr(executor.context, "settings", original_settings))
        executor.context.settings = Settings(max_parallel_agents=2)
        with executor._inflight_lock:
            self.addCleanup(executor._inflight.clear)
            executor._inflight.clear()

    def _submit(self, task_id: str) -> bool:
        # Patch the worker, not the thread: a launched task must really claim
        # its slot, which is what the cap is counting.
        with patch.object(executor, "_run_task") as worker:
            launched = executor._submit_task(task_id, f"/skill {task_id}",
                                             f"sid-{task_id}", "skill")
        if launched:
            worker.assert_called_once()
        return launched

    def test_launches_up_to_the_configured_cap(self) -> None:
        self.assertTrue(self._submit("NIM-1"))
        self.assertTrue(self._submit("NIM-2"))
        self.assertFalse(self._submit("NIM-3"))

    def test_a_raised_cap_applies_without_a_restart(self) -> None:
        self._submit("NIM-1")
        self._submit("NIM-2")

        executor.context.settings = Settings(max_parallel_agents=3)

        self.assertTrue(self._submit("NIM-3"))

    def test_a_lowered_cap_applies_without_a_restart(self) -> None:
        executor.context.settings = Settings(max_parallel_agents=4)
        self._submit("NIM-1")

        executor.context.settings = Settings(max_parallel_agents=1)

        # The one already running is left alone; nothing new starts.
        self.assertFalse(self._submit("NIM-2"))
        self.assertEqual(executor._inflight, {"NIM-1"})

    def test_a_task_already_in_flight_is_not_launched_twice(self) -> None:
        self.assertTrue(self._submit("NIM-1"))
        self.assertFalse(self._submit("NIM-1"))

    def test_a_finished_task_frees_its_slot(self) -> None:
        self._submit("NIM-1")
        self._submit("NIM-2")

        with patch.object(executor, "_run_agent", return_value="done"), \
                patch.object(runs_db, "record_run"):
            executor._run_task("NIM-1", "/skill NIM-1", "sid-1", "skill")

        self.assertEqual(executor._inflight, {"NIM-2"})
        self.assertTrue(self._submit("NIM-3"))

    def test_a_nonsense_cap_still_leaves_one_slot(self) -> None:
        executor.context.settings = Settings(max_parallel_agents=0)
        self.assertEqual(executor._max_parallel_agents(), 1)
        self.assertTrue(self._submit("NIM-1"))
        self.assertFalse(self._submit("NIM-2"))


class _Exploding:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("bad credentials")


if __name__ == "__main__":
    unittest.main()
