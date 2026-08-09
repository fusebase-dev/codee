from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from codee_main_context.context import project_root, skills_dir as default_skills_dir

REPO_ROOT = project_root()
SKILLS_DIR = default_skills_dir(REPO_ROOT)
ISSUE_TYPES = ("story", "task")


@dataclass(frozen=True)
class IssueTriggeredSkill:
    name: str
    slug: str
    path: Path
    statuses: tuple[str, ...]
    issue_type: str
    model: str = ""


def find_issue_triggered_skills(
    skills_dir: Path = SKILLS_DIR,
) -> list[IssueTriggeredSkill]:
    """Load valid issue-triggered skills from skill frontmatter."""
    skills: list[IssueTriggeredSkill] = []
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            metadata = _parse_frontmatter(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            print(f"[issue_skills] Failed to read {path}: {exc}")
            continue

        if str(metadata.get("x-codee-trigger", "")).strip().lower() != "issue":
            continue
        if metadata.get("disable-model-invocation") is not True:
            print(
                f"[issue_skills] ERROR: {path} declares x-codee-trigger: issue but is "
                "missing disable-model-invocation: true; skipping."
            )
            continue

        statuses = _status_values(metadata.get("x-codee-issue-status"))
        if not statuses:
            print(
                f"[issue_skills] ERROR: {path} declares x-codee-trigger: issue but is "
                "missing x-codee-issue-status; skipping."
            )
            continue

        raw_issue_type = metadata.get("x-codee-issue-type")
        issue_type = str(raw_issue_type).strip().lower()
        if not isinstance(raw_issue_type, str) or issue_type not in ISSUE_TYPES:
            print(
                f"[issue_skills] ERROR: {path} declares x-codee-trigger: issue but "
                "x-codee-issue-type must be story or task; skipping."
            )
            continue
        skills.append(IssueTriggeredSkill(
            name=str(metadata.get("name", path.parent.name)
                     ).strip() or path.parent.name,
            slug=path.parent.name,
            path=path,
            statuses=statuses,
            issue_type=issue_type,
            model=str(metadata.get("model", "")).strip(),
        ))
    return skills


def issue_statuses(skills: list[IssueTriggeredSkill]) -> list[str]:
    """Return unique configured statuses while preserving declaration order."""
    return list(dict.fromkeys(status for skill in skills for status in skill.statuses))


def match_issue_skill(
    skills: list[IssueTriggeredSkill], status: str, issue_type: str
) -> IssueTriggeredSkill | None:
    """Find a skill matching both status and issue type."""
    normalized_status = status.casefold()
    normalized_issue_type = issue_type.casefold()

    return next((
        skill for skill in skills
        if skill.issue_type.casefold() == normalized_issue_type
        and any(value.casefold() == normalized_status for value in skill.statuses)
    ), None)


def _parse_frontmatter(contents: str) -> dict[str, Any]:
    if not contents.startswith("---"):
        return {}
    parts = contents.split("---", 2)
    if len(parts) < 3:
        return {}
    parsed = yaml.safe_load(parts[1]) or {}
    return parsed if isinstance(parsed, dict) else {}


def _status_values(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(
        status for item in values
        if item is not None and (status := str(item).strip())
    )
