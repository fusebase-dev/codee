from abc import ABC, abstractmethod
from pathlib import Path

from codee_main_context.context import Settings


class AbstractCodingAgent(ABC):
    """Base class every coding agent (e.g. Claude Code) inherits from.

    A coding agent is constructed from the app ``Settings`` and the working
    directory it should run in, and knows how to run one prompt against a
    resumable session. The executor never has to know which agent it's driving
    or what configuration that agent needs.
    """

    def __init__(self, settings: Settings, cwd: Path):
        self._cwd = cwd

    @abstractmethod
    def run(self, user_message: str, session_id: str) -> str:
        """Run the agent with the message in ``session_id`` and return its text.

        Must raise on any failure so callers can retry (SQS keeps the message,
        cron doesn't mark the slot done, email keeps the ``.eml``).
        """
        ...

    @abstractmethod
    def session_exists(self, session_id: str) -> bool:
        """Whether a resumable session with this id already exists."""
        ...

    def describe(self) -> str:
        """Human-readable one-liner about this agent, for logs."""
        return type(self).__name__
