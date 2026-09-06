import unittest
from unittest.mock import MagicMock, patch

from codee_main_context.context import CodingAgent, Settings, TasksProvider

from codee import setup_wizard


class ChooseCodingAgentTest(unittest.TestCase):
    def test_single_detected_agent_is_used_without_asking(self) -> None:
        with patch.object(setup_wizard, "installed_coding_agents",
                          return_value=[CodingAgent.GITHUB_COPILOT]), \
                patch("builtins.input") as prompt:
            self.assertEqual(setup_wizard.choose_coding_agent(),
                             CodingAgent.GITHUB_COPILOT)
        prompt.assert_not_called()

    def test_two_detected_agents_are_offered_as_a_choice(self) -> None:
        detected = [CodingAgent.CLAUDE_CODE, CodingAgent.GITHUB_COPILOT]
        with patch.object(setup_wizard, "installed_coding_agents",
                          return_value=detected), \
                patch("builtins.input", return_value="2"):
            self.assertEqual(setup_wizard.choose_coding_agent(),
                             CodingAgent.GITHUB_COPILOT)

    def test_no_detected_agent_still_offers_every_agent(self) -> None:
        with patch.object(setup_wizard, "installed_coding_agents",
                          return_value=[]), \
                patch("builtins.input", return_value="") as prompt:
            self.assertEqual(setup_wizard.choose_coding_agent(),
                             CodingAgent.CLAUDE_CODE)
        prompt.assert_called_once()


class PromptTest(unittest.TestCase):
    def test_re_asks_until_a_required_value_is_given(self) -> None:
        with patch("builtins.input", side_effect=["", "  ", "SRE"]):
            self.assertEqual(setup_wizard.prompt("Project key"), "SRE")

    def test_empty_answer_keeps_the_default(self) -> None:
        with patch("builtins.input", return_value=""):
            self.assertEqual(setup_wizard.prompt("Project key", default="SRE"),
                             "SRE")

    def test_secret_is_read_without_echo_and_keeps_the_stored_value(self) -> None:
        with patch.object(setup_wizard, "getpass", return_value="") as secret, \
                patch("builtins.input") as visible:
            self.assertEqual(
                setup_wizard.prompt("API token", default="old", secret=True),
                "old")
        secret.assert_called_once()
        visible.assert_not_called()


class NormalizeBaseUrlTest(unittest.TestCase):
    def test_adds_a_scheme_to_a_bare_host(self) -> None:
        self.assertEqual(setup_wizard.normalize_base_url("acme.atlassian.net"),
                         "https://acme.atlassian.net")

    def test_keeps_the_scheme_and_drops_the_trailing_slash(self) -> None:
        self.assertEqual(
            setup_wizard.normalize_base_url("http://jira.internal:8080/ "),
            "http://jira.internal:8080")


class CollectCredentialsTest(unittest.TestCase):
    def test_asks_for_every_declared_field_and_normalizes_the_url(self) -> None:
        answers = ["acme.atlassian.net", "bot@acme.com", "SRE"]
        with patch("builtins.input", side_effect=answers), \
                patch.object(setup_wizard, "getpass", return_value="token"):
            credentials = setup_wizard.collect_credentials(
                TasksProvider.JIRA, {})
        self.assertEqual(credentials, {
            "base_url": "https://acme.atlassian.net",
            "account_email": "bot@acme.com",
            "api_token": "token",
            "project": "SRE",
        })


def _service(settings: Settings | None = None) -> MagicMock:
    service = MagicMock()
    service.load_settings.return_value = settings or Settings()
    service.setup_tasks_mcp.return_value = (True, "configured")
    return service


class SetupInTerminalTest(unittest.TestCase):
    CREDENTIALS = ["acme.atlassian.net", "bot@acme.com", "SRE", "3"]

    def test_saves_writes_mcp_and_runs_both_checks(self) -> None:
        service = _service()
        service.verify_tasks_connection.return_value = iter([
            {"name": "Tasks", "ok": True, "message": "Pulled 2 task(s)"},
            {"name": "MCP", "ok": True, "message": "worked"},
        ])
        with patch("builtins.input", side_effect=[*self.CREDENTIALS, "y"]), \
                patch.object(setup_wizard, "getpass", return_value="token"):
            self.assertTrue(setup_wizard.setup_in_terminal(
                service, TasksProvider.JIRA, CodingAgent.CLAUDE_CODE))

        saved = service.save_settings.call_args.kwargs
        self.assertEqual(saved["tasks_provider"], "jira")
        self.assertEqual(saved["coding_agent"], "claude_code")
        self.assertEqual(saved["max_parallel_agents"], 3)
        self.assertEqual(saved["credentials"]["project"], "SRE")
        service.setup_tasks_mcp.assert_called_once()

    def test_declining_the_mcp_check_leaves_the_tracker_untouched(self) -> None:
        service = _service()
        checks = iter([
            {"name": "Tasks", "ok": True, "message": "Pulled 2 task(s)"},
            {"name": "MCP", "ok": True, "message": "should not be consumed"},
        ])
        service.verify_tasks_connection.return_value = checks
        with patch("builtins.input", side_effect=[*self.CREDENTIALS, "n"]), \
                patch.object(setup_wizard, "getpass", return_value="token"):
            self.assertTrue(setup_wizard.setup_in_terminal(
                service, TasksProvider.JIRA, CodingAgent.CLAUDE_CODE))
        # The generator is still holding the MCP check, so no agent was run.
        self.assertEqual(next(checks)["name"], "MCP")

    def test_a_refused_connection_skips_the_agent_run_and_reports_failure(self) -> None:
        service = _service()
        checks = iter([
            {"name": "Tasks", "ok": False, "message": "401 Unauthorized"},
            {"name": "MCP", "ok": False, "message": "should not be consumed"},
        ])
        service.verify_tasks_connection.return_value = checks
        with patch("builtins.input", side_effect=self.CREDENTIALS), \
                patch.object(setup_wizard, "getpass", return_value="token"):
            self.assertFalse(setup_wizard.setup_in_terminal(
                service, TasksProvider.JIRA, CodingAgent.CLAUDE_CODE))
        self.assertEqual(next(checks)["name"], "MCP")

    def test_keeps_a_custom_query_filter_the_wizard_never_asks_about(self) -> None:
        settings = Settings(task_filters={"jira": 'labels = "codee"'})
        service = _service(settings)
        service.verify_tasks_connection.return_value = iter([
            {"name": "Tasks", "ok": False, "message": "nope"}])
        with patch("builtins.input", side_effect=self.CREDENTIALS), \
                patch.object(setup_wizard, "getpass", return_value="token"):
            setup_wizard.setup_in_terminal(
                service, TasksProvider.JIRA, CodingAgent.CLAUDE_CODE)
        self.assertEqual(
            service.save_settings.call_args.kwargs["task_filter"],
            'labels = "codee"')


