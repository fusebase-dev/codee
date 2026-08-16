"""Read-only Azure DevOps tasks provider, authenticated through Entra ID.

Every call here is a read: a WIQL query for the ids assigned to the connected
account, then a batch fetch of those work items. The WIQL endpoint is a POST,
but it is a query — nothing in this module creates or modifies a work item.
"""
import requests
from codee_main_context.context import CodeeMainContext, Settings, data_dir
from codee_tasks_abstract.provider import (
    AbstractTasksProvider, McpServer, Task, TasksProviderError)

from codee_tasks_azure_devops.oauth import (
    AzureDevOpsAuth, AzureDevOpsAuthError, OAuthConfig)

API_VERSION = "7.1"

# Work item fields the executor and the issue-trigger matcher read.
_FIELDS = [
    "System.Id",
    "System.Title",
    "System.State",
    "System.WorkItemType",
    "System.Tags",
    "System.Parent",
    "Microsoft.VSTS.Common.Priority",
]

# The custom work item types Codee picks up, mapped to the provider-agnostic
# issue types the executor and the issue-trigger matcher speak. Anything else
# assigned to the connected account belongs to a human and is left alone.
_WORK_ITEM_TYPES = {"Codee Task": "Task", "Codee Story": "Story"}

# The work item type that marks a story as Codee-owned. Children of such a
# story are driven by the story's own agent run, so the executor leaves them
# alone — the Azure DevOps counterpart of JIRA's CodeeStory label.
CODEE_STORY_WORK_ITEM_TYPE = "Codee Story"

# Azure DevOps priority is 1-4 with 1 highest; the executor logs this next to
# JIRA-style names, so translate rather than print a bare digit.
_PRIORITY_NAMES = {1: "Highest", 2: "High", 3: "Medium", 4: "Low"}

# Microsoft's own Azure DevOps MCP server, run from npm through `npx` so
# nothing has to be installed alongside it.
MCP_SERVER_PACKAGE = "@azure-devops/mcp"

# Ceiling the WIQL query is capped at, matching the JIRA provider's page size.
_MAX_TASKS = 50

# Hard limit of the workitemsbatch endpoint.
_BATCH_LIMIT = 200

_TIMEOUT = 30


def _quote_wiql(value: str) -> str:
    """Single-quoted WIQL literal; a quote inside the value is doubled."""
    return "'" + value.replace("'", "''") + "'"


