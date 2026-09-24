import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


def project_root() -> Path:
    """Directory holding the content Codee operates on: ``.claude/skills`` and ``memory/``.

    Defaults to the working directory, so a project that installs Codee as a
    package supplies its own skills and memories rather than reaching into the
    installed package. Override with ``CODEE_PROJECT_ROOT``.
    """
    return Path(os.environ.get("CODEE_PROJECT_ROOT") or os.getcwd())


def skills_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / ".claude" / "skills"


def memory_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "memory"


def data_dir(root: Path | None = None) -> Path:
    """Codee's own state directory (settings.json, runs db). Override with ``CODEE_DATA_DIR``."""
    override = os.environ.get("CODEE_DATA_DIR")
    return Path(override) if override else (root or project_root()) / ".codee"


class TasksProvider(str, Enum):
    JIRA = "jira"
    AZURE_DEVOPS = "azure_devops"


class CodingAgent(str, Enum):
    CLAUDE_CODE = "claude_code"
    GITHUB_COPILOT = "github_copilot"
    CODEX = "codex"
    OPENCODE = "opencode"


@dataclass(frozen=True)
class CredentialField:
    key: str
    label: str
    secret: bool = False
    default: str = ""
    # One sentence saying what to type and where to find it, for fields whose
    # label is not self-explanatory. Shown under the input on the settings page
    # and above the prompt in `codee-agent init`, so the wording lives here
    # rather than being written twice and drifting apart.
    hint: str = ""


# Codee's own work item names. Every Codee installation has these two: skills
# declare which one they trigger on (``x-codee-issue-type``), and the executor
# treats a story as the thing whose children it must leave alone. The user can
# add more in Settings, but never take these away.
STORY_ISSUE_TYPE = "story"
TASK_ISSUE_TYPE = "task"
DEFAULT_ISSUE_TYPES = (STORY_ISSUE_TYPE, TASK_ISSUE_TYPE)

# What each Codee work item is called in a freshly configured provider. Only a
# starting point — the settings page lets the user point a Codee work item at
# whatever the backend actually calls it, which is the only way an organization
# with custom process templates can be polled at all. A list because one Codee
# work item can stand for several backend types at once: a Codee `task` may be
# both a "Task" and a "Bug" in the backend the user polls.
DEFAULT_WORK_ITEM_TYPES: dict[TasksProvider, dict[str, list[str]]] = {
    TasksProvider.JIRA: {"story": ["Story"], "task": ["Task"]},
    TasksProvider.AZURE_DEVOPS: {"story": ["User Story"], "task": ["Task"]},
}


@dataclass(frozen=True)
class WorkItemMapping:
    """How one Codee work item is picked out of the backend.

    Two ways of saying it, and a work item uses one or the other. ``types``
    names the backend work item types it is polled as — the ordinary answer,
    and the only one most installations need. ``query`` is a condition in the
    provider's own language (JQL, WIQL) that selects the work item on its own
    terms, for a backlog no list of type names can describe: a "Bug" that is
    only Codee's when it carries a label, or a work item split across two
    projects by a field nobody thought to make a type.

    A query wins where both are set. It is the whole selection, so the types
    beside it are left as the user last picked them rather than cleared — the
    settings page offers them again the moment the work item is switched back,
    and nothing reads them meanwhile.
    """

    name: str
    types: tuple[str, ...] = ()
    query: str = ""

    @property
    def is_query(self) -> bool:
        """Whether this work item is selected by its query rather than its types."""
        return bool(self.query)


# Credential fields each provider needs. Keyed by provider so the admin UI can
# render the right inputs and settings.json can store per-provider values.
TASKS_PROVIDER_FIELDS: dict[TasksProvider, list[CredentialField]] = {
    # The email is the API token's owner, not a filter: JIRA Cloud's REST API
    # authenticates with HTTP Basic where the username is that email, and it
    # rejects the token on its own. Nothing else reads it — which issues Codee
    # picks up is decided by their type and status alone.
    TasksProvider.JIRA: [
        CredentialField("base_url", "Base URL",
                        hint="Your Jira site, e.g. "
                             "https://your-company.atlassian.net"),
        CredentialField("account_email", "API Token Owner Email",
                        hint="The email the API token below belongs to. Jira "
                             "signs every request as this account."),
        CredentialField("api_token", "API token", secret=True),
        CredentialField("project", "Project key",
                        hint="The short prefix on every issue key in the "
                             "project, not its name \u2014 for issue "
                             "MYPRJ-4124 the project key is MYPRJ."),
    ],
    # Azure DevOps authenticates through an Entra ID app registration, so the
    # stored credentials describe the app; the tokens it yields live in SQLite
    # (codee_database.oauth_tokens) rather than in settings.json.
    # There is no project field: work items are queried across the whole
    # organization, which is the scope the granted access has anyway.
    TasksProvider.AZURE_DEVOPS: [
        CredentialField("organization_url", "Organization URL"),
        CredentialField("tenant_id", "Directory (tenant) ID"),
        CredentialField("client_id", "Application (client) ID"),
        CredentialField("client_secret", "Client secret", secret=True),
    ],
}


