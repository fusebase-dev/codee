import unittest
from unittest.mock import Mock, patch

import requests

from codee_tasks_jira.provider import JiraTasksProvider


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
        self.provider = JiraTasksProvider.__new__(JiraTasksProvider)

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
        self.provider = JiraTasksProvider.__new__(JiraTasksProvider)
        self.provider._base_url = "https://acme.atlassian.net"
        self.provider._user_email = "agent@example.com"
        self.provider._api_token = "token"
        self.provider._assignee_email = "agent@example.com"
        self.provider._project = "CORE"

    def test_build_jql_uses_requested_statuses(self) -> None:
        jql = self.provider._build_jql(["Custom Ready", 'Needs "review"'])

        self.assertIn('status in ("Custom Ready", "Needs \\"review\\"")', jql)
        self.assertNotIn("[AI]", jql)

    def test_no_statuses_drops_the_clause_rather_than_emptying_it(self) -> None:
        # `status in ()` is a JQL syntax error; the connection check asks for
        # every assigned issue whatever its status.
        jql = self.provider._build_jql([])

        self.assertNotIn("status in", jql)
        self.assertIn('assignee = "agent@example.com"', jql)


class JiraVerifyConnectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = JiraTasksProvider.__new__(JiraTasksProvider)
        self.provider._base_url = "https://acme.atlassian.net"
        self.provider._user_email = "agent@example.com"
        self.provider._api_token = "token"
        self.provider._assignee_email = "agent@example.com"
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


def main():
    print("OK")


if __name__ == "__main__":
    unittest.main()
