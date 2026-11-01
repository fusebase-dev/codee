import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codee_main_context.context import CodeeMainContext, Settings
from codee_plugin_abstract.plugin import (
    AbstractCodeePlugin, AbstractSkillTrigger, PluginField)

from codee.lib import plugins as plugins_module
from codee.lib.plugins import (
    GITIGNORE_START, PluginSource, active_plugins, build_plugins,
    find_plugin_triggered_skills, sync_plugin_skills, trigger_plugin_skills)


class WebhookTrigger(AbstractSkillTrigger):
    TYPE = "webhook"
    SKILL_FIELDS = [PluginField("x-acme-path", "Path")]

    def __init__(self) -> None:
        self.ticks: list[tuple] = []

    def tick(self, skills, run_skill, settings) -> None:
        self.ticks.append((skills, settings))
        for skill in skills:
            run_skill(skill, f"{skill.body} {skill.fields['x-acme-path']}")


def _plugin(name: str = "acme", skills: Path | None = None,
            triggers: list | None = None) -> type:
    class Plugin(AbstractCodeePlugin):
        NAME = name

        def skills_dir(self):
            return skills

        def triggers(self):
            return triggers or []
    return Plugin


def _skill(root: Path, slug: str, frontmatter: str = "") -> Path:
    directory = root / slug
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {slug}\n{frontmatter}---\nBody of {slug}\n")
    return directory


class BuildPluginsTest(unittest.TestCase):
    def test_a_broken_or_clashing_plugin_is_reported_and_skipped(self) -> None:
        def broken():
            raise ImportError("no module named acme_extra")

        class Builtin(AbstractSkillTrigger):
            TYPE = "cron"

        loaded = build_plugins([
            lambda: _plugin("acme", triggers=[WebhookTrigger()]),
            broken,
            lambda: _plugin("acme"),
            lambda: _plugin("other", triggers=[Builtin(), WebhookTrigger()]),
        ])

        self.assertEqual([plugin.NAME for plugin in loaded.plugins],
                         ["acme", "other"])
        self.assertEqual(list(loaded.triggers), ["webhook"])
        self.assertEqual(loaded.triggers["webhook"][0].NAME, "acme")
        self.assertEqual(len(loaded.errors), 4)

    def test_records_the_package_and_version(self) -> None:
        loaded = build_plugins(
            [PluginSource(lambda: _plugin("acme"), "codee-acme", "1.2.0")])

        self.assertEqual(loaded.packages["acme"], ("codee-acme", "1.2.0"))

    def test_a_disabled_plugin_drops_out_with_its_triggers(self) -> None:
        loaded = build_plugins([
            lambda: _plugin("acme", triggers=[WebhookTrigger()]),
            lambda: _plugin("other")])

        active = active_plugins(loaded, ["acme"])

        self.assertEqual([plugin.NAME for plugin in active.plugins], ["other"])
        self.assertEqual(active.triggers, {})


class SyncPluginSkillsTest(unittest.TestCase):
    def test_links_plugin_skills_and_keeps_them_out_of_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "plugin-skills"
            _skill(source, "acme-review")
            skills_dir = root / ".claude" / "skills"
            loaded = build_plugins([lambda: _plugin(skills=source)])

            installed, errors = sync_plugin_skills(skills_dir, loaded)

            self.assertEqual(installed, {"acme-review": "acme"})
            self.assertEqual(errors, [])
            link = skills_dir / "acme-review"
            self.assertTrue(link.is_symlink())
            self.assertTrue((link / "SKILL.md").is_file())
            gitignore = (skills_dir / ".gitignore").read_text()
            self.assertIn(GITIGNORE_START, gitignore)
            self.assertIn("/acme-review", gitignore)

    def test_a_local_skill_of_the_same_name_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "plugin-skills"
            _skill(source, "review")
            skills_dir = root / ".claude" / "skills"
            _skill(skills_dir, "review")
            loaded = build_plugins([lambda: _plugin(skills=source)])

            installed, errors = sync_plugin_skills(skills_dir, loaded)

            self.assertEqual(installed, {})
            self.assertFalse((skills_dir / "review").is_symlink())
            self.assertIn("local skill", errors[0])

    def test_a_removed_plugin_takes_its_skills_with_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "plugin-skills"
            _skill(source, "acme-review")
            skills_dir = root / ".claude" / "skills"
            (skills_dir).mkdir(parents=True)
            (skills_dir / ".gitignore").write_text("notes.txt\n")
            sync_plugin_skills(
                skills_dir, build_plugins([lambda: _plugin(skills=source)]))

            installed, _ = sync_plugin_skills(skills_dir, build_plugins([]))

            self.assertEqual(installed, {})
            self.assertFalse((skills_dir / "acme-review").exists())
            # Only Codee's own block goes; the user's lines stay.
            self.assertEqual((skills_dir / ".gitignore").read_text(),
                             "notes.txt\n")


