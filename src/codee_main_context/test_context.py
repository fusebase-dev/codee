import json
import tempfile
import unittest
from pathlib import Path

from codee_main_context.context import (
    Settings, TasksProvider, codee_issue_types, load_settings, save_settings,
    work_item_types)


class WorkItemTypesTest(unittest.TestCase):
    def test_an_unconfigured_provider_falls_back_to_its_own_defaults(self) -> None:
        # The two backends disagree on what a story is called, so the default
        # has to come from the provider rather than from one shared pair.
        self.assertEqual(work_item_types(Settings()),
                         {"story": "Story", "task": "Task"})
        self.assertEqual(
            work_item_types(Settings(tasks_provider=TasksProvider.AZURE_DEVOPS)),
            {"story": "User Story", "task": "Task"})

    def test_the_named_provider_wins_over_the_selected_one(self) -> None:
        # The settings page reads back the mapping it kept for the provider the
        # user just switched to, which is not the one still in the settings.
        settings = Settings(tasks_provider=TasksProvider.JIRA)

        self.assertEqual(
            work_item_types(settings, TasksProvider.AZURE_DEVOPS)["story"],
            "User Story")

    def test_stored_values_override_the_defaults_and_extra_rows_follow(self) -> None:
        settings = Settings(work_item_types={"jira": {
            "story": "Epic", "bug": "Bug"}})

        # story is repointed, task keeps its default, bug comes last.
        self.assertEqual(work_item_types(settings),
                         {"story": "Epic", "task": "Task", "bug": "Bug"})

    def test_the_mandatory_work_items_survive_a_file_that_drops_them(self) -> None:
        # Hand-edited or written before this setting existed: without the
        # defaults filling back in, every story skill would match nothing.
        settings = Settings(work_item_types={"jira": {"bug": "Bug"}})

        self.assertEqual(codee_issue_types(settings), ("story", "task", "bug"))

    def test_names_are_lower_cased_and_half_filled_rows_are_dropped(self) -> None:
        settings = Settings(work_item_types={"jira": {
            "story": "Story", "task": "Task",
            "Defect": " Bug ", "nameless": "", "": "Orphan"}})

        self.assertEqual(work_item_types(settings), {
            "story": "Story", "task": "Task", "defect": "Bug"})


class SettingsFileTest(unittest.TestCase):
    def test_the_mapping_survives_a_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            mapping = {"azure_devops": {"story": "Product Backlog Item",
                                        "task": "Task"}}
            save_settings(directory, Settings(work_item_types=mapping))

            self.assertEqual(load_settings(directory).work_item_types, mapping)

    def test_a_settings_file_written_before_the_setting_existed_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "settings.json").write_text(json.dumps({
                "tasks_provider": "jira", "coding_agent": "claude_code",
                "credentials": {}, "max_parallel_agents": 3}))

            settings = load_settings(directory)

            self.assertEqual(settings.work_item_types, {})
            self.assertEqual(codee_issue_types(settings), ("story", "task"))


if __name__ == "__main__":
    unittest.main()
