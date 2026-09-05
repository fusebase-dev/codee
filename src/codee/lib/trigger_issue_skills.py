from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from codee_main_context.context import (
    codee_issue_types, data_dir, load_settings, project_root,
    skills_dir as default_skills_dir)

REPO_ROOT = project_root()
SKILLS_DIR = default_skills_dir(REPO_ROOT)


def configured_issue_types() -> tuple[str, ...]:
    """The Codee work items a skill may declare, per the tasks provider settings.

    Read from disk rather than cached: the executor polls in a loop and the
    settings page can add a work item under it, and a skill written for that
    new work item has to start matching on the next tick rather than after a
    restart. Always contains story and task, whatever the file says.
    """
    return codee_issue_types(load_settings(data_dir()))


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
    issue_types: tuple[str, ...] | None = None,
) -> list[IssueTriggeredSkill]:
    """Load valid issue-triggered skills from skill frontmatter.

    ``issue_types`` is the set of Codee work items a skill may trigger on,
    defaulting to what Settings configures. Passed in by callers that already
    hold the settings, so one page load doesn't re-read the file per call.
    """
    if issue_types is None:
        issue_types = configured_issue_types()
    allowed = ", ".join(issue_types) or "none"
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
        if not isinstance(raw_issue_type, str) or issue_type not in issue_types:
            print(
                f"[issue_skills] ERROR: {path} declares x-codee-trigger: issue but "
                f"x-codee-issue-type must be one of {allowed}; skipping."
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
