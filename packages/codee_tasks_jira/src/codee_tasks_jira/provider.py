from typing import Callable

import requests

from codee_main_context.context import Settings, TasksProvider
from codee_tasks_abstract.provider import AbstractTasksProvider, Task


# The label that marks a JIRA story as Codee-owned. Children of such a story
# are driven by the story's own agent run, so the executor leaves them alone.
CODEE_STORY_LABEL = "CodeeStory"


def _quote_jql(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class JiraTask(Task):
    """A JIRA task whose labels are fetched the first time they're read.

    A parent reference in the search response carries no labels, so resolving
    them eagerly would cost an extra request per tick even when the caller
    never inspects them. Deferring the fetch keeps the common path request-free;
    the result is cached so repeated reads don't re-fetch.
    """

    def __init__(self, labels_loader: Callable[[], list[str]] | None = None, **kwargs):
        self._labels_loader = labels_loader
        self._resolved_labels: list[str] | None = None
        super().__init__(**kwargs)

    @property
    def labels(self) -> list[str]:
        if self._resolved_labels is None:
            if self._raw_labels is not None:
                self._resolved_labels = self._raw_labels
            elif self._labels_loader is not None:
                self._resolved_labels = self._labels_loader()
            else:
                self._resolved_labels = []
        return self._resolved_labels

    @labels.setter
    def labels(self, value: list[str] | None) -> None:
        # Set by Task.__init__; None means "not present in the response".
        self._raw_labels = value

    @property
    def is_parent_codee_story(self) -> bool:
        """In JIRA a Codee-owned story is marked with the CodeeStory label."""
        return (self.parent is not None
                and CODEE_STORY_LABEL in self.parent.labels)


class JiraTasksProvider(AbstractTasksProvider):
    """Fetches AI-owned issues from JIRA and maps them to provider-agnostic Tasks."""

    def __init__(self, settings: Settings):
        creds = settings.credentials.get(TasksProvider.JIRA.value, {})
        self._base_url = creds.get("base_url")
        self._user_email = creds.get("account_email")
        self._api_token = creds.get("api_token")
        # We poll for tasks assigned to the same account we authenticate as.
        self._assignee_email = self._user_email
        self._project = creds.get("project")

    def is_configured(self) -> bool:
        return bool(self._user_email and self._api_token)

    def describe(self) -> str:
        return (f"JIRA {self._base_url} "
                f"(project {self._project}, assignee {self._assignee_email})")

    def get_tasks(self, statuses: list[str]) -> list[Task]:
        """Fetch tasks assigned to the target user in the configured states."""
        if not statuses:
            return []
        url = f"{self._base_url}/rest/api/3/search/jql"
        params = {
            "jql": self._build_jql(statuses),
            "fields": "key,summary,status,issuetype,parent,labels,priority",
            "maxResults": 50,
        }

        print("PARAMS", params)

        try:
            resp = requests.get(
                url,
                params=params,
                auth=(self._user_email, self._api_token),
                headers={"Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()

            print("RESP", resp)

            data = resp.json()
        except requests.RequestException as exc:
            print(f"JIRA API error: {exc}")
            return []

        return [self._to_task(issue) for issue in data.get("issues", [])]

    def _build_jql(self, statuses: list[str]) -> str:
        """JQL for AI-owned issues, highest priority first, then oldest."""
        quoted_statuses = ", ".join(
            _quote_jql(status) for status in statuses
        )
        return (
            f'project = {self._project} '
            f'AND assignee = "{self._assignee_email}" '
            f'AND status in ({quoted_statuses}) '
            f'ORDER BY priority DESC, created ASC'
        )

    def _to_task(self, issue: dict) -> Task:
        fields = issue.get("fields", {})
        parent_issue = fields.get("parent")
        return JiraTask(
            key=issue["key"],
            summary=fields.get("summary", ""),
            status=fields.get("status", {}).get("name", ""),
            issue_type=fields.get("issuetype", {}).get("name", ""),
            priority=(fields.get("priority") or {}).get("name", "Unknown"),
            labels=fields.get("labels") or [],
            parent=self._to_parent_task(
                parent_issue) if parent_issue else None,
        )

    def _to_parent_task(self, parent_issue: dict) -> Task:
        # Labels aren't included for a parent, so defer the fetch until read.
        key = parent_issue["key"]
        fields = parent_issue.get("fields", {})
        return JiraTask(
            key=key,
            summary=fields.get("summary", ""),
            status=fields.get("status", {}).get("name", ""),
            issue_type=fields.get("issuetype", {}).get("name", ""),
            priority=(fields.get("priority") or {}).get("name", "Unknown"),
            labels=fields.get("labels"),
            labels_loader=lambda: self._fetch_issue_labels(key),
        )

    def _fetch_issue_labels(self, issue_key: str) -> list[str]:
        """Fetch labels for a single JIRA issue."""
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}"
        try:
            resp = requests.get(
                url,
                params={"fields": "labels"},
                auth=(self._user_email, self._api_token),
                headers={"Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json().get("fields", {}).get("labels", [])
        except requests.RequestException as exc:
            print(f"[cron_jira] Failed to fetch labels for {issue_key}: {exc}")
            return []