class HandOffToAdminUiTest(unittest.TestCase):
    def test_saves_the_choices_then_starts_the_ui_and_opens_the_browser(self) -> None:
        service = _service()
        process = MagicMock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch("builtins.input", return_value=""), \
                patch.object(setup_wizard.subprocess, "Popen",
                             return_value=process) as popen, \
                patch.object(setup_wizard, "wait_for_admin_ui",
                             return_value=True), \
                patch.object(setup_wizard.webbrowser, "open") as browser:
            self.assertEqual(setup_wizard.hand_off_to_admin_ui(
                service, TasksProvider.AZURE_DEVOPS,
                CodingAgent.GITHUB_COPILOT, 8501), 0)

        saved = service.save_settings.call_args.kwargs
        self.assertEqual(saved["tasks_provider"], "azure_devops")
        self.assertEqual(saved["coding_agent"], "github_copilot")
        # Nothing was asked for and nothing was checked: the browser does both.
        service.verify_tasks_connection.assert_not_called()
        self.assertIn("codee.start_cli", popen.call_args.args[0])
        browser.assert_called_once_with("http://localhost:8501/settings")

    def test_does_not_open_a_browser_when_the_ui_never_came_up(self) -> None:
        service = _service()
        process = MagicMock()
        process.wait.return_value = 1
        process.poll.return_value = 1
        with patch("builtins.input", return_value=""), \
                patch.object(setup_wizard.subprocess, "Popen",
                             return_value=process), \
                patch.object(setup_wizard, "wait_for_admin_ui",
                             return_value=False), \
                patch.object(setup_wizard.webbrowser, "open") as browser:
            self.assertEqual(setup_wizard.hand_off_to_admin_ui(
                service, TasksProvider.AZURE_DEVOPS,
                CodingAgent.CLAUDE_CODE, 8501), 1)
        browser.assert_not_called()


class WaitForAdminUiTest(unittest.TestCase):
    def test_gives_up_as_soon_as_the_ui_process_exits(self) -> None:
        process = MagicMock()
        process.poll.return_value = 1
        with patch.object(setup_wizard.socket, "create_connection") as connect:
            self.assertFalse(setup_wizard.wait_for_admin_ui(8501, process))
        connect.assert_not_called()

    def test_returns_once_the_port_accepts_a_connection(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(setup_wizard.socket, "create_connection"):
            self.assertTrue(setup_wizard.wait_for_admin_ui(8501, process))


class RunTest(unittest.TestCase):
    def test_a_terminal_provider_never_starts_the_admin_ui(self) -> None:
        service = _service()
        service.verify_tasks_connection.return_value = iter([
            {"name": "Tasks", "ok": True, "message": "Pulled 0 task(s)"}])
        with patch("codee.admin_service.AdminService", return_value=service), \
                patch.object(setup_wizard, "choose_coding_agent",
                             return_value=CodingAgent.CLAUDE_CODE), \
                patch.object(setup_wizard, "choose_tasks_provider",
                             return_value=TasksProvider.JIRA), \
                patch("builtins.input",
                      side_effect=["acme.atlassian.net", "bot@acme.com",
                                   "SRE", "3", "n"]), \
                patch.object(setup_wizard, "getpass", return_value="token"), \
                patch.object(setup_wizard.subprocess, "Popen") as popen:
            self.assertEqual(setup_wizard.run(setup_wizard.Path("."), 8501), 0)
        popen.assert_not_called()

    def test_a_browser_provider_is_handed_off(self) -> None:
        with patch("codee.admin_service.AdminService", return_value=_service()), \
                patch.object(setup_wizard, "choose_coding_agent",
                             return_value=CodingAgent.CLAUDE_CODE), \
                patch.object(setup_wizard, "choose_tasks_provider",
                             return_value=TasksProvider.AZURE_DEVOPS), \
                patch.object(setup_wizard, "hand_off_to_admin_ui",
                             return_value=0) as handoff:
            self.assertEqual(setup_wizard.run(setup_wizard.Path("."), 8501), 0)
        handoff.assert_called_once()


if __name__ == "__main__":
    unittest.main()
