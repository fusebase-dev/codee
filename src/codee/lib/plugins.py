"""Loading Codee plugins, installing their skills, and running their triggers."""
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable

import yaml

from codee_main_context.context import CodeeMainContext, data_dir
from codee_main_context.logging import get_logger
from codee_plugin_abstract.plugin import (
    AbstractCodeePlugin, AbstractSkillTrigger, RunKilled, TriggeredSkill,
    parse_field_options)

from codee.lib import runs_db

log = get_logger(__name__)

ENTRY_POINT_GROUP = "codee.plugins"
# The trigger types Codee runs itself; a plugin trigger may not take one.
BUILT_IN_TRIGGERS = ("issue", "cron", "email", "aws-sqs")
# Where the skills a plugin installs are listed so Git leaves them alone. The
# block is the record of which links Codee made, so a skill whose plugin is
# gone is removed rather than left pointing nowhere.
GITIGNORE_START = "# >>> codee plugin skills (managed by Codee, do not edit)"
GITIGNORE_END = "# <<< codee plugin skills"

RunAgent = Callable[..., str]
# The shortest interval a trigger may ask to be ticked at.
MIN_POLL_SECONDS = 10


@dataclass(frozen=True)
class PluginSource:
    """Where a plugin class comes from: its loader and the package shipping it."""
    load: Callable[[], type]
    package: str = ""
    version: str = ""


