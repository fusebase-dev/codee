"""Framework-independent operations for the Codee admin UI."""
import json
import re
import os
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

from codee_database import oauth_tokens
from codee_tasks_azure_devops import oauth as azure_oauth
from codee_agent_abstract.provider import AbstractCodingAgent, AgentModel
from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_agent_github_copilot.provider import GitHubCopilotAgent
from codee.lib import runs_db
from codee.lib.cron_describe import describe_cron
from codee.lib.trigger_cron_skills import trigger_cron_skills
from codee.lib.trigger_issue_skills import (
    ISSUE_TYPES,
    IssueTriggeredSkill,
    find_issue_triggered_skills,
)
from codee_main_context.context import (
    CodeeMainContext,
    CodingAgent,
    Settings,
    TasksProvider,
    data_dir,
    load_settings,
    memory_dir,
    project_root,
    save_settings,
    skills_dir,
)

load_dotenv()

MANAGED = {
    "name",
    "description",
    "model",
    "disable-model-invocation",
    "cron",
    "x-codee-trigger",
    "x-codee-issue-status",
    "x-codee-issue-type",
    "x-codee-cron",
    "x-codee-email-address",
    "x-codee-aws-sqs-queue",
}
AGENTS_FILE = "AGENTS.md"
SKILL_TYPES = [
    "knowledge",
    "slash command",
    "issue trigger",
    "cron trigger",
    "email trigger",
    "aws-sqs trigger",
]
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
INDEX_RE = re.compile(
    r"^- \[(?P<title>.+?)\]\((?P<file>[^)]+\.md)\)(?:\s*—\s*(?P<hook>.*))?$")

_CODING_AGENTS: dict[CodingAgent, type[AbstractCodingAgent]] = {
    CodingAgent.CLAUDE_CODE: ClaudeCodeAgent,
    CodingAgent.GITHUB_COPILOT: GitHubCopilotAgent,
}
WORKFLOW_NODE_SPACING = 440
WORKFLOW_NODE_CENTER_OFFSET = 110

# Port the admin UI listens on unless ``codee-admin --port`` says otherwise.
# The OAuth redirect URI is built from it, and Entra ID matches redirect URIs
# exactly — including the port — so both have to agree on one value.
DEFAULT_ADMIN_PORT = 8501


def parse_index(text: str) -> list[dict[str, Any]]:
    """Parse MEMORY.md while preserving non-conforming lines verbatim."""
    entries = []
    for lineno, raw in enumerate(text.splitlines()):
        match = INDEX_RE.match(raw)
        if match:
            entries.append({
                "title": match.group("title"),
                "file": match.group("file"),
                "hook": match.group("hook") or "",
                "lineno": lineno,
                "raw": raw,
                "matched": True,
            })
        elif raw.strip():
            entries.append({
                "title": "",
                "file": "",
                "hook": "",
                "lineno": lineno,
                "raw": raw,
                "matched": False,
            })
    return entries


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")


def parse_skill(text: str) -> tuple[dict[str, Any], str]:
    match = FM_RE.match(text)
    if not match:
        return {}, text
    return (yaml.safe_load(match.group(1)) or {}), match.group(2)


def infer_skill_type(frontmatter: dict[str, Any]) -> str:
    trigger = frontmatter.get("x-codee-trigger")
    if trigger == "issue":
        return "issue trigger"
    if trigger == "aws-sqs":
        return "aws-sqs trigger"
    if trigger == "email":
        return "email trigger"
    if trigger == "cron" or frontmatter.get("x-codee-cron") or frontmatter.get("cron"):
        return "cron trigger"
    if frontmatter.get("disable-model-invocation"):
        return "slash command"
    return "knowledge"


def dump_frontmatter(frontmatter: dict[str, Any]) -> str:
    return yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=10**9,
    )


