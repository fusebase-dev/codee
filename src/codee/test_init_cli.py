import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from codee.init_cli import main


class InitCliTest(unittest.TestCase):
    def test_copies_packaged_templates_to_current_directory(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                self.assertEqual(main(), 0)
                self.assertTrue(Path("AGENTS.md").is_file())
                self.assertEqual(Path("CLAUDE.md").read_text(), "@AGENTS.md")
                self.assertTrue(
                    Path(".claude/skills/story-planner/SKILL.md").is_file())
                self.assertTrue(
                    Path(
                        ".claude/skills/story-planner/assets/readme-template.md").is_file()
                )
            finally:
                os.chdir(original_directory)

    def test_copied_issue_skills_have_one_allowed_issue_type(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                self.assertEqual(main(), 0)
                issue_skills = 0
                for path in Path(".claude/skills").glob("*/SKILL.md"):
                    parts = path.read_text().split("---", 2)
                    frontmatter = yaml.safe_load(parts[1]) or {}
                    if frontmatter.get("x-codee-trigger") != "issue":
                        continue
                    issue_skills += 1
                    self.assertIn(
                        frontmatter.get("x-codee-issue-type"),
                        {"story", "task"},
                        path,
                    )
                self.assertGreater(issue_skills, 0)
            finally:
                os.chdir(original_directory)

    def test_creates_working_directories_and_ignores_them(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                self.assertEqual(main(), 0)
                self.assertTrue(Path("repositories").is_dir())
                self.assertTrue(Path("temp").is_dir())
                self.assertTrue(Path("memory").is_dir())
                # memory/ stays tracked: the admin UI commits memory edits.
                # .mcp.json is not: the MCP setup writes an API token into it.
                self.assertEqual(
                    Path(".gitignore").read_text(),
                    "/repositories\n/temp\n.mcp.json\n")
            finally:
                os.chdir(original_directory)

    def test_appends_only_missing_gitignore_entries(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                # No trailing newline, and `repositories` in an equivalent form.
                Path(".gitignore").write_text("*.log\nrepositories/")
                self.assertEqual(main(), 0)
                self.assertEqual(
                    Path(".gitignore").read_text(),
                    "*.log\nrepositories/\n/temp\n.mcp.json\n",
                )
            finally:
                os.chdir(original_directory)

    def test_leaves_gitignore_untouched_when_already_covered(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                covered = "/repositories\n/temp\n.mcp.json\n"
                Path(".gitignore").write_text(covered)
                self.assertEqual(main(), 0)
                self.assertEqual(Path(".gitignore").read_text(), covered)
            finally:
                os.chdir(original_directory)

    def test_declining_prompt_leaves_existing_files_unchanged(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                Path("AGENTS.md").write_text("custom")
                with patch("builtins.input", return_value="no") as prompt:
                    self.assertEqual(main(), 1)
                self.assertEqual(Path("AGENTS.md").read_text(), "custom")
                self.assertFalse(Path("CLAUDE.md").exists())
                self.assertFalse(Path("repositories").exists())
                self.assertFalse(Path(".gitignore").exists())
                prompt.assert_called_once()
            finally:
                os.chdir(original_directory)

    def test_confirming_prompt_updates_templates_and_preserves_claude_settings(self) -> None:
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                Path(".claude").mkdir()
                Path(".claude/settings.json").write_text("custom")
                Path("AGENTS.md").write_text("old")
                with patch("builtins.input", return_value="yes") as prompt:
                    self.assertEqual(main(), 0)
                self.assertNotEqual(Path("AGENTS.md").read_text(), "old")
                self.assertEqual(
                    Path(".claude/settings.json").read_text(), "custom")
                self.assertTrue(
                    Path(".claude/skills/task-qa/SKILL.md").is_file())
                prompt.assert_called_once()
            finally:
                os.chdir(original_directory)


if __name__ == "__main__":
    unittest.main()