@dataclass
class Settings:
    tasks_provider: TasksProvider = TasksProvider.JIRA
    # Which coding agent the executor drives to work on tasks.
    coding_agent: CodingAgent = CodingAgent.CLAUDE_CODE
    # Text prepended to every prompt sent to GitHub Copilot. Other coding
    # agents do not read this setting.
    github_copilot_prompt_prefix: str = ""
    # Per-provider credentials, keyed by provider value -> {field key: value}.
    # Values for all providers are kept so switching provider preserves them.
    credentials: dict[str, dict[str, str]] = field(default_factory=dict)
    # Max coding-agent runs the executor keeps in flight at once (>= 1).
    max_parallel_agents: int = 3
    # Which backend work item types each Codee work item is polled as, keyed by
    # provider value -> {codee work item: [provider work item type, ...]}. Kept
    # per provider like the credentials, because the names differ between them:
    # a Codee story is a "Story" in JIRA and a "User Story" in Azure DevOps.
    # Read it through :func:`work_item_mappings`, never directly — what is
    # stored here may predate a Codee work item that has since become
    # mandatory, settings written before one work item could name several types
    # hold a bare string where there is now a list, and a work item selected by
    # a query ignores its types entirely.
    work_item_types: dict[str, dict[str, list[str]]
                          ] = field(default_factory=dict)
    # The advanced answer to the same question: a condition in the provider's
    # own query language that picks one Codee work item out of the backend,
    # keyed by provider value -> {codee work item: condition}. Set for a work
    # item, it replaces that work item's type list — the query is then the whole
    # definition of what Codee polls under that name. Empty for everyone who
    # never opens it, which is what keeps the type list the normal answer.
    work_item_queries: dict[str, dict[str, str]] = field(default_factory=dict)
    # Whether the executor rotates between the connected Claude accounts when
    # the current one runs into its session or weekly usage limit. Off by
    # default: an installation that never opens this setting keeps using
    # whatever ``~/.claude/.credentials.json`` already holds, untouched.
    #
    # Only the switch is here. The accounts themselves are completed sign-ins —
    # access tokens, refresh tokens, the email each was granted by — and live
    # in SQLite (codee_database.claude_code_accounts) for the same reason the
    # OAuth tokens do: this file is rewritten by the admin UI on every save.
    claude_code_rotate_keys: bool = False
    # An extra clause every task query is narrowed by, keyed by provider value.
    # Written in the provider's own query language — JQL for JIRA, WIQL for
    # Azure DevOps — so it is kept per provider like the credentials are, and
    # empty by default: nothing is added to the query until the user asks for
    # it. Read it through :func:`task_filter`, which normalizes what was typed.
    task_filters: dict[str, str] = field(default_factory=dict)


@dataclass
class CodeeMainContext:
    data_dir: Path
    settings: Settings = field(default_factory=Settings)


def settings_file(data_dir: Path) -> Path:
    return Path(data_dir) / "settings.json"


