from typing import Callable

import requests

from codee_main_context.context import Settings, TasksProvider
from codee_tasks_abstract.provider import (
    AbstractTasksProvider, McpServer, Task, TasksProviderError)


# The label that marks a JIRA story as Codee-owned. Children of such a story
# are driven by the story's own agent run, so the executor leaves them alone.
CODEE_STORY_LABEL = "CodeeStory"

# Atlassian's own MCP server, run straight from PyPI through `uvx` so the only
# thing that has to exist on the machine is uv — no install step to keep in sync
# with the credentials below.
MCP_SERVER_PACKAGE = "mcp-atlassian"


def _quote_jql(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _describe_error(exc: requests.RequestException) -> str:
    """Turn a failed request into something a user can act on.

    JIRA answers a bad token, an unknown project or a malformed JQL with the
    same 400/401 status and puts the actual reason in the body, so the status
    alone would tell the settings page nothing.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    try:
        payload = response.json()
    except ValueError:
        detail = response.text.strip()
    else:
        messages = list(payload.get("errorMessages") or [])
        messages += [f"{field}: {message}"
                     for field, message in (payload.get("errors") or {}).items()]
        detail = "; ".join(messages)
    return f"JIRA returned HTTP {response.status_code}" + (
        f": {detail[:300]}" if detail else "")


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

    MCP_SERVER_NAME = "mcp-atlassian"

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

    def mcp_server(self) -> McpServer | None:
        """mcp-atlassian, wired to the same account the executor polls with.

        The base URL matters here in a way it doesn't for ``is_configured``: the
        server is a separate process that gets no chance to ask for it later, so
        an incomplete set of credentials yields no server at all rather than one
        that fails on first use.
        """
        if not (self._base_url and self._user_email and self._api_token):
            return None
        return McpServer(
            name=self.MCP_SERVER_NAME,
            command="uvx",
            args=[MCP_SERVER_PACKAGE],
            env={
                "JIRA_URL": self._base_url,
                "JIRA_USERNAME": self._user_email,
                "JIRA_API_TOKEN": self._api_token,
            },
            requires="It runs through `uvx`, so uv has to be installed "
                     "wherever the coding agent runs.",
        )

    def mcp_check_steps(self, summary: str) -> list[str] | None:
        """Create an issue assigned to the polled account, then close it again.

        Between them the two steps cover everything the executor asks of JIRA:
        it reads issues assigned to this account and moves them along their
        workflow. Which resolution the project calls "closed" varies, so the
        step names both rather than a status that may not exist here.
        """
        if not (self._project and self._assignee_email):
            return None
        return [
            f'Create a new Task in JIRA project {self._project} with the '
            f'summary "{summary}", assigned to {self._assignee_email}.',
            "Move that issue to a Done or Cancelled status — whichever its "
            "workflow offers — so it does not stay open.",
        ]

    def get_tasks(self, statuses: list[str],
                  raise_errors: bool = False) -> list[Task]:
        """Fetch tasks assigned to the target user in the configured states."""
        # Nothing is waiting on an issue, so there is no request worth making.
        # The settings check passes no statuses too, but there the whole point
        # is to reach JIRA, so it queries without a status filter.
        if not statuses and not raise_errors:
            return []
        url = f"{self._base_url}/rest/api/3/search/jql"
        params = {
            "jql": self._build_jql(statuses),
            "fields": "key,summary,status,issuetype,parent,labels,priority",
            "maxResults": 50,
        }

        try:
            resp = requests.get(
                url,
                params=params,
                auth=(self._user_email, self._api_token),
                headers={"Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            if raise_errors:
                raise TasksProviderError(_describe_error(exc)) from exc
            print(f"JIRA API error: {exc}")
            return []

        return [self._to_task(issue) for issue in data.get("issues", [])]

    def _build_jql(self, statuses: list[str]) -> str:
        """JQL for AI-owned issues, highest priority first, then oldest.

        With no statuses the clause is dropped rather than left empty: an
        ``in ()`` is a JQL syntax error, and the only caller that asks for no
        statuses is the connection check, which wants every assigned issue.
        """
        status_clause = ""
        if statuses:
            quoted_statuses = ", ".join(
                _quote_jql(status) for status in statuses)
            status_clause = f'AND status in ({quoted_statuses}) '
        return (
            f'project = {self._project} '
            f'AND assignee = "{self._assignee_email}" '
            f'{status_clause}'
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
