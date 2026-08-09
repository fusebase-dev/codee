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


# Credential fields each provider needs. Keyed by provider so the admin UI can
# render the right inputs and settings.json can store per-provider values.
TASKS_PROVIDER_FIELDS: dict[TasksProvider, list[CredentialField]] = {
    TasksProvider.JIRA: [
        CredentialField("base_url", "Base URL"),
        CredentialField("account_email", "Account email"),
        CredentialField("api_token", "API token", secret=True),
        CredentialField("project", "Project key"),
    ],
    # Azure DevOps authenticates through an Entra ID app registration, so the
    # stored credentials describe the app; the tokens it yields live in SQLite
    # (codee_database.oauth_tokens) rather than in settings.json.
    TasksProvider.AZURE_DEVOPS: [
        CredentialField("organization_url", "Organization URL"),
        CredentialField("project", "Project"),
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
        "max_parallel_agents": settings.max_parallel_agents,
    }, indent=2) + "\n"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(payload)
    os.replace(temp, path)
