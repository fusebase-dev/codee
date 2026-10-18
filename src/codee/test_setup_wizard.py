import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from codee_main_context.context import CodingAgent, Settings, TasksProvider

from codee import setup_wizard


class OutputStyleTest(unittest.TestCase):
    def test_command_and_guides_are_highlighted_only_when_styling_is_enabled(self) -> None:
        urls = [
            "https://fusebase-dev.github.io/codee/configuration",
            "https://fusebase-dev.github.io/codee/tutorials/updating-skills-through-git",
        ]
        for interactive, no_color, styled in ((True, "", True),
                                              (False, "", False),
                                              (True, "1", False)):
            with self.subTest(interactive=interactive, no_color=no_color):
                output = io.StringIO()
                with redirect_stdout(output), \
                        patch("sys.stdout.isatty", return_value=interactive), \
                        patch.dict("os.environ", {"NO_COLOR": no_color}), \
                        patch.object(setup_wizard, "unzip_missing",
                                     return_value=False), \
                        patch.object(setup_wizard, "_width", return_value=40):
                    setup_wizard.describe_manual_setup(8501)
                text = output.getvalue()
                self.assertIn("http://localhost:8501/settings", text)
                self.assertNotIn("`uv run codee-start`", text)
                if styled:
                    self.assertIn(
                        "    \033[1;32muv run codee-start\033[0m\n", text)
                    for url in urls:
                        self.assertIn(f"  \033[1;36;4m{url}\033[0m\n", text)
                else:
                    self.assertIn("    uv run codee-start\n", text)
                    self.assertNotIn("\033[", text)
                    for url in urls:
                        self.assertIn(f"  {url}\n", text)


class DescribeGuidesTest(unittest.TestCase):
    def test_shows_common_guides_and_only_the_selected_provider_tutorial(self) -> None:
        docs_url = "https://fusebase-dev.github.io/codee"
        provider_guides = {
            TasksProvider.JIRA: f"{docs_url}/tutorials/working-on-tasks-in-jira",
            TasksProvider.AZURE_DEVOPS:
                f"{docs_url}/tutorials/working-on-tasks-in-azure-devops",
        }
        for provider in (None, *TasksProvider):
            with self.subTest(provider=provider):
                output = io.StringIO()
                with redirect_stdout(output), \
                        patch.object(setup_wizard, "_width", return_value=40):
                    setup_wizard.describe_guides(provider)
                text = output.getvalue()
                self.assertIn(f"{docs_url}/configuration", text)
                self.assertIn(
                    f"{docs_url}/tutorials/updating-skills-through-git", text)
                for source, url in provider_guides.items():
                    if source == provider:
                        self.assertIn(url, text)
                    else:
                        self.assertNotIn(url, text)


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

        def start_process(*args, **kwargs):
            text = output.getvalue()
            self.assertIn("https://fusebase-dev.github.io/codee/configuration", text)
            self.assertIn(
                "https://fusebase-dev.github.io/codee/tutorials/updating-skills-through-git",
                text)
            self.assertIn(
                "https://fusebase-dev.github.io/codee/tutorials/working-on-tasks-in-azure-devops",
                text)
            self.assertNotIn("working-on-tasks-in-jira", text)
            self.assertIn("    \033[1;32muv run codee-start\033[0m\n", text)
            self.assertIn(
                "\033[1;36;4mhttps://fusebase-dev.github.io/codee/tutorials/"
                "working-on-tasks-in-azure-devops\033[0m", text)
            return process

        output = io.StringIO()
        with redirect_stdout(output), \
                patch.object(setup_wizard, "_color_enabled", return_value=True), \
                patch.object(setup_wizard, "unzip_missing", return_value=False), \
                patch("builtins.input", return_value=""), \
                patch.object(setup_wizard.subprocess, "Popen",
                             side_effect=start_process) as popen, \
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
                patch.object(setup_wizard, "unzip_missing", return_value=False), \
                patch.object(setup_wizard.subprocess, "Popen",
                             return_value=process), \
                patch.object(setup_wizard, "wait_for_admin_ui",
                             return_value=False), \
                patch.object(setup_wizard.webbrowser, "open") as browser:
            self.assertEqual(setup_wizard.hand_off_to_admin_ui(
                service, TasksProvider.AZURE_DEVOPS,
                CodingAgent.CLAUDE_CODE, 8501), 1)
        browser.assert_not_called()

    def test_stops_before_starting_the_ui_when_unzip_is_missing(self) -> None:
        service = _service()
        output = io.StringIO()
        with redirect_stdout(output), \
                patch("builtins.input", return_value=""), \
                patch.object(setup_wizard, "unzip_missing", return_value=True), \
                patch.object(setup_wizard.subprocess, "Popen") as popen, \
                patch.object(setup_wizard.webbrowser, "open") as browser:
            self.assertEqual(setup_wizard.hand_off_to_admin_ui(
                service, TasksProvider.AZURE_DEVOPS,
                CodingAgent.CLAUDE_CODE, 8501), 1)
        # The choices are still saved, so the Settings page picks up from them.
        service.save_settings.assert_called_once()
        popen.assert_not_called()
        browser.assert_not_called()
        text = output.getvalue()
        self.assertIn("unzip is not installed", text)
        self.assertIn("sudo apt update && sudo apt install unzip", text)
        self.assertIn("uv run codee-start", text)


class DescribeMissingUnzipTest(unittest.TestCase):
    def test_repeats_the_warning_next_to_the_start_command(self) -> None:
        for missing in (True, False):
            with self.subTest(missing=missing):
                output = io.StringIO()
                with redirect_stdout(output), \
                        patch.object(setup_wizard, "unzip_missing",
                                     return_value=missing):
                    setup_wizard.describe_manual_setup(8501)
                text = output.getvalue()
                self.assertEqual("unzip is not installed" in text, missing)
                self.assertEqual("sudo apt install unzip" in text, missing)
                if missing:
                    # Right above the start command, not lost at the top.
                    self.assertLess(text.index("sudo apt install unzip"),
                                    text.index("uv run codee-start"))


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
    def test_terminal_setup_shows_guides_even_if_not_connected(self) -> None:
        for configured in (True, False):
            with self.subTest(configured=configured):
                output = io.StringIO()
                with redirect_stdout(output), \
                        patch.object(setup_wizard, "_color_enabled",
                                     return_value=True), \
                        patch.object(setup_wizard, "unzip_missing",
                                     return_value=False), \
                        patch("codee.admin_service.AdminService",
                              return_value=_service()), \
                        patch.object(setup_wizard, "choose_coding_agent",
                                     return_value=CodingAgent.CLAUDE_CODE), \
                        patch.object(setup_wizard, "choose_tasks_provider",
                                     return_value=TasksProvider.JIRA), \
                        patch.object(setup_wizard, "setup_in_terminal",
                                     return_value=configured):
                    self.assertEqual(
                        setup_wizard.run(setup_wizard.Path("."), 8501), 0)
                text = output.getvalue()
                self.assertIn("    \033[1;32muv run codee-start\033[0m\n", text)
                self.assertIn(
                    "\033[1;36;4mhttps://fusebase-dev.github.io/codee/tutorials/"
                    "working-on-tasks-in-jira\033[0m", text)
                self.assertIn(
                    "https://fusebase-dev.github.io/codee/configuration", text)
                self.assertIn(
                    "https://fusebase-dev.github.io/codee/tutorials/updating-skills-through-git",
                    text)
                self.assertIn(
                    "https://fusebase-dev.github.io/codee/tutorials/working-on-tasks-in-jira",
                    text)
                self.assertNotIn("working-on-tasks-in-azure-devops", text)

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