def parse_extra_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Read the free-form frontmatter field, or explain why it cannot be used."""
    if not text.strip():
        return {}, ""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return {}, f"Other frontmatter fields are not valid YAML: {error}"
    if not isinstance(parsed, dict):
        return {}, "Write other frontmatter fields as `key: value` lines"
    managed = [str(key) for key in parsed if key in MANAGED]
    if managed:
        return {}, (f"{', '.join(managed)} already has a field of its own: "
                    "remove it from the other frontmatter fields")
    return {str(key): value for key, value in parsed.items()}, ""


def build_skill(frontmatter: dict[str, Any], extra: dict[str, Any], body: str) -> str:
    return f"---\n{dump_frontmatter({**frontmatter, **extra})}---\n\n{body.lstrip()}\n"


def _format_issue_status(value: Any) -> str:
    values = value if isinstance(value, list) else [value]
    return ", ".join(str(status) for status in values if status)


def _string_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        text for item in value
        if (text := str(item).strip())
    ))


def _transition_values(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    transitions = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        label = str(item.get("label", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if source and target:
            transitions.append({
                "source": source,
                "target": target,
                "label": label,
                "evidence": evidence,
            })
    return transitions


def _remove_redundant_skill_transitions(
    transitions: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Remove same-skill edges that bypass an inferred multi-step path."""
    retained = []
    for candidate in transitions:
        source = candidate["source"].casefold()
        target = candidate["target"].casefold()
        label = candidate["label"].casefold()
        adjacency: dict[str, set[str]] = {}
        for transition in transitions:
            if transition is candidate or transition["label"].casefold() != label:
                continue
            adjacency.setdefault(transition["source"].casefold(), set()).add(
                transition["target"].casefold()
            )

        pending = list(adjacency.get(source, ()))
        visited = {source}
        while pending:
            status = pending.pop()
            if status == target:
                break
            if status in visited:
                continue
            visited.add(status)
            pending.extend(adjacency.get(status, ()))
        else:
            retained.append(candidate)
    return retained


