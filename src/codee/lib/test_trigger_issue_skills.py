import tempfile
import unittest
from pathlib import Path

from codee.lib.trigger_issue_skills import (
    find_issue_triggered_skills,
    issue_statuses,
    match_issue_skill,
)


class IssueTriggeredSkillsTest(unittest.TestCase):
    def _skill(self, root: Path, slug: str, frontmatter: str) -> None:
        directory = root / slug
        directory.mkdir()
        (directory / "SKILL.md").write_text(f"---\n{frontmatter}---\nBody\n")

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


if __name__ == "__main__":
    unittest.main()