def load_settings(data_dir: Path) -> Settings:
    """Load Settings from ``settings.json`` in data_dir, or defaults if absent."""
    path = settings_file(data_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return Settings(
                tasks_provider=TasksProvider(data["tasks_provider"]),
                coding_agent=CodingAgent(
                    data.get("coding_agent", CodingAgent.CLAUDE_CODE.value)),
                github_copilot_prompt_prefix=str(
                    data.get("github_copilot_prompt_prefix", "")),
                credentials=data.get("credentials", {}),
                work_item_types=data.get("work_item_types", {}),
                work_item_queries=data.get("work_item_queries", {}),
                task_filters=data.get("task_filters", {}),
                claude_code_rotate_keys=bool(
                    data.get("claude_code_rotate_keys", False)),
                max_parallel_agents=max(1, int(data.get("max_parallel_agents", 3))))
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            pass
    return Settings()


def save_settings(data_dir: Path, settings: Settings) -> None:
    """Persist Settings to ``settings.json`` in data_dir.

    Written through a temp file and renamed into place: the executor re-reads
    this file on every poll, and a torn read there would look like "no settings"
    and silently reset it to the defaults.
    """
    path = settings_file(data_dir)
    payload = json.dumps({
        "tasks_provider": settings.tasks_provider.value,
        "coding_agent": settings.coding_agent.value,
        "github_copilot_prompt_prefix": settings.github_copilot_prompt_prefix,
        "credentials": settings.credentials,
        "work_item_types": settings.work_item_types,
        "work_item_queries": settings.work_item_queries,
        "task_filters": settings.task_filters,
        "claude_code_rotate_keys": settings.claude_code_rotate_keys,
        "max_parallel_agents": settings.max_parallel_agents,
    }, indent=2) + "\n"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(payload)
    os.replace(temp, path)


def credential_field(provider: TasksProvider, key: str) -> CredentialField:
    """One provider field by key, for a surface that renders fields by hand.

    The settings page binds each input to its own state variable, so it cannot
    loop over the list the way the setup wizard does; this lets it still read
    the label and hint from the same definition instead of restating them.
    """
    for field in TASKS_PROVIDER_FIELDS[provider]:
        if field.key == key:
            return field
    raise KeyError(f"{provider.value} has no credential field {key!r}")


def _backend_types(value: Any) -> list[str]:
    """One mapping entry's backend types, however the settings file holds them.

    A bare string is what a file written before a Codee work item could name
    several backend types holds, and it reads as the one-type list it meant.
    Blank entries are dropped and a type repeated within the entry is kept
    once: both would only widen the query with a name already in it.
    """
    values = value if isinstance(value, (list, tuple)) else [value]
    types: list[str] = []
    seen: set[str] = set()
    for item in values:
        backend_type = str(item).strip()
        if backend_type and backend_type.casefold() not in seen:
            seen.add(backend_type.casefold())
            types.append(backend_type)
    return types


def work_item_mappings(
    settings: Settings,
    provider: TasksProvider | None = None,
) -> list[WorkItemMapping]:
    """How each Codee work item is picked out of one provider's backend.

    In the order the settings page lists them: the mandatory work items first,
    filled in from the provider's defaults when nothing has been stored for
    them yet, then anything the user added. That fill-in is what keeps a
    settings file written before this setting existed — or one a user
    hand-edited down to a single row — from leaving the executor with no story
    to recognize.

    Names are normalized to lower case, matching how skills declare
    ``x-codee-issue-type``; a row that neither names a type nor carries a query
    is dropped, since there would be nothing for it to select.
    """
    provider = provider or settings.tasks_provider
    defaults = DEFAULT_WORK_ITEM_TYPES.get(provider, {})
    stored_types = settings.work_item_types.get(provider.value) or {}
    stored_queries = settings.work_item_queries.get(provider.value) or {}
    queries = {str(name).strip().lower(): str(query).strip()
               for name, query in stored_queries.items()}
    resolved: dict[str, list[str]] = {
        name: list(defaults[name]) for name in DEFAULT_ISSUE_TYPES
        if name in defaults}
    for name, value in stored_types.items():
        name = str(name).strip().lower()
        if name:
            resolved[name] = _backend_types(value)
    # A work item that is nothing but a query never reached the type mapping,
    # so it has to be picked up from the queries as well.
    for name in queries:
        if name and name not in resolved:
            resolved[name] = []
    return [
        WorkItemMapping(name=name, types=tuple(types),
                        query=queries.get(name, ""))
        for name, types in resolved.items()
        if name and (types or queries.get(name))
    ]


def codee_work_items(mappings: Iterable[WorkItemMapping]) -> dict[str, str]:
    """Backend type, case-folded -> the Codee work item it was mapped to.

    The reverse of the type mapping. Not how a polled item is named — the query
    that found it settles that — but how one that nothing polled for is: a
    parent is fetched without a type filter, and this is what lets the story
    above a Codee task be recognized as one.

    A backend type mapped to two Codee work items keeps the first: the settings
    page refuses to save that, so it can only reach here in a hand-edited file,
    where the stored order is the only answer available.
    """
    reversed_mapping: dict[str, str] = {}
    for mapping in mappings:
        if mapping.is_query:
            continue
        for backend_type in mapping.types:
            reversed_mapping.setdefault(backend_type.casefold(), mapping.name)
    return reversed_mapping


def task_filter(settings: Settings,
                provider: TasksProvider | None = None) -> str:
    """The extra query clause one provider narrows its task query with.

    Empty unless the user configured one, which is what keeps the query the
    executor polls with unchanged for everyone who never opens this setting.
    What comes back is a bare condition, ready to be joined onto the clauses
    the provider builds itself — the provider decides where it goes and how it
    is bracketed, since only it knows its own query language.

    A leading ``AND`` is dropped: writing the clause the way it will be joined
    is the obvious thing to do, and the doubled keyword would come back as a
    syntax error naming neither this setting nor the word it objects to.
    """
    provider = provider or settings.tasks_provider
    value = str(settings.task_filters.get(provider.value, "") or "").strip()
    if value[:4].casefold() == "and ":
        value = value[4:].strip()
    return value


def codee_issue_types(settings: Settings) -> tuple[str, ...]:
    """The Codee work item names skills may trigger on, for the current provider."""
    return tuple(mapping.name for mapping in work_item_mappings(settings))
