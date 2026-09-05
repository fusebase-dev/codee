import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codee.lib.trigger_issue_skills import (
    configured_issue_types,
    find_issue_triggered_skills,
    issue_statuses,
    match_issue_skill,
)
from codee_main_context.context import Settings, save_settings


class IssueTriggeredSkillsTest(unittest.TestCase):
    def _skill(self, root: Path, slug: str, frontmatter: str) -> None:
        directory = root / slug
        directory.mkdir()
        (directory / "SKILL.md").write_text(f"---\n{frontmatter}---\nBody\n")

    def test_reads_the_model_frontmatter_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._skill(
                root, "with-model",
                "name: With model\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: story\nmodel: claude-opus-5\n",
            )
            self._skill(
                root, "without-model",
                "name: Without model\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: task\n",
            )

            models = {skill.slug: skill.model
                      for skill in find_issue_triggered_skills(root)}

            self.assertEqual(models,
                             {"with-model": "claude-opus-5", "without-model": ""})

    def test_loads_valid_issue_type_and_rejects_invalid_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._skill(
                root,
                "valid",
                "name: Valid\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\n"
                "x-codee-issue-status: [Ready, Custom status]\n"
                "x-codee-issue-type: story\n",
            )
            self._skill(
                root,
                "invalid",
                "name: Invalid\nx-codee-trigger: issue\n"
                "x-codee-issue-status: [Ready]\n",
            )
            self._skill(
                root,
                "invalid-type",
                "name: Invalid type\ndisable-model-invocation: true\n"
                "x-codee-trigger: issue\nx-codee-issue-status: [Ready]\n"
                "x-codee-issue-type: bug\n",
            )

            skills = find_issue_triggered_skills(root)

            self.assertEqual([skill.slug for skill in skills], ["valid"])
            self.assertEqual(issue_statuses(skills), [
                             "Ready", "Custom status"])
            self.assertEqual(match_issue_skill(
                skills, "custom STATUS", "Story").slug, "valid")

    def test_matches_only_the_requested_issue_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common = (
                "disable-model-invocation: true\nx-codee-trigger: issue\n"
                "x-codee-issue-status: ['In Progress']\n"
            )
            self._skill(root, "story", f"{common}x-codee-issue-type: story\n")
            self._skill(root, "task", f"{common}x-codee-issue-type: task\n")
            skills = find_issue_triggered_skills(root)

            self.assertEqual(match_issue_skill(
                skills, "in progress", "Story").slug, "story")
            self.assertEqual(match_issue_skill(
                skills, "in progress", "Task").slug, "task")
            self.assertIsNone(match_issue_skill(
                skills, "in progress", "Bug"))


class ConfiguredIssueTypesTest(unittest.TestCase):
    """A work item added in Settings is one a skill may trigger on."""

    def _skill(self, root: Path, slug: str, issue_type: str) -> None:
        directory = root / slug
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            "---\ndisable-model-invocation: true\nx-codee-trigger: issue\n"
            "x-codee-issue-status: [Ready]\n"
            f"x-codee-issue-type: {issue_type}\n---\nBody\n")

    def test_a_skill_for_a_configured_work_item_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._skill(root, "triage", "bug")
            self._skill(root, "review", "epic")

            skills = find_issue_triggered_skills(root, ("story", "task", "bug"))

            self.assertEqual([skill.slug for skill in skills], ["triage"])
            self.assertEqual(
                match_issue_skill(skills, "ready", "Bug").slug, "triage")

    def test_the_settings_file_decides_when_nothing_is_passed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._skill(root, "triage", "bug")
            save_settings(root, Settings(work_item_types={
                "jira": {"story": "Story", "task": "Task", "bug": "Bug"}}))

            with patch.dict(os.environ, {"CODEE_DATA_DIR": str(root)}):
                self.assertEqual(configured_issue_types(),
                                 ("story", "task", "bug"))
                skills = find_issue_triggered_skills(root)

            self.assertEqual([skill.slug for skill in skills], ["triage"])


if __name__ == "__main__":
    unittest.main()
