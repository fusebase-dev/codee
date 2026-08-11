import unittest
from unittest.mock import patch

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


class JiraTasksProviderTest(unittest.TestCase):
    def test_build_jql_uses_requested_statuses(self) -> None:
        provider = JiraTasksProvider.__new__(JiraTasksProvider)
        provider._project = "CORE"
        provider._assignee_email = "agent@example.com"

        jql = provider._build_jql(["Custom Ready", 'Needs "review"'])

        self.assertIn('status in ("Custom Ready", "Needs \\"review\\"")', jql)
        self.assertNotIn("[AI]", jql)


def main():
    print("OK")


if __name__ == "__main__":
    unittest.main()
