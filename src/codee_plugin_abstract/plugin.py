"""What a Codee plugin is: skills it ships, and triggers that run skills.

A plugin is a separate Python package that registers a subclass of
:class:`AbstractCodeePlugin` under the ``codee.plugins`` entry point group::

    [project.entry-points."codee.plugins"]
    acme = "codee_acme.plugin:AcmePlugin"

A plugin kept in a folder of the Codee project is the same package, added to
that project as a path dependency (``[tool.uv.sources]``), so there is one way
in either way.
"""
import re
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class PluginField:
    """One text field a plugin asks for in the admin UI.

    On a trigger's ``SKILL_FIELDS``, ``key`` is the frontmatter key the value
    is written under, so it should carry the plugin's own prefix
    (``x-acme-channel``) to stay clear of everyone else's. On a plugin's
    ``SETTINGS_FIELDS`` it is the key in that plugin's section of
    ``settings.json``.
    """

    key: str
    label: str
    hint: str = ""
    placeholder: str = ""
    # Shown as a password input; stored as plain text like the provider tokens.
    secret: bool = False
    # Shown as a text area, for a value that is one item per line.
    multiline: bool = False
    # A picker instead of a text input: the user searches (the plugin's
    # ``search_field``) and picks any number of options, shown by label.
    # Codee stores them one per line as ``<value>  # <label>`` and hands the
    # trigger only the values, one per line.
    searchable: bool = False
    # A select of these (value, label) pairs instead of a text input.
    choices: tuple[tuple[str, str], ...] = ()
    # The value a field starts with when nothing is stored yet.
    default: str = ""
    # Shown only while another field of the same form, (key, value), has that
    # value: a provider's credentials, a mode's options.
    visible_when: tuple[str, str] | None = None


@dataclass(frozen=True)
class FieldOption:
    """One option of a searchable field: what is stored, and what is shown."""

    value: str
    label: str


LABEL_SEPARATOR = "  # "


def parse_field_options(text: str) -> list[FieldOption]:
    """A searchable field's stored lines, as options; label "" if none."""
    options = []
    for line in (text or "").splitlines():
        # A label follows whitespace and "#", so a "#" inside a value (a URL
        # fragment) stays part of it.
        value, label = (re.split(r"\s+#\s*", line, maxsplit=1) + [""])[:2]
        if value.strip():
            options.append(FieldOption(value.strip(), label.strip()))
    return options


def format_field_options(options: list[FieldOption]) -> str:
    return "\n".join(f"{option.value}{LABEL_SEPARATOR}{option.label}"
                     if option.label else option.value for option in options)


@dataclass(frozen=True)
class TriggeredSkill:
    """A skill whose ``x-codee-trigger`` names one of the plugin's triggers."""

    slug: str
    name: str
    path: Path
    # The prompt: everything after the frontmatter.
    body: str
    # The trigger's ``SKILL_FIELDS`` as the skill sets them, keyed by
    # ``PluginField.key``; a field the skill leaves out is an empty string.
    fields: dict[str, str] = field(default_factory=dict)
    model: str = ""
    agent: str = ""
    effort: str = ""
    context_tier: str = ""


class RunKilled(Exception):
    """The run was stopped from the dashboard: on purpose, not to retry."""


# Runs a skill's agent and records the run on the Runs page under the
# trigger's type. ``prompt`` defaults to the skill body. Returns the agent's
# reply; raises if the run failed, or RunKilled if it was killed, after
# recording it, so the trigger can decide whether the event that fired it is
# used up.
RunSkill = Callable[[TriggeredSkill, str], str]


class AbstractSkillTrigger(ABC):
    """Something that decides when a skill runs, polled once per executor tick.

    ``TYPE`` is the ``x-codee-trigger`` value a skill uses to pick it, and the
    skill type the editor offers is ``"<TYPE> trigger"``. It must not reuse a
    built-in one (issue, cron, email, aws-sqs) or another plugin's.
    """

    TYPE: str = ""
    # How often ``tick`` is called, in seconds; Codee won't go below 10.
    POLL_SECONDS: int = 60
    # Asked for under the Skill type picker when this trigger is selected.
    SKILL_FIELDS: list[PluginField] = []
    # Markdown shown beside the skill body when this trigger is selected, in a
    # panel the reader can collapse: what the skill receives, which
    # placeholders its body can use.
    DESCRIPTION: str = ""

    def tick(self, skills: list[TriggeredSkill], run_skill: RunSkill,
             settings: dict[str, str]) -> None:
        """Run whichever of ``skills`` are due. Called every POLL_SECONDS.

        Only called while Codee is not paused and when at least one skill uses
        this trigger. ``settings`` is the plugin's section of the Settings
        page. Runs in the executor's main loop, so anything slow should be
        bounded: the other triggers wait for it.
        """