class TriggerPluginSkillsTest(unittest.TestCase):
    def test_runs_the_skills_that_name_the_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skills_dir = Path(directory)
            _skill(skills_dir, "on-hook",
                   "disable-model-invocation: true\nx-codee-trigger: webhook\n"
                   "x-acme-path: /deploy\nmodel: opus\n")
            _skill(skills_dir, "not-disabled",
                   "x-codee-trigger: webhook\nx-acme-path: /x\n")
            _skill(skills_dir, "cron",
                   "disable-model-invocation: true\nx-codee-trigger: cron\n")
            trigger = WebhookTrigger()
            loaded = build_plugins(
                [lambda: _plugin(triggers=[trigger])])
            context = CodeeMainContext(data_dir=skills_dir, settings=Settings(
                plugin_settings={"acme": {"token": "secret"}}))
            run_agent = Mock(return_value="done")

            with patch.object(plugins_module.runs_db, "record_run") as record:
                trigger_plugin_skills(run_agent, skills_dir=skills_dir,
                                      main_context=context, plugins=loaded)

            skills, settings = trigger.ticks[0]
            self.assertEqual([skill.slug for skill in skills], ["on-hook"])
            self.assertEqual(settings, {"token": "secret"})
            self.assertEqual(run_agent.call_args.args[0],
                             "Body of on-hook /deploy")
            self.assertEqual(run_agent.call_args.args[2], "opus")
            self.assertEqual(record.call_args.args[:2], ("on-hook", "webhook"))
            self.assertEqual(record.call_args.args[3], "succeeded")

    def test_a_disabled_plugin_is_not_ticked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skills_dir = Path(directory)
            _skill(skills_dir, "on-hook",
                   "disable-model-invocation: true\nx-codee-trigger: webhook\n")
            trigger = WebhookTrigger()
            loaded = build_plugins([lambda: _plugin(triggers=[trigger])])
            context = CodeeMainContext(data_dir=skills_dir, settings=Settings(
                disabled_plugins=["acme"]))
            run_agent = Mock()

            trigger_plugin_skills(run_agent, skills_dir=skills_dir,
                                  main_context=context, plugins=loaded)

            self.assertEqual(trigger.ticks, [])
            run_agent.assert_not_called()

    def test_a_failing_trigger_does_not_stop_the_tick(self) -> None:
        class Broken(AbstractSkillTrigger):
            TYPE = "broken"

            def tick(self, skills, run_skill, settings):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as directory:
            skills_dir = Path(directory)
            _skill(skills_dir, "b",
                   "disable-model-invocation: true\nx-codee-trigger: broken\n")
            loaded = build_plugins([lambda: _plugin(triggers=[Broken()])])

            trigger_plugin_skills(Mock(), skills_dir=skills_dir,
                                  main_context=CodeeMainContext(
                                      data_dir=skills_dir),
                                  plugins=loaded)

    def test_a_picker_field_reaches_the_trigger_without_its_labels(self) -> None:
        class Picker(AbstractSkillTrigger):
            TYPE = "picker"
            SKILL_FIELDS = [PluginField("x-acme-rooms", "Rooms",
                                        searchable=True)]

        with tempfile.TemporaryDirectory() as directory:
            skills_dir = Path(directory)
            _skill(skills_dir, "rooms",
                   "disable-model-invocation: true\nx-codee-trigger: picker\n"
                   "x-acme-rooms: |-\n  r1  # Lobby\n  https://x/r2#a\n")

            skills = find_plugin_triggered_skills(skills_dir, Picker())

            self.assertEqual(skills[0].fields, {"x-acme-rooms":
                                                "r1\nhttps://x/r2#a"})

    def test_a_field_the_skill_leaves_out_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skills_dir = Path(directory)
            _skill(skills_dir, "hook",
                   "disable-model-invocation: true\nx-codee-trigger: Webhook\n")

            skills = find_plugin_triggered_skills(skills_dir, WebhookTrigger())

            self.assertEqual(skills[0].fields, {"x-acme-path": ""})


if __name__ == "__main__":
    unittest.main()


class PluginSchedulerTest(unittest.TestCase):
    def _scheduler(self, *triggers, paused=False):
        from codee.lib.plugins import PluginScheduler
        self.now = 1000.0
        loaded = build_plugins([lambda: _plugin(triggers=list(triggers))])
        return PluginScheduler(Mock(), Path("."), CodeeMainContext(
            data_dir=Path(".")), lambda: paused, plugins=loaded,
            clock=lambda: self.now)

    def test_each_trigger_is_due_at_its_own_interval(self) -> None:
        class Fast(AbstractSkillTrigger):
            TYPE = "fast"
            POLL_SECONDS = 30

        class Slow(AbstractSkillTrigger):
            TYPE = "slow"

        scheduler = self._scheduler(Fast(), Slow())
        seen = []
        for second in range(0, 121, 1):
            self.now = 1000.0 + second
            seen.extend(f"{kind}@{second}" for kind in sorted(scheduler.due()))

        self.assertEqual(seen, ["fast@0", "slow@0", "fast@30", "fast@60",
                                "slow@60", "fast@90", "fast@120", "slow@120"])

    def test_nothing_is_ticked_while_paused_and_the_floor_holds(self) -> None:
        from codee.lib.plugins import poll_seconds

        class Eager(AbstractSkillTrigger):
            TYPE = "eager"
            POLL_SECONDS = 1

        scheduler = self._scheduler(Eager(), paused=True)
        with patch.object(plugins_module, "trigger_plugin_skills") as tick:
            scheduler.tick()

        tick.assert_not_called()
        self.assertEqual(poll_seconds(Eager()), 10)