class AdminService:
    """Synchronous local operations used by Reflex event handlers."""

    def __init__(self) -> None:
        self.root = project_root()
        self.skills_dir = skills_dir(self.root)
        self.agents_file = self.root / AGENTS_FILE
        self.memory_dir = memory_dir(self.root)
        self.memory_index = self.memory_dir / "MEMORY.md"
        self.data_dir = data_dir(self.root)
        self.session_viewer = os.environ.get("CODEE_SESSION_VIEWER_URL", "")
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.context = CodeeMainContext(data_dir=self.data_dir)
        self.context.settings = load_settings(self.data_dir)
        self._workflow_cache: dict[str, Any] | None = None
        self._workflow_lock = threading.Lock()
        # Asking an agent for its catalog can mean spawning its CLI, so the
        # answer is cached per agent for the life of the process.
        self._models_cache: dict[CodingAgent, list[AgentModel]] = {}
        self._models_lock = threading.Lock()

    def _git_push(self, message: str) -> tuple[bool, str]:
        # AGENTS.md is only staged once it exists, so git add never fails on it.
        staged = [str(self.skills_dir), str(self.memory_dir)]
        if self.agents_file.exists():
            staged.append(str(self.agents_file))
        try:
            subprocess.run(
                ["git", "-C", str(self.root), "add", "-A", *staged],
                check=True,
                capture_output=True,
                text=True,
            )
            result = subprocess.run(
                ["git", "-C", str(self.root), "commit", "-m", message],
                capture_output=True,
                text=True,
            )
            output = result.stdout + result.stderr
            if result.returncode != 0 and "nothing to commit" in output:
                return True, "nothing to commit"
            if result.returncode != 0:
                return False, output
            result = subprocess.run(
                ["git", "-C", str(self.root), "push"],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0, result.stdout + result.stderr
        except Exception as error:
            return False, str(error)

    def _write_and_push(self, path: Path, content: str, message: str) -> tuple[bool, bool, str]:
        """Write the file, then push. The write succeeds even when the push fails."""
        path.write_text(content)
        pushed, output = self._git_push(message)
        relative_path = path.relative_to(self.root)
        if pushed:
            return True, True, f"Saved and pushed {relative_path}"
        return True, False, f"Saved {relative_path}, but Git push failed: {output}"

    def list_skills(self) -> list[dict[str, str]]:
        skills = []
        for path in sorted(self.skills_dir.glob("*/SKILL.md")):
            frontmatter, _ = parse_skill(path.read_text())
            skills.append({
                "slug": path.parent.name,
                "name": str(frontmatter.get("name", path.parent.name)),
                "description": str(frontmatter.get("description", "")),
                "type": infer_skill_type(frontmatter),
                "issue_status": _format_issue_status(
                    frontmatter.get("x-codee-issue-status", [])
                ),
                "issue_type": str(
                    frontmatter.get("x-codee-issue-type", "")
                ).strip().lower(),
            })
        return skills

    def list_agent_models(self) -> list[dict[str, str]]:
        """Models the configured coding agent offers, for the skill editor's picker.

        Best-effort: an agent that can't be asked yields an empty list and the
        editor falls back to a hand-typed model id.
        """
        agent_key = self.context.settings.coding_agent
        with self._models_lock:
            models = self._models_cache.get(agent_key)
            if models is None:
                agent_type = _CODING_AGENTS.get(agent_key)
                try:
                    models = agent_type.list_models() if agent_type else []
                except Exception as error:
                    print(f"[admin] Failed to list models for "
                          f"{agent_key.value}: {error}")
                    models = []
                self._models_cache[agent_key] = models
        return [{"id": model.id, "name": model.name} for model in models]

    def resolve_skill_slug(self, label: str) -> str:
        """Map a workflow transition label back to the skill directory it names."""
        target = label.strip().casefold()
        if not target:
            return ""
        for skill in self.list_skills():
            if target in (skill["slug"].casefold(), skill["name"].casefold()):
                return skill["slug"]
        return ""

    def generate_workflow(self, force: bool = False) -> dict[str, Any]:
        """Return cached workflows by issue type, regenerating when requested."""
        workflow_lock = getattr(self, "_workflow_lock", None)
        if workflow_lock is None:
            workflow_lock = self._workflow_lock = threading.Lock()
        with workflow_lock:
            workflow_cache = getattr(self, "_workflow_cache", None)
            if not force and workflow_cache is not None:
                return workflow_cache
            self._workflow_cache = self._generate_workflow()
            return self._workflow_cache

    def _generate_workflow(self) -> dict[str, Any]:
        """Infer separate status graphs for story and task skills."""
        skills = find_issue_triggered_skills(self.skills_dir)
        return {
            issue_type: self._generate_issue_type_workflow(
                [skill for skill in skills if skill.issue_type == issue_type],
                issue_type,
            )
            for issue_type in ISSUE_TYPES
        }

    def _generate_issue_type_workflow(
        self,
        skills: list[IssueTriggeredSkill],
        issue_type: str,
    ) -> dict[str, Any]:
        """Infer one status graph from skills for a single issue type."""
        if not skills:
            return {"nodes": [], "edges": [], "warnings": []}

        documents = []
        skill_documents = {}
        for skill in skills:
            statuses = ", ".join(skill.statuses)
            skill_document = skill.path.read_text()
            skill_documents[skill.name.casefold()] = (skill, skill_document)
            skill_documents[skill.slug.casefold()] = (skill, skill_document)
            documents.append(
                f"## Skill: {skill.name}\nEntry statuses: {statuses}\n\n"
                f"{skill_document}"
            )
        prompt = (
            f"Build the {issue_type} workflow represented by the issue-trigger skills below. "
            "The frontmatter statuses are entry points only; infer outgoing status "
            "transitions from the human instructions in each complete skill. Transitions "
            "must be defined directly in skill text or they do not exist. Never invent or "
            "infer a status name that is not written in the supplied skill documents. "
            "Return only JSON with this shape: "
            '{"statuses":["..."],"transitions":['
            '{"source":"...","target":"...","label":"skill name",'
            '"evidence":"exact quote from that skill"}],'
            '"final_statuses":["..."]}. Every status must be copied exactly from the '
            "skill documents. Every transition must connect two listed statuses and its "
            "label must be the skill that defines it. Its source must be one of that "
            "skill's Entry statuses. evidence must be a verbatim quote from that same "
            "skill which explicitly names the target status. statuses must contain "
            "every status named in workflow instructions, even when no skill handles it. "
            "Preserve mandatory status changes in execution order. If a skill says work "
            "must start in an intermediate status before later moving to another status, "
            "emit consecutive transitions through that intermediate status and do not "
            "emit a direct transition that bypasses it. "
            "Order statuses by the primary forward workflow so a return or rework "
            "transition targets an earlier item in the statuses list. "
            "final_statuses must contain only statuses explicitly described as completion "
            "or handoff to a human. Do not include prose or non-status process steps.\n\n"
            + "\n\n".join(documents)
        )
        agent_type = _CODING_AGENTS.get(self.context.settings.coding_agent)
        if agent_type is None:
            raise RuntimeError(
                f"Coding agent '{self.context.settings.coding_agent.value}' is not available"
            )
        agent = agent_type(self.context.settings, self.root)
        validation_error = ""
        for attempt in range(2):
            request = prompt
            if validation_error:
                request += (
                    "\n\nYour previous response was invalid: "
                    f"{validation_error}. Return corrected JSON only."
                )
            response = agent.run(request, str(uuid.uuid4()))
            payload_text = response.strip()
            if payload_text.startswith("```") and payload_text.endswith("```"):
                payload_text = re.sub(
                    r"^```(?:json)?\s*|\s*```$", "", payload_text,
                    flags=re.IGNORECASE,
                )
            try:
                payload = json.loads(payload_text)
                if not isinstance(payload, dict):
                    raise ValueError(
                        "Coding agent did not return a workflow object")
                statuses = _string_values(payload.get("statuses"))
                transitions = _transition_values(payload.get("transitions"))
                declared_statuses = {
                    status.casefold() for status in statuses
                }
                for transition in transitions:
                    if (transition["source"].casefold() not in declared_statuses
                            or transition["target"].casefold() not in declared_statuses):
                        raise ValueError(
                            "each transition must connect two declared statuses")
                    label = transition["label"]
                    evidence = transition["evidence"]
                    skill_entry = skill_documents.get(label.casefold())
                    if not label or skill_entry is None:
                        raise ValueError(
                            "each transition label must name its defining skill")
                    skill, skill_document = skill_entry
                    if not any(
                        transition["source"].casefold() == status.casefold()
                        for status in skill.statuses
                    ):
                        raise ValueError(
                            f"transition source is not an entry status of {skill.name}")
                    if not evidence or evidence not in skill_document:
                        raise ValueError(
                            f"transition evidence is not an exact quote from {skill.name}")
                    if transition["target"].casefold() not in evidence.casefold():
                        raise ValueError(
                            f"transition evidence does not name its target for {skill.name}")
                final_statuses = _string_values(payload.get("final_statuses"))
                if any(
                    status.casefold() not in declared_statuses
                    for status in final_statuses
                ):
                    raise ValueError(
                        "each final status must be a declared status")
                break
            except (json.JSONDecodeError, ValueError) as error:
                validation_error = str(error)
                if attempt == 1:
                    raise ValueError(
                        f"Coding agent returned invalid workflow data: {error}"
                    ) from error

        transitions = _remove_redundant_skill_transitions(transitions)

        status_ids = {
            status.casefold(): f"status-{index}"
            for index, status in enumerate(statuses)
        }
        status_order = {
            status.casefold(): index for index, status in enumerate(statuses)
        }
        grouped_transitions: dict[tuple[str, str], dict[str, Any]] = {}
        for transition in transitions:
            key = (transition["source"].casefold(),
                   transition["target"].casefold())
            grouped = grouped_transitions.setdefault(key, {
                "source": transition["source"],
                "target": transition["target"],
                "labels": [],
            })
            if (transition["label"]
                    and transition["label"] not in grouped["labels"]):
                grouped["labels"].append(transition["label"])
        triggered = {status.casefold()
                     for skill in skills for status in skill.statuses}
        final = {status.casefold() for status in final_statuses}
        # Statuses no issue-trigger skill picks up are flagged on the graph
        # node itself instead of as a warning callout above the diagram.
        unhandled = {
            status.casefold() for status in statuses
            if status.casefold() not in triggered and status.casefold() not in final
        }
        warnings: list[str] = []
        disconnected = len(statuses) > 1 and not transitions
        if disconnected:
            warnings.append(
                "Workflow statuses are disconnected: no status transitions were found."
            )
        if not final_statuses:
            warnings.append(
                "No final human-handoff status is defined in the issue skill workflow."
            )
        nodes = [
            {
                "id": status_ids[status.casefold()],
                "position": {"x": index * WORKFLOW_NODE_SPACING, "y": 0},
                "sourcePosition": "right",
                "targetPosition": "left",
                "data": {"label": status},
                "className": " ".join(
                    ["workflow-node"]
                    + (["workflow-node--disconnected"] if disconnected else [])
                    + (["workflow-node--unhandled"]
                       if status.casefold() in unhandled else [])
                ),
            }
            for index, status in enumerate(statuses)
        ]
        edges = []
        return_index = 0
        forward_route_index = 0
        for index, transition in enumerate(grouped_transitions.values()):
            source_order = status_order[transition["source"].casefold()]
            target_order = status_order[transition["target"].casefold()]
            is_return = target_order <= source_order
            is_long_forward = target_order > source_order + 1
            color = "#d97706" if is_return else "#167d5a"
            edge_data = {
                "data": {"skills": transition["labels"]},
                "type": "smoothstep",
                "animated": is_return,
                "className": (
                    "workflow-edge workflow-edge--return"
                    if is_return else "workflow-edge"
                ),
                "markerEnd": {"type": "arrowclosed", "color": color},
                "style": {
                    "stroke": color,
                    "strokeWidth": 2,
                    **({"strokeDasharray": "8 6"} if is_return else {}),
                },
            }
            aria_label = (
                f"{transition['source']} to {transition['target']}"
                + (f" via {', '.join(transition['labels'])}"
                   if transition["labels"] else "")
            )
            label = ", ".join(transition["labels"])
            label_data = ({
                "label": label,
                "labelStyle": {
                    "fill": "#d7e1dc",
                    "fontSize": 12,
                    "fontWeight": 600,
                },
                "labelBgStyle": {
                    "fill": "#17211d",
                    "fillOpacity": 0.96,
                },
                "labelBgPadding": [6, 4],
                "labelBgBorderRadius": 4,
            } if label else {})
            if not is_return and not is_long_forward:
                edges.append({
                    **edge_data,
                    **label_data,
                    "id": f"transition-{index}",
                    "source": status_ids[transition["source"].casefold()],
                    "target": status_ids[transition["target"].casefold()],
                    "ariaLabel": aria_label,
                })
                continue

            if is_long_forward:
                route_y = -180 - forward_route_index * 90
                route_ids = [
                    f"forward-route-{forward_route_index}-out",
                    f"forward-route-{forward_route_index}-in",
                ]
                route_points = [
                    (
                        route_ids[0],
                        source_order * WORKFLOW_NODE_SPACING
                        + WORKFLOW_NODE_SPACING - WORKFLOW_NODE_CENTER_OFFSET,
                    ),
                    (
                        route_ids[1],
                        target_order * WORKFLOW_NODE_SPACING
                        - WORKFLOW_NODE_CENTER_OFFSET,
                    ),
                ]
                for route_id, route_x in route_points:
                    nodes.append({
                        "id": route_id,
                        "position": {"x": route_x, "y": route_y},
                        "sourcePosition": "right",
                        "targetPosition": "left",
                        "data": {"label": ""},
                        "className": (
                            "workflow-route-node "
                            "workflow-route-node--forward"
                        ),
                        "selectable": False,
                        "draggable": False,
                        "style": {
                            "background": "transparent",
                            "border": "none",
                            "height": 1,
                            "minHeight": 1,
                            "opacity": 1,
                            "padding": 0,
                            "width": 1,
                        },
                    })
                edges.extend([
                    {
                        **edge_data,
                        **label_data,
                        "id": f"transition-{index}-out",
                        "source": status_ids[transition["source"].casefold()],
                        "target": route_ids[0],
                    },
                    {
                        **edge_data,
                        "id": f"transition-{index}-route",
                        "source": route_ids[0],
                        "target": route_ids[1],
                    },
                    {
                        **edge_data,
                        "id": f"transition-{index}-in",
                        "source": route_ids[1],
                        "target": status_ids[transition["target"].casefold()],
                        "ariaLabel": aria_label,
                    },
                ])
                edges[-3].pop("markerEnd", None)
                edges[-2].pop("markerEnd", None)
                forward_route_index += 1
                continue

            route_id = f"return-route-{return_index}"
            nodes.append({
                "id": route_id,
                "position": {
                    "x": (
                        (source_order + target_order)
                        * WORKFLOW_NODE_SPACING / 2
                        + WORKFLOW_NODE_CENTER_OFFSET
                    ),
                    "y": 180 + return_index * 90,
                },
                "sourcePosition": "left",
                "targetPosition": "right",
                "data": {"label": ""},
                "className": (
                    "workflow-route-node "
                    "workflow-route-node--return"
                ),
                "selectable": False,
                "draggable": False,
                "style": {
                    "background": "transparent",
                    "border": "none",
                    "height": 1,
                    "minHeight": 1,
                    "opacity": 1,
                    "padding": 0,
                    "width": 1,
                },
            })
            edges.extend([
                {
                    **edge_data,
                    **label_data,
                    "id": f"transition-{index}-out",
                    "source": status_ids[transition["source"].casefold()],
                    "target": route_id,
                },
                {
                    **edge_data,
                    "id": f"transition-{index}-in",
                    "source": route_id,
                    "target": status_ids[transition["target"].casefold()],
                    "ariaLabel": aria_label,
                },
            ])
            edges[-2].pop("markerEnd", None)
            return_index += 1
        return {"nodes": nodes, "edges": edges, "warnings": warnings}

    def load_skill(self, slug: str) -> dict[str, str]:
        path = self.skills_dir / slug / "SKILL.md"
        frontmatter, body = parse_skill(path.read_text())
        extra = {key: value for key, value in frontmatter.items()
                 if key not in MANAGED}
        return {
            "slug": slug,
            "name": str(frontmatter.get("name", slug)),
            "description": str(frontmatter.get("description", "")),
            "model": str(frontmatter.get("model", "") or ""),
            "type": infer_skill_type(frontmatter),
            "cron": str(frontmatter.get("x-codee-cron", frontmatter.get("cron", "0 0 * * *"))),
            "email": str(frontmatter.get("x-codee-email-address", "")),
            "sqs": str(frontmatter.get("x-codee-aws-sqs-queue", "")),
            "issue_status": _format_issue_status(frontmatter.get("x-codee-issue-status", [])),
            "issue_type": str(frontmatter.get("x-codee-issue-type", "")).strip().lower(),
            "body": body,
            "extra": dump_frontmatter(extra) if extra else "",
        }

    def create_skill(self, name: str) -> tuple[bool, bool, str, str]:
        slug = slugify(name)
        if not slug:
            return False, False, "Enter a valid skill name", ""
        directory = self.skills_dir / slug
        if directory.exists():
            return False, False, f"{slug} already exists", slug
        directory.mkdir(parents=True)
        saved, pushed, message = self._write_and_push(
            directory / "SKILL.md",
            build_skill({"name": slug, "description": ""}, {}, ""),
            f"skill: create {slug}",
        )
        return saved, pushed, message, slug

    def save_skill(self, skill: dict[str, str]) -> tuple[bool, bool, str, str]:
        old_slug = skill["slug"]
        name = slugify(skill["name"])
        if not name:
            return False, False, "Enter a valid skill name", old_slug

        # A caller that leaves `extra` out is not editing the free-form fields,
        # so whatever the file already carries is kept below.
        edits_extra = "extra" in skill
        extra, extra_error = parse_extra_frontmatter(skill.get("extra", ""))
        if extra_error:
            return False, False, extra_error, old_slug

        frontmatter: dict[str, Any] = {
            "name": name,
            "description": skill["description"],
        }
        # Left out entirely when unset, so the skill keeps running on whatever
        # the agent defaults to rather than on an empty model id.
        model = skill.get("model", "").strip()
        if model:
            frontmatter["model"] = model
        skill_type = skill["type"]
        if skill_type == "slash command":
            frontmatter["disable-model-invocation"] = True
        elif skill_type == "issue trigger":
            issue_type = skill.get("issue_type", "").strip().lower()
            if issue_type not in ISSUE_TYPES:
                return False, False, "Select an issue type: story or task", old_slug
            frontmatter.update({
                "disable-model-invocation": True,
                "x-codee-trigger": "issue",
                "x-codee-issue-status": [
                    status.strip() for status in skill["issue_status"].split(",")
                    if status.strip()
                ],
                "x-codee-issue-type": issue_type,
            })
        elif skill_type == "cron trigger":
            frontmatter.update({
                "disable-model-invocation": True,
                "x-codee-trigger": "cron",
                "x-codee-cron": skill["cron"],
            })
        elif skill_type == "email trigger":
            frontmatter.update({
                "disable-model-invocation": True,
                "x-codee-trigger": "email",
                "x-codee-email-address": skill["email"],
            })
        elif skill_type == "aws-sqs trigger":
            frontmatter.update({
                "disable-model-invocation": True,
                "x-codee-trigger": "aws-sqs",
                "x-codee-aws-sqs-queue": skill["sqs"],
            })

        current_path = self.skills_dir / old_slug / "SKILL.md"
        destination = self.skills_dir / name
        if name != old_slug:
            if destination.exists():
                return False, False, f"{name} already exists", old_slug
            current_path.parent.rename(destination)
            current_path = destination / "SKILL.md"

        if not edits_extra:
            existing, _ = parse_skill(current_path.read_text())
            extra = {key: value for key, value in existing.items()
                     if key not in MANAGED}
        action =f"rename {old_slug} -> {name}" if name != old_slug else f"update {name}"
        saved, pushed, message = self._write_and_push(
            current_path,
            build_skill(frontmatter, extra, skill["body"]),
            f"skill: {action}",
        )
        return saved, pushed, message, name

    def delete_skill(self, slug: str) -> tuple[bool, bool, str]:
        directory = self.skills_dir / slug
        if not slug or not directory.is_dir():
            return False, False, f"{slug or 'Skill'} does not exist"
        shutil.rmtree(directory)
        pushed, output = self._git_push(f"skill: delete {slug}")
        if pushed:
            return True, True, f"Deleted {slug}"
        return True, False, f"Deleted {slug} locally, but Git push failed: {output}"

    def load_agents(self) -> str:
        """Read AGENTS.md verbatim: it is plain text, not a skill document."""
        return self.agents_file.read_text() if self.agents_file.exists() else ""

    def save_agents(self, content: str) -> tuple[bool, bool, str]:
        return self._write_and_push(
            self.agents_file, content, f"agents: update {AGENTS_FILE}"
        )

    def force_run_skill(self, slug: str) -> None:
        trigger_cron_skills.request_force_run(
            self.skills_dir / slug / "SKILL.md", main_context=self.context
        )

    def describe_cron(self, expression: str) -> str:
        return describe_cron(expression) or "Unrecognized cron expression"

    def list_memories(self) -> list[dict[str, Any]]:
        if not self.memory_index.exists() or not self.memory_index.read_text().strip():
            return []
        return parse_index(self.memory_index.read_text())

    def load_memory(self, filename: str) -> str:
        path = self.memory_dir / filename
        return path.read_text() if path.exists() else ""

    def save_memory(self, filename: str, content: str) -> tuple[bool, bool, str]:
        return self._write_and_push(
            self.memory_dir / filename, content, f"memory: update {filename}"
        )

    def delete_memory(self, filename: str, raw: str) -> tuple[bool, bool, str]:
        lines = self.memory_index.read_text().splitlines(keepends=True)
        lines = [line for line in lines if line.rstrip("\n") != raw]
        self.memory_index.write_text("".join(lines))
        (self.memory_dir / filename).unlink(missing_ok=True)
        pushed, output = self._git_push(f"memory: delete {filename}")
        if pushed:
            return True, True, f"Deleted {filename}"
        return True, False, f"Deleted {filename} locally, but Git push failed: {output}"

    def dashboard(self) -> dict[str, Any]:
        return {
            "active": [
                {**job, "elapsed_label": runs_db.fmt_elapsed(job["elapsed"])}
                for job in runs_db.active_jobs(main_context=self.context)
            ],
            "counts": runs_db.counts(main_context=self.context),
            "hourly": runs_db.runs_by_hour(main_context=self.context),
        }

    def recent_runs(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return runs_db.recent_runs(limit, offset, main_context=self.context)

    def load_settings(self) -> Settings:
        self.context.settings = load_settings(self.data_dir)
        return self.context.settings

    def save_settings(
        self,
        tasks_provider: str,
        coding_agent: str,
        max_parallel_agents: int,
        credentials: dict[str, str],
    ) -> None:
        current = self.context.settings
        all_credentials = dict(current.credentials)
        all_credentials[tasks_provider] = credentials
        self.context.settings = Settings(
            tasks_provider=TasksProvider(tasks_provider),
            coding_agent=CodingAgent(coding_agent),
            credentials=all_credentials,
            max_parallel_agents=max(1, max_parallel_agents),
        )
        save_settings(self.data_dir, self.context.settings)

    # --- Azure DevOps OAuth -------------------------------------------------

    def admin_base_url(self) -> str:
        """Origin the browser reaches this admin UI on.

        Taken from the port the launcher passed through ``REFLEX_API_URL`` so the
        redirect URI follows ``codee-admin --port``. Set ``CODEE_ADMIN_BASE_URL``
        when the UI is reached through some other host or scheme.
        """
        override = os.environ.get("CODEE_ADMIN_BASE_URL", "").strip()
        if override:
            return override.rstrip("/")
        port = urlparse(os.environ.get(
            "REFLEX_API_URL", "")).port or DEFAULT_ADMIN_PORT
        return f"http://localhost:{port}"

    def azure_redirect_uri(self) -> str:
        """The redirect URI to register on the Entra app, and to send to Entra."""
        return self.admin_base_url() + azure_oauth.CALLBACK_PATH

    def azure_connection(self) -> dict[str, Any]:
        """Whether Azure DevOps is connected, and as whom, for the settings page."""
        tokens = oauth_tokens.load_tokens(
            azure_oauth.PROVIDER, main_context=self.context)
        if not tokens:
            return {"connected": False, "account": "", "expires_label": ""}
        return {
            "connected": True,
            "account": tokens.get("account") or "",
            "expires_label": _format_token_expiry(tokens.get("expires_at")),
        }

    def start_azure_authorization(self) -> tuple[bool, str]:
        """Open an authorization: returns (True, url) or (False, error message).

        The pending state and PKCE verifier go to SQLite rather than to the UI
        state, because the callback arrives as a plain HTTP request that has no
        access to the Reflex session that started the flow.
        """
        config = azure_oauth.OAuthConfig.from_settings(self.load_settings())
        if not config.is_complete():
            return False, ("Fill in organization URL, project, client ID and "
                           "client secret before connecting.")
        state = azure_oauth.new_state()
        code_verifier = azure_oauth.new_code_verifier()
        redirect_uri = self.azure_redirect_uri()
        oauth_tokens.create_pending(
            azure_oauth.PROVIDER, state, code_verifier, redirect_uri,
            main_context=self.context)
        return True, azure_oauth.build_authorization_url(
            config, redirect_uri, state, code_verifier)

    def complete_azure_authorization(self, code: str, state: str) -> tuple[bool, str]:
        """Exchange the callback's code for tokens and store them."""
        # Re-read from disk: this runs on the callback request, not on the
        # session that started the flow, so in-memory settings may be stale.
        config = azure_oauth.OAuthConfig.from_settings(self.load_settings())
        pending = oauth_tokens.consume_pending(
            azure_oauth.PROVIDER, state, main_context=self.context)
        if pending is None:
            return False, ("That authorization link was already used or expired. "
                           "Start the connection again.")
        try:
            tokens = azure_oauth.exchange_code(
                config, pending["redirect_uri"], code, pending["code_verifier"])
        except azure_oauth.AzureDevOpsAuthError as exc:
            return False, str(exc)
        account = azure_oauth.fetch_account(tokens["access_token"])
        azure_oauth.AzureDevOpsAuth(config, self.context).store(
            tokens, account=account)
        return True, (f"Connected to Azure DevOps as {account}"
                      if account else "Connected to Azure DevOps")

    def disconnect_azure(self) -> None:
        """Drop the stored tokens. The app registration itself is untouched."""
        oauth_tokens.delete_tokens(
            azure_oauth.PROVIDER, main_context=self.context)


def _format_token_expiry(expires_at: str | None) -> str:
    """Human-readable life left in the access token; it is refreshed on demand."""
    if not expires_at:
        return "refreshes on next check"
    try:
        deadline = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return "refreshes on next check"
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    minutes = int((deadline - datetime.now(timezone.utc)).total_seconds() // 60)
    if minutes < 1:
        return "refreshes on next check"
    if minutes < 60:
        return f"access token valid for {minutes} min"
    return f"access token valid for {minutes // 60}h {minutes % 60}m"
