from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from codee_main_context.context import Settings


class TasksProviderError(Exception):
    """The provider could not reach its backend, or was refused by it.

    Carries a message meant for a human: the settings page shows it verbatim
    when a connection check fails, so it should say what went wrong (bad token,
    unknown project, unreachable host) rather than name an internal call.
    """


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
    def get_tasks(self, statuses: list[str],
                  raise_errors: bool = False) -> list[Task]:
        """Return agent-owned tasks in the requested statuses, highest priority first.

        ``raise_errors`` says who is asking. The polling executor leaves it off:
        a tick has to survive a rotated token or an outage, so a failure is
        logged and reported as "nothing to do" and the next tick tries again.
        The settings check turns it on, because there the failure is the answer,
        and it gets a ``TasksProviderError`` carrying what the backend said.
        """
        ...

    def verify_connection(self, statuses: list[str]) -> tuple[bool, str]:
        """Pull tasks for real and report the outcome, for the settings page."""
        try:
            tasks = self.get_tasks(statuses, raise_errors=True)
        except TasksProviderError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - the UI must never see a traceback
            return False, f"{type(exc).__name__}: {exc}"
        return True, self._verified_message(tasks)

    def _verified_message(self, tasks: list[Task]) -> str:
        """Say what came back, naming a few tasks so the user recognizes them.

        The statuses queried are left out: there is one per issue-triggered
        skill, and the list is long enough to bury the answer the user came for.
        """
        if not tasks:
            return (f"Connected to {self.describe()}. "
                    "No tasks are assigned to it right now.")
        preview = ", ".join(f"{task.key} {task.summary}" for task in tasks[:3])
        more = f", +{len(tasks) - 3} more" if len(tasks) > 3 else ""
        return (f"Connected to {self.describe()}. "
                f"Pulled {len(tasks)} task(s): {preview}{more}")

    def describe(self) -> str:
        """Human-readable one-liner about this provider's config, for logs."""
        return type(self).__name__
