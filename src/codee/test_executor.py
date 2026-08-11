import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codee_agent_github_copilot.provider import GitHubCopilotAgent
from codee_main_context.context import (
    CodingAgent, Settings, TasksProvider, save_settings)
from codee_tasks_azure_devops.provider import AzureDevOpsTasksProvider
from codee_tasks_jira.provider import JiraTasksProvider

from codee import executor
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
        executor.tasks_provider = executor._build_tasks_provider(Settings())

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
        with patch.dict(executor._TASKS_PROVIDERS,
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

    def test_failed_run_is_recorded_with_the_error(self) -> None:
        with patch.object(executor, "_run_agent", side_effect=RuntimeError("over limit")):
            executor._run_task("NIM-2", "/story-developer NIM-2",
                               "sid-2", "story-developer")

        run, = self._runs()
        self.assertEqual(run["status"], "failed")
        self.assertIn("over limit", run["error"])

    def test_counts_include_issue_runs(self) -> None:
        with patch.object(executor, "_run_agent", return_value="done"):
            executor._run_task("NIM-3", "/story-developer NIM-3",
                               "sid-3", "story-developer")

        counts = runs_db.counts(executor.context)
        self.assertEqual(counts, {"total": 1, "last_24h": 1})


class _Exploding:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("bad credentials")


if __name__ == "__main__":
    unittest.main()
