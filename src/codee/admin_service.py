"""Framework-independent operations for the Codee admin UI."""
import hashlib
import json
import re
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

from codee_database import claude_code_accounts, oauth_tokens
from codee_tasks_azure_devops import oauth as azure_oauth
from codee_agent_abstract.provider import AbstractCodingAgent, AgentModel
from codee_agent_claude_code import oauth as claude_oauth
from codee_agent_claude_code.account import AccountUnavailable, fetch_account
from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_agent_claude_code.usage import (
    SESSION_WINDOW, WEEKLY_WINDOW, UsageUnavailable, fetch_usage)
from codee_tasks_abstract.provider import (
    AbstractTasksProvider, TasksProviderError)
from codee.coding_agents import (
    CODING_AGENTS, agent_label, build_coding_agent, resolve_agent_code)
from codee.lib import runs_db
from codee.lib.claude_key_rotation import ensure_fresh
from codee.lib.cron_describe import describe_cron
from codee.lib.mcp_config import find_mcp_server, write_mcp_server
from codee.lib.trigger_cron_skills import trigger_cron_skills
from codee.lib.trigger_issue_skills import (
    IssueTriggeredSkill,
    find_issue_triggered_skills,
    issue_statuses,
)
from codee.tasks_providers import TASKS_PROVIDERS, build_tasks_provider
from codee_main_context.context import (
    CodeeMainContext,
    CodingAgent,
    DEFAULT_ISSUE_TYPES,
    Settings,
    TasksProvider,
    codee_issue_types,
    data_dir,
    load_settings,
    memory_dir,
    project_root,
    save_settings,
    skills_dir,
    work_item_types,
)

load_dotenv()

MANAGED = {
    "name",
    "description",
    "model",
    "disable-model-invocation",
    "cron",
    "x-codee-agent",
    "x-codee-trigger",
    "x-codee-issue-status",
    "x-codee-issue-type",
    "x-codee-cron",
    "x-codee-email-address",
    "x-codee-aws-sqs-queue",
}
AGENTS_FILE = "AGENTS.md"
# Layout the coding agents are told to expect (see the AGENTS.md template):
# `repositories/<repo>/.bare` holds the bare clone, `repositories/<repo>/.git`
# points git at it, and every branch is checked out as its own worktree
# directory beside them.
REPOSITORIES_DIR = "repositories"
BARE_DIR = ".bare"
# Cloning happens on a worker thread with nobody to answer a prompt, so git is
# told to fail instead of blocking on an unknown host key or missing credentials.
GIT_NON_INTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
}
GIT_TIMEOUT = 60
GIT_NETWORK_TIMEOUT = 900
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

WORKFLOW_NODE_SPACING = 440
WORKFLOW_NODE_CENTER_OFFSET = 110
# The yellow that marks a person's work on the graph, matching the human node
# border. Edge colours have to be literals: React Flow builds an SVG arrow
# marker per colour, keyed by the string, so a CSS variable cannot be used.
WORKFLOW_HUMAN_EDGE_COLOR = "#d1a207"
# Marks a status an issue-trigger skill picks up, so the page can say on hover
# which agent and model work it.
WORKFLOW_AGENT_CLASS = "workflow-node--agent"
# What the tooltip says for a skill with no ``model`` in its frontmatter: the
# executor passes no model at all there and the agent's CLI picks one, so
# naming a model id would be a guess.
WORKFLOW_DEFAULT_MODEL_LABEL = "agent default"
# How many transitions get a hover-highlight group class. The matching CSS is
# one static rule per group, so the count is bounded; a workflow with more
# transitions than this is unreadable long before the cap bites.
WORKFLOW_HIGHLIGHT_GROUPS = 80
# Inferring the workflow costs a coding-agent run, so the graph is kept in the
# data directory and reused by later admin processes.
WORKFLOW_CACHE_FILE = "workflow.json"
# Bumped whenever the stored graph gains a field the page reads or the
# inference changes what it draws, so a cache written by an older Codee is
# regenerated instead of rendered.
WORKFLOW_CACHE_VERSION = 5

# How long the dashboard's account usage stands before it is asked for again.
# The page redraws every second and the answer moves over hours, so anything
# shorter would be a request per account per second for a number that has not
# changed.
USAGE_CACHE_SECONDS = 60


@dataclass(frozen=True)
class ConnectedAccount:
    """One connected Claude account, as the settings page lists it.

    Carries no token: the page only ever needs to say which account this is and
    whether it is the one in use, and a credential that never leaves the server
    cannot leak from the browser.
    """

    id: int
    label: str
    subscription: str = ""
    in_use: bool = False
    # Percent of the rolling session window and the weekly one already spent,
    # and when each comes back. -1 means "not read": an account whose usage
    # could not be asked for has to read differently from one sitting at zero.
    session_percent: float = -1.0
    weekly_percent: float = -1.0
    session_resets: str = ""
    weekly_resets: str = ""
    # Why its usage could not be read, when it could not be.
    usage_error: str = ""
    # True once the refresh token has run out: the account cannot renew itself
    # any more and has to be signed in again. The one thing on this row the
    # user has to act on, so it is the one thing besides the email worth
    # carrying to the page.
    needs_reconnect: bool = False


@dataclass(frozen=True)
class WorkflowGeneration:
    """What the shared workflow generation is doing right now.

    One run belongs to the whole process rather than to the page that asked
    for it, so this is what every viewer reads to find out where it is.
    """

    running: bool = False
    # What the generation is doing, newest line last.
    progress: tuple[str, ...] = ()
    # The last graph that finished, kept across a failed regeneration.
    workflow: dict[str, Any] | None = None
    error: str = ""

# The checks the settings page runs against the tasks provider, in the order it
# shows them: the second is only worth attempting once the first passes.
TASKS_CHECK = "Tasks can be pulled"
MCP_CHECK = "The coding agent can work through the MCP server"
# The plan, for a caller that wants to show the checks before it has results.
TASKS_CHECKS = (TASKS_CHECK, MCP_CHECK)
# Title the check's throwaway task carries, so it is recognizable in the backend
# afterwards. The agent closes it as part of the check.
MCP_CHECK_SUMMARY = "Codee MCP connection check"

# Port the admin UI listens on unless ``codee-admin --port`` says otherwise.
# The OAuth redirect URI is built from it, and Entra ID matches redirect URIs
# exactly — including the port — so both have to agree on one value.
DEFAULT_ADMIN_PORT = 8501


def public_base_url() -> str:
    """Public origin the admin UI is reached on, or "" when it is localhost.

    Set ``CODEE_ADMIN_BASE_URL`` when the UI sits behind a reverse proxy on a
    custom domain. Both the launcher and the OAuth redirect URI read it from
    here, so a single value covers the whole deployment.
    """
    return os.environ.get("CODEE_ADMIN_BASE_URL", "").strip().rstrip("/")


def _check(name: str, ok: bool, message: str) -> dict[str, Any]:
    """One line of the settings page's check list."""
    return {"name": name, "ok": ok, "message": message}


