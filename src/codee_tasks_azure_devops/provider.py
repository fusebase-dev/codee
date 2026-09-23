"""Read-only Azure DevOps tasks provider, authenticated through Entra ID.

Every call here is a read: a WIQL query for the ids of the Codee work items in
the states the skills trigger on, then a batch fetch of those work items. The WIQL endpoint is a POST,
but it is a query — nothing in this module creates or modifies a work item.
"""
from collections.abc import Iterable
from urllib.parse import quote

import requests
from codee_main_context.context import (
    CodeeMainContext, Settings, TASK_ISSUE_TYPE, TasksProvider,
    WorkItemMapping, codee_work_items, data_dir, task_filter,
    work_item_mappings)
from codee_main_context.logging import get_logger
from codee_tasks_abstract.provider import (
    AbstractTasksProvider, McpServer, Task, TasksProviderError,
    merge_work_item_tasks)

from codee_tasks_azure_devops.oauth import (
    AzureDevOpsAuth, AzureDevOpsAuthError, OAuthConfig)

log = get_logger(__name__)

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

# Which Azure DevOps work item types Codee picks up, and what each stands for,
# is configured per installation in Settings — organizations disagree on the
# names ("User Story" in Agile, "Product Backlog Item" in Scrum, anything at
# all in a custom process). A work item of any other type belongs to a human
# and is left alone, whatever state it reaches.

# Azure DevOps priority is 1-4 with 1 highest; the executor logs this next to
# JIRA-style names, so translate rather than print a bare digit.
_PRIORITY_NAMES = {1: "Highest", 2: "High", 3: "Medium", 4: "Low"}

# Microsoft's own Azure DevOps MCP server, run from npm through `npx` so
# nothing has to be installed alongside it. Version 2.8.0 keeps work item
# creation compatible with coding agents that stringify the consolidated
# write tool's fields parameter introduced in 2.9.0.
MCP_SERVER_PACKAGE = "@azure-devops/mcp@2.8.0"

# Ceiling the WIQL query is capped at, matching the JIRA provider's page size.
_MAX_TASKS = 50

# How many projects the settings page's type list is gathered from. Work item
# types are defined per process, not per organization, so the only way to see
# them all is to ask project by project — and an organization with hundreds of
# projects would turn one dropdown into hundreds of requests. Past this many
# the list is what the first projects offer, which in practice is every process
# in use.
_MAX_TYPE_PROJECTS = 25

# Hard limit of the workitemsbatch endpoint.
_BATCH_LIMIT = 200

_TIMEOUT = 30


def _describe_task(task: Task) -> str:
    """One work item as a log fragment: what it is and what Codee decided it is."""
    raw = task.work_item_type
    mapped = f"{raw}->{task.issue_type}" if raw != task.issue_type else task.issue_type
    return f"{task.key} [{task.status}/{mapped}]"


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