@dataclass(frozen=True)
class PluginAction:
    """A button on the plugin's card, e.g. "Test connection"."""

    key: str
    label: str


@dataclass(frozen=True)
class LogEntry:
    """One line of a plugin's log, as its card's log dialog shows it."""

    # ISO time it happened.
    time: str
    # "ok", "skip" or "error": how the entry is coloured.
    status: str
    # One line: what it was about.
    title: str
    # The rest, shown under the title as preformatted text.
    detail: str = ""


class SignInError(Exception):
    """Signing in could not start or finish; the message is shown to the user."""


class AbstractCodeePlugin(ABC):
    """A plugin: a name, optional settings, skills, and triggers.

    A plugin that signs in to a service as a user (OAuth) sets
    ``SUPPORTS_SIGN_IN`` and implements the four sign-in methods. Codee then
    puts a Sign in button on its card, serves the callback at
    ``/api/oauth/plugins/<NAME>/callback`` and checks the ``state`` it sent;
    the plugin exchanges the code and keeps the tokens itself, in
    ``data_dir``.
    """

    # Stable code: the key of the plugin's settings and what a plugin skill
    # is marked as belonging to. Lowercase, no spaces.
    NAME: str = ""
    DISPLAY_NAME: str = ""
    # Shown on the plugin's card on the Plugins page.
    SETTINGS_FIELDS: list[PluginField] = []
    # Markdown on the plugin's card, in a panel the reader can collapse: how
    # to set the plugin up. ``{REDIRECT_URI}`` is replaced with the sign-in
    # redirect URI of this Codee.
    DESCRIPTION: str = ""
    SUPPORTS_SIGN_IN: bool = False
    # Buttons on the plugin's card; pressing one calls ``run_action``.
    ACTIONS: list[PluginAction] = []
    # Set to put a button by this name on the card that opens ``read_log``.
    LOG_LABEL: str = ""

    # A directory of the plugin's own, for tokens and whatever it has to
    # remember between ticks. Set by Codee before the plugin is used; created
    # on first use by whoever writes to it.
    data_dir: Path = Path(".")

    def sign_in_url(self, settings: dict[str, str], redirect_uri: str,
                    state: str, code_verifier: str) -> str:
        """Where to send the browser to sign in.

        ``state`` and ``code_verifier`` (PKCE) are made by Codee and given back
        to :meth:`complete_sign_in`. Raise :class:`SignInError` when the
        settings are not filled in yet.
        """
        raise SignInError(f"{self.NAME} does not sign in")

    def complete_sign_in(self, settings: dict[str, str], redirect_uri: str,
                         code: str, code_verifier: str) -> str:
        """Exchange the callback's code and store the tokens.

        Returns who is signed in, as the Plugins page should show it. Raise
        :class:`SignInError` on failure.
        """
        raise SignInError(f"{self.NAME} does not sign in")

    def search_field(self, key: str, query: str,
                     settings: dict[str, str]) -> list[FieldOption]:
        """Options for a ``searchable`` field of one of the plugin's triggers.

        ``key`` names the field, ``query`` is what was typed (may be empty).
        Raise :class:`SignInError` for a message the user should see.
        """
        return []

    def describe_field_values(self, key: str, values: list[str],
                              settings: dict[str, str]) -> dict[str, str]:
        """Labels for stored values that came without one (typed by hand).

        Returns value -> label for those it knows; the rest are shown as they
        are stored.
        """
        return {}

    def run_action(self, key: str, settings: dict[str, str]) -> str:
        """Do one of ``ACTIONS``; return what happened, shown on the card.

        ``settings`` are the card's fields as they are on screen, saved or
        not, so a key can be tried before it is saved. Raise an exception
        whose message says what went wrong.
        """
        raise NotImplementedError(f"{self.NAME} has no action {key}")

    def read_log(self, limit: int) -> list[LogEntry]:
        """The plugin's most recent log entries, newest first."""
        return []

    def signed_in_as(self) -> str:
        """The account signed in, or "" when nobody is."""
        return ""

    def sign_out(self) -> None:
        """Forget the stored tokens."""

    def skills_dir(self) -> Path | None:
        """A directory of ``<slug>/SKILL.md`` skills to install, or None.

        They are linked into ``.claude/skills`` so every agent finds them, kept
        out of Git, and read-only in the admin UI.
        """
        return None

    def triggers(self) -> list[AbstractSkillTrigger]:
        return []