def _strip_code_fence(text: str) -> str:
    """Unwrap a ```-fenced block, for agents that answer JSON inside one."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped,
                      flags=re.IGNORECASE)
    return stripped


def _mcp_check_prompt(server_name: str, steps: list[str]) -> str:
    """Ask the agent to carry out the provider's steps through MCP and nothing else.

    The restriction is the point of the check: a coding agent handed JIRA
    credentials can reach the REST API by writing a script, and would then
    report success for a setup the executor's agents can't actually use. So the
    prompt closes every other route and tells it to fail loudly instead.
    """
    numbered = "\n".join(f"{number}. {step}"
                         for number, step in enumerate(steps, start=1))
    return (
        f"You are verifying that the `{server_name}` MCP server works from this "
        "project. Carry out every step below using only that MCP server's "
        "tools. No other way is allowed: no REST or HTTP calls, no curl, no "
        "CLI, no browser, and no script you write yourself. If the MCP server "
        "is not available to you, or one of its tools is missing or fails, stop "
        "and report that failure rather than reaching for another route.\n\n"
        f"{numbered}\n\n"
        "Then reply with one JSON object and no other text. On success:\n"
        '{"ok": true, "task": "<identifier of the task you created>", '
        '"status": "<the status you left it in>"}\n'
        "If any step did not complete:\n"
        '{"ok": false, "error": "<one sentence naming the step that failed '
        'and why>"}'
    )


def _mcp_check_result(server_name: str, response: str) -> dict[str, Any]:
    """Read the agent's verdict out of its reply.

    That reply is the only evidence there is — nothing else watched the agent
    do the work — so a reply that isn't the JSON object it was asked for counts
    as a failure rather than a pass: an agent that ignored the format can't be
    taken at its word on the part that matters either.
    """
    try:
        payload = json.loads(_strip_code_fence(response))
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return _check(MCP_CHECK, False, "The agent did not report a result: "
                      f"{response.strip()[:300] or 'it said nothing'}")
    if not payload.get("ok"):
        return _check(MCP_CHECK, False,
                      str(payload.get("error")
                          or "The agent reported a failure."))
    task = str(payload.get("task") or "a task").strip()
    status = str(payload.get("status") or "a closed status").strip()
    return _check(MCP_CHECK, True,
                  f"The agent created {task} through {server_name} and moved "
                  f"it to {status}.")


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


def repository_name(url: str) -> str:
    """Directory a clone URL lands in: its last path segment, without `.git`.

    Handles the scp-style SSH form (`git@host:org/repo.git`) too, which has no
    scheme for ``urlparse`` to read.
    """
    text = url.strip().rstrip("/")
    if not text:
        return ""
    path = urlparse(text).path if "://" in text else text.rsplit(":", 1)[-1]
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")


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


def _agent_code(value: Any) -> str:
    """A skill's ``x-codee-agent`` as a canonical agent code, or "" for none."""
    agent = resolve_agent_code(str(value or ""))
    return agent.value if agent else ""


def _format_issue_status(value: Any) -> str:
    values = value if isinstance(value, list) else [value]
    return ", ".join(str(status) for status in values if status)


def _count(quantity: int, noun: str, plural: str = "") -> str:
    """Pluralize a noun for the progress lines: 1 skill, 2 skills."""
    return f"{quantity} {noun if quantity == 1 else (plural or noun + 's')}"


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


def _human_action_values(value: Any) -> dict[str, str]:
    """Map each status a person owns to the sentence telling them what to do."""
    if not isinstance(value, list):
        return {}
    actions: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).strip()
        action = str(item.get("action", "")).strip()
        if status and action:
            actions.setdefault(status.casefold(), action)
    return actions


# Words that name a work item other than the Codee work item whose graph is
# being built, when a skill does not name that work item outright. A story
# skill spends most of its text on the story's subtasks, and a subtask skill on
# its parent, so a sentence that moves one of those describes a status on that
# item's own graph.
RELATED_WORK_ITEMS = {
    "subtask": ("subtask", "subtasks", "sub-task", "sub-tasks"),
    "child": ("child", "children"),
    "parent": ("parent", "parents"),
}
# The verbs a skill changes a status with. Only what one of these moves counts
# as another work item's: the names are ordinary English in these documents,
# and "after the task is complete, move it to Review" is a story skill talking
# about its own work, not about a task.
MOVE_VERB_RE = re.compile(
    r"(?<!\w)(?:move|moves|moved|moving|transition|transitions|transitioned|"
    r"set|sets|put|puts|leave|leaves|left|return|returns|returned|send|sends|"
    r"sent|mark|marks|marked|place|places|placed|advance|advances|advanced)"
    r"(?!\w)",
    re.IGNORECASE,
)
# Where the phrase naming what is moved ends: past "to" or "in" comes the
# status, whose own name may contain a work item word ("Task Ready").
MOVE_TARGET_WORDS = {"to", "into", "in", "onto", "back", "at"}
WORD_RE = re.compile(r"[\w-]+")
# How many words after the verb are still part of the phrase naming what moves.
MOVE_WINDOW = 4


def _work_item_terms(name: str) -> tuple[str, ...]:
    """A work item's name and its plural, as skill prose writes them."""
    if name.endswith("y"):
        return (name, f"{name[:-1]}ies")
    if name.endswith(("s", "x", "ch", "sh")):
        return (name, f"{name}es")
    return (name, f"{name}s")


class _WorkItemScope:
    """Tells what a skill moves this work item to from what it moves another to.

    The skills for one work item are the only ones the graph is built from, but
    their text still describes the items around it: a story skill says what
    happens to the story's subtasks, and a subtask skill what happens to its
    parent story. A status only those sentences move belongs on that item's
    graph, so it is kept out of this one.

    Naming another item is not on its own enough — "move the story to
    `AI Ready for CR` once every subtask is implemented" moves the story, and
    "task" in a story skill is as often the English word as the work item. It
    has to be what the sentence says is moved.
    """

    def __init__(self, issue_type: str, issue_types: Iterable[str]) -> None:
        own = _work_item_terms(issue_type.casefold())
        others = {
            other.casefold(): _work_item_terms(other.casefold())
            for other in issue_types
        } | dict(RELATED_WORK_ITEMS)
        # A work item literally called "subtask" owns that word, so the generic
        # names are only another item's when this item is not one of them.
        others = {name: terms for name, terms in others.items()
                  if not set(terms) & set(own)}
        self.issue_type = issue_type
        # The names as the prompt lists them: one per work item, no plurals.
        self.other_names = sorted(others)
        self._own = set(own)
        self._others = {term for terms in others.values() for term in terms}
        # "the subtask moves to Done" says what moves before naming the verb.
        self._subject = re.compile(
            r"(?<!\w)(" + "|".join(
                re.escape(term) for term in sorted(
                    self._own | self._others, key=len, reverse=True)
            ) + r")(?:\s+\w+){0,2}?\s+(?:moves|moved|transitions|transitioned|"
            r"returns|returned|goes|go)(?!\w)",
            re.IGNORECASE,
        )

    def other_work_item(self, text: str) -> str:
        """The other work item ``text`` moves, or "" when it moves this one.

        "" is also the answer for a sentence that moves nothing nameable: an
        unparsed phrasing keeps its status rather than losing it to a work
        item the text never actually names.
        """
        moved = ""
        for name in self._moved_items(text or ""):
            if name in self._own:
                return ""
            moved = moved or name
        return moved

    def _moved_items(self, text: str) -> Iterator[str]:
        """Every work item the text says something is moved to a status."""
        for match in self._subject.finditer(text):
            yield match.group(1).casefold()
        for verb in MOVE_VERB_RE.finditer(text):
            for word in WORD_RE.findall(text[verb.end():])[:MOVE_WINDOW]:
                lowered = word.casefold()
                if lowered in MOVE_TARGET_WORDS:
                    break
                if lowered in self._own or lowered in self._others:
                    yield lowered
                    break


def _css_string(text: str) -> str:
    """Quote text for a CSS ``content`` value.

    The human-action tooltip is drawn by one stylesheet rule shared by every
    node, so the sentence itself travels as a custom property on the node.
    That makes it CSS source rather than text, and an apostrophe in a skill's
    wording would otherwise close the string early and break the rule.
    """
    collapsed = re.sub(r"\s+", " ", text).strip()
    escaped = collapsed.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _css_lines(lines: Iterable[str]) -> str:
    r"""Quote several lines for a CSS ``content`` value, one per line.

    ``\A`` is the line break a ``content`` string can carry; the rule that
    reads it sets ``white-space: pre-line`` so the browser honours it. Each
    line is escaped on its own, so nothing a skill is called can break out of
    the string.
    """
    return "'" + "\\A ".join(
        _css_string(line)[1:-1] for line in lines) + "'"


def _skill_run_summary(
    skill: IssueTriggeredSkill, default_agent: CodingAgent
) -> list[str]:
    """What runs this skill: the agent, the model, and the skill's own name.

    The reading matches the executor's: a skill that names no agent, or one
    Codee cannot run, is driven by the default agent from Settings, and a
    skill with no ``model`` leaves the choice to that agent's CLI.
    """
    agent = resolve_agent_code(skill.agent) or default_agent
    return [
        f"AI agent: {agent_label(agent)}",
        f"Model: {skill.model or WORKFLOW_DEFAULT_MODEL_LABEL}",
        f"Skill: {skill.name}",
    ]


