import shutil
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

    # How the agent shows up to a human choosing one, and the executable that
    # has to be on PATH for it to run at all. Both are answered by the class
    # rather than by the caller, so the setup wizard can list and detect the
    # agents without knowing anything about them.
    DISPLAY_NAME = ""
    CLI_COMMAND = ""

    def __init__(self, settings: Settings, cwd: Path):
        self._cwd = cwd

    @classmethod
    def is_installed(cls) -> bool:
        """Whether this agent's CLI can be found on PATH.

        Only says the executable exists — not that it is signed in, or that the
        account behind it has credit. That is deliberate: the cheap check is the
        one worth making before anything is configured, and the expensive
        answer comes from the first real run.
        """
        return bool(cls.CLI_COMMAND) and shutil.which(cls.CLI_COMMAND) is not None

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
    def best_model(cls) -> str:
        """The id to pass for "the most capable model", for callers with no skill.

        Skill-triggered runs take their model from frontmatter, but internal
        prompts (workflow inference, setup checks) have no frontmatter to read
        and must not silently land on whichever default the CLI happens to
        resolve. Agents that offer a version-free alias return that alias so the
        choice tracks the newest release without an edit here.
        """
        return ""

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
