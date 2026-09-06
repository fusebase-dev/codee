import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


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
# with custom process templates can be polled at all.
DEFAULT_WORK_ITEM_TYPES: dict[TasksProvider, dict[str, str]] = {
    TasksProvider.JIRA: {"story": "Story", "task": "Task"},
    TasksProvider.AZURE_DEVOPS: {"story": "User Story", "task": "Task"},
}


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
                             "MYPRJ-4124 the project key is MYPRJ. Jira shows "
                             "it under Project settings \u2192 Details, and "
                             "it is in the URL of any board."),
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
    # Per-provider credentials, keyed by provider value -> {field key: value}.
    # Values for all providers are kept so switching provider preserves them.
    credentials: dict[str, dict[str, str]] = field(default_factory=dict)
    # Max coding-agent runs the executor keeps in flight at once (>= 1).
    max_parallel_agents: int = 3
    # Which backend work item type each Codee work item is polled as, keyed by
    # provider value -> {codee work item: provider work item type}. Kept per
    # provider like the credentials, because the names differ between them: a
    # Codee story is a "Story" in JIRA and a "User Story" in Azure DevOps.
    # Read it through :func:`work_item_types`, never directly — what is stored
    # here may predate a Codee work item that has since become mandatory.
    work_item_types: dict[str, dict[str, str]] = field(default_factory=dict)
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
                credentials=data.get("credentials", {}),
                work_item_types=data.get("work_item_types", {}),
                task_filters=data.get("task_filters", {}),
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
        "credentials": settings.credentials,
        "work_item_types": settings.work_item_types,
        "task_filters": settings.task_filters,
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


def work_item_types(settings: Settings,
                    provider: TasksProvider | None = None) -> dict[str, str]:
    """Codee work item -> provider work item type, for one provider.

    The mandatory work items are always present and always first, filled in
    from the provider's defaults when nothing has been stored for them yet.
    That is what keeps a settings file written before this setting existed —
    or one a user hand-edited down to a single row — from leaving the executor
    with no story to recognize. Anything the user added follows, in the order
    it was saved.

    Names are normalized to lower case, matching how skills declare
    ``x-codee-issue-type``; a row with no name or no backend type is dropped,
    since neither side of it could ever match anything.
    """
    provider = provider or settings.tasks_provider
    defaults = DEFAULT_WORK_ITEM_TYPES.get(provider, {})
    stored = settings.work_item_types.get(provider.value) or {}
    resolved = {name: defaults[name] for name in DEFAULT_ISSUE_TYPES
                if name in defaults}
    for name, backend_type in stored.items():
        name = str(name).strip().lower()
        backend_type = str(backend_type).strip()
        if name and backend_type:
            resolved[name] = backend_type
    return resolved


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
    return tuple(work_item_types(settings))
