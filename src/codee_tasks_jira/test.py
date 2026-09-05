import unittest
from unittest.mock import Mock, patch

import requests

from codee_main_context.context import Settings, work_item_types
from codee_tasks_abstract.provider import TasksProviderError
from codee_tasks_jira.provider import JiraTasksProvider


def _configure(provider: JiraTasksProvider,
               mapping: dict[str, str] | None = None) -> JiraTasksProvider:
    """Give a hand-built provider the work item mapping its __init__ would."""
    mapping = mapping or work_item_types(Settings())
    provider._work_item_types = mapping
    provider._codee_types = {issue_type.casefold(): codee_type
                             for codee_type, issue_type in mapping.items()}
    return provider


def _issue(parent_labels: list[str] | None = None) -> dict:
    issue = {"key": "CORE-1", "fields": {
        "summary": "A task",
        "status": {"name": "Ready"},
        "issuetype": {"name": "Task"},
        "priority": {"name": "High"},
        "labels": [],
    }}
    if parent_labels is not None:
        issue["fields"]["parent"] = {
            "key": "CORE-9",
            "fields": {"summary": "A story",
                       "status": {"name": "In Progress"},
                       "issuetype": {"name": "Story"},
                       "labels": parent_labels},
        }
    return issue


class JiraParentStoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _configure(
            JiraTasksProvider.__new__(JiraTasksProvider))

    def test_parent_with_the_label_is_a_codee_story(self) -> None:
        task = self.provider._to_task(_issue(["CodeeStory", "backend"]))

        self.assertTrue(task.is_parent_codee_story)

    def test_parent_without_the_label_is_not(self) -> None:
        task = self.provider._to_task(_issue(["backend"]))

        self.assertFalse(task.is_parent_codee_story)

    def test_a_task_with_no_parent_is_not(self) -> None:
        task = self.provider._to_task(_issue())

        self.assertFalse(task.is_parent_codee_story)

    def test_labels_missing_from_the_parent_are_fetched_once(self) -> None:
        parent = _issue([])
        del parent["fields"]["parent"]["fields"]["labels"]
        task = self.provider._to_task(parent)

        with patch.object(self.provider, "_fetch_issue_labels",
                          return_value=["CodeeStory"]) as fetch:
            self.assertTrue(task.is_parent_codee_story)
            self.assertTrue(task.is_parent_codee_story)

        fetch.assert_called_once_with("CORE-9")


def _error(status: int, payload: dict | None = None) -> requests.HTTPError:
    response = Mock(status_code=status, text="")
    response.json.side_effect = (
        ValueError("no body") if payload is None else None)
    response.json.return_value = payload
    return requests.HTTPError(f"{status} Client Error", response=response)


class JiraTasksProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _configure(
            JiraTasksProvider.__new__(JiraTasksProvider))
        self.provider._base_url = "https://acme.atlassian.net"
        self.provider._user_email = "agent@example.com"
        self.provider._api_token = "token"
        self.provider._project = "CORE"

    def test_build_jql_uses_requested_statuses(self) -> None:
        jql = self.provider._build_jql(["Custom Ready", 'Needs "review"'])

        self.assertIn('status in ("Custom Ready", "Needs \\"review\\"")', jql)
        self.assertNotIn("[AI]", jql)

    def test_no_statuses_drops_the_clause_rather_than_emptying_it(self) -> None:
        # `status in ()` is a JQL syntax error; the connection check asks for
        # every issue in the project whatever its status.
        jql = self.provider._build_jql([])

        self.assertNotIn("status in", jql)
        self.assertIn("project = CORE", jql)

    def test_the_query_does_not_filter_on_an_assignee(self) -> None:
        # Type and status are the handover; who the issue is assigned to is
        # nobody's business but the humans working alongside it.
        jql = self.provider._build_jql(["Ready"])

        self.assertNotIn("assignee", jql)

    def test_only_the_mapped_issue_types_are_asked_for(self) -> None:
        # An issue of a type Codee was never pointed at would otherwise eat one
        # of the 50 rows a page returns.
        jql = self.provider._build_jql(["Ready"])

        self.assertIn('issuetype in ("Story", "Task")', jql)

    def test_a_remapped_work_item_changes_the_type_clause(self) -> None:
        provider = _configure(JiraTasksProvider.__new__(JiraTasksProvider),
                              {"story": "Epic", "task": "Sub-task",
                               "bug": "Bug"})
        provider._project = "CORE"

        jql = provider._build_jql(["Ready"])

        self.assertIn('issuetype in ("Epic", "Sub-task", "Bug")', jql)


class JiraVerifyConnectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _configure(
            JiraTasksProvider.__new__(JiraTasksProvider))
        self.provider._base_url = "https://acme.atlassian.net"
        self.provider._user_email = "agent@example.com"
        self.provider._api_token = "token"
        self.provider._project = "CORE"

    def _verify(self, response) -> tuple[bool, str]:
        with patch("codee_tasks_jira.provider.requests.get",
                   return_value=response):
            return self.provider.verify_connection(["Ready"])

    def test_a_successful_pull_names_the_tasks_it_found(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"issues": [_issue()]}

        verified, message = self._verify(response)

        self.assertTrue(verified)
        self.assertIn("CORE-1 A task", message)
        # The queried statuses are noise here: one per issue-triggered skill.
        self.assertNotIn("status", message)

    def test_an_empty_result_still_counts_as_connected(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"issues": []}

        verified, message = self._verify(response)

        self.assertTrue(verified)
        self.assertIn("No tasks", message)

    def test_a_rejected_query_reports_what_jira_said(self) -> None:
        # The status alone can't tell a bad token from an unknown project.
        response = Mock(status_code=400)
        response.raise_for_status.side_effect = _error(
            400, {"errorMessages": ["The value 'NOPE' does not exist "
                                    "for the field 'project'."]})

        verified, message = self._verify(response)

        self.assertFalse(verified)
        self.assertIn("HTTP 400", message)
        self.assertIn("does not exist for the field 'project'", message)

    def test_an_unreachable_host_reports_the_transport_error(self) -> None:
        with patch("codee_tasks_jira.provider.requests.get",
                   side_effect=requests.ConnectionError("name resolution failed")):
            verified, message = self.provider.verify_connection(["Ready"])

        self.assertFalse(verified)
        self.assertIn("name resolution failed", message)

    def test_the_polling_path_still_swallows_the_failure(self) -> None:
        # An executor tick must survive what the settings page reports loudly.
        with patch("codee_tasks_jira.provider.requests.get",
                   side_effect=requests.ConnectionError("boom")):
            self.assertEqual(self.provider.get_tasks(["Ready"]), [])


class JiraWorkItemTypesTest(unittest.TestCase):
    CREDENTIALS = {
        "base_url": "https://acme.atlassian.net",
        "account_email": "agent@example.com",
        "api_token": "token",
        "project": "CORE",
    }

    def _provider(self, **overrides) -> JiraTasksProvider:
        return JiraTasksProvider(
            Settings(credentials={"jira": {**self.CREDENTIALS, **overrides}}))

    def test_it_lists_the_types_the_configured_project_defines(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"issueTypes": [
            {"name": "Task"}, {"name": "story"}, {"name": "Bug"}]}

        with patch("codee_tasks_jira.provider.requests.get",
                   return_value=response) as get:
            types = self._provider().list_work_item_types()

        # Sorted case-insensitively so the dropdown reads alphabetically.
        self.assertEqual(types, ["Bug", "story", "Task"])
        self.assertIn("/rest/api/3/project/CORE", get.call_args.args[0])

    def test_without_a_project_it_falls_back_to_every_type_on_the_site(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = [{"name": "Task"}, {"name": "Epic"}]

        with patch("codee_tasks_jira.provider.requests.get",
                   return_value=response) as get:
            types = self._provider(project="").list_work_item_types()

        self.assertEqual(types, ["Epic", "Task"])
        self.assertTrue(get.call_args.args[0].endswith("/rest/api/3/issuetype"))

    def test_a_rejected_request_reports_what_jira_said(self) -> None:
        response = Mock(status_code=404)
        response.raise_for_status.side_effect = _error(
            404, {"errorMessages": ["No project could be found with key 'CORE'."]})

        with patch("codee_tasks_jira.provider.requests.get",
                   return_value=response):
            with self.assertRaises(TasksProviderError) as raised:
                self._provider().list_work_item_types()

        self.assertIn("No project could be found", str(raised.exception))

    def test_the_scope_names_the_project_the_query_is_bound_to(self) -> None:
        # "Why is my type missing" is almost always "it lives in another
        # project", so the count is useless without the project beside it.
        self.assertEqual(self._provider().work_item_types_scope(),
                         "project CORE")

    def test_without_a_project_the_scope_says_so(self) -> None:
        self.assertEqual(self._provider(project="").work_item_types_scope(),
                         "every project on the site")

    def test_it_refuses_before_the_credentials_can_reach_jira(self) -> None:
        with patch("codee_tasks_jira.provider.requests.get") as get:
            with self.assertRaises(TasksProviderError):
                self._provider(api_token="").list_work_item_types()

        get.assert_not_called()


class JiraIssueTypeMappingTest(unittest.TestCase):
    def test_an_issue_arrives_under_the_codee_work_item_it_maps_to(self) -> None:
        # The executor and the skills speak Codee's names, not JIRA's.
        provider = _configure(JiraTasksProvider.__new__(JiraTasksProvider))

        task = provider._to_task(_issue())

        self.assertEqual(task.issue_type, "task")

    def test_a_custom_mapping_is_what_decides_the_name(self) -> None:
        provider = _configure(JiraTasksProvider.__new__(JiraTasksProvider),
                              {"story": "Story", "task": "Sub-task",
                               "bug": "Task"})

        task = provider._to_task(_issue())

        self.assertEqual(task.issue_type, "bug")

    def test_an_unmapped_parent_type_passes_through_unchanged(self) -> None:
        # Parents aren't type-filtered by the query, so one Codee was never
        # pointed at still has to be describable.
        provider = _configure(JiraTasksProvider.__new__(JiraTasksProvider),
                              {"story": "Epic", "task": "Task"})

        task = provider._to_task(_issue(["backend"]))

        self.assertEqual(task.parent.issue_type, "Story")


class JiraDebugLoggingTest(unittest.TestCase):
    """What `codee-start --debug` prints when a poll comes back empty."""

    def setUp(self) -> None:
        self.provider = _configure(
            JiraTasksProvider.__new__(JiraTasksProvider))
        self.provider._base_url = "https://acme.atlassian.net"
        self.provider._user_email = "agent@example.com"
        self.provider._api_token = "token"
        self.provider._project = "CORE"

    def test_the_query_and_its_result_are_logged(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"issues": [_issue()]}

        with self.assertLogs("codee_tasks_jira.provider", "DEBUG") as logs:
            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response):
                self.provider.get_tasks(["Ready"])

        output = "\n".join(logs.output)
        self.assertIn('issuetype in ("Story", "Task")', output)
        self.assertIn('status in ("Ready")', output)
        # The mapped name, so a status/type that matched no skill is visible.
        self.assertIn("CORE-1 [Ready/task]", output)

    def test_an_empty_result_still_logs_the_query_that_produced_it(self) -> None:
        # The whole point: nothing came back, and the query says why.
        response = Mock(status_code=200)
        response.json.return_value = {"issues": []}

        with self.assertLogs("codee_tasks_jira.provider", "DEBUG") as logs:
            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response):
                self.provider.get_tasks(["Ready"])

        output = "\n".join(logs.output)
        self.assertIn("JQL: project = CORE", output)
        self.assertIn("JQL matched 0 issue(s)", output)

    def test_a_failed_poll_logs_what_jira_said(self) -> None:
        response = Mock(status_code=400)
        response.raise_for_status.side_effect = _error(
            400, {"errorMessages": ["The value 'NOPE' does not exist."]})

        with self.assertLogs("codee_tasks_jira.provider", "ERROR") as logs:
            with patch("codee_tasks_jira.provider.requests.get",
                       return_value=response):
                self.assertEqual(self.provider.get_tasks(["Ready"]), [])

        self.assertIn("does not exist", "\n".join(logs.output))