@dataclass
class LoadedPlugins:
    plugins: list[AbstractCodeePlugin] = field(default_factory=list)
    # Plugin name -> (package, version) it was installed from.
    packages: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Trigger type -> (plugin, trigger), for the types the plugins add.
    triggers: dict[str, tuple[AbstractCodeePlugin, AbstractSkillTrigger]
                   ] = field(default_factory=dict)
    # Why a plugin, a trigger or a skill was left out, for the Plugins page.
    errors: list[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def load_plugins() -> LoadedPlugins:
    """Every installed plugin, read once per process.

    Installing or removing one is a ``uv add``/``uv remove`` and a restart, so
    there is nothing to pick up between ticks.
    """
    return build_plugins([
        PluginSource(entry.load,
                     entry.dist.name if entry.dist else "",
                     entry.dist.version if entry.dist else "")
        for entry in entry_points(group=ENTRY_POINT_GROUP)],
        data_root=data_dir() / "plugins")


def build_plugins(
    sources: list[PluginSource | Callable[[], type]],
    data_root: Path | None = None,
) -> LoadedPlugins:
    """Instantiate plugins and check what they declare.

    Each source loads a plugin class; a bare callable is a source with no
    package to name. Each plugin gets ``data_root/<NAME>`` as its
    ``data_dir``. One that fails to import or declares a name or trigger
    already taken is reported and skipped: a broken plugin must not stop Codee
    or the plugins that work.
    """
    loaded = LoadedPlugins()
    names: set[str] = set()
    for source in sources:
        if not isinstance(source, PluginSource):
            source = PluginSource(source)
        try:
            plugin = source.load()()
        except Exception as exc:
            loaded.errors.append(f"A plugin failed to load: {exc}")
            continue
        if not isinstance(plugin, AbstractCodeePlugin):
            loaded.errors.append(
                f"{type(plugin).__name__} is not an AbstractCodeePlugin")
            continue
        name = plugin.NAME.strip()
        if not name or name in names:
            loaded.errors.append(
                f"{type(plugin).__name__}: plugin name {name!r} is "
                f"{'empty' if not name else 'already taken'}")
            continue
        names.add(name)
        if data_root is not None:
            plugin.data_dir = data_root / name
        loaded.plugins.append(plugin)
        loaded.packages[name] = (source.package, source.version)
        try:
            triggers = plugin.triggers()
        except Exception as exc:
            loaded.errors.append(f"{name}: triggers() failed: {exc}")
            continue
        for trigger in triggers:
            kind = trigger.TYPE.strip().lower()
            if not kind or kind in BUILT_IN_TRIGGERS or kind in loaded.triggers:
                loaded.errors.append(
                    f"{name}: trigger type {kind!r} is "
                    f"{'empty' if not kind else 'already taken'}")
                continue
            loaded.triggers[kind] = (plugin, trigger)
    for error in loaded.errors:
        log.error("%s", error)
    return loaded


def active_plugins(plugins: LoadedPlugins,
                   disabled: list[str]) -> LoadedPlugins:
    """The plugins that are not turned off, with only their triggers.

    Load errors stay with the result: a broken plugin is worth reporting
    whether or not anyone has turned it off.
    """
    off = set(disabled)
    return LoadedPlugins(
        plugins=[plugin for plugin in plugins.plugins if plugin.NAME not in off],
        packages=plugins.packages,
        triggers={kind: entry for kind, entry in plugins.triggers.items()
                  if entry[0].NAME not in off},
        errors=plugins.errors)


def plugin_skill_sources(
    plugins: LoadedPlugins,
) -> tuple[dict[str, tuple[str, Path]], list[str]]:
    """slug -> (plugin name, skill directory), plus slugs two plugins share."""
    sources: dict[str, tuple[str, Path]] = {}
    errors: list[str] = []
    for plugin in plugins.plugins:
        try:
            directory = plugin.skills_dir()
        except Exception as exc:
            errors.append(f"{plugin.NAME}: skills_dir() failed: {exc}")
            continue
        if directory is None or not Path(directory).is_dir():
            continue
        for path in sorted(Path(directory).glob("*/SKILL.md")):
            slug = path.parent.name
            if slug in sources:
                errors.append(f"{plugin.NAME}: skill {slug} is already "
                              f"installed by {sources[slug][0]}")
                continue
            sources[slug] = (plugin.NAME, path.parent.resolve())
    return sources, errors


def sync_plugin_skills(skills_dir: Path,
                       plugins: LoadedPlugins) -> tuple[dict[str, str], list[str]]:
    """Link each plugin skill into ``skills_dir`` and drop links no longer wanted.

    Returns slug -> plugin name for the skills now installed, plus why any were
    not. A local skill of the same name wins: it is the user's file, and a
    plugin must never replace it. Safe to call from the executor and the admin
    at the same time; whichever gets there first makes the link.
    """
    sources, errors = plugin_skill_sources(plugins)
    skills_dir.mkdir(parents=True, exist_ok=True)
    previous = _managed_slugs(skills_dir)
    installed: dict[str, str] = {}
    for slug, (plugin_name, source) in sources.items():
        link = skills_dir / slug
        if link.is_symlink() and slug in previous:
            if link.resolve() != source:
                link.unlink(missing_ok=True)
        elif link.is_symlink() and link.resolve() == source:
            pass
        elif link.exists() or link.is_symlink():
            errors.append(f"{plugin_name}: skill {slug} is not installed "
                          "because a local skill has the same name")
            continue
        try:
            if not link.is_symlink():
                os.symlink(source, link, target_is_directory=True)
        except FileExistsError:
            pass
        except OSError as exc:
            errors.append(f"{plugin_name}: could not install {slug}: {exc}")
            continue
        installed[slug] = plugin_name
    for slug in previous - set(installed):
        link = skills_dir / slug
        if link.is_symlink():
            link.unlink(missing_ok=True)
    _write_managed_slugs(skills_dir, sorted(installed))
    return installed, errors


def _managed_slugs(skills_dir: Path) -> set[str]:
    lines = _gitignore(skills_dir).splitlines()
    if GITIGNORE_START not in lines or GITIGNORE_END not in lines:
        return set()
    block = lines[lines.index(GITIGNORE_START) + 1:lines.index(GITIGNORE_END)]
    return {line.strip().strip("/") for line in block if line.strip()}


def _write_managed_slugs(skills_dir: Path, slugs: list[str]) -> None:
    lines = _gitignore(skills_dir).splitlines()
    if GITIGNORE_START in lines and GITIGNORE_END in lines:
        lines = (lines[:lines.index(GITIGNORE_START)]
                 + lines[lines.index(GITIGNORE_END) + 1:])
    if slugs:
        lines += [GITIGNORE_START, *(f"/{slug}" for slug in slugs),
                  GITIGNORE_END]
    path = skills_dir / ".gitignore"
    content = "\n".join(lines) + "\n" if lines else ""
    if content == _gitignore(skills_dir):
        return
    if content:
        path.write_text(content)
    else:
        path.unlink(missing_ok=True)


def _gitignore(skills_dir: Path) -> str:
    path = skills_dir / ".gitignore"
    return path.read_text() if path.exists() else ""


def find_plugin_triggered_skills(
    skills_dir: Path, trigger: AbstractSkillTrigger,
) -> list[TriggeredSkill]:
    """The skills whose ``x-codee-trigger`` names ``trigger``.

    Held to the rule every triggered skill follows: one without
    ``disable-model-invocation: true`` is skipped and logged.
    """
    kind = trigger.TYPE.strip().lower()
    skills: list[TriggeredSkill] = []
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            metadata, body = _parse_skill(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            log.error("Failed to read %s: %s", path, exc)
            continue
        if str(metadata.get("x-codee-trigger", "")).strip().lower() != kind:
            continue
        if metadata.get("disable-model-invocation") is not True:
            log.error("%s declares x-codee-trigger: %s but is missing "
                      "disable-model-invocation: true; skipping.", path, kind)
            continue
        skills.append(TriggeredSkill(
            slug=path.parent.name,
            name=str(metadata.get("name") or path.parent.name).strip(),
            path=path,
            body=body.strip(),
            fields={item.key: _field_value(item, metadata.get(item.key))
                    for item in trigger.SKILL_FIELDS},
            model=_text(metadata.get("model")),
            agent=_text(metadata.get("x-codee-agent")),
            effort=_text(metadata.get("effort")),
            context_tier=_text(metadata.get("x-codee-context-tier")),
        ))
    return skills


def poll_seconds(trigger: AbstractSkillTrigger) -> int:
    try:
        seconds = int(trigger.POLL_SECONDS)
    except (TypeError, ValueError):
        seconds = 60
    return max(MIN_POLL_SECONDS, seconds)


def trigger_plugin_skills(run_agent: RunAgent, *, skills_dir: Path,
                          main_context: CodeeMainContext,
                          plugins: LoadedPlugins | None = None,
                          due: set[str] | None = None) -> None:
    """One tick of every enabled plugin trigger that has a skill to drive.

    ``due`` limits it to those trigger types, for the scheduler that ticks
    each at its own interval; None ticks them all.
    """
    plugins = active_plugins(
        plugins if plugins is not None else load_plugins(),
        main_context.settings.disabled_plugins)
    for kind, (plugin, trigger) in plugins.triggers.items():
        if due is not None and kind not in due:
            continue
        skills = find_plugin_triggered_skills(skills_dir, trigger)
        if not skills:
            continue
        settings = dict(
            main_context.settings.plugin_settings.get(plugin.NAME, {}))
        try:
            trigger.tick(skills, _skill_runner(run_agent, kind, main_context),
                         settings)
        except Exception as exc:
            # One plugin's bug costs that plugin its tick, nothing more.
            log.exception("Plugin %s trigger %s failed: %s",
                          plugin.NAME, kind, exc)


class PluginScheduler:
    """Ticks each plugin trigger at its own POLL_SECONDS, off the main loop.

    The executor's loop ticks every 60 seconds and does much more each time
    (git pull, the tasks provider, cron, email, SQS); a trigger that wants to
    poll faster can't wait on it. Skips while Codee is paused, like the loop.
    """

    def __init__(self, run_agent: RunAgent, skills_dir: Path,
                 main_context: CodeeMainContext,
                 paused: Callable[[], bool],
                 plugins: LoadedPlugins | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.run_agent = run_agent
        self.skills_dir = skills_dir
        self.main_context = main_context
        self.paused = paused
        self.plugins = plugins
        self.clock = clock
        self._next: dict[str, float] = {}

    def due(self) -> set[str]:
        """The trigger types whose time has come, scheduling their next."""
        plugins = self.plugins if self.plugins is not None else load_plugins()
        now = self.clock()
        due = set()
        for kind, (_, trigger) in plugins.triggers.items():
            if self._next.get(kind, 0.0) <= now:
                due.add(kind)
                self._next[kind] = now + poll_seconds(trigger)
        return due

    def tick(self) -> None:
        if self.paused():
            return
        due = self.due()
        if due:
            trigger_plugin_skills(self.run_agent, skills_dir=self.skills_dir,
                                  main_context=self.main_context,
                                  plugins=self.plugins, due=due)

    def run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:
                log.exception("Plugin triggers failed: %s", exc)
            time.sleep(1)

    def start(self) -> None:
        threading.Thread(target=self.run_forever, name="plugin-triggers",
                         daemon=True).start()


def _skill_runner(run_agent: RunAgent, kind: str,
                  main_context: CodeeMainContext):
    def run_skill(skill: TriggeredSkill, prompt: str = "") -> str:
        prompt = prompt or skill.body
        session_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            response = run_agent(prompt, session_id, skill.model, skill.agent,
                                 effort=skill.effort,
                                 context_tier=skill.context_tier)
        except runs_db.JobKilled as exc:
            runs_db.record_run(skill.name, kind, session_id, "killed",
                               error=str(exc), started_at=started_at,
                               message=prompt, user_message=prompt,
                               main_context=main_context)
            raise RunKilled(str(exc)) from exc
        except Exception as exc:
            runs_db.record_run(skill.name, kind, session_id, "failed",
                               error=str(exc)[:500], started_at=started_at,
                               message=prompt, user_message=prompt,
                               main_context=main_context)
            raise
        runs_db.record_run(skill.name, kind, session_id, "succeeded",
                           started_at=started_at, message=prompt,
                           user_message=prompt, response=response,
                           main_context=main_context)
        return response
    return run_skill


def _parse_skill(contents: str) -> tuple[dict, str]:
    if not contents.startswith("---"):
        return {}, contents
    parts = contents.split("---", 2)
    if len(parts) < 3:
        return {}, contents
    parsed = yaml.safe_load(parts[1]) or {}
    return (parsed if isinstance(parsed, dict) else {}), parts[2]


def _field_value(item, value: object) -> str:
    """What the trigger gets for a field: a picker's values, one per line."""
    if item.searchable:
        return "\n".join(option.value
                         for option in parse_field_options(_text(value)))
    return _text(value)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()