def _with_agent_tooltips(
    workflow: dict[str, Any],
    skills: list[IssueTriggeredSkill],
    default_agent: CodingAgent,
) -> dict[str, Any]:
    """Mark every status an issue-trigger skill picks up with what runs it.

    Applied to the graph on its way to the page rather than baked into it,
    because none of this comes from the inference: the answer changes when a
    skill's frontmatter or the default agent in Settings changes, and neither
    is worth minutes of a coding-agent run to redraw the same arrows.
    """
    handlers: dict[str, dict[str, list[IssueTriggeredSkill]]] = {}
    for skill in skills:
        by_status = handlers.setdefault(skill.issue_type, {})
        for status in skill.statuses:
            by_status.setdefault(status.casefold(), []).append(skill)
    return {
        issue_type: {
            **graph,
            "nodes": [
                _node_with_agent_tooltip(
                    node, handlers.get(issue_type, {}), default_agent)
                for node in graph.get("nodes", [])
            ],
        }
        for issue_type, graph in workflow.items()
    }


def _node_with_agent_tooltip(
    node: dict[str, Any],
    handlers: dict[str, list[IssueTriggeredSkill]],
    default_agent: CodingAgent,
) -> dict[str, Any]:
    """One node, plus the tooltip naming the agent that works that status.

    Nodes the graph draws for routing carry no status name, so they match no
    skill and are handed back untouched.
    """
    status = str((node.get("data") or {}).get("label", ""))
    handled = handlers.get(status.casefold())
    if not handled:
        return node
    lines: list[str] = []
    for skill in handled:
        # A blank line between skills: several can share an entry status, and
        # run on different agents when they do.
        if lines:
            lines.append("")
        lines.extend(_skill_run_summary(skill, default_agent))
    # The models named under the status name on the node itself. Skills sharing
    # an entry status often ask for the same one, so it is said once.
    models = list(dict.fromkeys(
        skill.model or WORKFLOW_DEFAULT_MODEL_LABEL for skill in handled))
    return {
        **node,
        "className": " ".join(
            filter(None, [str(node.get("className", "")), WORKFLOW_AGENT_CLASS])),
        "style": {
            **(node.get("style") or {}),
            "--codee-agent-run": _css_lines(lines),
            "--codee-node-model": _css_string(", ".join(models)),
        },
    }


def normalize_work_items(rows: list[tuple[str, str]]) -> tuple[dict[str, str], str]:
    """Turn the settings form's mapping rows into what ``Settings`` stores.

    Returns the mapping and an error message, one of which is always empty.

    Names are lower-cased because that is how a skill declares the work item it
    triggers on, and the two have to meet. The mandatory work items must
    survive: a settings page that let them be renamed away would leave every
    story and task skill matching nothing, with no error to explain it.
    """
    mapping: dict[str, str] = {}
    for raw_name, raw_type in rows:
        name = raw_name.strip().lower()
        item_type = raw_type.strip()
        if not name:
            return {}, "Give every work item a name"
        if not item_type:
            return {}, f"Choose the provider work item type for '{name}'"
        if name in mapping:
            return {}, f"'{name}' is listed twice"
        mapping[name] = item_type
    missing = [name for name in DEFAULT_ISSUE_TYPES if name not in mapping]
    if missing:
        return {}, f"Work items {' and '.join(missing)} cannot be removed"
    return mapping, ""


def _is_workflow(value: Any, issue_types: tuple[str, ...]) -> bool:
    """Whether a stored graph still has the shape the workflow page reads.

    Checked against the work items configured now rather than the ones the
    graph was built for: adding a work item leaves the cache a section short,
    and the page would render it as "no skills" instead of regenerating.
    """
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(value.get(issue_type), dict)
        and all(isinstance(value[issue_type].get(key), list)
                for key in ("nodes", "edges", "warnings"))
        for issue_type in issue_types
    )


