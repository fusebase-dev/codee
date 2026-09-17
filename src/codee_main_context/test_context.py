import json
import tempfile
import unittest
from pathlib import Path

from codee_main_context.context import (
    Settings, TasksProvider, WorkItemMapping, codee_issue_types,
    codee_work_items, load_settings, save_settings, task_filter,
    work_item_mappings)


def _types(settings: Settings,
           provider: TasksProvider | None = None) -> dict[str, list[str]]:
    """The mappings as "work item -> its types", which most of these assert on."""
    return {mapping.name: list(mapping.types)
            for mapping in work_item_mappings(settings, provider)}


class WorkItemMappingsTest(unittest.TestCase):
    def test_an_unconfigured_provider_falls_back_to_its_own_defaults(self) -> None:
        # The two backends disagree on what a story is called, so the default
        # has to come from the provider rather than from one shared pair.
        self.assertEqual(_types(Settings()),
                         {"story": ["Story"], "task": ["Task"]})
        self.assertEqual(
            _types(Settings(tasks_provider=TasksProvider.AZURE_DEVOPS)),
            {"story": ["User Story"], "task": ["Task"]})

    def test_the_named_provider_wins_over_the_selected_one(self) -> None:
        # The settings page reads back the mapping it kept for the provider the
        # user just switched to, which is not the one still in the settings.
        settings = Settings(tasks_provider=TasksProvider.JIRA)

        self.assertEqual(_types(settings, TasksProvider.AZURE_DEVOPS)["story"],
                         ["User Story"])

    def test_stored_values_override_the_defaults_and_extra_rows_follow(self) -> None:
        settings = Settings(work_item_types={"jira": {
            "story": ["Epic"], "bug": ["Bug"]}})

        # story is repointed, task keeps its default, bug comes last.
        self.assertEqual(_types(settings),
                         {"story": ["Epic"], "task": ["Task"], "bug": ["Bug"]})

    def test_one_work_item_can_be_polled_as_several_provider_types(self) -> None:
        settings = Settings(work_item_types={"jira": {
            "story": ["Story"], "task": ["Task", "Bug", "Task"]}})

        # The repeat is dropped: it would only name the same type twice in the
        # query the provider builds from this.
        self.assertEqual(_types(settings),
                         {"story": ["Story"], "task": ["Task", "Bug"]})

    def test_a_single_type_saved_before_lists_existed_still_reads(self) -> None:
        # Every settings.json written before this setting took a list holds a
        # bare string, and upgrading Codee must not empty the mapping.
        settings = Settings(work_item_types={"jira": {
            "story": "Epic", "task": "Task"}})

        self.assertEqual(_types(settings),
                         {"story": ["Epic"], "task": ["Task"]})

    def test_the_mandatory_work_items_survive_a_file_that_drops_them(self) -> None:
        # Hand-edited or written before this setting existed: without the
        # defaults filling back in, every story skill would match nothing.
        settings = Settings(work_item_types={"jira": {"bug": "Bug"}})

        self.assertEqual(codee_issue_types(settings), ("story", "task", "bug"))

    def test_names_are_lower_cased_and_half_filled_rows_are_dropped(self) -> None:
        settings = Settings(work_item_types={"jira": {
            "story": ["Story"], "task": ["Task"],
            "Defect": [" Bug ", "  "], "nameless": [], "": ["Orphan"]}})

        self.assertEqual(_types(settings), {
            "story": ["Story"], "task": ["Task"], "defect": ["Bug"]})

    def test_a_query_is_how_a_work_item_says_it_another_way(self) -> None:
        settings = Settings(
            work_item_types={"jira": {"story": ["Story"], "task": ["Task"]}},
            work_item_queries={"jira": {"task": ' labels = "codee" '}})

        task = work_item_mappings(settings)[1]

        self.assertTrue(task.is_query)
        self.assertEqual(task.query, 'labels = "codee"')
        # Kept, unused, so switching the row back offers them again.
        self.assertEqual(task.types, ("Task",))

    def test_a_work_item_that_is_only_a_query_is_still_polled(self) -> None:
        # Nothing put it in the type mapping, but it is a work item all the
        # same — and a skill has to be able to declare it.
        settings = Settings(work_item_queries={"jira": {
            "bug": 'issuetype = Bug AND labels = "codee"'}})

        self.assertEqual(codee_issue_types(settings), ("story", "task", "bug"))
        self.assertTrue(work_item_mappings(settings)[2].is_query)

    def test_a_row_with_neither_types_nor_a_query_is_dropped(self) -> None:
        settings = Settings(work_item_types={"jira": {
            "story": ["Story"], "task": ["Task"], "ghost": []}})

        self.assertEqual(codee_issue_types(settings), ("story", "task"))


