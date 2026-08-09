from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from codee_main_context.context import Settings


@dataclass(frozen=True)
class AgentModel:
    """One model a coding agent can run, as the admin UI shows and stores it.

    ``id`` is what goes into the skill's ``model:`` frontmatter and onto the CLI;
    ``name`` is the human-readable label ("Claude Opus 5" for ``claude-opus-5``).
    """

    id: str
    name: str


class AbstractCodingAgent(ABC):
    """Base class every coding agent (e.g. Claude Code) inherits from.

    A coding agent is constructed from the app ``Settings`` and the working
    directory it should run in, and knows how to run one prompt under a session
    id the caller hands it. The executor never has to know which agent it's
    driving or what configuration that agent needs.
    """

    def __init__(self, settings: Settings, cwd: Path):
        self._cwd = cwd

    @abstractmethod
    def run(self, user_message: str, session_id: str, model: str = "") -> str:
        """Run the agent with the message in ``session_id`` and return its text.

        ``model`` is the skill's ``model:`` frontmatter, or empty for the agent's
        default. Agents that read the frontmatter themselves may ignore it.

        Must raise on any failure so callers can retry (SQS keeps the message,
        cron doesn't mark the slot done, email keeps the ``.eml``).
        """
        ...

    @classmethod
    def list_models(cls) -> list[AgentModel]:
        """Models this agent offers, best-effort, for the admin UI's picker.

        Returns an empty list when the agent can't be asked. Callers must still
        accept a model id typed by hand, since the list is a convenience rather
        than an allowlist.
        """
        return []

    def describe(self) -> str:
        """Human-readable one-liner about this agent, for logs."""
        return type(self).__name__
