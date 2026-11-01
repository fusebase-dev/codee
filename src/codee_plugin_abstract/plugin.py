"""What a Codee plugin is: skills it ships, and triggers that run skills.

A plugin is a separate Python package that registers a subclass of
:class:`AbstractCodeePlugin` under the ``codee.plugins`` entry point group::

    [project.entry-points."codee.plugins"]
    acme = "codee_acme.plugin:AcmePlugin"

A plugin kept in a folder of the Codee project is the same package, added to
that project as a path dependency (``[tool.uv.sources]``), so there is one way
in either way.
"""
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


# Runs a skill's agent and records the run on the Runs page under the
# trigger's type. ``prompt`` defaults to the skill body. Returns the agent's
# reply; raises if the run failed or was killed, after recording it, so the
# trigger can decide whether the event that fired it is used up.
RunSkill = Callable[[TriggeredSkill, str], str]


class AbstractSkillTrigger(ABC):
    """Something that decides when a skill runs, polled once per executor tick.

    ``TYPE`` is the ``x-codee-trigger`` value a skill uses to pick it, and the
    skill type the editor offers is ``"<TYPE> trigger"``. It must not reuse a
    built-in one (issue, cron, email, aws-sqs) or another plugin's.
    """

    TYPE: str = ""
    # Asked for under the Skill type picker when this trigger is selected.
    SKILL_FIELDS: list[PluginField] = []

    def tick(self, skills: list[TriggeredSkill], run_skill: RunSkill,
             settings: dict[str, str]) -> None:
        """Run whichever of ``skills`` are due. Called every 60 seconds.

        Only called while Codee is not paused and when at least one skill uses
        this trigger. ``settings`` is the plugin's section of the Settings
        page. Runs in the executor's main loop, so anything slow should be
        bounded: the other triggers wait for it.
        """


class AbstractCodeePlugin(ABC):
    """A plugin: a name, optional settings, skills, and triggers."""

    # Stable code: the key of the plugin's settings and what a plugin skill
    # is marked as belonging to. Lowercase, no spaces.
    NAME: str = ""
    DISPLAY_NAME: str = ""
    # Shown on the plugin's card on the Plugins page.
    SETTINGS_FIELDS: list[PluginField] = []

    def skills_dir(self) -> Path | None:
        """A directory of ``<slug>/SKILL.md`` skills to install, or None.

        They are linked into ``.claude/skills`` so every agent finds them, kept
        out of Git, and read-only in the admin UI.
        """
        return None

    def triggers(self) -> list[AbstractSkillTrigger]:
        return []