class AzureDevOpsTasksProvider(AbstractTasksProvider):
    """Fetches the organization's Codee work items as provider-agnostic Tasks."""

    DISPLAY_NAME = "Azure DevOps"
    MCP_SERVER_NAME = "ado"

    def __init__(self, settings: Settings, main_context: CodeeMainContext | None = None):
        self._config = OAuthConfig.from_settings(settings)
        # The executor constructs providers with settings alone, so fall back to
        # the default data directory to reach the token store.
        context = main_context or CodeeMainContext(data_dir=data_dir())
        self._auth = AzureDevOpsAuth(self._config, context)
        # How each Codee work item is picked out of Azure DevOps: a list of
        # work item types, or a WIQL condition of the user's own. One query is
        # built per work item, so what an item comes back as is settled by the
        # query that found it. The reverse type map is still needed for the
        # items no query asked for — a parent is fetched without a type filter.
        self._work_items = work_item_mappings(
            settings, TasksProvider.AZURE_DEVOPS)
        self._codee_types = codee_work_items(self._work_items)
        # The same mapping read as a set of backend types: a work item whose
        # parent is one of them is left to that parent's own run. Only the
        # types are in it — ``codee_work_items`` leaves out the work items
        # selected by a WIQL condition, which is also what the executor's rule
        # is defined in terms of.
        self._codee_parent_types = frozenset(self._codee_types)
        # An extra WIQL condition the user narrowed the poll with, empty unless
        # one was configured. Kept as written: it is theirs to get right, and
        # Azure DevOps explains a rejected query better than a parser here could.
        self._task_filter = task_filter(settings, TasksProvider.AZURE_DEVOPS)

    def _work_item(self, name: str) -> WorkItemMapping:
        """One Codee work item by name, empty when this install has no such row.

        Only the mandatory two are ever looked up this way, and the reader
        fills those in from the defaults — but a hand-edited settings file can
        still drop one, and an empty mapping is the answer that keeps every
        caller from having to say so again.
        """
        for mapping in self._work_items:
            if mapping.name == name:
                return mapping
        return WorkItemMapping(name=name)

    def is_configured(self) -> bool:
        """Configured means the app details are filled in *and* OAuth completed."""
        return self._config.is_complete() and self._auth.is_connected()

    def describe(self) -> str:
        connection = self._auth.connection() or {}
        account = connection.get("account") or "connected account"
        types = ", ".join(self._describe_work_items()) or "no work item types"
        # The account is named as the identity the query runs as, not as a
        # filter — the poll matches on type and state, whoever a work item is
        # assigned to.
        # The filter only gets a mention when there is one: it is off for most
        # installations, and "filter none" reads like a setting gone wrong.
        extra = f", filter {self._task_filter}" if self._task_filter else ""
        return (f"Azure DevOps {self._config.organization_url} "
                f"(all projects, connected as {account}, "
                f"work items {types}{extra})")

    def _describe_work_items(self) -> list[str]:
        """Each work item as "name: how it is selected", for a log line.

        A query is named rather than quoted: it can be a paragraph of WIQL, and
        the point of this line is what Codee is pointed at, not the filter's
        small print — which the debug log prints in full anyway.
        """
        return [f"{mapping.name} (custom WIQL)" if mapping.is_query
                else f"{mapping.name} ({', '.join(mapping.types)})"
                for mapping in self._work_items]

    def task_url(self, key: str) -> str:
        """The organization-level editor link for one work item.

        Work items are numbered per organization rather than per project, so
        this resolves any of them without having to know which project it lives
        in — the same reason the queries here name no project. A key that is not
        a work item id gets no link: it belongs to another provider.
        """
        if not (self._config.organization_url and key.isdigit()):
            return ""
        return f"{self._config.organization_url}/_workitems/edit/{key}"

    def verify_connection(self, statuses: list[str]) -> tuple[bool, str]:
        """Pull tasks and include the exact WIQL in a successful check."""
        verified, message = super().verify_connection(statuses)
        if not verified:
            return verified, message
        queries = "\n\n".join(
            f"WIQL for work item {mapping.name}: "
            f"{self._build_wiql(mapping, statuses)}"
            for mapping in self._work_items)
        return verified, f"{message}\n\n{queries}"

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

        The type comes from the work item mapping rather than a fixed name: an
        organization whose backlog is "Product Backlog Item" would otherwise be
        checked with a type it does not define, and fail a check the executor
        would have passed. No project is named — there is no project setting,
        queries span the organization — so the agent picks one it can write to.
        """
        account = (self._auth.connection() or {}).get("account")
        organization = self._config.organization
        # The first of them, where a Codee task stands for several backend
        # types: one created work item is all the check needs, and the rest of
        # the mapping would only make it longer.
        item_type = next(iter(self._work_item(TASK_ISSUE_TYPE).types), "")
        if not (organization and account and item_type):
            return None
        return [
            f'Create a new "{item_type}" work item in the {organization} '
            "organization, in any project you can create work items in, with "
            f'the title "{summary}".',
            "Move that work item to a Done, Closed or Removed state — "
            "whichever its board offers — so it does not stay open.",
        ]

    def work_item_types_scope(self) -> str:
        """Every project the listing reached, capped the same way it is."""
        return f"up to {_MAX_TYPE_PROJECTS} projects in the organization"

    def list_work_item_types(self) -> list[str]:
        """Every work item type name defined across the organization's projects.

        Work item types belong to a process, not to the organization, so there
        is no single endpoint that lists them — two projects on different
        process templates offer different types, and the queries here span
        every project. So does this: the names are gathered project by project
        and merged, capped at ``_MAX_TYPE_PROJECTS``.

        A project that refuses is skipped rather than fatal: read access to one
        project is enough to configure a mapping, and an organization where
        some projects are closed off is normal. Only a failure that leaves
        nothing at all to show is raised.
        """
        try:
            token = self._auth.access_token()
        except AzureDevOpsAuthError as exc:
            raise TasksProviderError(
                f"Azure DevOps sign-in failed: {exc}") from exc

        try:
            projects = self._fetch_projects(token)
        except requests.RequestException as exc:
            raise TasksProviderError(_describe_error(exc)) from exc

        names: set[str] = set()
        last_error: requests.RequestException | None = None
        for project in projects[:_MAX_TYPE_PROJECTS]:
            try:
                names.update(self._fetch_work_item_types(token, project))
            except requests.RequestException as exc:
                last_error = exc
        if not names and last_error is not None:
            raise TasksProviderError(_describe_error(last_error)) from last_error
        resolved = sorted(names, key=str.casefold)
        log.debug("%d project(s) offer %d work item type(s): %s",
                  min(len(projects), _MAX_TYPE_PROJECTS), len(resolved),
                  ", ".join(resolved))
        return resolved

    def _fetch_projects(self, token: str) -> list[str]:
        """Names of the projects the connected account can see."""
        response = requests.get(
            f"{self._config.organization_url}/_apis/projects",
            params={"api-version": API_VERSION, "$top": _MAX_TYPE_PROJECTS},
            headers=self._headers(token),
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        return [name for project in (response.json().get("value") or [])
                if (name := str(project.get("name", "")).strip())]

    def _fetch_work_item_types(self, token: str, project: str) -> list[str]:
        """Work item type names one project defines. Quoted: names carry spaces."""
        response = requests.get(
            f"{self._config.organization_url}/{quote(project, safe='')}"
            "/_apis/wit/workitemtypes",
            params={"api-version": API_VERSION},
            headers=self._headers(token),
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        return [name for item_type in (response.json().get("value") or [])
                if (name := str(item_type.get("name", "")).strip())]

    def get_tasks(self, statuses: list[str],
                  raise_errors: bool = False) -> list[Task]:
        """Fetch the Codee work items sitting in the given states.

        One WIQL query per Codee work item rather than one for all of them. A
        work item selected by a condition of the user's own can only be
        recognized by asking Azure DevOps for it on its own terms — nothing in
        a returned item says which condition matched it — and once one work
        item needs its own query they all do, or two of them would be ordered
        against each other by an accident of which path they took.

        Only the id lists are fetched per work item. WIQL returns ids and
        nothing else, so the expensive half — the batch read of the items and
        their parents — is still done once, over everything the queries found
        between them.
        """
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
            log.error("Azure DevOps auth error: %s", exc)
            return []

        ids_by_work_item = [
            (mapping, self._work_item_ids(token, mapping, statuses,
                                          raise_errors))
            for mapping in self._work_items
        ]
        ids = _unique_ids(found for _, found in ids_by_work_item)
        items: list[dict] = []
        parents: dict[int, dict] = {}
        if ids:
            try:
                items = self._fetch_work_items(token, ids)
                parents = self._fetch_parents(token, items)
            except requests.RequestException as exc:
                if raise_errors:
                    raise TasksProviderError(_describe_error(exc)) from exc
                log.error("Azure DevOps API error: %s", _describe_error(exc))
                return []

        # The batch endpoint doesn't preserve the WIQL ordering, so each work
        # item's tasks are rebuilt in the order its own query asked for.
        by_id = {item["id"]: item for item in items}
        results = []
        for mapping, found in ids_by_work_item:
            tasks = [self._to_task(by_id[item_id], parents, mapping.name)
                     for item_id in found if item_id in by_id]
            log.debug("WIQL for work item %s matched %d work item(s)%s",
                      mapping.name, len(tasks),
                      ": " + ", ".join(_describe_task(task) for task in tasks)
                      if tasks else "")
            results.append(tasks)
        return merge_work_item_tasks(results)

    def _work_item_ids(self, token: str, mapping: WorkItemMapping,
                       statuses: list[str], raise_errors: bool) -> list[int]:
        """The ids one Codee work item's query returned, in its own order.

        A failure is that work item's alone when the executor is asking: the
        rest of the poll is still worth having, and a mistyped condition on one
        work item must not stop the others being worked. The settings check
        asks for the opposite — there the failure is the answer.
        """
        if not (mapping.is_query or mapping.types):
            # Nothing to ask for. Querying anyway would drop the type clause,
            # and with no assignee clause to fall back on that hands the
            # executor every item in the organization. Only a hand-edited
            # settings file gets here — the reader drops such a row, and the
            # settings page refuses to save one.
            return []
        try:
            found = self._query_work_item_ids(token, mapping, statuses)
        except requests.RequestException as exc:
            if raise_errors:
                raise TasksProviderError(_describe_error(exc)) from exc
            log.error("Azure DevOps API error for work item %s: %s",
                      mapping.name, _describe_error(exc))
            return []
        return found

    def _query_work_item_ids(self, token: str, mapping: WorkItemMapping,
                             statuses: list[str]) -> list[int]:
        # Organization-scoped, like the batch fetch below: the endpoint's
        # project segment is optional, and leaving it off is what lets one query
        # span every project the connected account can read.
        query = self._build_wiql(mapping, statuses)
        # Logged verbatim: "Codee isn't picking up my work item" is answered by
        # reading what this work item resolved to, the states the skills asked
        # for and the custom filter from Settings, all of which are in this one
        # string.
        log.debug("WIQL for work item %s: %s", mapping.name, query)
        response = requests.post(
            f"{self._config.organization_url}/_apis/wit/wiql",
            params={"api-version": API_VERSION, "$top": _MAX_TASKS},
            json={"query": query},
            headers=self._headers(token),
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        work_items = response.json().get("workItems") or []
        return [item["id"] for item in work_items][:_BATCH_LIMIT]

    def _build_wiql_status_clause(self, statuses: list[str]) -> str:
        """The state filter, dropped entirely when nothing was requested.

        ``IN ()`` is not valid WIQL, and the only caller that passes no statuses
        is the connection check — it wants every Codee work item it can see,
        whatever state it sits in.
        """
        if not statuses:
            return ""
        quoted = ", ".join(_quote_wiql(status) for status in statuses)
        return f"[System.State] IN ({quoted})"

    def _build_wiql(self, mapping: WorkItemMapping,
                    statuses: list[str]) -> str:
        """WIQL for one Codee work item, highest priority first, then oldest.

        There is no assignee clause: what hands a work item to Codee is its
        type and its state, not who it is assigned to. So a work item a human
        still owns is picked up the moment it reaches a state one of the skills
        triggers on, which is what makes those states the handover — they have
        to be ones only Codee's workflow uses. An installation that does want
        an owner filter writes one as the custom WIQL in Settings, e.g.
        ``[System.AssignedTo] = @Me``.

        Nothing here names a project either, and no ``@project`` macro is used
        — that is what keeps the query valid with no project in the route, so
        it spans the organization. What comes back is still narrow: only the
        Codee work item types, and only the states asked for.
        """
        clauses = [clause for clause in (
            self._build_wiql_work_item_clause(mapping),
            self._build_wiql_status_clause(statuses),
            self._build_wiql_filter_clause(),
        ) if clause]
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        return (
            "SELECT [System.Id] FROM WorkItems "
            f"{where}"
            "ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.CreatedDate] ASC"
        )

    def _build_wiql_filter_clause(self) -> str:
        """The custom WIQL from Settings, ANDed onto the clauses above.

        Bracketed, because a filter is a whole condition rather than a single
        term: an unparenthesized ``... OR ...`` would bind its OR across the
        type and state clauses and hand back items Codee does not own.
        """
        if not self._task_filter:
            return ""
        return f"({self._task_filter})"

    def _build_wiql_work_item_clause(self, mapping: WorkItemMapping) -> str:
        """What narrows the query to one Codee work item.

        Its work item types, or the WIQL the user wrote for it instead. A
        custom condition is bracketed, like the filter below and for the same
        reason: an unparenthesized ``... OR ...`` would bind across the state
        clause and hand back items Codee does not own.
        """
        if mapping.is_query:
            return f"({mapping.query})"
        quoted = ", ".join(_quote_wiql(item_type)
                           for item_type in mapping.types)
        return f"[System.WorkItemType] IN ({quoted})"

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

    def _to_task(self, item: dict, parents: dict[int, dict],
                 issue_type: str = "") -> Task:
        """One work item as the executor reads it.

        ``issue_type`` is the Codee work item whose query returned it, which is
        the only thing that can name an item a custom condition matched. Left
        out for a parent, which no query asked for: it falls back to whichever
        work item claims the type Azure DevOps gave it.
        """
        fields = item.get("fields", {})
        parent = parents.get(fields.get("System.Parent"))
        work_item_type = fields.get("System.WorkItemType", "")
        return Task(
            work_item_type=work_item_type,
            codee_work_item_types=self._codee_parent_types,
            key=str(item["id"]),
            summary=fields.get("System.Title", ""),
            status=fields.get("System.State", ""),
            # Parents aren't type-filtered by the query, so an unmapped type
            # (an "Epic" above a Codee task) passes through as-is.
            issue_type=issue_type or self._codee_types.get(
                work_item_type.casefold(), work_item_type),
            priority=_PRIORITY_NAMES.get(
                fields.get("Microsoft.VSTS.Common.Priority"), "Unknown"),
            labels=_split_tags(fields.get("System.Tags")),
            # A parent's own parent is left unresolved: the executor only ever
            # looks one level up, and chasing the chain would cost a call per level.
            parent=self._to_task(parent, {}) if parent else None,
        )


def _unique_ids(id_lists: Iterable[list[int]]) -> list[int]:
    """Every id the work item queries found, each once, in the order found.

    One batch read covers them all, and an item two queries both matched must
    not take two of the 200 places that read has.
    """
    seen: dict[int, None] = {}
    for ids in id_lists:
        for item_id in ids:
            seen.setdefault(item_id, None)
    return list(seen)[:_BATCH_LIMIT]


def _split_tags(tags: str | None) -> list[str]:
    """Azure DevOps returns tags as one '; '-joined string."""
    if not tags:
        return []
    return [tag.strip() for tag in tags.split(";") if tag.strip()]
