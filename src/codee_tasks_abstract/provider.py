from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from codee_main_context.context import Settings


@dataclass
class Task:
    """Provider-agnostic representation of a task to be worked on.

    Models the fields the executor reads out of a JIRA issue, so any provider
    (JIRA, Azure DevOps, ...) can be reduced to the same shape.
    """

    key: str
    summary: str
    status: str
    issue_type: str
    priority: str
    labels: list[str] = field(default_factory=list)
    parent: "Task | None" = None

    @property
    def is_parent_codee_story(self) -> bool:
        """Whether this task hangs under a story Codee owns.

        What marks a story as Codee-owned is provider-specific — a label in
        JIRA, a work item type in Azure DevOps — so each provider answers this
        for its own tasks. The executor only asks the question. A provider that
        has no notion of Codee stories inherits "no parent story".
        """
        return False


class AbstractTasksProvider(ABC):
    """Base class every tasks provider (e.g. JIRA) inherits from.

    A provider is constructed from the app ``Settings`` and initializes itself
    from its own stored credentials, so the executor never has to know which
    provider it's talking to or what configuration that provider needs.
    """

    def __init__(self, settings: Settings):
        """Initialize the provider from the stored settings."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether the provider has enough configuration to fetch tasks."""
        ...

    @abstractmethod
    def get_tasks(self, statuses: list[str]) -> list[Task]:
        """Return agent-owned tasks in the requested statuses, highest priority first."""
        ...

    def describe(self) -> str:
        """Human-readable one-liner about this provider's config, for logs."""
        return type(self).__name__
