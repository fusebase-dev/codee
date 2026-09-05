from typing import Callable

import requests

from codee_main_context.context import (
    Settings, TasksProvider, task_filter, work_item_types)
from codee_main_context.logging import get_logger
from codee_tasks_abstract.provider import (
    AbstractTasksProvider, McpServer, Task, TasksProviderError)


log = get_logger(__name__)

# The label that marks a JIRA story as Codee-owned. Children of such a story
# are driven by the story's own agent run, so the executor leaves them alone.
CODEE_STORY_LABEL = "CodeeStory"

# Atlassian's own MCP server, run straight from PyPI through `uvx` so the only
# thing that has to exist on the machine is uv — no install step to keep in sync
# with the credentials below.
MCP_SERVER_PACKAGE = "mcp-atlassian"


def _describe_task(task: Task) -> str:
    """One task as a log fragment: what it is and what Codee decided it is."""
    return f"{task.key} [{task.status}/{task.issue_type}]"


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
        # Only ever the HTTP Basic username: JIRA Cloud signs a request as the
        # account the API token belongs to, and rejects the token on its own.
        # It is not an assignee filter — see ``_build_jql``.
        self._user_email = creds.get("account_email")
        self._api_token = creds.get("api_token")
        self._project = creds.get("project")
        # Which JIRA issue types stand for which Codee work item. Both
        # directions are needed: the names go into the JQL type filter, and an
        # issue that comes back is reported to the executor under the Codee
        # name the user mapped it to.
        self._work_item_types = work_item_types(settings, TasksProvider.JIRA)
        self._codee_types = {issue_type.casefold(): codee_type
                             for codee_type, issue_type
                             in self._work_item_types.items()}
        # An extra JQL condition the user narrowed the poll with, empty unless
        # one was configured. Kept as written: it is theirs to get right, and
        # JIRA says what is wrong with it far better than a parser here could.
        self._task_filter = task_filter(settings, TasksProvider.JIRA)

    def is_configured(self) -> bool:
        return bool(self._user_email and self._api_token)

    def describe(self) -> str:
        types = ", ".join(self._work_item_types.values()) or "no issue types"
        # The filter only gets a mention when there is one: it is off for most
        # installations, and "filter none" reads like a setting gone wrong.
        extra = f", filter {self._task_filter}" if self._task_filter else ""
        return (f"JIRA {self._base_url} "
                f"(project {self._project}, types {types}{extra})")

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
        """Create an issue in the polled project, then close it again.

        Between them the two steps cover everything the executor asks of JIRA:
        it reads issues in this project and moves them along their workflow.
        Nothing is said about the assignee, because nothing depends on it —
        the poll below does not filter on one. Which resolution the project
        calls "closed" varies, so the step names both rather than a status that
        may not exist here.
        """
        if not self._project:
            return None
        return [
            f'Create a new Task in JIRA project {self._project} with the '
            f'summary "{summary}".',
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
        # The query verbatim, because "Codee isn't picking up my issue" is
        # answered by reading it: the project, the issue types the work item
        # mapping resolved to, the statuses the skills asked for and the custom
        # filter from Settings are all in this one string.
        log.debug("JQL: %s", params["jql"])

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
            log.error("JIRA API error: %s", _describe_error(exc))
            return []

        tasks = [self._to_task(issue) for issue in data.get("issues", [])]
        log.debug("JQL matched %d issue(s)%s", len(tasks),
                  ": " + ", ".join(_describe_task(task) for task in tasks)
                  if tasks else "")
        return tasks

    def _build_jql(self, statuses: list[str]) -> str:
        """JQL for Codee-owned issues, highest priority first, then oldest.

        There is no assignee clause: what hands an issue to Codee is its type
        and its status, not who it is assigned to. So an issue a human still
        owns is picked up the moment it reaches a status one of the skills
        triggers on, which is what makes those statuses the handover — they
        have to be ones only Codee's workflow uses.

        With no statuses the clause is dropped rather than left empty: an
        ``in ()`` is a JQL syntax error, and the only caller that asks for no
        statuses is the connection check, which wants every issue it can see.
        """
        status_clause = ""
        if statuses:
            quoted_statuses = ", ".join(
                _quote_jql(status) for status in statuses)
            status_clause = f'AND status in ({quoted_statuses}) '
        return (
            f'project = {self._project} '
            f'{self._build_type_clause()}'
            f'{status_clause}'
            f'{self._build_filter_clause()}'
            f'ORDER BY priority DESC, created ASC'
        )

    def _build_filter_clause(self) -> str:
        """The custom JQL from Settings, ANDed onto the clauses above.

        Bracketed, because a filter is a whole condition rather than a single
        term: an unparenthesized ``a = 1 OR b = 2`` would bind its OR across
        the project and type clauses and hand back issues Codee does not own.
        """
        if not self._task_filter:
            return ""
        return f'AND ({self._task_filter}) '

    def _build_type_clause(self) -> str:
        """The issue-type filter, from the work items configured in Settings.

        Narrowing the query rather than filtering the response is what keeps an
        issue of a type Codee was never pointed at from consuming one of the 50
        rows a page returns. Dropped when nothing is mapped, for the same reason
        the status clause is: ``in ()`` is a JQL syntax error.
        """
        issue_types = list(self._work_item_types.values())
        if not issue_types:
            return ""
        quoted = ", ".join(_quote_jql(issue_type) for issue_type in issue_types)
        return f'AND issuetype in ({quoted}) '

    def _codee_issue_type(self, issue_type: str) -> str:
        """The Codee work item this JIRA issue type was mapped to.

        An unmapped type keeps the name JIRA gave it. The query above only
        returns mapped types, but a parent is not type-filtered, so this is
        what lets a story above a Codee task pass through recognizably.
        """
        return self._codee_types.get(issue_type.casefold(), issue_type)

    def work_item_types_scope(self) -> str:
        """Which project the types come from — the one the JQL is bound to.

        Worth saying out loud: a team-managed project defines its own handful
        of types while the site next door has dozens, and "why is my type
        missing" is almost always "that type lives in another project".
        """
        if self._project:
            return f"project {self._project}"
        return "every project on the site"

    def list_work_item_types(self) -> list[str]:
        """The issue types the configured project offers, else the whole site's.

        The project is the useful answer — mapping a Codee work item to a type
        the polled project doesn't define would give the JQL above nothing to
        match. Without a project key there is still something worth listing, so
        it falls back to every type defined on the site.
        """
        if not (self._base_url and self._user_email and self._api_token):
            raise TasksProviderError(
                "Fill in base URL, API Token Owner Email and API token first.")
        if self._project:
            url = f"{self._base_url}/rest/api/3/project/{self._project}"
        else:
            url = f"{self._base_url}/rest/api/3/issuetype"
        try:
            resp = requests.get(
                url,
                auth=(self._user_email, self._api_token),
                headers={"Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            raise TasksProviderError(_describe_error(exc)) from exc
        except ValueError as exc:
            raise TasksProviderError(
                f"JIRA returned something that is not JSON: {exc}") from exc
        # The project endpoint nests them, the site-wide one returns them flat.
        entries = payload.get("issueTypes", []) if isinstance(
            payload, dict) else payload
        names = {str(entry.get("name", "")).strip()
                 for entry in entries or [] if isinstance(entry, dict)}
        resolved = sorted((name for name in names if name), key=str.casefold)
        log.debug("%s offers %d issue type(s): %s", url, len(resolved),
                  ", ".join(resolved))
        return resolved

    def _to_task(self, issue: dict) -> Task:
        fields = issue.get("fields", {})
        parent_issue = fields.get("parent")
        return JiraTask(
            key=issue["key"],
            summary=fields.get("summary", ""),
            status=fields.get("status", {}).get("name", ""),
            issue_type=self._codee_issue_type(
                fields.get("issuetype", {}).get("name", "")),
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
            issue_type=self._codee_issue_type(
                fields.get("issuetype", {}).get("name", "")),
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
            log.error("Failed to fetch labels for %s: %s", issue_key, exc)
            return []