class CodeeWorkItemsTest(unittest.TestCase):
    """How a provider reads back an item no query of its own asked for."""

    MAPPINGS = [
        WorkItemMapping(name="story", types=("Story",)),
        WorkItemMapping(name="task", types=("Task", "Bug")),
        WorkItemMapping(name="defect", types=("Bug",),
                        query='labels = "codee"'),
    ]

    def test_each_type_names_the_work_item_it_was_mapped_to(self) -> None:
        # The queried work item contributes nothing: the types its condition
        # matches are the query's business, not something Codee can read off.
        self.assertEqual(codee_work_items(self.MAPPINGS),
                         {"story": "story", "task": "task", "bug": "task"})


class TaskFilterTest(unittest.TestCase):
    """The extra clause the task query is narrowed by, when there is one."""

    def test_nothing_is_configured_by_default(self) -> None:
        # Every installation that never opens this setting has to keep the
        # query it had before the setting existed.
        self.assertEqual(task_filter(Settings()), "")

    def test_the_filter_is_read_for_the_named_provider(self) -> None:
        # JQL means nothing to Azure DevOps, so the two are kept apart.
        settings = Settings(task_filters={
            "jira": 'labels = "codee"',
            "azure_devops": "[System.Tags] CONTAINS 'codee'"})

        self.assertEqual(task_filter(settings), 'labels = "codee"')
        self.assertEqual(task_filter(settings, TasksProvider.AZURE_DEVOPS),
                         "[System.Tags] CONTAINS 'codee'")

    def test_a_leading_and_is_dropped(self) -> None:
        # Writing the clause the way it will be joined is the obvious mistake,
        # and the doubled keyword comes back as a syntax error naming neither
        # this setting nor the word it objects to.
        settings = Settings(task_filters={"jira": '  AND labels = "codee" '})

        self.assertEqual(task_filter(settings), 'labels = "codee"')

    def test_a_field_that_merely_starts_with_and_survives(self) -> None:
        settings = Settings(task_filters={"jira": 'android = "yes"'})

        self.assertEqual(task_filter(settings), 'android = "yes"')

    def test_a_blank_filter_is_no_filter(self) -> None:
        settings = Settings(task_filters={"jira": "   "})

        self.assertEqual(task_filter(settings), "")


class SettingsFileTest(unittest.TestCase):
    def test_the_mapping_survives_a_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            mapping = {"azure_devops": {"story": ["Product Backlog Item"],
                                        "task": ["Task", "Bug"]}}
            queries = {"azure_devops": {"bug": "[System.Tags] CONTAINS 'x'"}}
            save_settings(directory, Settings(work_item_types=mapping,
                                              work_item_queries=queries))

            stored = load_settings(directory)
            self.assertEqual(stored.work_item_types, mapping)
            self.assertEqual(stored.work_item_queries, queries)

    def test_the_task_filter_survives_a_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            filters = {"jira": 'labels = "codee"'}
            save_settings(directory, Settings(task_filters=filters))

            self.assertEqual(load_settings(directory).task_filters, filters)

    def test_a_settings_file_written_before_the_setting_existed_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "settings.json").write_text(json.dumps({
                "tasks_provider": "jira", "coding_agent": "claude_code",
                "credentials": {}, "max_parallel_agents": 3}))

            settings = load_settings(directory)

            self.assertEqual(settings.work_item_types, {})
            self.assertEqual(settings.task_filters, {})
            self.assertEqual(task_filter(settings), "")
            self.assertEqual(codee_issue_types(settings), ("story", "task"))
            # Rotation stays off for an installation that predates it, so the
            # credentials file it is signed in with is left alone.
            self.assertFalse(settings.claude_code_rotate_keys)


class ClaudeCodeRotationSettingTest(unittest.TestCase):
    """The switch that decides whether Codee writes the credentials file at all."""

    def test_rotation_is_off_by_default(self) -> None:
        self.assertFalse(Settings().claude_code_rotate_keys)

    def test_the_switch_survives_a_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            save_settings(directory, Settings(claude_code_rotate_keys=True))

            self.assertTrue(load_settings(directory).claude_code_rotate_keys)

    def test_no_account_credentials_are_written_to_settings(self) -> None:
        # The accounts are completed sign-ins — access and refresh tokens —
        # and belong in SQLite with the other OAuth credentials, not in a file
        # the admin UI rewrites on every save.
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            save_settings(directory, Settings(claude_code_rotate_keys=True))

            stored = json.loads((directory / "settings.json").read_text())

            self.assertEqual(
                [key for key in stored if key.startswith("claude_code")],
                ["claude_code_rotate_keys"])


if __name__ == "__main__":
    unittest.main()