class JiraMcpServerTest(unittest.TestCase):
    CREDENTIALS = {
        "base_url": "https://acme.atlassian.net",
        "account_email": "agent@example.com",
        "api_token": "token",
        "project": "CORE",
    }

    def _server(self, **overrides):
        credentials = {**self.CREDENTIALS, **overrides}
        settings = Settings(credentials={"jira": credentials})
        return JiraTasksProvider(settings).mcp_server()

    def test_it_describes_mcp_atlassian_with_the_stored_credentials(self) -> None:
        server = self._server()

        self.assertEqual(server.command, "uvx")
        self.assertEqual(server.args, ["mcp-atlassian"])
        self.assertEqual(server.env, {
            "JIRA_URL": "https://acme.atlassian.net",
            "JIRA_USERNAME": "agent@example.com",
            "JIRA_API_TOKEN": "token",
        })

    def test_a_missing_base_url_yields_no_server(self) -> None:
        # The server is a separate process: it can't be asked for the URL later.
        self.assertIsNone(self._server(base_url=""))

    def test_a_missing_token_yields_no_server(self) -> None:
        self.assertIsNone(self._server(api_token=""))

    def test_the_project_key_is_not_required(self) -> None:
        # The agent searches and updates issues, rather than polling one project.
        self.assertIsNotNone(self._server(project=""))

    def test_the_check_steps_create_an_issue_and_close_it_again(self) -> None:
        settings = Settings(credentials={"jira": self.CREDENTIALS})

        steps = JiraTasksProvider(settings).mcp_check_steps("Codee check 1234")

        self.assertEqual(len(steps), 2)
        self.assertIn('project CORE with the summary "Codee check 1234"',
                      steps[0])
        # Nothing depends on the assignee, so the check does not set one.
        self.assertNotIn("assigned to", steps[0])
        self.assertIn("Done or Cancelled", steps[1])

    def test_no_check_steps_without_a_project_to_create_the_issue_in(self) -> None:
        settings = Settings(
            credentials={"jira": {**self.CREDENTIALS, "project": ""}})

        self.assertIsNone(JiraTasksProvider(settings).mcp_check_steps("x"))


def main():
    print("OK")


if __name__ == "__main__":
    unittest.main()