def _describe_error(exc: requests.RequestException) -> str:
    """Turn a failed request into something a user can act on.

    Azure DevOps explains a rejected query — an unknown state name, a work item
    type this organization doesn't define, an account with no access — in the
    body's ``message``, so the status code alone would say nothing useful.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    try:
        detail = (response.json().get("message") or "").strip()
    except ValueError:
        detail = response.text.strip()
    return f"Azure DevOps returned HTTP {response.status_code}" + (
        f": {detail[:300]}" if detail else "")


class AzureDevOpsWorkItem(Task):
    """A Task that remembers the raw Azure DevOps work item type.

    ``issue_type`` carries the mapped, provider-agnostic name, and that mapping
    is lossy: a "Codee Story" and a plain "Story" both arrive as "Story".
    Keeping the type Azure DevOps actually reported is what lets
    ``is_parent_codee_story`` tell one parent from the other.
    """

    def __init__(self, work_item_type: str = "", **kwargs):
        self.work_item_type = work_item_type
        super().__init__(**kwargs)

    @property
    def is_parent_codee_story(self) -> bool:
        """In Azure DevOps a Codee-owned story is a "Codee Story" work item."""
        parent = self.parent
        return (isinstance(parent, AzureDevOpsWorkItem)
                and parent.work_item_type == CODEE_STORY_WORK_ITEM_TYPE)


class AzureDevOpsTasksProvider(AbstractTasksProvider):
    """Fetches work items assigned to the connected account as provider-agnostic Tasks."""

    MCP_SERVER_NAME = "ado"

    def __init__(self, settings: Settings, main_context: CodeeMainContext | None = None):
        self._config = OAuthConfig.from_settings(settings)
        # The executor constructs providers with settings alone, so fall back to
        # the default data directory to reach the token store.
        context = main_context or CodeeMainContext(data_dir=data_dir())
        self._auth = AzureDevOpsAuth(self._config, context)

    def is_configured(self) -> bool:
        """Configured means the app details are filled in *and* OAuth completed."""
        return self._config.is_complete() and self._auth.is_connected()

    def describe(self) -> str:
        connection = self._auth.connection() or {}
        account = connection.get("account") or "connected account"
        return (f"Azure DevOps {self._config.organization_url} "
                f"(all projects, assignee {account})")

    def mcp_server(self) -> McpServer | None:
        """Microsoft's Azure DevOps MCP server, addressed at this organization.

        It signs the agent in through the Azure CLI rather than through the app
        registration above. That is deliberate on both sides: the tokens this
        package stores are delegated read-only ones (see the module docstring in
        ``oauth``), and an agent working a task has to write. So the changes it
        makes are attributed to whoever ran ``az login`` where the agent runs,
        and that machine needs the Azure CLI signed in for the server to start.
        """
        organization = self._config.organization
        if not organization:
            return None
        return McpServer(
            name=self.MCP_SERVER_NAME,
            command="npx",
            args=["-y", MCP_SERVER_PACKAGE, organization,
                  "--authentication", "azcli"],
            requires="It runs through `npx`, so Node.js has to be installed "
                     "wherever the coding agent runs, with the Azure CLI "
                     "signed in there (`az login`) — that account is who its "
                     "changes are made as.",
        )

    def mcp_check_steps(self, summary: str) -> list[str] | None:
        """Create a work item of the type the executor polls, then close it again.

        The type is named explicitly because it is custom: an organization that
        never defined "Codee Task" is one the executor can never pick anything
        up from, and this is where that shows up. No project is named — there is
        no project setting, queries span the organization — so the agent picks
        one it can write to.
        """
        account = (self._auth.connection() or {}).get("account")
        organization = self._config.organization
        if not (organization and account):
            return None
        return [
            f'Create a new "Codee Task" work item in the {organization} '
            "organization, in any project you can create work items in, with "
            f'the title "{summary}", assigned to {account}.',
            "Move that work item to a Done, Closed or Removed state — "
            "whichever its board offers — so it does not stay open.",
        ]

    def get_tasks(self, statuses: list[str],
                  raise_errors: bool = False) -> list[Task]:
        """Fetch work items assigned to the connected account in the given states."""
        # Nothing is waiting on a work item, so there is no request worth
        # making. The settings check passes no statuses too, but there the whole
        # point is to reach Azure DevOps, so it queries without a state filter.
        if not statuses and not raise_errors:
            return []
        try:
            token = self._auth.access_token()
        except AzureDevOpsAuthError as exc:
            if raise_errors:
                raise TasksProviderError(
                    f"Azure DevOps sign-in failed: {exc}") from exc
            print(f"Azure DevOps auth error: {exc}")
            return []

        try:
            ids = self._query_work_item_ids(token, statuses)
            if not ids:
                return []
            items = self._fetch_work_items(token, ids)
            parents = self._fetch_parents(token, items)
        except requests.RequestException as exc:
            if raise_errors:
                raise TasksProviderError(_describe_error(exc)) from exc
            print(f"Azure DevOps API error: {exc}")
            return []

        # The batch endpoint doesn't preserve the WIQL ordering, so restore the
        # priority-then-age order the query asked for.
        by_id = {item["id"]: item for item in items}
        return [self._to_task(by_id[item_id], parents)
                for item_id in ids if item_id in by_id]

    def _query_work_item_ids(self, token: str, statuses: list[str]) -> list[int]:
        # Organization-scoped, like the batch fetch below: the endpoint's
        # project segment is optional, and leaving it off is what lets one query
        # span every project the connected account can read.
        response = requests.post(
            f"{self._config.organization_url}/_apis/wit/wiql",
            params={"api-version": API_VERSION, "$top": _MAX_TASKS},
            json={"query": self._build_wiql(statuses)},
            headers=self._headers(token),
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        work_items = response.json().get("workItems") or []
        return [item["id"] for item in work_items][:_BATCH_LIMIT]

    def _build_wiql_status_clause(self, statuses: list[str]) -> str:
        """The state filter, dropped entirely when nothing was requested.

        ``IN ()`` is not valid WIQL, and the only caller that passes no statuses
        is the connection check — it wants every Codee work item assigned to the
        account, whatever state it sits in.
        """
        if not statuses:
            return ""
        quoted = ", ".join(_quote_wiql(status) for status in statuses)
        return f"AND [System.State] IN ({quoted}) "

    def _build_wiql(self, statuses: list[str]) -> str:
        """WIQL for Codee work items owned by the connected account, highest priority first.

        Nothing here names a project, and no ``@project`` macro is used — that
        is what keeps the query valid with no project in the route, so it spans
        the organization. What comes back is still narrow: only the Codee work
        item types, only the states asked for, and only what is assigned to the
        connected account.
        """
        quoted_types = ", ".join(_quote_wiql(item_type)
                                 for item_type in _WORK_ITEM_TYPES)
        return (
            "SELECT [System.Id] FROM WorkItems "
            "WHERE [System.AssignedTo] = @Me "
            f"AND [System.WorkItemType] IN ({quoted_types}) "
            f"{self._build_wiql_status_clause(statuses)}"
            "ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.CreatedDate] ASC"
        )

    def _fetch_work_items(self, token: str, ids: list[int]) -> list[dict]:
        """Batch-fetch the requested work items. Organization-scoped, as the API requires."""
        if not ids:
            return []
        response = requests.post(
            f"{self._config.organization_url}/_apis/wit/workitemsbatch",
            params={"api-version": API_VERSION},
            json={"ids": ids[:_BATCH_LIMIT], "fields": _FIELDS},
            headers=self._headers(token),
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        return response.json().get("value") or []

    def _fetch_parents(self, token: str, items: list[dict]) -> dict[int, dict]:
        """Resolve every referenced parent in one extra call.

        Parents are fetched eagerly, unlike JIRA's deferred labels: here the
        parent is a plain id, so there is no cheaper partial representation to
        start from, and one batch call covers the whole page of tasks.
        """
        parent_ids = {
            parent_id for parent_id in
            (item.get("fields", {}).get("System.Parent") for item in items)
            if parent_id
        }
        if not parent_ids:
            return {}
        parents = self._fetch_work_items(token, sorted(parent_ids))
        return {parent["id"]: parent for parent in parents}

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}",
                "Accept": "application/json"}

    def _to_task(self, item: dict, parents: dict[int, dict]) -> Task:
        fields = item.get("fields", {})
        parent = parents.get(fields.get("System.Parent"))
        work_item_type = fields.get("System.WorkItemType", "")
        return AzureDevOpsWorkItem(
            work_item_type=work_item_type,
            key=str(item["id"]),
            summary=fields.get("System.Title", ""),
            status=fields.get("System.State", ""),
            # Parents aren't type-filtered by the query, so an unmapped type
            # (a plain "User Story" above a Codee Task) passes through as-is.
            issue_type=_WORK_ITEM_TYPES.get(work_item_type, work_item_type),
            priority=_PRIORITY_NAMES.get(
                fields.get("Microsoft.VSTS.Common.Priority"), "Unknown"),
            labels=_split_tags(fields.get("System.Tags")),
            # A parent's own parent is left unresolved: the executor only ever
            # looks one level up, and chasing the chain would cost a call per level.
            parent=self._to_task(parent, {}) if parent else None,
        )


def _split_tags(tags: str | None) -> list[str]:
    """Azure DevOps returns tags as one '; '-joined string."""
    if not tags:
        return []
    return [tag.strip() for tag in tags.split(";") if tag.strip()]