def _own_work_item_statuses(
    statuses: list[str],
    scope: _WorkItemScope,
    known: set[str],
    documents: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Split declared statuses into this work item's and another item's.

    A status every mention of which moves a different work item — the
    subtask a story skill is working through, the parent a subtask skill
    reports to — is that item's, however plausible it looks beside the rest.

    ``known`` are the statuses that are this work item's whatever the prose
    around them says: the ones its skills trigger on, which the frontmatter
    settles, and the ends of the transitions whose evidence was already read
    as moving this work item.
    """
    lines = [line for document in documents
             for line in document.splitlines() if line.strip()]
    own: list[str] = []
    foreign: list[str] = []
    for status in statuses:
        if status.casefold() in known:
            own.append(status)
            continue
        mentions = [line for line in lines
                    if status.casefold() in line.casefold()]
        if mentions and all(scope.other_work_item(line) for line in mentions):
            foreign.append(status)
        else:
            own.append(status)
    return own, foreign


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
            # Another entry for the same pair is a duplicate, not a detour.
            # Counting it would make each copy the "longer path" that deletes
            # the other, and the edge would vanish entirely. A skill that
            # states one transition in several places (story-planner names
            # `AI Decomposition review` for both a finished plan and an open
            # question) is exactly what makes the agent emit those copies.
            if (transition["source"].casefold() == source
                    and transition["target"].casefold() == target):
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


# An issue-triggered run is launched with the skill slug and the work item it
# was started for and nothing else ("/story-planner NIM-44025"), so a prompt of
# exactly that shape names a task. Matched rather than searched for: a prompt
# someone typed may mention anything, and a word that merely looks like a key
# must not turn into a link to a task that doesn't exist.
ISSUE_PROMPT_RE = re.compile(r"/\S+[ \t]+(\S+)\s*\Z")


def issue_prompt_task(message: str) -> str:
    """The work item an issue-triggered prompt names, empty when it names none."""
    match = ISSUE_PROMPT_RE.fullmatch((message or "").strip())
    return match.group(1) if match else ""


class AdminService:
    """Synchronous local operations used by Reflex event handlers."""

    def __init__(self) -> None:
        self.root = project_root()
        self.skills_dir = skills_dir(self.root)
        self.agents_file = self.root / AGENTS_FILE
        self.memory_dir = memory_dir(self.root)
        self.memory_index = self.memory_dir / "MEMORY.md"
        self.repositories_dir = self.root / REPOSITORIES_DIR
        self.data_dir = data_dir(self.root)
        # Stripped so a value that is only whitespace counts as unset: it is
        # what decides whether the Sessions page is offered at all, and a
        # blank one would offer a link that goes nowhere.
        self.session_viewer = os.environ.get(
            "CODEE_SESSION_VIEWER_URL", "").strip()
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.context = CodeeMainContext(data_dir=self.data_dir)
        self.context.settings = load_settings(self.data_dir)
        # Cached graph plus the skill fingerprint it was generated from.
        self._workflow_cache: tuple[str, dict[str, Any]] | None = None
        self._workflow_lock = threading.Lock()
        # The generation in flight. It outlives the page visit that started
        # it, so a viewer who leaves and comes back attaches to the same run
        # instead of waiting on an earlier visit to report back.
        self._workflow_run = WorkflowGeneration()
        self._workflow_run_lock = threading.Lock()
        # Asking an agent for its catalog can mean spawning its CLI, so the
        # answer is cached per agent for the life of the process.
        self._models_cache: dict[CodingAgent, list[AgentModel]] = {}
        self._models_lock = threading.Lock()
        # The last account usage read, and when. Shared by every visitor to the
        # dashboard, because it is a property of the accounts rather than of
        # whoever is looking at them.
        self._usage_cache: tuple[list[Any] | None, float] = (None, 0.0)
        self._usage_lock = threading.Lock()

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
                "agent": _agent_code(frontmatter.get("x-codee-agent", "")),
                "model": str(frontmatter.get("model", "") or "").strip(),
                "issue_status": _format_issue_status(
                    frontmatter.get("x-codee-issue-status", [])
                ),
                "issue_type": str(
                    frontmatter.get("x-codee-issue-type", "")
                ).strip().lower(),
            })
        return skills

    def list_agents(self) -> list[dict[str, str]]:
        """Every agent Codee can run, for the pickers that choose between them.

        Not filtered by what is installed: the setup wizard asks that question
        of the machine it runs on, while a skill's agent is checked into the
        repository and may well name one that only the executor's host has.
        """
        return [{"code": agent.value, "name": agent_label(agent)}
                for agent in CODING_AGENTS]

    def list_agent_models(self, agent: str = "") -> list[dict[str, str]]:
        """Models one coding agent offers, for the skill editor's picker.

        ``agent`` is the skill's ``x-codee-agent``; an empty one — or one that
        names no agent Codee can run — is answered for the default agent from
        Settings, which is what would run that skill.

        Best-effort: an agent that can't be asked yields an empty list and the
        editor falls back to a hand-typed model id.
        """
        agent_key = (resolve_agent_code(agent)
                     or self.context.settings.coding_agent)
        with self._models_lock:
            models = self._models_cache.get(agent_key)
            if models is None:
                agent_type = CODING_AGENTS.get(agent_key)
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

    def start_workflow_generation(
        self, force: bool = False
    ) -> WorkflowGeneration:
        """Begin generating the workflow, or attach to the run already going.

        The run is the process's, not the caller's: whoever asks gets the
        state it is in right now, so a page that is opened again halfway
        through picks up the progress made so far rather than starting a
        second run or watching one it can no longer hear from.
        """
        with self._workflow_run_lock:
            if self._workflow_run.running:
                return self._workflow_run
            # Regenerating takes the graph off the screen: the point of
            # asking for it again is to watch a new one being built.
            self._workflow_run = WorkflowGeneration(
                running=True,
                workflow=None if force else self._workflow_run.workflow,
            )
            threading.Thread(
                target=self._run_workflow_generation,
                args=(force,),
                daemon=True,
            ).start()
            return self._workflow_run

    def workflow_generation_status(self) -> WorkflowGeneration:
        """Where the run started by ``start_workflow_generation`` has got to."""
        with self._workflow_run_lock:
            return self._workflow_run

    def _run_workflow_generation(self, force: bool) -> None:
        """Generate off the event loop, recording progress as it arrives.

        The run always ends, whatever happens inside it. One that stopped
        without saying so would leave every later visit watching the progress
        of a generation that is no longer running.
        """
        workflow: dict[str, Any] | None = None
        error = "Workflow generation stopped unexpectedly."
        try:
            workflow = self.generate_workflow(
                force, self._report_workflow_progress)
            error = ""
        except Exception as failure:  # noqa: BLE001 - shown on the page
            error = str(failure)
        finally:
            with self._workflow_run_lock:
                self._workflow_run = replace(
                    self._workflow_run,
                    running=False,
                    # A failed regeneration keeps the graph it could not
                    # replace, so the error is all that changes on screen.
                    workflow=(self._workflow_run.workflow if workflow is None
                              else workflow),
                    error=error,
                )

    def _report_workflow_progress(self, line: str) -> None:
        with self._workflow_run_lock:
            self._workflow_run = replace(
                self._workflow_run,
                progress=(*self._workflow_run.progress, line))

    def generate_workflow(
        self,
        force: bool = False,
        report: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Return cached workflows by issue type, regenerating when requested.

        The cache outlives the admin process: it is stored in the data
        directory under the fingerprint of the skill documents the graph was
        inferred from, so a restart reuses it while an edited skill still
        regenerates it.

        Generating costs one coding-agent run per work item and can take
        minutes, so ``report`` is called with a line of progress whenever
        there is something to say: which work item is being inferred, every
        rejected answer the agent is asked to correct, and what came out.
        """
        announce = report or (lambda message: None)
        workflow_lock = getattr(self, "_workflow_lock", None)
        if workflow_lock is None:
            workflow_lock = self._workflow_lock = threading.Lock()
        with workflow_lock:
            fingerprint = self._workflow_fingerprint()
            if not force:
                cached = self._cached_workflow(fingerprint)
                if cached is not None:
                    announce("Skills are unchanged: showing the stored workflow.")
                    return self._workflow_for_page(cached)
            workflow = self._generate_workflow(announce)
            self._workflow_cache = (fingerprint, workflow)
            self._store_workflow(fingerprint, workflow)
            return self._workflow_for_page(workflow)

    def _workflow_for_page(self, workflow: dict[str, Any]) -> dict[str, Any]:
        """The inferred graph with what the page knows without inferring it.

        Which agent and model work a status is read from skill frontmatter and
        the saved settings here, off the cached graph, so it is right after a
        skill's model changes or Settings picks another agent — neither of
        which changes an arrow, and so neither is worth a fresh inference run.
        """
        settings = load_settings(self.data_dir)
        return _with_agent_tooltips(
            workflow,
            find_issue_triggered_skills(
                self.skills_dir, codee_issue_types(settings)),
            settings.coding_agent,
        )

    def _workflow_fingerprint(self) -> str:
        """Digest the skill documents the workflow graph is inferred from."""
        digest = hashlib.sha256()
        # The work items are part of the fingerprint: adding one adds a section
        # to the graph without any skill file changing.
        digest.update(",".join(self.issue_types()).encode())
        digest.update(b"\0")
        for skill in find_issue_triggered_skills(
                self.skills_dir, self.issue_types()):
            digest.update(skill.slug.encode())
            digest.update(b"\0")
            digest.update(skill.path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _workflow_cache_path(self) -> Path:
        return self.data_dir / WORKFLOW_CACHE_FILE

    def _cached_workflow(self, fingerprint: str) -> dict[str, Any] | None:
        """This process's graph, else the one an earlier process stored."""
        cached = getattr(self, "_workflow_cache", None)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        try:
            stored = json.loads(self._workflow_cache_path().read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            print(f"[admin] Ignoring unreadable workflow cache: {error}")
            return None
        if not isinstance(stored, dict) or stored.get("fingerprint") != fingerprint:
            return None
        if stored.get("version") != WORKFLOW_CACHE_VERSION:
            return None
        workflow = stored.get("workflow")
        if not _is_workflow(workflow, self.issue_types()):
            return None
        self._workflow_cache = (fingerprint, workflow)
        return workflow

    def _store_workflow(self, fingerprint: str, workflow: dict[str, Any]) -> None:
        """Persist the graph for the next admin process; failing is not fatal."""
        path = self._workflow_cache_path()
        pending = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            pending.write_text(json.dumps({
                "version": WORKFLOW_CACHE_VERSION,
                "fingerprint": fingerprint,
                "workflow": workflow,
            }))
            # Replace in one step so a crash mid-write cannot leave a
            # half-written cache behind.
            pending.replace(path)
        except OSError as error:
            print(f"[admin] Failed to store workflow cache: {error}")

    def _generate_workflow(
        self,
        report: Callable[[str], None] = lambda message: None,
    ) -> dict[str, Any]:
        """Infer one status graph per configured Codee work item."""
        issue_types = self.issue_types()
        skills = find_issue_triggered_skills(self.skills_dir, issue_types)
        return {
            issue_type: self._generate_issue_type_workflow(
                [skill for skill in skills if skill.issue_type == issue_type],
                issue_type,
                report,
                issue_types,
            )
            for issue_type in issue_types
        }

    def _generate_issue_type_workflow(
        self,
        skills: list[IssueTriggeredSkill],
        issue_type: str,
        report: Callable[[str], None] = lambda message: None,
        issue_types: tuple[str, ...] = DEFAULT_ISSUE_TYPES,
    ) -> dict[str, Any]:
        """Infer one status graph from skills for a single issue type.

        The graph covers that work item alone. Its skills talk about the items
        around it as well — a story skill about the story's subtasks, a
        subtask skill about its parent — and a status only those sentences
        name belongs on the other item's graph, so it is kept out of this one.
        """
        if not skills:
            report(f"No issue-trigger skills for work item {issue_type.capitalize()}.")
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
        # Every status a skill picks itself up in; anything else is a person's.
        triggered = {status.casefold()
                     for skill in skills for status in skill.statuses}
        scope = _WorkItemScope(issue_type, issue_types)
        other_items = ", ".join(scope.other_names)
        prompt = (
            f"Build the {issue_type} workflow represented by the issue-trigger skills below. "
            f"The graph is the lifecycle of the {issue_type} work item alone. These "
            f"skills also describe the work items around it ({other_items}): a status "
            "a skill moves one of those to belongs on that work item's own workflow, "
            f"so never list it here. Only include a status the {issue_type} itself is "
            "moved to or waits in, and only where the skill text says the "
            f"{issue_type} makes that move. "
            "The frontmatter statuses are entry points only; infer outgoing status "
            "transitions from the human instructions in each complete skill. Transitions "
            "must be defined directly in skill text or they do not exist. Never invent or "
            "infer a status name that is not written in the supplied skill documents. "
            "Return only JSON with this shape: "
            '{"statuses":["..."],"transitions":['
            '{"source":"...","target":"...","label":"skill name",'
            '"evidence":"exact quote from that skill"}],'
            '"final_statuses":["..."],'
            '"human_actions":[{"status":"...","action":"..."}],'
            '"human_transitions":[{"source":"...","target":"...",'
            '"evidence":"exact quote from a skill"}]}'
            ". Every status must be copied exactly from the "
            "skill documents. Every transition must connect two listed statuses and its "
            "label must be the skill that defines it. Its source must be one of that "
            "skill's Entry statuses. evidence must be a verbatim quote from that same "
            "skill which explicitly names the target status, and it must be a "
            f"sentence that moves the {issue_type} rather than one of the other "
            f"work items. statuses must contain every {issue_type} status named "
            "in workflow instructions, even when no skill handles it. "
            "Preserve mandatory status changes in execution order. If a skill says work "
            "must start in an intermediate status before later moving to another status, "
            "emit consecutive transitions through that intermediate status and do not "
            "emit a direct transition that bypasses it. "
            "Order statuses by the primary forward workflow so a return or rework "
            "transition targets an earlier item in the statuses list. "
            "final_statuses must contain only statuses explicitly described as completion "
            "or handoff to a human. Do not include prose or non-status process steps. "
            "human_actions must cover every status a person has to act on: one whose "
            "name is in no skill's Entry statuses and which is not a final status. "
            "action is one sentence of at most 140 characters, addressed to that "
            "person, saying what they have to do for the work item to leave the "
            "status. Base it on the skill instructions that reach or leave the status "
            "and do not invent work the skills never describe. "
            "human_transitions are the status changes a person makes rather than a "
            "skill: every one must start from a status a person acts on, so its "
            "source must not appear in any skill's Entry statuses. Emit one wherever "
            "a skill says a person carries the work on from such a status, so the "
            "graph does not stop there. Its evidence must be a verbatim quote from "
            "one of the supplied skills that names its target status. Never repeat a "
            "transition already listed in transitions.\n\n"
            + "\n\n".join(documents)
        )
        agent = self._build_coding_agent()
        report(
            f"Generating workflow for work item {issue_type.capitalize()} "
            f"from {_count(len(skills), 'issue-trigger skill')}..."
        )
        validation_error = ""
        for attempt in range(2):
            request = prompt
            if validation_error:
                request += (
                    "\n\nYour previous response was invalid: "
                    f"{validation_error}. Return corrected JSON only."
                )
            response = agent.run(
                request, str(uuid.uuid4()), agent.best_model())
            payload_text = _strip_code_fence(response)
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
                    other_item = scope.other_work_item(evidence)
                    if other_item:
                        raise ValueError(
                            f"transition evidence from {skill.name} moves the "
                            f"{other_item} rather than the {issue_type}, so "
                            f"{transition['target']} is not a {issue_type} status")
                final_statuses = _string_values(payload.get("final_statuses"))
                if any(
                    status.casefold() not in declared_statuses
                    for status in final_statuses
                ):
                    raise ValueError(
                        "each final status must be a declared status")
                human_actions = _human_action_values(
                    payload.get("human_actions"))
                if any(status not in declared_statuses
                       for status in human_actions):
                    raise ValueError(
                        "each human action must name a declared status")
                human_transitions = _transition_values(
                    payload.get("human_transitions"))
                for transition in human_transitions:
                    if (transition["source"].casefold() not in declared_statuses
                            or transition["target"].casefold()
                            not in declared_statuses):
                        raise ValueError(
                            "each human transition must connect two declared statuses")
                    if transition["source"].casefold() in triggered:
                        raise ValueError(
                            f"human transition source {transition['source']} is an "
                            "entry status of a skill, so the skill makes that move")
                    evidence = transition["evidence"]
                    if not evidence or not any(
                        evidence in document
                        for _, document in skill_documents.values()
                    ):
                        raise ValueError(
                            "human transition evidence is not an exact quote "
                            "from a skill")
                    if transition["target"].casefold() not in evidence.casefold():
                        raise ValueError(
                            "human transition evidence does not name its target")
                    other_item = scope.other_work_item(evidence)
                    if other_item:
                        raise ValueError(
                            f"human transition evidence moves the {other_item} "
                            f"rather than the {issue_type}, so "
                            f"{transition['target']} is not a {issue_type} status")
                break
            except (json.JSONDecodeError, ValueError) as error:
                validation_error = str(error)
                if attempt == 1:
                    report(
                        f"Detected error in the workflow: {error}. "
                        f"The coding agent could not correct it."
                    )
                    raise ValueError(
                        f"Coding agent returned invalid workflow data: {error}"
                    ) from error
                report(
                    f"Detected error in the workflow: {error}, "
                    f"asking agent to fix..."
                )

        # Every transition that got this far quotes a sentence moving this work
        # item, so its statuses stay. A status with no such transition behind it
        # only has the prose to vouch for it, and prose about a subtask is how
        # the subtask's statuses end up on the story's graph.
        moved = {status.casefold()
                 for transition in transitions + human_transitions
                 for status in (transition["source"], transition["target"])}
        statuses, foreign_statuses = _own_work_item_statuses(
            statuses, scope, triggered | moved,
            dict.fromkeys(document for _, document in skill_documents.values()),
        )
        if foreign_statuses:
            report(
                f"Left {_count(len(foreign_statuses), 'status', 'statuses')} "
                f"off the {issue_type.capitalize()} workflow, moved on another "
                f"work item: {', '.join(foreign_statuses)}."
            )
            kept = {status.casefold() for status in statuses}
            final_statuses = [status for status in final_statuses
                              if status.casefold() in kept]
            human_actions = {status: action
                             for status, action in human_actions.items()
                             if status in kept}

        transitions = _remove_redundant_skill_transitions(transitions)
        report(
            f"{issue_type.capitalize()} workflow: "
            f"{_count(len(statuses), 'status', 'statuses')} and "
            f"{_count(len(transitions), 'transition')}."
        )

        status_ids = {
            status.casefold(): f"status-{index}"
            for index, status in enumerate(statuses)
        }
        status_order = {
            status.casefold(): index for index, status in enumerate(statuses)
        }
        grouped_transitions: dict[tuple[str, str], dict[str, Any]] = {}
        # Human moves come first so the arrows a person makes keep the order
        # the agent listed them in. The two kinds can never meet in one group:
        # a human transition starts where no skill's Entry statuses reach.
        for transition, human_made in (
            [(transition, True) for transition in human_transitions]
            + [(transition, False) for transition in transitions]
        ):
            key = (transition["source"].casefold(),
                   transition["target"].casefold())
            grouped = grouped_transitions.setdefault(key, {
                "source": transition["source"],
                "target": transition["target"],
                "labels": [],
                "reasons": [],
                "human": human_made,
            })
            if (transition["label"]
                    and transition["label"] not in grouped["labels"]):
                grouped["labels"].append(transition["label"])
            # The quote the agent had to supply for the transition to be
            # accepted, kept so the graph can say why the arrow is there.
            if (transition["evidence"]
                    and transition["evidence"] not in grouped["reasons"]):
                grouped["reasons"].append(transition["evidence"])
        final = {status.casefold() for status in final_statuses}
        # A status no issue-trigger skill picks up, and which is not the end of
        # the road, only moves when a person moves it. Those are flagged on the
        # graph node itself instead of as a warning callout above the diagram.
        human = {
            status.casefold() for status in statuses
            if status.casefold() not in triggered and status.casefold() not in final
        }
        # What the person waiting on each of those statuses has to do. Absent
        # for a status the agent skipped, and for every automated status.
        human_actions = {
            status: action for status, action in human_actions.items()
            if status in human
        }
        warnings: list[str] = []
        disconnected = len(statuses) > 1 and not grouped_transitions
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
                "data": {
                    "label": status,
                    **({"humanAction": human_actions[status.casefold()]}
                       if status.casefold() in human_actions else {}),
                },
                "className": " ".join(
                    ["workflow-node"]
                    + (["workflow-node--disconnected"] if disconnected else [])
                    + (["workflow-node--human"]
                       if status.casefold() in human else [])
                ),
                # The hover tooltip is a `::after` on the shared node rule, so
                # this node's own sentence has to reach it as a custom
                # property; the rule falls back to generic wording without one.
                **({"style": {
                    "--codee-human-action": _css_string(
                        human_actions[status.casefold()]),
                }} if status.casefold() in human_actions else {}),
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
            is_human = transition["human"]
            if is_human:
                color = WORKFLOW_HUMAN_EDGE_COLOR
            else:
                color = "#d97706" if is_return else "#167d5a"
            edge_data = {
                "data": {
                    "skills": transition["labels"],
                    "reasons": transition["reasons"],
                },
                "type": "smoothstep",
                "animated": is_return,
                "className": " ".join(
                    ["workflow-edge"]
                    + (["workflow-edge--return"] if is_return else [])
                    + (["workflow-edge--human"] if is_human else [])
                    # Every segment of a transition that is drawn as a detour
                    # through route nodes carries the same group class, so
                    # hovering any one of them lights the whole run of arrows
                    # and dims the lines it crosses. Transitions past the cap
                    # simply keep the plain hover treatment.
                    + ([f"workflow-edge--g{index}"]
                       if index < WORKFLOW_HIGHLIGHT_GROUPS else [])
                ),
                "markerEnd": {"type": "arrowclosed", "color": color},
                "style": {
                    "stroke": color,
                    "strokeWidth": 2,
                    # Only read by the hover rule's glow, which has no other
                    # way to reach this edge's colour from a static stylesheet.
                    "color": color,
                    **({"strokeDasharray": "8 6"} if is_return else {}),
                },
            }
            aria_label = (
                f"{transition['source']} to {transition['target']}"
                + (f" via {', '.join(transition['labels'])}"
                   if transition["labels"]
                   # Colour is the only other thing saying who makes the move.
                   else " by a person" if is_human else "")
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
        """One skill's editable fields, as the admin UI's editor shows them.

        ``agent`` comes back as the canonical agent code, and empty when the
        skill names no agent Codee can run — the same reading the executor
        takes, which runs such a skill on the default agent from Settings.
        """
        path = self.skills_dir / slug / "SKILL.md"
        frontmatter, body = parse_skill(path.read_text())
        extra = {key: value for key, value in frontmatter.items()
                 if key not in MANAGED}
        return {
            "slug": slug,
            "name": str(frontmatter.get("name", slug)),
            "description": str(frontmatter.get("description", "")),
            "model": str(frontmatter.get("model", "") or ""),
            "agent": _agent_code(frontmatter.get("x-codee-agent", "")),
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
        # Both are left out entirely when unset, so the skill keeps running on
        # the default agent and whatever model that agent defaults to, rather
        # than on an empty agent code or model id.
        agent = skill.get("agent", "").strip()
        if agent:
            if resolve_agent_code(agent) is None:
                return False, False, f"{agent} is not an agent Codee can run", old_slug
            frontmatter["x-codee-agent"] = agent
        model = skill.get("model", "").strip()
        if model:
            frontmatter["model"] = model
        skill_type = skill["type"]
        if skill_type == "slash command":
            frontmatter["disable-model-invocation"] = True
        elif skill_type == "issue trigger":
            issue_type = skill.get("issue_type", "").strip().lower()
            issue_types = self.issue_types()
            if issue_type not in issue_types:
                return False, False, ("Select an issue type: "
                                      f"{', '.join(issue_types)}"), old_slug
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

    # --- Repositories -------------------------------------------------------

    def _run_git(
        self,
        arguments: list[str],
        cwd: Path,
        timeout: int = GIT_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **GIT_NON_INTERACTIVE_ENV},
        )

    def _git_output(self, arguments: list[str], cwd: Path) -> str:
        """One-line git output, or '' when the command fails. Never raises."""
        try:
            result = self._run_git(arguments, cwd)
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def list_repositories(self) -> list[dict[str, Any]]:
        """The repositories under `repositories/`, in name order.

        Only directories carrying a bare clone are listed: `repositories/` is
        the agents' working area, so anything else in it is scratch.
        """
        if not self.repositories_dir.is_dir():
            return []
        repositories = []
        for directory in sorted(self.repositories_dir.iterdir()):
            if not (directory / BARE_DIR).is_dir():
                continue
            repositories.append({
                "name": directory.name,
                "url": self._git_output(
                    ["remote", "get-url", "origin"], directory),
                "default_branch": self._git_output(
                    ["symbolic-ref", "--short", "HEAD"], directory),
            })
        return repositories

    def add_repository(self, url: str) -> tuple[bool, str, str]:
        """Clone `url` into the bare-plus-worktree layout the agents expect.

        Leaves behind `repositories/<name>/.bare`, a `.git` file pointing at it,
        and a worktree for whichever branch the remote calls its default. A
        failure anywhere removes the half-built directory so a retry is clean.
        """
        url = url.strip()
        if not url:
            return False, "Enter a repository URL", ""
        name = repository_name(url)
        if not name:
            return False, f"Could not work out a folder name from {url}", ""
        directory = self.repositories_dir / name
        if directory.exists():
            return False, f"{REPOSITORIES_DIR}/{name} already exists", name

        directory.mkdir(parents=True)
        try:
            self._clone_repository(url, directory)
            branch = self._git_output(
                ["symbolic-ref", "--short", "HEAD"], directory)
            if not branch:
                raise RuntimeError(
                    "Cloned, but the remote names no default branch")
            self._check(
                self._run_git(["worktree", "add", branch], directory),
                f"Could not create the {branch} worktree",
            )
        except Exception as error:
            shutil.rmtree(directory, ignore_errors=True)
            return False, str(error), name
        return True, f"Cloned {name} and checked out {branch}", name

    def _clone_repository(self, url: str, directory: Path) -> None:
        """Bare clone plus the wiring that makes the directory usable as a repo."""
        self._check(
            self._run_git(["clone", "--bare", url, BARE_DIR], directory,
                          timeout=GIT_NETWORK_TIMEOUT),
            f"Could not clone {url}",
        )
        # Makes git treat the repository directory itself as the repo, so the
        # worktree commands in AGENTS.md run from there without pointing at
        # `.bare` every time.
        (directory / ".git").write_text(f"gitdir: ./{BARE_DIR}\n")
        # A bare clone fetches straight into `refs/heads/*` and configures no
        # remote-tracking refspec, so without this `origin/<branch>` never
        # exists and later worktrees have nothing to branch off.
        self._check(
            self._run_git(["config", "remote.origin.fetch",
                           "+refs/heads/*:refs/remotes/origin/*"], directory),
            "Could not configure the origin refspec",
        )
        self._check(
            self._run_git(["fetch", "origin"], directory,
                          timeout=GIT_NETWORK_TIMEOUT),
            "Could not fetch from origin",
        )

    @staticmethod
    def _check(result: subprocess.CompletedProcess[str], message: str) -> None:
        if result.returncode != 0:
            detail = (result.stderr.strip() or result.stdout.strip()
                      or f"git exited with {result.returncode}")
            raise RuntimeError(f"{message}: {detail}")

    def dashboard(self) -> dict[str, Any]:
        task_url = self._task_url()
        active = []
        for job in runs_db.active_jobs(main_context=self.context):
            task = issue_prompt_task(job.get("message"))
            active.append({**job,
                           "elapsed_label": runs_db.fmt_elapsed(job["elapsed"]),
                           "task_key": task,
                           "task_url": task_url(task)})
        return {
            "active": active,
            "counts": runs_db.counts(main_context=self.context),
            "hourly": runs_db.runs_by_hour(main_context=self.context),
        }

    def _task_url(self) -> Callable[[str], str]:
        """How to link a work item key, per the configured tasks provider.

        Built once per refresh rather than once per row, because the dashboard
        redraws every second while anything is running. A provider that cannot
        be built — none configured yet — links nothing rather than failing the
        refresh: the live list is the part of the page that matters.
        """
        # Nothing is logged when this fails: the refresh it belongs to runs
        # once a second, and a provider that cannot be built stays that way
        # until someone changes it in Settings, where the failure is reported.
        try:
            provider = build_tasks_provider(self.context.settings)
        except Exception:  # noqa: BLE001 - no provider just means no link
            return lambda key: ""

        def url(key: str) -> str:
            try:
                return provider.task_url(key) if key else ""
            except Exception:  # noqa: BLE001 - as above
                return ""

        return url

    def recent_runs(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return runs_db.recent_runs(limit, offset, main_context=self.context)

    def load_settings(self) -> Settings:
        self.context.settings = load_settings(self.data_dir)
        return self.context.settings

    def issue_types(self) -> tuple[str, ...]:
        """The Codee work items skills may trigger on, per the saved settings.

        Read off disk rather than off the last-loaded copy: the skill editor
        and the workflow page are separate requests, and a work item added in
        Settings has to be selectable in the very next one.
        """
        return codee_issue_types(load_settings(self.data_dir))

    def work_item_types(self, tasks_provider: str = "") -> dict[str, str]:
        """Codee work item -> backend work item type, for the settings form.

        Defaults to the selected provider; naming another is how the settings
        page reads back the mapping it kept for a provider the user just
        switched to.
        """
        settings = load_settings(self.data_dir)
        try:
            provider = (TasksProvider(tasks_provider) if tasks_provider
                        else settings.tasks_provider)
        except ValueError:
            provider = settings.tasks_provider
        return work_item_types(settings, provider)

    def list_work_item_types(
        self,
        tasks_provider: str,
        credentials: dict[str, str],
    ) -> tuple[list[str], str, str]:
        """What the backend calls its work item types, for the mapping dropdown.

        Answers from the credentials on the form rather than from disk, like
        the connection check, so a user can pick their types before saving
        anything. Returns the names, where the provider looked for them, and an
        error message — the names and the error are never both filled in.
        """
        try:
            provider = self._provider_from_form(tasks_provider, credentials)
            return (provider.list_work_item_types(),
                    provider.work_item_types_scope(), "")
        except TasksProviderError as error:
            return [], "", str(error)
        except Exception as error:  # noqa: BLE001 - the UI must never see a traceback
            return [], "", f"{type(error).__name__}: {error}"

    def save_settings(
        self,
        tasks_provider: str,
        coding_agent: str,
        max_parallel_agents: int,
        credentials: dict[str, str],
        work_items: dict[str, str] | None = None,
        task_filter: str = "",
        claude_code_rotate_keys: bool = False,
    ) -> None:
        current = self.context.settings
        all_credentials = dict(current.credentials)
        all_credentials[tasks_provider] = credentials
        # Like the credentials: only the selected provider's mapping is being
        # edited, and the others are carried over so switching provider and
        # back doesn't lose the work items configured for it.
        all_work_items = dict(current.work_item_types)
        if work_items is not None:
            all_work_items[tasks_provider] = work_items
        # Same again for the custom query clause, which is written in the
        # selected provider's own query language and means nothing to the other.
        all_task_filters = dict(current.task_filters)
        all_task_filters[tasks_provider] = task_filter.strip()
        self.context.settings = Settings(
            tasks_provider=TasksProvider(tasks_provider),
            coding_agent=CodingAgent(coding_agent),
            credentials=all_credentials,
            work_item_types=all_work_items,
            task_filters=all_task_filters,
            claude_code_rotate_keys=claude_code_rotate_keys,
            max_parallel_agents=max(1, max_parallel_agents),
        )
        save_settings(self.data_dir, self.context.settings)
        self._reconcile_claude_code_account()

    def claude_code_available(self) -> bool:
        """Whether this machine has the Claude Code CLI at all.

        What decides if the settings page offers the account rotation section:
        the accounts are written into Claude Code's own credentials file, so on
        a machine running only Copilot or Codex the section would configure
        something that can never happen.
        """
        return ClaudeCodeAgent.is_installed()

    def claude_code_accounts(self) -> list[ConnectedAccount]:
        """The connected accounts, in rotation order, for the settings page."""
        try:
            current = claude_code_accounts.current_account_id(self.context)
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            return [ConnectedAccount(id=account.id, label=account.label,
                                     subscription=account.subscription_type,
                                     in_use=account.id == current,
                                     needs_reconnect=account.needs_reconnect(now))
                    for account in claude_code_accounts.accounts(self.context)]
        except Exception as error:  # noqa: BLE001 - the list must not break the page
            print(f"[admin] Failed to read the connected Claude Code accounts: "
                  f"{error}")
            return []

    def claude_code_account_usage(self) -> list[ConnectedAccount]:
        """The connected accounts with each one's remaining allowance.

        One network round trip per account, so it is cached for
        :data:`USAGE_CACHE_SECONDS` — the dashboard redraws every second and
        must not turn that into a request per second per account.

        Tokens are renewed first where they need it: an account that has been
        waiting its turn is holding an expired one and would report itself
        rejected rather than report its allowance.

        Never raises. A widget that cannot read one account's usage says so on
        that row and still draws the others.
        """
        listed = self.claude_code_accounts()
        if not listed:
            return []
        now = time.monotonic()
        with self._usage_lock:
            cached, fetched_at = self._usage_cache
            if cached is not None and now - fetched_at < USAGE_CACHE_SECONDS:
                return cached

        measured = [self._account_usage(account) for account in listed]
        with self._usage_lock:
            self._usage_cache = (measured, time.monotonic())
        return measured

    def _account_usage(self, listed: ConnectedAccount) -> ConnectedAccount:
        """One account's row, with its windows filled in where they could be read."""
        if listed.needs_reconnect:
            return replace(listed, usage_error="needs to be connected again")
        try:
            stored = {account.id: account for account
                      in claude_code_accounts.accounts(self.context)}[listed.id]
        except Exception as error:  # noqa: BLE001 - drawn as unreadable, not raised
            return replace(listed, usage_error=str(error))
        try:
            account = ensure_fresh(stored, self.context)
            usage = fetch_usage(account.access_token)
        except UsageUnavailable as error:
            return replace(listed, usage_error=str(error))
        except Exception as error:  # noqa: BLE001
            return replace(listed, usage_error=f"{type(error).__name__}: {error}")
        windows = usage.windows or {}
        resets = usage.resets_at or {}
        return replace(
            listed,
            session_percent=windows.get(SESSION_WINDOW, -1.0),
            weekly_percent=windows.get(WEEKLY_WINDOW, -1.0),
            session_resets=resets.get(SESSION_WINDOW, ""),
            weekly_resets=resets.get(WEEKLY_WINDOW, ""))

    def start_claude_code_authorization(self) -> claude_oauth.Authorization:
        """Begin a sign-in: the URL to open, and the secrets its code needs.

        Nothing is stored yet. The authorization is held by the page that
        started it and handed back to :meth:`complete_claude_code_authorization`,
        because a sign-in only means anything to the visit that began it — and a
        verifier parked on disk would outlive the browser tab it belongs to.
        """
        return claude_oauth.start_authorization()

    def complete_claude_code_authorization(
        self, pasted: str, authorization: claude_oauth.Authorization,
    ) -> tuple[bool, str]:
        """Redeem what the user pasted back, and store the account it yields.

        Returns whether it worked and a sentence to show either way. The
        account is named by asking whose it is — which this flow's token can
        answer and an inference-only one could not, and which is most of why
        the sign-in is worth doing at all.
        """
        code, state = claude_oauth.split_code(pasted)
        if not code:
            return False, "Paste the code from the Anthropic page first"
        # The page prints code and state together; a copy that included the
        # state has to agree with the sign-in it came from, or the code belongs
        # to a different authorization than the verifier about to redeem it.
        if state and state != authorization.state:
            return False, ("That code belongs to a different sign-in. Start "
                           "again and use the code from the page it opens.")
        try:
            tokens = claude_oauth.exchange_code(
                code, authorization.state, authorization.code_verifier)
        except claude_oauth.OAuthApiError as error:
            return False, f"Could not complete the sign-in: {error}"
        except Exception as error:  # noqa: BLE001 - the UI must never see a traceback
            return False, f"Could not complete the sign-in: {type(error).__name__}: {error}"

        # Best-effort: an account that cannot be named is still an account that
        # works, and the label can be filled in later.
        try:
            label = fetch_account(tokens.access_token)
        except AccountUnavailable as error:
            print(f"[admin] Connected a Claude Code account but could not read "
                  f"its profile: {error}")
            label = ""

        account_id = claude_code_accounts.add_account(
            label, tokens.access_token, tokens.refresh_token,
            tokens.expires_at, " ".join(tokens.scopes),
            tokens.subscription_type, self.context, tokens.refresh_expires_at)
        self._reconcile_claude_code_account()
        return True, (f"Connected {label}" if label
                      else f"Connected account {account_id}")

    def disconnect_claude_code_account(self, account_id: int) -> None:
        """Forget one account, and repoint the rotation if it was the one in use."""
        claude_code_accounts.remove_account(account_id, self.context)
        self._reconcile_claude_code_account()

    def _reconcile_claude_code_account(self) -> None:
        """Point the current account at a connected one.

        Two cases, both of which would otherwise leave the executor on an
        account the user can no longer see: rotation is on and nothing has ever
        been current, and the account that was current has just been
        disconnected. Both land on the first one.

        Rotation being off clears it, so switching back on later starts from the
        top rather than from whatever a previous run left behind.

        Best-effort: a change that landed must not be reported as failed
        because this could not be written.
        """
        try:
            accounts = claude_code_accounts.accounts(self.context)
            if not self.context.settings.claude_code_rotate_keys or not accounts:
                claude_code_accounts.clear_current_account(self.context)
                return
            current = claude_code_accounts.current_account_id(self.context)
            if current not in [account.id for account in accounts]:
                claude_code_accounts.set_current_account(accounts[0].id,
                                                         self.context)
        except Exception as error:  # noqa: BLE001
            print(f"[admin] Failed to record the current Claude Code account: "
                  f"{error}")

    def verify_tasks_connection(
        self,
        tasks_provider: str,
        credentials: dict[str, str],
        task_filter: str = "",
    ) -> Iterator[dict[str, Any]]:
        """Yield each check the settings page reports on, as it finishes.

        Each answers for one thing the executor depends on: that tasks can be
        pulled with these credentials, and that the coding agent can act on one
        through the provider's MCP server. They arrive one at a time rather than
        as a verdict, because the second runs a whole coding agent — the caller
        can put the first result on screen and a spinner on the second instead
        of holding a blank page until both are done.

        Runs against the values sitting in the settings form rather than what is
        on disk, so the checks answer "do these credentials work?" without first
        making the user save credentials that may be wrong. The custom query
        clause comes from the form too: a filter the backend rejects is exactly
        what this check exists to catch, and catching it after saving would mean
        every poll failing until someone reads the log.
        """
        try:
            provider = self._provider_from_form(
                tasks_provider, credentials, task_filter)
        except Exception as exc:
            yield _check(TASKS_CHECK, False, str(exc))
            return
        if not provider.is_configured():
            yield _check(TASKS_CHECK, False,
                         "Not configured yet — fill in every field above "
                         "(and connect, where the provider needs it).")
            return
        # The statuses are the ones the issue-triggered skills declare, which is
        # exactly what the executor polls for — a check that passes here is a
        # poll that works.
        pulled, message = provider.verify_connection(
            issue_statuses(find_issue_triggered_skills(
                self.skills_dir, self.issue_types())))
        yield _check(TASKS_CHECK, pulled, message)
        yield self._check_tasks_mcp(tasks_provider, provider, blocked=not pulled)

    def _check_tasks_mcp(
        self,
        tasks_provider: str,
        provider: AbstractTasksProvider,
        blocked: bool,
    ) -> dict[str, Any]:
        """Have the coding agent drive the provider's backend through MCP alone.

        This one costs a real agent run and leaves a real, closed task behind, so
        the cheap reasons not to attempt it are all taken first: no server in
        ``.mcp.json``, or credentials the pull above already rejected.
        """
        server_name = type(provider).MCP_SERVER_NAME
        if not self.tasks_mcp_configured(tasks_provider):
            return _check(MCP_CHECK, False,
                          "MCP server is not configured, click Setup MCP "
                          "server above")
        if blocked:
            return _check(MCP_CHECK, False,
                          "Not attempted: the check above has to pass first.")

        session_id = str(uuid.uuid4())
        # The id makes the task recognizable afterwards, and keeps two checks
        # run back to back from looking like the same leftover.
        steps = provider.mcp_check_steps(
            f"{MCP_CHECK_SUMMARY} {session_id[:8]}")
        if steps is None:
            return _check(MCP_CHECK, False,
                          f"{tasks_provider} cannot describe an MCP check; "
                          "fill in every field above.")
        try:
            agent = self._build_coding_agent()
            response = agent.run(_mcp_check_prompt(server_name, steps), session_id)
        except Exception as exc:
            return _check(MCP_CHECK, False, f"The coding agent failed: {exc}")
        return _mcp_check_result(server_name, response)

    def _build_coding_agent(self) -> AbstractCodingAgent:
        """The configured coding agent, pointed at the project root."""
        try:
            return build_coding_agent(self.context.settings, self.root)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    def setup_tasks_mcp(
        self,
        tasks_provider: str,
        credentials: dict[str, str],
    ) -> tuple[bool, str]:
        """Write the provider's MCP server into the project's ``.mcp.json``.

        Gives the coding agent a way to read and update the task it was handed,
        at the source. The provider says which server that is and what it needs;
        the file it goes into, and the two shapes it has to be written in for
        Claude Code and GitHub Copilot both, are decided here.
        """
        try:
            provider = self._provider_from_form(tasks_provider, credentials)
            server = provider.mcp_server()
        except Exception as exc:
            return False, str(exc)
        if server is None:
            return False, (f"{tasks_provider} has no MCP server to set up, or "
                           "the fields it would need aren't filled in yet.")
        try:
            path = write_mcp_server(self.root, server)
        except (OSError, ValueError) as exc:
            return False, str(exc)
        return True, f"{server.name} is configured in {path}"

    def tasks_mcp_configured(self, tasks_provider: str) -> bool:
        """Whether this provider's MCP server is already in the project's ``.mcp.json``.

        Asked of the file rather than of the credentials, because that is the
        question the settings page is answering: the server may have been set up
        in an earlier session, or by hand. Whether the credentials in it are
        still the right ones is what running the setup again is for.
        """
        try:
            provider_class = TASKS_PROVIDERS[TasksProvider(tasks_provider)]
        except (KeyError, ValueError):
            return False
        name = provider_class.MCP_SERVER_NAME
        return bool(name) and find_mcp_server(self.root, name) is not None

    def _provider_from_form(
        self,
        tasks_provider: str,
        credentials: dict[str, str],
        task_filter: str = "",
    ) -> AbstractTasksProvider:
        """Build a provider from the credentials as they stand on the settings form.

        The form rather than the disk, so the settings page can act on what the
        user is looking at without first making them save credentials that may
        be wrong. Nothing here is persisted.

        The custom query clause defaults to none rather than to what is stored,
        because the callers that don't pass one aren't querying tasks — listing
        work item types and writing the MCP config are both unaffected by it,
        and a saved filter that no longer parses would break them for no reason.
        """
        try:
            provider_key = TasksProvider(tasks_provider)
        except ValueError:
            raise ValueError(f"Unknown tasks provider: {tasks_provider}")
        current = self.load_settings()
        settings = replace(
            current,
            tasks_provider=provider_key,
            credentials={**current.credentials, tasks_provider: credentials},
            task_filters={**current.task_filters,
                          tasks_provider: task_filter},
        )
        return build_tasks_provider(settings)

    # --- Azure DevOps OAuth -------------------------------------------------

    def admin_base_url(self) -> str:
        """Origin the browser reaches this admin UI on.

        Taken from the port the launcher passed through ``REFLEX_API_URL`` so the
        redirect URI follows ``codee-admin --port``. Set ``CODEE_ADMIN_BASE_URL``
        when the UI is reached through some other host or scheme.
        """
        override = public_base_url()
        if override:
            return override
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
            return False, ("Fill in organization URL, client ID and client "
                           "secret before connecting.")
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
