"""Reflex UI for managing Codee."""
from __future__ import annotations

import asyncio
from typing import Any

import reflex as rx
from pydantic import BaseModel

from codee.admin_api import api_app
from codee.admin_service import (
    AGENTS_FILE, TASKS_CHECKS, WORKFLOW_HIGHLIGHT_GROUPS, AdminService,
    SKILL_TYPES, WorkflowGeneration, normalize_work_items)
from codee.workflow_graph import workflow_graph
from codee_main_context.context import (
    DEFAULT_ISSUE_TYPES, TasksProvider, credential_field)

SERVICE = AdminService()

RUNS_PAGE_SIZE = 20
# The agents a skill can be run by, as the editor's picker lists them. Built
# once: which agents exist is decided by this build, not by the settings.
DEFAULT_AGENT_OPTION = "Default agent"
AGENT_NAMES = {agent["code"]: agent["name"] for agent in SERVICE.list_agents()}
AGENT_CODES = {name: code for code, name in AGENT_NAMES.items()}
AGENT_OPTIONS = [DEFAULT_AGENT_OPTION, *AGENT_NAMES.values()]
# How often the workflow page picks up the lines the generation reported.
WORKFLOW_PROGRESS_INTERVAL = 0.5
# How often the dashboard asks for the accounts' allowances. The service caches
# the answer for longer still; this only decides how soon a fresh cache is
# picked up, and how long a reading can be older than the cache allows before
# the page catches up. Allowance moves over hours, so neither number needs to
# be small — and a dashboard left open all day is the thing that turns a small
# one into a rate limit on Anthropic's side.
USAGE_POLL_INTERVAL = 60
# Whether this machine has the Claude Code CLI, and so whether the settings
# page offers its access keys at all. Asked once: an agent does not get
# installed or uninstalled under a running Codee, and the answer shapes the
# page rather than anything the user can change on it.
CLAUDE_CODE_AVAILABLE = SERVICE.claude_code_available()


def _skill_summary(skill: dict[str, str]) -> SkillSummary:
    """One listing row, with its agent named the way the editor's picker does."""
    return SkillSummary(**{
        **skill,
        "agent": AGENT_NAMES.get(skill["agent"], DEFAULT_AGENT_OPTION)})


def _usage_row(account: Any) -> "ClaudeAccount":
    """One account's dashboard row, with its percentages rounded to whole numbers.

    Rounded here rather than in the service because it is a presentation
    choice: the meter takes an integer, and nobody reads an allowance to a
    decimal place. A window that was never read keeps its -1.
    """
    return ClaudeAccount(**{
        **account.__dict__,
        "session_percent": _percent(account.session_percent),
        "weekly_percent": _percent(account.weekly_percent)})


def _percent(value: float) -> int:
    return -1 if value < 0 else round(value)


def local_datetime(value: rx.Var) -> rx.Component:
    """Render an ISO timestamp in the browser's locale and timezone."""
    return rx.moment(
        date=value,
        local=True,
        locale=rx.Var(_js_expr="navigator.language"),
        format="L LT",
    )


def _save_toast(persisted: bool, pushed: bool, message: str) -> Any:
    """Warn instead of erroring when the change landed on disk but not in Git."""
    if not persisted:
        return rx.toast.error(message)
    return rx.toast.success(message) if pushed else rx.toast.warning(message)


class SkillSummary(BaseModel):
    slug: str
    name: str
    description: str
    type: str
    # The agent as the card names it, so always filled in: a skill that names
    # none is run by the default one, which is worth saying on the card.
    agent: str = ""
    model: str = ""
    issue_status: str = ""
    issue_type: str = ""


class ModelOption(BaseModel):
    """One entry in the skill editor's model picker: code plus friendly name."""

    id: str
    name: str


class ConversationMessage(BaseModel):
    role: str
    content: str


class ClaudeAccount(BaseModel):
    """One connected Claude account as the settings page lists it.

    No token in sight: the page needs to say which account this is and whether
    it is the one in use, and nothing more. A credential that never leaves the
    server cannot leak from the browser.
    """

    id: int
    # The email the sign-in was granted by. Only empty for an account whose
    # profile could not be read when it was connected.
    label: str
    subscription: str = ""
    # The one the executor is running Claude Code on right now, per SQLite.
    # It just answers "which of these is live?", which is the first thing
    # anyone looking at this list wants to know.
    in_use: bool = False
    # The account's refresh token has run out, so nothing can renew it and
    # rotation skips it. The only thing on this row the user has to act on.
    needs_reconnect: bool = False
    # Percent of each window already spent, and when each comes back. Whole
    # numbers, because that is what the meter takes and what anyone reads off
    # it. -1 means the usage was never read, which has to draw differently
    # from zero.
    session_percent: int = -1
    weekly_percent: int = -1
    session_resets: str = ""
    weekly_resets: str = ""
    usage_error: str = ""


class MemoryEntry(BaseModel):
    title: str
    file: str
    hook: str
    lineno: int
    raw: str
    matched: bool


class RepositorySummary(BaseModel):
    name: str
    url: str
    default_branch: str


class ActiveJob(BaseModel):
    message: str
    elapsed_label: str
    viewer_url: str
    # The prompt up to the work item it names, so the row can print that part
    # as a link; the whole prompt when there is nothing to link.
    prompt_prefix: str = ""
    # The work item an issue-triggered run was started for, and where the tasks
    # provider shows it. Both empty unless the provider can address it.
    task_key: str = ""
    task_url: str = ""
    # Who is doing the work: the coding agent's display name, and the model the
    # triggering skill asked for. ``model`` is empty when the skill named none,
    # which the row reads as the agent running on its own default.
    agent: str = ""
    model: str = ""


def _active_job(job: dict[str, Any]) -> ActiveJob:
    """One in-flight run as its dashboard row reads it.

    The prompt is split around the work item the run was triggered for, so the
    row can print that part as a link into the tasks provider and still read as
    the single line the agent was handed. Only a work item the provider can
    address is split off — and only while the truncated prompt still ends with
    it, so a long prompt cut mid-key stays plain text rather than linking to
    half a key.
    """
    message = (job.get("message") or "(no prompt)")[:140]
    key = job.get("task_key") or ""
    url = job.get("task_url") or ""
    linked = bool(url) and message.rstrip().endswith(key)
    return ActiveJob(
        message=message,
        prompt_prefix=message.rstrip()[:-len(key)] if linked else message,
        task_key=key if linked else "",
        task_url=url if linked else "",
        elapsed_label=job["elapsed_label"],
        viewer_url=(SERVICE.session_viewer.format(session_id=job["session_id"])
                    if SERVICE.session_viewer and job.get("session_id") else ""),
        agent=job.get("agent") or "",
        model=job.get("model") or "",
    )


class CheckResult(BaseModel):
    """One line of the tasks-provider check list on the settings page.

    ``status`` is where it is rather than how it went: a check has to be on
    screen before it has an answer, so "waiting" and "running" are states of the
    same row that later becomes ``ok`` or ``failed``.
    """

    name: str
    status: str
    message: str = ""


def pending_checks(done: list[CheckResult]) -> list[CheckResult]:
    """The finished checks, then the running one, then the ones still to come."""
    rows = list(done)
    for position, name in enumerate(TASKS_CHECKS[len(done):], start=len(done)):
        rows.append(CheckResult(
            name=name,
            status="running" if position == len(done) else "waiting"))
    return rows


def check_result(check: dict[str, Any]) -> CheckResult:
    return CheckResult(name=check["name"],
                       status="ok" if check["ok"] else "failed",
                       message=check["message"])


class WorkItem(BaseModel):
    """One row of the work item mapping on the settings page.

    A row says how its work item is found, in one of two ways. In ``types``
    mode it names the backend's own work item types — a list, because one Codee
    work item can stand for several of them at once: a Codee `task` that is
    both a "Task" and a "Bug" over there is still one work item, handled by one
    set of skills. In ``query`` mode it carries a condition in the provider's
    own language, which replaces the type list entirely.

    Both are kept while the page is open, so switching between the modes does
    not throw away what the other one held. Only the active one is saved —
    a stored query is what says the row is in query mode, so a row switched
    back to types has to give its query up.

    ``fixed`` marks the work items Codee cannot run without: they are listed
    like the rest and pointed at whatever the backend calls them, but their
    name is not the user's to change and they have no remove button.
    """

    name: str
    provider_types: list[str] = []
    query: str = ""
    mode: str = "types"
    fixed: bool = False


class WorkflowSection(BaseModel):
    """One Codee work item's status graph, as the workflow page renders it."""

    issue_type: str
    title: str
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    warnings: list[str] = []


class RunRecord(BaseModel):
    skill_name: str
    trigger_type: str
    status: str
    error: str
    started_at: str
    session_id: str
    message: str
    user_message: str
    response: str
    preview: str
    viewer_url: str


class AdminState(rx.State):
    skills: list[SkillSummary] = []
    skill_query: str = ""
    skill_filter: str = "All"
    new_skill_name: str = ""
    selected_skill: str = ""
    skill_name: str = ""
    skill_description: str = ""
    skill_model: str = ""
    # The agent code the skill declares, empty for the default agent.
    skill_agent: str = ""
    skill_type: str = "knowledge"
    skill_cron: str = "0 0 * * *"
    skill_email: str = ""
    skill_sqs: str = ""
    skill_issue_status: str = ""
    skill_issue_type: str = "story"
    skill_body: str = ""
    skill_extra: str = ""
    skill_extra_enabled: bool = False

    agent_models: list[ModelOption] = []
    model_query: str = ""
    models_loading: bool = False

    editing_agents: bool = False
    agents_content: str = ""

    memories: list[MemoryEntry] = []
    selected_memory: str = ""
    memory_content: str = ""

    repositories: list[RepositorySummary] = []
    new_repository_url: str = ""
    adding_repository: bool = False

    active_jobs: list[ActiveJob] = []
    total_runs: int = 0
    last_24h_runs: int = 0
    hourly_runs: list[dict[str, Any]] = []
    # Which visit is polling the dashboard. Every visit takes the poll over
    # from the one before it, so a poll no visit is watching any more cannot
    # leave the page frozen on the numbers it last wrote.
    dashboard_watch: int = 0

    # The accounts with their allowance, for the dashboard widget. Separate
    # from ``claude_code_accounts``, which the settings page fills without a
    # network call: this one costs a request per account and is refreshed on a
    # slow loop of its own.
    claude_account_usage: list[ClaudeAccount] = []
    claude_usage_loading: bool = False

    runs: list[RunRecord] = []
    runs_has_more: bool = False
    runs_loading: bool = False
    session_viewer: str = SERVICE.session_viewer

    workflow_sections: list[WorkflowSection] = []
    workflow_error: str = ""
    workflow_loading: bool = False
    # Whether a generation is in flight at all. Separate from the spinner that
    # replaces the page, because a run started while a graph is on screen has
    # to show somewhere too: a Regenerate that reports nothing reads as a
    # button that does nothing.
    workflow_running: bool = False
    # What the generation is doing right now, newest line last. Inferring a
    # graph is minutes of coding-agent work, so a bare spinner says too little.
    workflow_progress: list[str] = []
    # Which visit is watching the generation. Every visit takes the watch over
    # from the one before it, so a watcher that can no longer reach this page
    # cannot leave it on a spinner until Codee is restarted.
    workflow_watch: int = 0
    edge_menu_skills: list[str] = []
    edge_menu_left: str = "0px"
    edge_menu_top: str = "0px"
    # The hovered transition's evidence quotes, shown next to the pointer so
    # an arrow can explain itself without being clicked.
    edge_tooltip_reasons: list[str] = []
    edge_tooltip_left: str = "0px"
    edge_tooltip_top: str = "0px"

    tasks_provider: str = "jira"
    coding_agent: str = "claude_code"
    max_parallel_agents: str = "3"
    agent_test_open: bool = False
    agent_test_input: str = ""
    agent_test_session_id: str = ""
    agent_test_messages: list[ConversationMessage] = []
    agent_test_sending: bool = False
    agent_test_generation: int = 0
    agent_test_model: str = ""
    agent_test_models: list[ModelOption] = []
    agent_test_model_query: str = ""
    agent_test_models_loading: bool = False
    # Whether the executor runs Claude Code on the connected accounts instead
    # of leaving ~/.claude/.credentials.json alone.
    claude_code_rotate_keys: bool = False
    claude_code_accounts: list[ClaudeAccount] = []
    # The sign-in in flight, if any: the page shows the URL to open and waits
    # for the code. The verifier behind it is backend-only — it is what proves
    # the code was redeemed by whoever asked for it, so it never goes to the
    # browser.
    claude_code_authorize_url: str = ""
    _claude_code_authorization: Any = None
    claude_code_auth_code: str = ""
    claude_code_connecting: bool = False
    jira_base_url: str = ""
    jira_account_email: str = ""
    jira_api_token: str = ""
    jira_project: str = ""
    # The extra query clause each provider's poll is narrowed by, kept in a
    # field of its own per provider like the credentials are: it is written in
    # that provider's query language, so switching provider has to swap it.
    jira_task_filter: str = ""
    azure_task_filter: str = ""
    azure_organization_url: str = ""
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_connected: bool = False
    azure_account: str = ""
    azure_expires_label: str = ""
    azure_redirect_uri: str = ""
    work_items: list[WorkItem] = []
    # Rows edited this session but left behind by a provider switch, keyed by
    # the provider they were edited for. The credentials get this for free by
    # having a flat field per provider; the mapping is one shared list, so it
    # has to be parked by hand or switching away and back would discard it.
    other_work_items: dict[str, list[WorkItem]] = {}
    provider_work_item_types: list[str] = []
    # Where the fetched list came from, in the provider's own words. Shown
    # beside the count, because a list narrower than the backend as a whole
    # otherwise reads as types gone missing.
    provider_work_item_types_scope: str = ""
    work_item_types_loading: bool = False
    work_item_types_error: str = ""
    skill_issue_types: list[str] = list(DEFAULT_ISSUE_TYPES)
    tasks_verifying: bool = False
    tasks_checks: list[CheckResult] = []
    mcp_setup_ok: bool = False
    mcp_setup_message: str = ""
    mcp_configured: bool = False

    @rx.var
    def filtered_skills(self) -> list[SkillSummary]:
        query = self.skill_query.strip().lower()
        return [
            skill for skill in self.skills
            if (not query or query in f"{skill.name} {skill.description}".lower())
            and (self.skill_filter == "All" or skill.type == self.skill_filter)
        ]

    @rx.var
    def agents_card_visible(self) -> bool:
        """AGENTS.md is not a skill, so only an unfiltered search can hide it."""
        return (self.skill_filter == "All"
                and self.skill_query.strip().lower() in AGENTS_FILE.lower())

    @rx.var
    def cron_description(self) -> str:
        return SERVICE.describe_cron(self.skill_cron)

    @rx.var
    def filtered_models(self) -> list[ModelOption]:
        query = self.model_query.strip().lower()
        return [
            model for model in self.agent_models
            if not query or query in f"{model.name} {model.id}".lower()
        ]

    @rx.var
    def skill_agent_label(self) -> str:
        """The picker's label for the agent the skill declares."""
        return AGENT_NAMES.get(self.skill_agent, DEFAULT_AGENT_OPTION)

    @rx.var
    def skill_agent_hint(self) -> str:
        """What the picked agent means for the skill, under the select."""
        if not self.skill_agent:
            return ("Runs on the default agent from Settings, so changing that "
                    "setting moves this skill too.")
        return (f"Saved as x-codee-agent: {self.skill_agent} in the skill "
                "frontmatter.")

    @rx.var
    def skill_model_label(self) -> str:
        """Friendly name of the selected model, falling back to the raw code."""
        if not self.skill_model:
            return "Agent default"
        for model in self.agent_models:
            if model.id == self.skill_model:
                return model.name
        return self.skill_model

    @rx.var
    def custom_model_query(self) -> str:
        """The search text when it names no known model, so it can be used as-is."""
        query = self.model_query.strip()
        if not query or any(model.id == query for model in self.agent_models):
            return ""
        return query

    @rx.var
    def filtered_agent_test_models(self) -> list[ModelOption]:
        query = self.agent_test_model_query.strip().lower()
        return [
            model for model in self.agent_test_models
            if not query or query in f"{model.name} {model.id}".lower()
        ]

    @rx.var
    def agent_test_model_label(self) -> str:
        if not self.agent_test_model:
            return "Agent default"
        for model in self.agent_test_models:
            if model.id == self.agent_test_model:
                return model.name
        return self.agent_test_model

    @rx.var
    def custom_agent_test_model_query(self) -> str:
        query = self.agent_test_model_query.strip()
        if not query or any(
            model.id == query for model in self.agent_test_models
        ):
            return ""
        return query

    @rx.var
    def active_route(self) -> str:
        return self.router.url.path.rstrip("/") or "/"

    def set_skill_query(self, value: str) -> None:
        self.skill_query = value

    def set_skill_filter(self, value: str) -> None:
        self.skill_filter = value

    def set_new_skill_name(self, value: str) -> None:
        self.new_skill_name = value

    def set_skill_name(self, value: str) -> None:
        self.skill_name = value

    def set_skill_description(self, value: str) -> None:
        self.skill_description = value

    def set_skill_type(self, value: str) -> None:
        self.skill_type = value

    def set_skill_agent(self, label: str) -> Any:
        """Pick the agent the skill runs on, and re-fetch its model catalog.

        The model goes back to the agent's default: model ids belong to one
        agent, so the one that was picked names nothing on the new one.
        """
        agent = AGENT_CODES.get(label, "")
        if agent == self.skill_agent:
            return None
        self.skill_agent = agent
        self.skill_model = ""
        self.model_query = ""
        return AdminState.load_agent_models

    def set_model_query(self, value: str) -> None:
        self.model_query = value

    def choose_model(self, model_id: str) -> None:
        """Pick a model from the list, or use whatever the user typed."""
        self.skill_model = model_id.strip()
        self.model_query = ""

    @rx.event(background=True)
    async def load_agent_models(self) -> None:
        """Fetch the catalog of the agent this skill runs on, off the event loop.

        Asking an agent can mean spawning its CLI, so this runs in the
        background while the skill list renders; the picker still accepts a
        hand-typed model code if the list never arrives.

        A fetch whose agent has been picked away from while it was in flight
        drops its answer: the newer pick has one of its own coming, and the
        catalogs of two agents share no model ids to confuse.
        """
        async with self:
            agent = self.skill_agent
            self.agent_models = []
            self.models_loading = True
        try:
            models = await asyncio.to_thread(SERVICE.list_agent_models, agent)
        except Exception:
            models = []
        async with self:
            if self.skill_agent != agent:
                return
            self.agent_models = [ModelOption(**model) for model in models]
            self.models_loading = False

    def set_skill_cron(self, value: str) -> None:
        self.skill_cron = value

    def set_skill_email(self, value: str) -> None:
        self.skill_email = value

    def set_skill_sqs(self, value: str) -> None:
        self.skill_sqs = value

    def set_skill_issue_status(self, value: str) -> None:
        self.skill_issue_status = value

    def set_skill_issue_type(self, value: str) -> None:
        self.skill_issue_type = value

    def set_skill_body(self, value: str) -> None:
        self.skill_body = value

    def set_skill_extra(self, value: str) -> None:
        self.skill_extra = value

    def set_skill_extra_enabled(self, value: bool) -> None:
        self.skill_extra_enabled = value

    def load_skills(self) -> None:
        self.skills = [_skill_summary(skill)
                       for skill in SERVICE.list_skills()]
        # The editor's issue-type picker offers the work items Settings
        # configures, so a work item added there is selectable right away.
        self.skill_issue_types = list(SERVICE.issue_types())

    def create_skill(self) -> Any:
        saved, pushed, message, slug = SERVICE.create_skill(
            self.new_skill_name)
        toast = _save_toast(saved, pushed, message)
        if not saved:
            return toast
        self.new_skill_name = ""
        self.load_skills()
        return [self.edit_skill(slug), toast]

    def edit_skill(self, slug: str) -> Any:
        """Open one skill in the editor, and load the models its agent offers."""
        skill = SERVICE.load_skill(slug)
        self.editing_agents = False
        self.selected_skill = skill["slug"]
        self.skill_name = skill["name"]
        self.skill_description = skill["description"]
        self.skill_model = skill["model"]
        self.skill_agent = skill["agent"]
        self.model_query = ""
        self.skill_type = skill["type"]
        self.skill_cron = skill["cron"]
        self.skill_email = skill["email"]
        self.skill_sqs = skill["sqs"]
        self.skill_issue_status = skill["issue_status"]
        self.skill_issue_type = skill["issue_type"] or "story"
        self.skill_body = skill["body"]
        self.skill_extra = skill["extra"]
        self.skill_extra_enabled = bool(skill["extra"])
        # The catalog on screen belongs to whichever skill was open before this
        # one, and only this agent's models can be picked for this skill.
        return AdminState.load_agent_models

    def close_skill(self) -> None:
        self.selected_skill = ""

    def save_skill(self) -> Any:
        saved, pushed, message, slug = SERVICE.save_skill({
            "slug": self.selected_skill,
            "name": self.skill_name,
            "description": self.skill_description,
            "model": self.skill_model,
            "agent": self.skill_agent,
            "type": self.skill_type,
            "cron": self.skill_cron,
            "email": self.skill_email,
            "sqs": self.skill_sqs,
            "issue_status": self.skill_issue_status,
            "issue_type": self.skill_issue_type,
            "body": self.skill_body,
            # Unchecking the box drops the fields from the frontmatter, while
            # the text stays around in case the box goes back on.
            "extra": self.skill_extra if self.skill_extra_enabled else "",
        })
        if saved:
            self.selected_skill = slug
            self.load_skills()
        return _save_toast(saved, pushed, message)

    def delete_skill(self) -> Any:
        deleted, pushed, message = SERVICE.delete_skill(self.selected_skill)
        if deleted:
            self.selected_skill = ""
            self.load_skills()
        return _save_toast(deleted, pushed, message)

    def force_run_skill(self) -> Any:
        SERVICE.force_run_skill(self.selected_skill)
        return rx.toast.success("Queued to run on the next trigger tick")

    def edit_agents(self) -> None:
        self.selected_skill = ""
        self.agents_content = SERVICE.load_agents()
        self.editing_agents = True

    def open_agents_editor(self) -> Any:
        """Open the AGENTS.md editor from a page that isn't Skills."""
        self.edit_agents()
        return rx.redirect("/skills")

    def set_agents_content(self, value: str) -> None:
        self.agents_content = value

    def close_agents(self) -> None:
        self.editing_agents = False

    def save_agents(self) -> Any:
        return _save_toast(*SERVICE.save_agents(self.agents_content))

    def load_memories(self) -> None:
        self.load_coding_agent()
        if self.coding_agent == "github_copilot":
            self.memories = []
            self.selected_memory = ""
            return
        self.memories = [MemoryEntry(**entry)
                         for entry in SERVICE.list_memories()]

    def edit_memory(self, filename: str) -> None:
        self.selected_memory = filename
        self.memory_content = SERVICE.load_memory(filename)

    def set_memory_content(self, value: str) -> None:
        self.memory_content = value

    def close_memory(self) -> None:
        self.selected_memory = ""

    def save_memory(self) -> Any:
        saved, pushed, message = SERVICE.save_memory(
            self.selected_memory, self.memory_content)
        if saved:
            self.load_memories()
        return _save_toast(saved, pushed, message)

    def delete_memory(self, filename: str, raw: str) -> Any:
        deleted, pushed, message = SERVICE.delete_memory(filename, raw)
        if filename == self.selected_memory:
            self.selected_memory = ""
        self.load_memories()
        return _save_toast(deleted, pushed, message)

    def load_repositories(self) -> None:
        self.repositories = [RepositorySummary(**repository)
                             for repository in SERVICE.list_repositories()]

    def set_new_repository_url(self, value: str) -> None:
        self.new_repository_url = value

    @rx.event(background=True)
    async def add_repository(self) -> Any:
        """Clone in the background: a first clone can take minutes."""
        async with self:
            if self.adding_repository:
                return
            url = self.new_repository_url.strip()
            self.adding_repository = True
        try:
            added, message, _ = await asyncio.to_thread(
                SERVICE.add_repository, url)
        except Exception as error:
            added, message = False, str(error)
        async with self:
            self.adding_repository = False
            if added:
                self.new_repository_url = ""
                self.load_repositories()
        yield rx.toast.success(message) if added else rx.toast.error(message)

    def load_dashboard_page(self) -> Any:
        """Everything the dashboard needs, in the order it needs it.

        The setting is read first and on the main thread: the usage poll starts
        only when rotation is on, and a background event listed alongside would
        start before this one committed and find the flag still false.
        """
        self.claude_code_rotate_keys = SERVICE.load_settings(
        ).claude_code_rotate_keys
        if not self.claude_code_rotate_keys:
            self.claude_account_usage = []
        return [AdminState.poll_dashboard, AdminState.poll_claude_account_usage]

    def _refresh_dashboard(self) -> None:
        dashboard = SERVICE.dashboard()
        self.active_jobs = [_active_job(job) for job in dashboard["active"]]
        self.total_runs = dashboard["counts"]["total"]
        self.last_24h_runs = dashboard["counts"]["last_24h"]
        self.hourly_runs = dashboard["hourly"]

    @rx.event(background=True)
    async def poll_dashboard(self) -> None:
        """Keep the dashboard live for as long as it is the page on screen.

        Elapsed times and the in-flight list only move because this rewrites
        them, so the poll belongs to the visit rather than to a flag saying
        someone once started one: a poll that ended with the page it was
        drawing is replaced by the next visit, instead of leaving a dashboard
        that never ticks again. Leaving the dashboard ends it too, rather than
        reading the database every second behind another page.
        """
        async with self:
            self.dashboard_watch += 1
            watch = self.dashboard_watch
        while True:
            async with self:
                # A later visit is polling now, or the dashboard is no longer
                # the page on screen: either way this poll is done.
                if self.dashboard_watch != watch or self.active_route != "/":
                    return
                try:
                    self._refresh_dashboard()
                except Exception as error:  # ponytail: one failed read must not
                    # end the poll; the next tick draws the numbers again.
                    print(f"[admin] Failed to refresh dashboard: {error}")
            await asyncio.sleep(1)

    @rx.event(background=True)
    async def poll_claude_account_usage(self) -> None:
        """Keep the dashboard's account allowances current while it is on screen.

        A loop of its own rather than a line in the dashboard poll: that one
        redraws every second off the local database, and this is a network
        round trip per connected account. It follows the same watch, so it ends
        when the dashboard does instead of polling Anthropic behind another
        page.
        """
        async with self:
            watch = self.dashboard_watch
            enabled = self.claude_code_rotate_keys
        if not enabled:
            return
        while True:
            async with self:
                if self.dashboard_watch != watch or self.active_route != "/":
                    return
                self.claude_usage_loading = not self.claude_account_usage

            try:
                measured = await asyncio.to_thread(
                    SERVICE.claude_code_account_usage)
            except Exception as error:  # noqa: BLE001 - one bad read must not
                # end the loop; the next pass draws the numbers again.
                print(f"[admin] Failed to read Claude account usage: {error}")
                measured = None

            async with self:
                self.claude_usage_loading = False
                if measured is not None:
                    self.claude_account_usage = [_usage_row(account)
                                                 for account in measured]
            await asyncio.sleep(USAGE_POLL_INTERVAL)

    def _fetch_runs_page(self, offset: int) -> list[RunRecord]:
        """One page of runs. Reads one row past the page to learn whether more exist."""
        rows = SERVICE.recent_runs(RUNS_PAGE_SIZE + 1, offset)
        self.runs_has_more = len(rows) > RUNS_PAGE_SIZE
        records = []
        for run in rows[:RUNS_PAGE_SIZE]:
            message = (run.get("message") or "").strip()
            preview = message.splitlines()[0] if message else "No message"
            records.append(RunRecord(
                skill_name=run["skill_name"],
                trigger_type=run["trigger_type"],
                status=run["status"],
                error=run.get("error") or "",
                started_at=run["started_at"],
                session_id=run.get("session_id") or "",
                message=message,
                user_message=(run.get("user_message") or message).strip(),
                response=(run.get("response") or "").strip(),
                preview=preview[:120] + ("..." if len(preview) > 120 else ""),
                viewer_url=(SERVICE.session_viewer.format(session_id=run["session_id"])
                            if SERVICE.session_viewer and run.get("session_id") else ""),
            ))
        return records

    def load_runs(self) -> None:
        """Load (or reload) the first page. Runs on every visit to /runs."""
        self.runs_loading = False
        self.runs = self._fetch_runs_page(0)

    def load_more_runs(self) -> None:
        if self.runs_loading or not self.runs_has_more:
            return
        self.runs_loading = True
        try:
            self.runs = self.runs + self._fetch_runs_page(len(self.runs))
        finally:
            self.runs_loading = False

    @rx.event(background=True)
    async def load_workflow(self, force: bool = False) -> None:
        """Show the workflow, attaching to a generation already under way.

        The generation belongs to the service, so every visit reads where it
        has got to rather than trusting a flag an earlier visit set: leaving
        the page mid-generation and coming back shows the run's own progress
        and then its graph, instead of a spinner nothing can clear.
        """
        status = SERVICE.start_workflow_generation(force)
        async with self:
            self.workflow_watch += 1
            watch = self.workflow_watch
            self.edge_menu_skills = []
            self._show_workflow(status)
        while status.running:
            await asyncio.sleep(WORKFLOW_PROGRESS_INTERVAL)
            latest = SERVICE.workflow_generation_status()
            # Inference goes minutes between the lines it reports, so most
            # polls have nothing to send.
            if latest == status:
                continue
            status = latest
            async with self:
                # A later visit is watching now; two watchers would write the
                # same vars over each other.
                if self.workflow_watch != watch:
                    return
                self._show_workflow(status)

    def _show_workflow(self, status: WorkflowGeneration) -> None:
        """Put one snapshot of the generation on the page."""
        self.workflow_error = status.error
        self.workflow_progress = list(status.progress)
        # One section per Codee work item, in the order Settings lists them,
        # so a work item added there shows up here as its own graph.
        self.workflow_sections = [
            WorkflowSection(
                issue_type=issue_type,
                title=f"{issue_type.capitalize()} workflow",
                nodes=graph.get("nodes", []),
                edges=graph.get("edges", []),
                warnings=graph.get("warnings", []),
            )
            for issue_type, graph in (status.workflow or {}).items()
        ]
        # A graph already on screen stays there while the next run confirms
        # it; only a generation with nothing to show yet gets the spinner.
        self.workflow_running = status.running
        self.workflow_loading = status.running and not self.workflow_sections

    def open_edge_menu(self, skills: list[str], x: float, y: float) -> None:
        self.edge_menu_skills = skills
        self.edge_menu_left = f"{round(x)}px"
        self.edge_menu_top = f"{round(y)}px"
        # The menu opens where the tooltip sits; leaving both up stacks two
        # panels on the same arrow.
        self.edge_tooltip_reasons = []

    def close_edge_menu(self) -> None:
        self.edge_menu_skills = []

    def show_edge_tooltip(
        self, reasons: list[str], x: float, y: float
    ) -> None:
        if self.edge_menu_skills:
            return
        self.edge_tooltip_reasons = reasons
        self.edge_tooltip_left = f"{round(x) + EDGE_TOOLTIP_OFFSET}px"
        self.edge_tooltip_top = f"{round(y) + EDGE_TOOLTIP_OFFSET}px"

    def hide_edge_tooltip(self) -> None:
        self.edge_tooltip_reasons = []

    def edit_workflow_skill(self, label: str) -> Any:
        self.edge_menu_skills = []
        slug = SERVICE.resolve_skill_slug(label)
        if not slug:
            return rx.toast.error(f"No skill found for transition '{label}'")
        self.load_skills()
        return [self.edit_skill(slug), rx.redirect("/skills")]

    def load_coding_agent(self) -> None:
        """Refresh the configured agent for the pages that branch on it.

        Only the Memory page needs this on its own: the agent decides whether
        memory lives in the repository or in a GitHub account. Everywhere else
        it arrives with the rest of the settings.
        """
        self.coding_agent = SERVICE.load_settings().coding_agent.value

    def load_settings(self) -> Any:
        settings = SERVICE.load_settings()
        self.tasks_provider = settings.tasks_provider.value
        self.coding_agent = settings.coding_agent.value
        self.max_parallel_agents = str(settings.max_parallel_agents)
        self.claude_code_rotate_keys = settings.claude_code_rotate_keys
        self._cancel_claude_code_sign_in()
        self.load_claude_code_accounts()
        jira = settings.credentials.get("jira", {})
        azure = settings.credentials.get("azure_devops", {})
        self.jira_base_url = jira.get("base_url", "")
        self.jira_account_email = jira.get("account_email", "")
        self.jira_api_token = jira.get("api_token", "")
        self.jira_project = jira.get("project", "")
        self.jira_task_filter = settings.task_filters.get("jira", "")
        self.azure_task_filter = settings.task_filters.get("azure_devops", "")
        self.azure_organization_url = azure.get("organization_url", "")
        self.azure_tenant_id = azure.get("tenant_id", "")
        self.azure_client_id = azure.get("client_id", "")
        self.azure_client_secret = azure.get("client_secret", "")
        # A fresh page load has nothing parked: what is on disk is the truth.
        self.other_work_items = {}
        self._load_work_items()
        self._drop_provider_results()
        self.load_mcp_status()
        self.load_azure_connection()
        return self._azure_callback_toast()

    def _load_work_items(self) -> None:
        """Fill the mapping rows for the selected provider.

        From the rows parked by an earlier switch when there are any — those
        are edits the user has not saved and would not expect to lose — and
        from the saved mapping otherwise.
        """
        parked = self.other_work_items.get(self.tasks_provider)
        if parked is not None:
            self.work_items = [row.model_copy(deep=True) for row in parked]
            return
        self.work_items = [
            WorkItem(name=mapping.name, provider_types=list(mapping.types),
                     query=mapping.query,
                     mode="query" if mapping.is_query else "types",
                     fixed=mapping.name in DEFAULT_ISSUE_TYPES)
            for mapping in SERVICE.work_item_mappings(self.tasks_provider)
        ]

    def load_settings_page(self) -> Any:
        """Everything the settings page needs, in the order it needs it.

        The types are fetched *after* the load rather than beside it in
        ``on_load``: the fetch reads the credentials the load puts in state,
        and a background event listed alongside starts before the event before
        it has committed — it would find an empty form and decline to run.
        """
        events: list[Any] = [AdminState.load_work_item_types]
        toast = self.load_settings()
        # The OAuth outcome, when the browser came back from a consent screen.
        return [toast, *events] if toast is not None else events

    def load_mcp_status(self) -> None:
        """Read back whether the selected provider's MCP server is already installed."""
        self.mcp_configured = SERVICE.tasks_mcp_configured(self.tasks_provider)

    def load_azure_connection(self) -> None:
        connection = SERVICE.azure_connection()
        self.azure_connected = connection["connected"]
        self.azure_account = connection["account"]
        self.azure_expires_label = connection["expires_label"]
        self.azure_redirect_uri = SERVICE.azure_redirect_uri()

    def _azure_callback_toast(self) -> Any:
        """Surface the OAuth outcome the callback route passed back in the URL."""
        params = self.router.url.query_parameters
        outcome = params.get("azure", "")
        if not outcome:
            return None
        message = params.get("message", "")
        if outcome == "connected":
            return rx.toast.success(message or "Connected to Azure DevOps")
        return rx.toast.error(message or "Could not connect to Azure DevOps")

    def _drop_check_results(self) -> None:
        """Forget the last checks and MCP setup: they spoke for a form that just changed."""
        self.tasks_verifying = False
        self.tasks_checks = []
        self.mcp_setup_ok = False
        self.mcp_setup_message = ""

    def _drop_provider_results(self) -> None:
        """Everything on screen that was answered by the credentials just edited."""
        self._drop_check_results()
        # The fetched work item types came from the same backend the changed
        # credentials address, so they are no more current than the checks are.
        # The mapping rows themselves stay: they are the user's edit, not a
        # result, and the dropdowns keep offering whatever they already name.
        self.provider_work_item_types = []
        self.provider_work_item_types_scope = ""
        self.work_item_types_error = ""

    def set_tasks_provider(self, value: str) -> Any:
        # Park the rows on screen under the provider they were edited for, or
        # switching away and back would silently reset them to the defaults.
        self.other_work_items = {
            **self.other_work_items,
            self.tasks_provider: [row.model_copy(deep=True)
                                  for row in self.work_items],
        }
        self.tasks_provider = value
        self._load_work_items()
        self._drop_provider_results()
        self.load_mcp_status()
        # The types just cleared belong to the provider being left behind.
        return AdminState.load_work_item_types

    def set_work_item_name(self, index: int, value: str) -> None:
        self.work_items[index].name = value
        # Reflex only re-renders on assignment, not on a mutated element.
        self.work_items = list(self.work_items)

    def add_work_item_type(self, index: int, value: str) -> None:
        """Point a work item at one more of the backend's types.

        A type the row already names is dropped rather than added twice: the
        dropdown offers every type whatever a row holds, since which of them
        are still free is a per-row answer a shared list cannot give.
        """
        provider_type = value.strip()
        item = self.work_items[index]
        if not provider_type or provider_type.casefold() in {
                existing.casefold() for existing in item.provider_types}:
            return
        item.provider_types = item.provider_types + [provider_type]
        # Reflex only re-renders on assignment, not on a mutated element.
        self.work_items = list(self.work_items)

    def remove_work_item_type(self, index: int, value: str) -> None:
        item = self.work_items[index]
        item.provider_types = [provider_type
                               for provider_type in item.provider_types
                               if provider_type != value]
        self.work_items = list(self.work_items)

    def set_work_item_query(self, index: int, value: str) -> None:
        self.work_items[index].query = value
        self.work_items = list(self.work_items)

    def set_work_item_mode(self, index: int, value: str) -> None:
        """Switch one row between naming types and carrying a query.

        Both are kept: a row switched to a query and back finds its types where
        it left them, and the save is what decides which of the two is stored.
        """
        if value not in ("types", "query"):
            return
        self.work_items[index].mode = value
        self.work_items = list(self.work_items)

    def add_work_item(self) -> None:
        self.work_items = self.work_items + \
            [WorkItem(name="", provider_types=[])]

    def remove_work_item(self, index: int) -> None:
        if self.work_items[index].fixed:
            return
        self.work_items = [item for position, item
                           in enumerate(self.work_items) if position != index]

    @rx.var
    def work_item_type_options(self) -> list[str]:
        """What the mapping dropdowns offer.

        The types fetched from the backend, plus whatever the rows already
        name. Without that union a mapping saved against a project this account
        can no longer see — or one saved before the types were ever fetched —
        would render as an empty select, and the user would have no way to tell
        a lost setting from an unset one.
        """
        options = set(self.provider_work_item_types)
        options.update(provider_type.strip() for item in self.work_items
                       for provider_type in item.provider_types
                       if provider_type.strip())
        return sorted(options, key=str.casefold)

    @rx.var
    def work_item_types_hint(self) -> str:
        """The line under the mapping rows: what the dropdowns currently hold.

        Naming the scope is the point. A backend where most types live in a
        project Codee is not pointed at will offer a short list, and without
        this the only reading available is "types are missing".
        """
        if self.work_item_types_loading:
            return "This can take a moment."
        if not self.work_item_types_can_load:
            return "Connect to the provider above to list its work item types."
        if self.work_item_types_error:
            return "The dropdowns still offer the types already mapped below."
        if not self.provider_work_item_types:
            return "Loaded from the provider when the page opens."
        count = len(self.provider_work_item_types)
        scope = self.provider_work_item_types_scope
        return (f"{count} type{'' if count == 1 else 's'}"
                + (f" from {scope}" if scope else ""))

    @rx.event(background=True)
    async def load_work_item_types(self) -> None:
        """Ask the provider what its work item types are called.

        Runs on page load and on a provider switch, and again whenever the
        button is pressed. In the background because it is a network round trip
        — several, for Azure DevOps, which has to walk the organization's
        projects — and the rest of the settings page is usable while it runs.

        Failing is not an error state for the page: the dropdowns fall back to
        the names already mapped, and the message says why there is nothing new
        to pick from. That matters more now that it runs unasked — a provider
        that is unreachable must not make opening Settings look broken.
        """
        async with self:
            if self.work_item_types_loading or not self.work_item_types_can_load:
                return
            self.work_item_types_loading = True
            self.work_item_types_error = ""
            provider, credentials = self.tasks_provider, self._credentials()

        types, scope, error = await asyncio.to_thread(
            SERVICE.list_work_item_types, provider, credentials)

        async with self:
            self.provider_work_item_types = types
            self.provider_work_item_types_scope = scope
            self.work_item_types_error = error
            self.work_item_types_loading = False

    def set_coding_agent(self, value: str) -> None:
        self.coding_agent = value

    def set_max_parallel_agents(self, value: str) -> None:
        self.max_parallel_agents = value

    def set_claude_code_rotate_keys(self, value: bool) -> None:
        self.claude_code_rotate_keys = value

    def load_claude_code_accounts(self) -> None:
        self.claude_code_accounts = [ClaudeAccount(**account.__dict__)
                                     for account in SERVICE.claude_code_accounts()]

    def start_claude_code_sign_in(self) -> Any:
        """Open a sign-in: send the browser off to it, and wait for the code.

        The machine running Codee may have no browser of its own — it is a
        server as often as a laptop — so the URL is also shown, to be opened
        wherever the user actually is, and the code comes back by hand. That is
        the flow ``claude /login`` falls back to, and the only one Anthropic
        will redirect to a page rather than to a callback Codee cannot host.
        """
        authorization = SERVICE.start_claude_code_authorization()
        self._claude_code_authorization = authorization
        self.claude_code_authorize_url = authorization.url
        self.claude_code_auth_code = ""
        return rx.redirect(authorization.url, is_external=True)

    def set_claude_code_auth_code(self, value: str) -> None:
        self.claude_code_auth_code = value

    def cancel_claude_code_sign_in(self) -> None:
        self._cancel_claude_code_sign_in()

    def _cancel_claude_code_sign_in(self) -> None:
        """Drop the sign-in in flight, verifier included."""
        self._claude_code_authorization = None
        self.claude_code_authorize_url = ""
        self.claude_code_auth_code = ""
        self.claude_code_connecting = False

    @rx.event(background=True)
    async def finish_claude_code_sign_in(self) -> Any:
        """Redeem the pasted code and add the account it yields.

        In the background because it is two network round trips — the exchange,
        then asking whose account the token is — and neither should freeze the
        settings page while it happens.
        """
        async with self:
            if self.claude_code_connecting:
                return
            authorization = self._claude_code_authorization
            if authorization is None:
                yield rx.toast.error("Start the sign-in first")
                return
            pasted = self.claude_code_auth_code
            self.claude_code_connecting = True

        try:
            connected, message = await asyncio.to_thread(
                SERVICE.complete_claude_code_authorization, pasted, authorization)
        except Exception as error:  # noqa: BLE001 - never leave the page spinning
            connected, message = False, f"{type(error).__name__}: {error}"

        async with self:
            self.claude_code_connecting = False
            if connected:
                # The code is single-use, so a sign-in that worked is over
                # whether or not the user closes the box.
                self._cancel_claude_code_sign_in()
                self.load_claude_code_accounts()
        yield rx.toast.success(message) if connected else rx.toast.error(message)

    def disconnect_claude_code_account(self, account_id: int) -> Any:
        SERVICE.disconnect_claude_code_account(account_id)
        self.load_claude_code_accounts()
        return rx.toast.success("Account disconnected")

    def set_jira_base_url(self, value: str) -> None:
        self.jira_base_url = value
        self._drop_provider_results()

    def set_jira_account_email(self, value: str) -> None:
        self.jira_account_email = value
        self._drop_provider_results()

    def set_jira_api_token(self, value: str) -> None:
        self.jira_api_token = value
        self._drop_provider_results()

    def set_jira_project(self, value: str) -> None:
        self.jira_project = value
        self._drop_provider_results()

    def set_jira_task_filter(self, value: str) -> None:
        self.jira_task_filter = value
        # Only the checks: the filter narrows the query, it does not change
        # which backend answers it, so the fetched work item types still stand.
        self._drop_check_results()

    def set_azure_task_filter(self, value: str) -> None:
        self.azure_task_filter = value
        self._drop_check_results()

    def set_azure_organization_url(self, value: str) -> None:
        self.azure_organization_url = value
        self._drop_provider_results()

    def set_azure_tenant_id(self, value: str) -> None:
        self.azure_tenant_id = value
        self._drop_provider_results()

    def set_azure_client_id(self, value: str) -> None:
        self.azure_client_id = value
        self._drop_provider_results()

    def set_azure_client_secret(self, value: str) -> None:
        self.azure_client_secret = value
        self._drop_provider_results()

    @rx.var
    def azure_can_connect(self) -> bool:
        """Everything the authorization request and the later queries need."""
        return all(value.strip() for value in (
            self.azure_organization_url,
            self.azure_client_id, self.azure_client_secret))

    @rx.var
    def tasks_can_verify(self) -> bool:
        """Whether a real pull is worth attempting with what's on the form.

        Every field the provider reads has to be filled in, and Azure DevOps
        additionally needs the OAuth consent — without it the check could only
        ever report "not connected", which the status line above already says.
        """
        if self.tasks_provider == "jira":
            return all(value.strip() for value in (
                self.jira_base_url, self.jira_account_email,
                self.jira_api_token, self.jira_project))
        return self.azure_can_connect and self.azure_connected

    @rx.var
    def work_item_types_can_load(self) -> bool:
        """Whether the provider can be asked what its work item types are.

        The same reach the task pull needs, minus JIRA's project key: without
        one the listing falls back to every type defined on the site, which is
        still a better dropdown than an empty one.
        """
        if self.tasks_provider == "jira":
            return all(value.strip() for value in (
                self.jira_base_url, self.jira_account_email,
                self.jira_api_token))
        return self.azure_can_connect and self.azure_connected

    @rx.var
    def mcp_can_setup(self) -> bool:
        """Whether the selected provider can describe its MCP server yet.

        Less than the pull needs in both cases. mcp-atlassian doesn't take a
        project key — the agent searches and updates issues wherever they live
        rather than polling one project — and the Azure DevOps server takes only
        the organization, since it signs in through the Azure CLI rather than
        through the app registration.
        """
        if self.tasks_provider == "jira":
            return all(value.strip() for value in (
                self.jira_base_url, self.jira_account_email,
                self.jira_api_token))
        return bool(self.azure_organization_url.strip())

    def _query_language(self) -> str:
        """What the selected provider calls its query language."""
        return "WIQL" if self.tasks_provider == "azure_devops" else "JQL"

    @rx.var
    def task_filter_label(self) -> str:
        """Named after the language it has to be written in, not after Codee."""
        return ("Custom WIQL" if self.tasks_provider == "azure_devops"
                else "Custom JQL")

    @rx.var
    def work_item_query_label(self) -> str:
        """The second way to select a work item, named after its language.

        "Advanced" because it is: it replaces the type list with a condition
        nothing here can check, and gets it wrong loudly rather than quietly —
        a clause the backend rejects fails that work item's whole query.
        """
        return ("WIQL (Advanced)" if self.tasks_provider == "azure_devops"
                else "JQL (Advanced)")

    @rx.var
    def work_item_query_placeholder(self) -> str:
        """An example in the provider's own language, so the box is self-explaining."""
        if self.tasks_provider == "azure_devops":
            return ("[System.WorkItemType] = 'Bug' "
                    "AND [System.Tags] CONTAINS 'codee'")
        return 'issuetype = Bug AND labels = "codee"'

    @rx.var
    def mcp_provider_label(self) -> str:
        return ("Azure DevOps" if self.tasks_provider == "azure_devops"
                else "Jira")

    @rx.var
    def mcp_button_label(self) -> str:
        """Re-running is how stale credentials in the file get replaced."""
        return (f"Setup {self.mcp_provider_label} MCP again"
                if self.mcp_configured
                else f"Setup {self.mcp_provider_label} MCP")

    @rx.var
    def mcp_missing_hint(self) -> str:
        if self.tasks_provider == "jira":
            return "Fill in base URL, API Token Owner Email and API token first."
        return "Fill in the organization URL first."

    def setup_tasks_mcp(self) -> None:
        """Write the provider's MCP server into the project's .mcp.json.

        Synchronous, unlike the connection check: this only touches a local file,
        and the result should be on screen by the time the click lands.
        """
        self.mcp_setup_ok, self.mcp_setup_message = SERVICE.setup_tasks_mcp(
            self.tasks_provider, self._credentials())
        self.load_mcp_status()

    @rx.event(background=True)
    async def verify_tasks_connection(self) -> Any:
        """Run the provider checks with the credentials on the form.

        In the background because none of it is quick: the provider allows
        itself 30s per request, and the MCP check runs a whole coding agent. The
        whole list goes up at once and each row is filled in as its check
        finishes, so the wait is spent looking at what is still running rather
        than at nothing. Nothing is saved — verifying shouldn't commit anything.
        """
        async with self:
            if self.tasks_verifying or not self.tasks_can_verify:
                return
            self.tasks_verifying = True
            self.tasks_checks = pending_checks([])
            provider, credentials = self.tasks_provider, self._credentials()
            # Verified as it stands on the form, so a filter the backend
            # refuses is caught here rather than by the first silent poll.
            task_filter = self._task_filter()

        # A generator, so nothing has run yet: each next() is one check, and the
        # thread keeps the page responsive while it does.
        checks = SERVICE.verify_tasks_connection(
            provider, credentials, task_filter)
        done: list[CheckResult] = []
        failure = ""
        try:
            while (result := await asyncio.to_thread(next, checks, None)) is not None:
                done.append(check_result(result))
                async with self:
                    self.tasks_checks = pending_checks(done)
        except Exception as error:
            failure = str(error)
        async with self:
            self.tasks_verifying = False
            # Whatever the generator stopped short of is dropped rather than
            # left spinning: a run that yielded one check has only one to show.
            self.tasks_checks = done + ([CheckResult(
                name="Verification failed", status="failed", message=failure)]
                if failure else [])

    def connect_azure_devops(self) -> Any:
        """Save the app registration, then hand the browser to Entra ID for consent.

        Saving first is what makes the callback work: it arrives as its own HTTP
        request and reads the client secret back off disk to exchange the code.
        """
        if not self.azure_can_connect:
            return rx.toast.error(
                "Fill in organization URL, client ID and client secret first.")
        error = self._persist_settings()
        if error:
            return rx.toast.error(error)
        started, result = SERVICE.start_azure_authorization()
        if not started:
            return rx.toast.error(result)
        # Same tab, so the callback lands back on /settings once consent is done.
        return rx.redirect(result)

    def disconnect_azure_devops(self) -> Any:
        SERVICE.disconnect_azure()
        self._drop_provider_results()
        self.load_azure_connection()
        return rx.toast.success("Disconnected from Azure DevOps")

    def _credentials(self) -> dict[str, str]:
        """The selected provider's credentials as they stand on the form."""
        if self.tasks_provider == "jira":
            return {
                "base_url": self.jira_base_url,
                "account_email": self.jira_account_email,
                "api_token": self.jira_api_token,
                "project": self.jira_project,
            }
        return {
            "organization_url": self.azure_organization_url,
            "tenant_id": self.azure_tenant_id,
            "client_id": self.azure_client_id,
            "client_secret": self.azure_client_secret,
        }

    def _task_filter(self) -> str:
        """The selected provider's custom query clause as it stands on the form."""
        if self.tasks_provider == "jira":
            return self.jira_task_filter
        return self.azure_task_filter

    def _persist_settings(self) -> str:
        """Write the settings to disk. Returns an error message, or '' when saved."""
        try:
            parallel_agents = int(self.max_parallel_agents)
        except ValueError:
            return "Max parallel tasks must be a number"
        for item in self.work_items:
            # Only the page knows a row is in query mode while its query is
            # still empty; saved, it would silently become a types row.
            if item.mode == "query" and not item.query.strip():
                name = item.name.strip().lower() or "the new work item"
                return f"Write the {self._query_language()} for '{name}'"
        work_items, work_item_queries, error = normalize_work_items(
            # A row in types mode saves no query, which is what makes a stored
            # query mean "this work item is selected by one".
            [(item.name, list(item.provider_types),
              item.query if item.mode == "query" else "")
             for item in self.work_items])
        if error:
            return error
        SERVICE.save_settings(
            self.tasks_provider,
            self.coding_agent,
            parallel_agents,
            self._credentials(),
            work_items,
            work_item_queries,
            self._task_filter(),
            self.claude_code_rotate_keys,
        )
        # Saving is what picks the first account when rotation has just been
        # switched on, so the badge has to be redrawn from what that decided.
        self.load_claude_code_accounts()
        return ""

    def save_settings(self) -> Any:
        error = self._persist_settings()
        return rx.toast.error(error) if error else rx.toast.success("Settings saved")

    def open_agent_test(self) -> Any:
        self.agent_test_input = ""
        self.agent_test_session_id = ""
        self.agent_test_messages = []
        self.agent_test_sending = False
        self.agent_test_generation += 1
        self.agent_test_model = ""
        self.agent_test_models = []
        self.agent_test_model_query = ""
        self.agent_test_open = True
        return AdminState.load_agent_test_models

    def set_agent_test_open(self, open_: bool) -> None:
        self.agent_test_open = open_
        if not open_:
            self.agent_test_generation += 1
            self.agent_test_sending = False

    def set_agent_test_input(self, value: str) -> None:
        self.agent_test_input = value

    def set_agent_test_model_query(self, value: str) -> None:
        self.agent_test_model_query = value

    def choose_agent_test_model(self, model_id: str) -> None:
        self.agent_test_model = model_id.strip()
        self.agent_test_model_query = ""

    @rx.event(background=True)
    async def load_agent_test_models(self) -> None:
        async with self:
            agent = self.coding_agent
            self.agent_test_models_loading = True
        try:
            models = await asyncio.to_thread(SERVICE.list_agent_models, agent)
        except Exception:
            models = []
        async with self:
            if self.coding_agent != agent:
                return
            self.agent_test_models = [ModelOption(**model) for model in models]
            self.agent_test_models_loading = False

    @rx.event(background=True)
    async def send_agent_test_message(self) -> Any:
        async with self:
            message = self.agent_test_input.strip()
            if not message or self.agent_test_sending:
                return
            agent = self.coding_agent
            session_id = self.agent_test_session_id
            model = self.agent_test_model
            generation = self.agent_test_generation
            self.agent_test_input = ""
            self.agent_test_sending = True
            self.agent_test_messages = [
                *self.agent_test_messages,
                ConversationMessage(role="user", content=message),
            ]
        try:
            response, session_id = await asyncio.to_thread(
                SERVICE.test_agent_conversation,
                agent,
                message,
                session_id,
                model,
            )
        except Exception as error:
            async with self:
                if self.agent_test_generation == generation:
                    self.agent_test_sending = False
            yield rx.toast.error(f"The coding agent failed: {error}")
            return
        async with self:
            if self.agent_test_generation != generation:
                return
            self.agent_test_session_id = session_id
            self.agent_test_messages = [
                *self.agent_test_messages,
                ConversationMessage(role="assistant", content=response),
            ]
            self.agent_test_sending = False


ACCENT = "var(--codee-accent)"
ACCENT_DEEP = "var(--codee-accent-deep)"
BORDER = "1px solid var(--codee-border)"
# Keeps the transition tooltip clear of the pointer it follows.
EDGE_TOOLTIP_OFFSET = 14
MUTED = "var(--codee-muted)"
SURFACE = "var(--codee-surface)"
PAGE_BACKGROUND = "var(--codee-page-background)"
NAV_BACKGROUND = "var(--codee-nav-background)"
TEXT = "var(--codee-text)"
HOVER = "var(--codee-hover)"
ACTIVE = "var(--codee-active)"
GRID = "var(--codee-grid)"
SUBTLE_ICON = "var(--codee-subtle-icon)"
RUNNING_BACKGROUND = "var(--codee-running-background)"
RUNNING_GLOW = "var(--codee-running-glow)"
MONO = "IBM Plex Mono, monospace"
LOGO = "👨🏻‍💻"
# Inline SVG carrying the logo emoji, so the favicon needs no binary asset.
FAVICON = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMD"
    "AgMTAwIj48dGV4dCB5PSIuOWVtIiBmb250LXNpemU9IjkwIj7wn5Go8J+Pu+KAjfCfkrs8L3Rl"
    "eHQ+PC9zdmc+"
)


def nav_link(label: str, icon: str, href: str) -> rx.Component:
    is_active = AdminState.active_route == href
    return rx.link(
        rx.hstack(rx.icon(icon, size=17), rx.text(
            label), spacing="3", align="center"),
        href=href,
        aria_current=rx.cond(is_active, "page", ""),
        color=rx.cond(is_active, ACCENT, TEXT),
        background=rx.cond(is_active, ACTIVE, "transparent"),
        font_weight=rx.cond(is_active, "600", "500"),
        box_shadow=rx.cond(
            is_active, f"inset 3px 0 0 0 {ACCENT}", "inset 0 0 0 0 transparent"),
        padding="0.6rem 0.75rem",
        border_radius="6px",
        _hover={"background": rx.cond(is_active, ACTIVE, HOVER),
                "color": ACCENT},
        text_decoration="none",
        width=rx.breakpoints(initial="auto", lg="100%"),
    )


def shell(content: rx.Component) -> rx.Component:
    return rx.box(
        rx.grid(
            rx.box(
                rx.vstack(
                    rx.hstack(
                        rx.box(LOGO, color="white", background=ACCENT, width="2rem",
                               height="2rem", display="grid", place_items="center",
                               border_radius="6px", font_weight="700",
                               font_size="1.1rem", line_height="1"),
                        rx.text("Codee", font_size="1.1rem",
                                font_weight="700"),
                        rx.spacer(),
                        rx.color_mode.button(
                            position="static",
                            variant="soft",
                            aria_label="Toggle color mode",
                        ),
                        spacing="3",
                        align="center",
                        width="100%",
                    ),
                    rx.flex(
                        nav_link("Dashboard", "layout-dashboard", "/"),
                        nav_link("Skills", "blocks", "/skills"),
                        nav_link("Workflow", "git-branch", "/workflow"),
                        nav_link("Memory", "notebook-text", "/memory"),
                        nav_link("Repositories", "folder-git-2",
                                 "/repositories"),
                        nav_link("Runs", "history", "/runs"),
                        # Shown for whatever agent is configured: the viewer is
                        # pointed at by CODEE_SESSION_VIEWER_URL, and every agent
                        # now reports the session it actually ran under.
                        rx.cond(
                            AdminState.session_viewer != "",
                            nav_link("Sessions", "key-round", "/sessions"),
                        ),
                        nav_link("Settings", "settings", "/settings"),
                        direction=rx.breakpoints(initial="row", lg="column"),
                        wrap="wrap",
                        gap="0.2rem",
                        width="100%",
                    ),
                    spacing="6",
                    align="start",
                    width="100%",
                ),
                padding=rx.breakpoints(initial="1rem", lg="1.5rem"),
                border_right=rx.breakpoints(initial="none", lg=BORDER),
                border_bottom=rx.breakpoints(initial=BORDER, lg="none"),
                background=NAV_BACKGROUND,
                min_height=rx.breakpoints(initial="auto", lg="100vh"),
            ),
            rx.box(content, padding=rx.breakpoints(initial="1.25rem", md="2rem", xl="3rem"),
                   min_width="0", max_width="1440px", width="100%"),
            columns=rx.breakpoints(initial="1fr", lg="230px minmax(0, 1fr)"),
            min_height="100vh",
        ),
        background=PAGE_BACKGROUND,
        color=TEXT,
        font_family="IBM Plex Sans, sans-serif",
        style={
            "--codee-accent": rx.color_mode_cond("#2f6fd6", "#7aa9f0"),
            # The darker half of the two-blue brand pairing, for filled marks
            # that should recede behind the interactive blue (step markers).
            "--codee-accent-deep": rx.color_mode_cond("#232c9b", "#5866de"),
            "--codee-border": rx.color_mode_cond("#dbe1ec", "#2b3350"),
            "--codee-muted": rx.color_mode_cond("#5d687f", "#a3adc4"),
            "--codee-surface": rx.color_mode_cond("#ffffff", "#171d33"),
            "--codee-page-background": rx.color_mode_cond("#f2f5fb", "#0e1224"),
            "--codee-nav-background": rx.color_mode_cond("#f7f9fd", "#121729"),
            "--codee-text": rx.color_mode_cond("#1b2440", "#e9edf7"),
            "--codee-hover": rx.color_mode_cond("#edf3fc", "#1e2742"),
            "--codee-active": rx.color_mode_cond("#e0eafb", "#24304f"),
            "--codee-grid": rx.color_mode_cond("#e2e7f1", "#2a3350"),
            "--codee-subtle-icon": rx.color_mode_cond("#8e99b0", "#6e7a94"),
            "--codee-warning-background": rx.color_mode_cond("#fff5ed", "#33200f"),
            "--codee-warning-border": rx.color_mode_cond("#e26128", "#f5854a"),
            # Yellow marks the statuses waiting on a person rather than on
            # Codee, kept clear of the orange warning pair so a hand-off does
            # not read as something having gone wrong.
            "--codee-human-background": rx.color_mode_cond("#fdf4c8", "#3a3211"),
            "--codee-human-border": rx.color_mode_cond("#d1a207", "#e8c33c"),
            # Tint reserved for in-flight work, so a live run reads at a glance.
            "--codee-running-background": rx.color_mode_cond("#eff4fd", "#14203a"),
            "--codee-running-glow": rx.color_mode_cond(
                "rgba(47, 111, 214, 0.16)", "rgba(122, 169, 240, 0.18)"),
            "color_scheme": "light dark",
        },
    )


def page_header(title: str, description: str) -> rx.Component:
    return rx.vstack(
        rx.heading(title, size="7", letter_spacing="0"),
        rx.text(description, color=MUTED, font_size="0.95rem"),
        spacing="1",
        align="start",
        margin_bottom="2rem",
    )


def empty_state(icon: str, text: str) -> rx.Component:
    return rx.center(
        rx.vstack(rx.icon(icon, size=28, color=SUBTLE_ICON), rx.text(text, color=MUTED),
                  spacing="3", align="center"),
        border="1px dashed var(--codee-border)",
        min_height="10rem",
        width="100%",
    )


def live_dot(size: str = "0.6rem") -> rx.Component:
    """Accent dot with an expanding halo: the "this is live right now" marker."""
    return rx.box(
        rx.box(position="absolute", inset="0", border_radius="50%", background=ACCENT,
               animation="codee-ping 1.8s cubic-bezier(0, 0, 0.2, 1) infinite"),
        rx.box(position="absolute", inset="0",
               border_radius="50%", background=ACCENT),
        class_name="codee-live-dot",
        position="relative",
        width=size,
        height=size,
        flex_shrink="0",
    )


def elapsed_pill(label: rx.Var | str) -> rx.Component:
    return rx.hstack(
        rx.icon("timer", size=14, color=ACCENT),
        rx.text(label, font_family=MONO,
                font_size="0.85rem", font_weight="500"),
        spacing="2",
        align="center",
        flex_shrink="0",
        padding="0.2rem 0.6rem",
        background=SURFACE,
        border=BORDER,
        border_radius="999px",
    )


def running_agent_line(job: ActiveJob) -> rx.Component:
    """Who is running the prompt: the coding agent, and the model it was given.

    Sits under the prompt rather than beside it so a long prompt keeps the whole
    first line to itself. A run whose agent went unrecorded — a row written
    before the columns existed — shows nothing at all instead of a blank label.
    """
    return rx.cond(
        (job.agent != "") | (job.model != ""),
        rx.hstack(
            rx.cond(
                job.agent != "",
                rx.hstack(rx.icon("bot", size=13, color=SUBTLE_ICON),
                          rx.text(job.agent, color=MUTED, font_size="0.78rem"),
                          spacing="1", align="center"),
            ),
            rx.cond(
                job.model != "",
                rx.code(job.model, font_size="0.7rem", color_scheme="gray"),
            ),
            spacing="2",
            align="center",
            width="100%",
        ),
    )


def active_prompt(job: ActiveJob) -> rx.Component:
    """The prompt the run was given, with the work item it names linked.

    The link opens the item in the tasks provider in a new tab, so following it
    never takes the dashboard — and the live list it is drawing — off screen.
    """
    return rx.text(
        job.prompt_prefix,
        rx.cond(
            job.task_url != "",
            rx.link(job.task_key, href=job.task_url, is_external=True,
                    color=ACCENT, text_decoration="underline"),
        ),
        font_weight="600", font_family=MONO, font_size="0.9rem",
        width="100%", overflow="hidden", text_overflow="ellipsis",
        white_space="nowrap", custom_attrs={"title": job.message},
    )


def active_job_row(job: ActiveJob) -> rx.Component:
    return rx.flex(
        live_dot(),
        rx.vstack(
            active_prompt(job),
            running_agent_line(job),
            spacing="1",
            align="start",
            flex="1",
            min_width="0",
        ),
        elapsed_pill(job.elapsed_label),
        rx.cond(
            job.viewer_url != "",
            rx.link(rx.icon("external-link", size=16), href=job.viewer_url, is_external=True,
                    aria_label="View session", color=ACCENT, display="flex",
                    align_items="center"),
        ),
        gap="0.85rem",
        align="center",
        padding="0.85rem 1rem",
        background=RUNNING_BACKGROUND,
        border=BORDER,
        border_left=f"3px solid {ACCENT}",
        border_radius="4px",
        width="100%",
    )


def running_tile() -> rx.Component:
    """Stat tile that lights up while sessions are in flight."""
    running = AdminState.active_jobs.length()
    is_running = running > 0
    return rx.box(
        rx.hstack(rx.text("Running now", color=MUTED),
                  rx.cond(is_running, live_dot("0.5rem")),
                  spacing="2", align="center"),
        rx.heading(running, size="8", color=rx.cond(is_running, ACCENT, TEXT)),
        padding="1.25rem",
        background=rx.cond(is_running, RUNNING_BACKGROUND, SURFACE),
        border=rx.cond(is_running, f"1px solid {ACCENT}", BORDER),
    )


def running_panel() -> rx.Component:
    """Live sessions panel — accent-lit while anything is in flight, quiet when idle."""
    running = AdminState.active_jobs.length()
    is_running = running > 0
    return rx.box(
        rx.hstack(
            rx.cond(is_running, live_dot("0.55rem")),
            rx.heading("Currently running", size="4"),
            rx.cond(
                is_running,
                rx.box(running, color=ACCENT, background=RUNNING_BACKGROUND,
                       border=f"1px solid {ACCENT}", border_radius="999px",
                       padding="0.05rem 0.55rem", font_size="0.8rem", font_weight="600",
                       font_family=MONO, class_name="codee-breathe",
                       animation="codee-breathe 2.4s ease-in-out infinite"),
            ),
            spacing="3",
            align="center",
            width="100%",
            margin_bottom="0.9rem",
        ),
        rx.cond(
            is_running,
            rx.vstack(rx.foreach(AdminState.active_jobs, active_job_row),
                      spacing="2", width="100%"),
            rx.hstack(rx.icon("moon", size=16, color=SUBTLE_ICON),
                      rx.text("No sessions running right now.", color=MUTED),
                      spacing="2", align="center"),
        ),
        padding="1.25rem",
        background=SURFACE,
        border=rx.cond(is_running, f"1px solid {ACCENT}", BORDER),
        box_shadow=rx.cond(is_running, f"0 0 0 4px {RUNNING_GLOW}", "none"),
        width="100%",
    )


def usage_meter(label: str, percent: rx.Var, resets: rx.Var) -> rx.Component:
    """One window's allowance: how much is gone, and when it comes back.

    A bar rather than a number alone, because the only question anyone asks of
    this is "how close is it?" — and an amber-then-red bar answers that before
    the percentage is read.
    """
    known = percent >= 0
    return rx.vstack(
        rx.hstack(
            rx.text(label, color=MUTED, font_size="0.75rem"),
            rx.spacer(),
            rx.text(rx.cond(known, percent.to_string() + "%", "\u2014"),
                    font_size="0.75rem", font_family=MONO,
                    color=rx.cond(percent >= 100, "var(--red-11)", TEXT)),
            spacing="2", align="center", width="100%"),
        rx.progress(
            value=rx.cond(known, percent, 0), max=100, size="1",
            color_scheme=rx.cond(percent >= 100, "red",
                                 rx.cond(percent >= 80, "amber", "green")),
            width="100%"),
        # Only worth the line when the window is actually under pressure: a
        # reset time on an account at 4% is noise.
        rx.cond(
            (resets != "") & (percent >= 80),
            rx.text("resets ", local_datetime(resets), color=MUTED,
                    font_size="0.68rem"),
            rx.fragment()),
        spacing="1", width="100%")


def claude_account_usage_row(account: ClaudeAccount) -> rx.Component:
    """One account on the dashboard: who it is, and what it has left."""
    return rx.box(
        rx.hstack(
            rx.cond(account.in_use, live_dot("0.45rem"), rx.fragment()),
            rx.text(rx.cond(account.label != "", account.label,
                            "Account " + account.id.to_string()),
                    font_size="0.85rem",
                    font_weight=rx.cond(account.in_use, "600", "400"),
                    color=rx.cond(account.in_use, ACCENT, TEXT),
                    overflow="hidden", text_overflow="ellipsis",
                    white_space="nowrap"),
            rx.cond(account.in_use,
                    rx.badge("in use", color_scheme="green", variant="soft",
                             flex_shrink="0"),
                    rx.fragment()),
            rx.cond(account.needs_reconnect,
                    rx.badge("sign in again", color_scheme="amber",
                             variant="soft", flex_shrink="0"),
                    rx.fragment()),
            rx.spacer(),
            spacing="2", align="center", width="100%",
            margin_bottom="0.6rem"),
        rx.cond(
            account.usage_error != "",
            rx.text(account.usage_error, color=MUTED, font_size="0.75rem",
                    font_style="italic"),
            rx.grid(
                usage_meter("Session", account.session_percent,
                            account.session_resets),
                usage_meter("This week", account.weekly_percent,
                            account.weekly_resets),
                columns=rx.breakpoints(initial="1", sm="2"), gap="1rem",
                width="100%")),
        padding="0.85rem",
        border=rx.cond(account.in_use, f"1px solid {ACCENT}", BORDER),
        background=rx.cond(account.in_use, RUNNING_BACKGROUND,
                           PAGE_BACKGROUND),
        width="100%")


def claude_accounts_panel() -> rx.Component:
    """What each connected Claude account has left, and which one is live.

    Only on the page when rotation is on — with it off there is one account and
    it is whatever the machine is signed in as, which this panel could not name
    and would have nothing to say about.
    """
    return rx.box(
        rx.hstack(
            rx.heading("Claude accounts", size="4"),
            rx.cond(AdminState.claude_usage_loading,
                    rx.spinner(size="1"), rx.fragment()),
            spacing="3", align="center", width="100%",
            margin_bottom="0.9rem"),
        rx.cond(
            AdminState.claude_account_usage,
            rx.vstack(rx.foreach(AdminState.claude_account_usage,
                                 claude_account_usage_row),
                      spacing="2", width="100%"),
            rx.hstack(
                rx.icon("circle-user-round", size=16, color=SUBTLE_ICON),
                rx.text(rx.cond(AdminState.claude_usage_loading,
                                "Reading each account's allowance\u2026",
                                "No accounts connected yet."), color=MUTED),
                spacing="2", align="center")),
        padding="1.25rem", background=SURFACE, border=BORDER, width="100%")


def dashboard_page() -> rx.Component:
    return shell(rx.vstack(
        page_header(
            "Dashboard", "Run activity and live coding-agent sessions."),
        # First, because it is the only part of this page that answers "what is
        # happening right now"; the counts and the chart are history.
        running_panel(),
        rx.cond(AdminState.claude_code_rotate_keys,
                claude_accounts_panel(), rx.fragment()),
        rx.grid(
            rx.box(rx.text("Total runs", color=MUTED), rx.heading(AdminState.total_runs, size="8"),
                   padding="1.25rem", background=SURFACE, border=BORDER),
            rx.box(rx.text("Last 24 hours", color=MUTED), rx.heading(AdminState.last_24h_runs, size="8"),
                   padding="1.25rem", background=SURFACE, border=BORDER),
            running_tile(),
            columns=rx.breakpoints(initial="1", sm="2", lg="3"), gap="1rem", width="100%"),
        rx.box(
            rx.heading("Last 24 hours by hour",
                       size="4", margin_bottom="1rem"),
            rx.recharts.bar_chart(
                rx.recharts.cartesian_grid(
                    stroke_dasharray="3 3", stroke=GRID),
                rx.recharts.x_axis(data_key="hour", tick={"fontSize": 11}),
                rx.recharts.y_axis(allow_decimals=False,
                                   tick={"fontSize": 11}),
                rx.recharts.tooltip(),
                rx.recharts.bar(data_key="runs", fill=ACCENT,
                                radius=[3, 3, 0, 0]),
                data=AdminState.hourly_runs,
                width="100%", height=280,
            ),
            padding="1.25rem", background=SURFACE, border=BORDER, width="100%"),
        spacing="5", align="start", width="100%",
    ))


def skill_card(skill: SkillSummary) -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.hstack(rx.heading(skill.name, size="4"), rx.spacer(),
                      rx.badge(skill.type, color_scheme="blue", variant="soft"), width="100%"),
            rx.text(skill.description, color=MUTED, font_size="0.9rem", min_height="2.7rem",
                    overflow="hidden"),
            rx.hstack(
                rx.icon("bot", size=14, color=SUBTLE_ICON),
                rx.text(skill.agent, color=MUTED, font_size="0.82rem"),
                rx.cond(skill.model != "",
                        rx.code(skill.model, font_size="0.72rem",
                                color_scheme="gray")),
                spacing="2", align="center", width="100%"),
            rx.cond(
                skill.issue_status != "",
                rx.hstack(
                    rx.badge(skill.issue_type, color_scheme="blue",
                             variant="outline"),
                    rx.icon("circle-dot", size=14, color=SUBTLE_ICON),
                    rx.text(skill.issue_status, color=MUTED,
                            font_size="0.82rem"),
                    spacing="2",
                    align="center",
                    width="100%",
                    background=HOVER,
                    padding="0.55rem 0.65rem",
                    border_radius="4px",
                ),
            ),
            rx.button(rx.icon("pencil", size=15), "Edit", variant="soft",
                      on_click=AdminState.edit_skill(skill.slug), width="100%"),
            spacing="4", align="start", height="100%", width="100%"),
        padding="1rem", background=SURFACE, border=BORDER, min_height="185px"),


def agents_card() -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.hstack(rx.heading(AGENTS_FILE, size="4"), rx.spacer(),
                      rx.badge("always on", color_scheme="gray", variant="soft"), width="100%"),
            rx.text("Plain-text instructions every coding-agent run loads. Cannot be deleted.",
                    color=MUTED, font_size="0.9rem", min_height="2.7rem", overflow="hidden"),
            rx.button(rx.icon("pencil", size=15), "Edit", variant="soft",
                      on_click=AdminState.edit_agents, width="100%"),
            spacing="4", align="start", height="100%", width="100%"),
        padding="1rem", background=SURFACE, border=BORDER, min_height="185px")


def agents_editor() -> rx.Component:
    return rx.vstack(
        rx.hstack(rx.button(rx.icon("arrow-left", size=16), "Back", variant="ghost",
                            on_click=AdminState.close_agents),
                  rx.heading(AGENTS_FILE, size="4"), rx.spacer(),
                  rx.button(rx.icon("save", size=16), f"Save {AGENTS_FILE}",
                            on_click=AdminState.save_agents),
                  spacing="3", align="center", width="100%"),
        rx.text("Edited as plain text, without skill frontmatter.",
                color=MUTED, font_size="0.85rem"),
        rx.text_area(value=AdminState.agents_content, on_change=AdminState.set_agents_content,
                     width="100%", min_height="32rem", font_family="IBM Plex Mono, monospace"),
        spacing="4", align="start", width="100%")


def credential_hint(provider: TasksProvider, key: str) -> rx.Component | None:
    """The shared explanation for one provider field, as muted help text.

    Read from ``TASKS_PROVIDER_FIELDS`` rather than written here, so the
    settings page and `codee-agent init` explain a field the same way.
    """
    hint = credential_field(provider, key).hint
    if not hint:
        return None
    return rx.text(hint, color=MUTED, font_size="0.8rem")


def field(label: str, control: rx.Component, hint: rx.Component | None = None) -> rx.Component:
    children = [rx.text(label, font_weight="600",
                        font_size="0.85rem"), control]
    if hint is not None:
        children.append(hint)
    return rx.vstack(*children, spacing="2", align="start", width="100%")


def model_menu_item(button: rx.Component) -> rx.Component:
    """Make a picker row dismiss the popover while staying clickable end to end.

    ``rx.popover.close`` wraps any child carrying an ``on_click`` in a Flex of
    its own, and that wrapper hugs its content — so a full-width button inside
    it is still only clickable across the text. The width has to be restated at
    every level to give the row a full-width hit area.
    """
    return rx.popover.close(rx.flex(button, width="100%"), width="100%")


def _model_option_row(option: ModelOption, choose_model: Any) -> rx.Component:
    """One row of the model picker: friendly name left, model code right."""
    return model_menu_item(
        rx.button(
            # No spacer between the two: pinning the code to the right edge ran
            # it under the scroll bar.
            rx.hstack(rx.text(option.name, font_size="0.85rem"),
                      rx.code(option.id, font_size="0.72rem",
                              color_scheme="gray"),
                      align="center", spacing="2", width="100%"),
            variant="ghost", color_scheme="gray", width="100%",
            justify_content="start", padding="0.45rem 0.6rem",
            on_click=choose_model(option.id)))


def model_option_row(option: ModelOption) -> rx.Component:
    return _model_option_row(option, AdminState.choose_model)


def agent_test_model_option_row(option: ModelOption) -> rx.Component:
    return _model_option_row(option, AdminState.choose_agent_test_model)


def agent_picker() -> rx.Component:
    """Which agent runs this skill, or the default one from Settings.

    The model picker beside it lists that agent's models, so picking here
    decides what can be picked there.
    """
    return field(
        "Agent",
        rx.select(AGENT_OPTIONS, value=AdminState.skill_agent_label,
                  on_change=AdminState.set_skill_agent, width="100%"),
        rx.text(AdminState.skill_agent_hint, color=MUTED, font_size="0.82rem"))


def model_picker() -> rx.Component:
    """Searchable model select that also accepts a model code typed by hand.

    The agent's own catalog is only a convenience — anything typed here is saved
    verbatim, so a model the agent gained after this list was built still works.
    """
    return field(
        "Model",
        rx.popover.root(
            rx.popover.trigger(
                rx.button(
                    rx.hstack(rx.text(AdminState.skill_model_label), rx.spacer(),
                              rx.icon("chevrons-up-down", size=14),
                              align="center", width="100%"),
                    variant="surface", color_scheme="gray", width="100%",
                    type="button")),
            rx.popover.content(
                rx.vstack(
                    rx.input(placeholder="Search models, or type a model code",
                             value=AdminState.model_query,
                             on_change=AdminState.set_model_query,
                             auto_focus=True, width="100%"),
                    rx.cond(
                        AdminState.custom_model_query != "",
                        model_menu_item(
                            rx.button(
                                rx.hstack(rx.icon("plus", size=14),
                                          rx.text("Use "),
                                          rx.code(
                                              AdminState.custom_model_query),
                                          align="center", spacing="2"),
                                variant="soft", width="100%",
                                justify_content="start",
                                padding="0.45rem 0.6rem",
                                on_click=AdminState.choose_model(
                                    AdminState.custom_model_query)))),
                    rx.scroll_area(
                        rx.vstack(
                            model_menu_item(
                                rx.button(
                                    "Agent default", variant="ghost",
                                    color_scheme="gray", width="100%",
                                    justify_content="start",
                                    padding="0.45rem 0.6rem",
                                    on_click=AdminState.choose_model(""))),
                            rx.foreach(AdminState.filtered_models,
                                       model_option_row),
                            rx.cond(
                                AdminState.models_loading,
                                rx.text("Loading models from the coding agent…",
                                        color=MUTED, font_size="0.8rem",
                                        padding="0.5rem")),
                            spacing="1", width="100%"),
                        type="auto", scrollbars="vertical",
                        max_height="15rem", width="100%"),
                    spacing="2", width="100%"),
                width="24rem"),
        ),
        rx.text(
            rx.cond(AdminState.skill_model == "",
                    "Runs on whatever that agent defaults to.",
                    rx.fragment("Saved as ", rx.code(AdminState.skill_model),
                                " in the skill frontmatter.")),
            color=MUTED, font_size="0.82rem"))


def delete_skill_dialog() -> rx.Component:
    return rx.alert_dialog.root(
        rx.alert_dialog.trigger(
            rx.button(rx.icon("trash-2", size=16), "Delete skill",
                      variant="outline", color_scheme="red")),
        rx.alert_dialog.content(
            rx.alert_dialog.title("Delete skill"),
            rx.alert_dialog.description(
                "This permanently deletes ", rx.text.strong(
                    AdminState.selected_skill),
                " and pushes the removal to Git. This cannot be undone."),
            rx.hstack(
                rx.alert_dialog.cancel(rx.button("Cancel", variant="soft",
                                                 color_scheme="gray")),
                rx.alert_dialog.action(rx.button("Delete skill", color_scheme="red",
                                                 on_click=AdminState.delete_skill)),
                spacing="3", justify="end", margin_top="1.25rem", width="100%"),
            max_width="27rem"),
    )


def skill_editor() -> rx.Component:
    return rx.vstack(
        rx.hstack(rx.button(rx.icon("arrow-left", size=16), "Back", variant="ghost",
                            on_click=AdminState.close_skill), rx.spacer(),
                  delete_skill_dialog(),
                  rx.button(rx.icon("save", size=16), "Save skill",
                            on_click=AdminState.save_skill),
                  spacing="3",
                  width="100%"),
        rx.grid(
            field("Name", rx.input(value=AdminState.skill_name,
                  on_change=AdminState.set_skill_name, width="100%")),
            field("Skill type", rx.select(SKILL_TYPES, value=AdminState.skill_type,
                                          on_change=AdminState.set_skill_type, width="100%")),
            columns=rx.breakpoints(initial="1", md="2"), gap="1rem", width="100%"),
        field("Description", rx.text_area(value=AdminState.skill_description,
                                          on_change=AdminState.set_skill_description,
                                          width="100%", min_height="5rem")),
        rx.grid(agent_picker(), model_picker(),
                columns=rx.breakpoints(initial="1", md="2"), gap="1rem",
                align="start", width="100%"),
        rx.cond(AdminState.skill_type == "cron trigger",
                field("Cron expression", rx.input(value=AdminState.skill_cron,
                                                  on_change=AdminState.set_skill_cron, width="100%"),
                      rx.hstack(rx.icon("clock-3", size=14), rx.text(AdminState.cron_description),
                                color=MUTED, font_size="0.82rem"))),
        rx.cond(AdminState.skill_type == "email trigger",
                field("Email address", rx.input(value=AdminState.skill_email,
                                                on_change=AdminState.set_skill_email, width="100%"))),
        rx.cond(AdminState.skill_type == "aws-sqs trigger",
                field("AWS SQS queue", rx.input(value=AdminState.skill_sqs,
                                                on_change=AdminState.set_skill_sqs, width="100%"))),
        rx.cond(AdminState.skill_type == "issue trigger",
                rx.grid(
                    field("Issue type", rx.select(
                        AdminState.skill_issue_types,
                        value=AdminState.skill_issue_type,
                        on_change=AdminState.set_skill_issue_type, width="100%")),
                    field("Issue statuses", rx.input(value=AdminState.skill_issue_status,
                                                     on_change=AdminState.set_skill_issue_status,
                                                     placeholder="Ready, In progress", width="100%")),
                    columns=rx.breakpoints(initial="1", md="2"), gap="1rem", width="100%")),
        rx.vstack(
            rx.checkbox("Use other fields in frontmatter (for instance allowed-tools)",
                        checked=AdminState.skill_extra_enabled,
                        on_change=AdminState.set_skill_extra_enabled,
                        size="2"),
            rx.cond(
                AdminState.skill_extra_enabled,
                field("Other frontmatter fields",
                      rx.text_area(value=AdminState.skill_extra,
                                   on_change=AdminState.set_skill_extra,
                                   placeholder="allowed-tools: Bash",
                                   width="100%", min_height="7rem",
                                   font_family="IBM Plex Mono, monospace"),
                      rx.text("YAML lines written into the frontmatter as they are.",
                              color=MUTED, font_size="0.82rem"))),
            spacing="3", align="start", width="100%"),
        field("Skill body", rx.text_area(value=AdminState.skill_body, on_change=AdminState.set_skill_body,
                                         width="100%", min_height="25rem",
                                         font_family="IBM Plex Mono, monospace")),
        rx.cond(AdminState.skill_type == "cron trigger",
                rx.button(rx.icon("play", size=16), "Run on next tick", variant="outline",
                          on_click=AdminState.force_run_skill)),
        spacing="5", align="start", width="100%",
    )


def skills_page() -> rx.Component:
    listing = rx.vstack(
        rx.flex(rx.input(placeholder="New skill name", value=AdminState.new_skill_name,
                         on_change=AdminState.set_new_skill_name, flex="1"),
                rx.button(rx.icon("plus", size=16), "Create",
                          on_click=AdminState.create_skill),
                gap="0.75rem", width="100%"),
        rx.grid(rx.input(placeholder="Search skills", value=AdminState.skill_query,
                         on_change=AdminState.set_skill_query, width="100%"),
                rx.select(["All", *SKILL_TYPES], value=AdminState.skill_filter,
                          on_change=AdminState.set_skill_filter, width="100%"),
                columns=rx.breakpoints(initial="1", md="3fr 1fr"), gap="0.75rem", width="100%"),
        rx.cond(AdminState.agents_card_visible | (AdminState.filtered_skills.length() > 0),
                rx.grid(rx.cond(AdminState.agents_card_visible, agents_card()),
                        rx.foreach(AdminState.filtered_skills, skill_card),
                        columns=rx.breakpoints(initial="1", md="2", xl="3"), gap="1rem", width="100%"),
                empty_state("search-x", "No skills match this view.")),
        spacing="5", align="start", width="100%")
    return shell(rx.vstack(page_header("Skills", "Create and configure agent capabilities."),
                           rx.cond(AdminState.editing_agents, agents_editor(),
                                   rx.cond(AdminState.selected_skill ==
                                           "", listing, skill_editor())),
                           align="start", width="100%"))


def memory_row(entry: MemoryEntry) -> rx.Component:
    return rx.cond(
        entry.matched,
        rx.flex(
            rx.vstack(rx.text(entry.title, font_weight="600"), rx.text(entry.hook, color=MUTED,
                                                                       font_size="0.85rem"),
                      spacing="1", align="start", flex="1"),
            rx.text(entry.file, color=MUTED,
                    font_family="IBM Plex Mono, monospace", font_size="0.8rem"),
            rx.button(rx.icon("pencil", size=15), variant="ghost",
                      on_click=AdminState.edit_memory(entry.file), aria_label="Edit memory"),
            rx.button(rx.icon("trash-2", size=15), variant="ghost", color_scheme="red",
                      on_click=AdminState.delete_memory(entry.file, entry.raw), aria_label="Delete memory"),
            gap="0.75rem", align="center", padding="1rem", background=SURFACE,
            border=BORDER, width="100%"),
        rx.box(rx.text(entry.raw, font_family="IBM Plex Mono, monospace", font_size="0.85rem"),
               padding="1rem", border=BORDER, background=SURFACE),
    )


def memory_editor() -> rx.Component:
    return rx.vstack(
        rx.hstack(rx.button(rx.icon("arrow-left", size=16), "Back", variant="ghost",
                            on_click=AdminState.close_memory),
                  rx.heading(AdminState.selected_memory,
                             size="4"), rx.spacer(),
                  rx.button(rx.icon("save", size=16), "Save memory",
                            on_click=AdminState.save_memory),
                  width="100%"),
        rx.text_area(value=AdminState.memory_content, on_change=AdminState.set_memory_content,
                     width="100%", min_height="32rem", font_family="IBM Plex Mono, monospace"),
        spacing="4", width="100%"),


COPILOT_MEMORY_URL = "https://github.com/settings/copilot/memory"


def copilot_memory_notice() -> rx.Component:
    """Github Copilot keeps memory in the GitHub account, not in the repository."""
    return rx.center(
        rx.vstack(
            rx.icon("notebook-text", size=28, color=SUBTLE_ICON),
            rx.text("Memory is stored in GitHub account and can be managed here",
                    color=MUTED),
            rx.link(COPILOT_MEMORY_URL, href=COPILOT_MEMORY_URL, is_external=True,
                    color=ACCENT),
            spacing="3", align="center"),
        border="1px dashed var(--codee-border)",
        min_height="10rem",
        width="100%",
    )


def memory_page() -> rx.Component:
    listing = rx.cond(AdminState.memories.length() > 0,
                      rx.vstack(rx.foreach(AdminState.memories,
                                memory_row), spacing="3", width="100%"),
                      empty_state("notebook-text", "No memories yet."))
    return shell(rx.vstack(page_header("Memory", "Manage agent provider's memory."),
                           rx.cond(AdminState.coding_agent == "github_copilot",
                                   copilot_memory_notice(),
                                   rx.cond(AdminState.selected_memory ==
                                           "", listing, memory_editor())),
                           align="start", width="100%"))


def repository_row(repository: RepositorySummary) -> rx.Component:
    return rx.flex(
        rx.icon("folder-git-2", size=18, color=SUBTLE_ICON),
        rx.vstack(
            rx.hstack(rx.text(repository.name, font_weight="600"),
                      rx.cond(repository.default_branch != "",
                              rx.badge(rx.icon("house", size=12),
                                       repository.default_branch,
                                       color_scheme="blue", variant="soft",
                                       custom_attrs={"title": "Default branch"})),
                      spacing="2", align="center"),
            rx.cond(repository.url != "",
                    rx.text(repository.url, color=MUTED, font_family=MONO,
                            font_size="0.8rem")),
            spacing="2", align="start", flex="1", min_width="0"),
        gap="0.85rem", align="start", padding="1rem", background=SURFACE,
        border=BORDER, width="100%")


def repositories_page() -> rx.Component:
    listing = rx.cond(
        AdminState.repositories.length() > 0,
        rx.vstack(rx.foreach(AdminState.repositories, repository_row),
                  spacing="3", width="100%"),
        empty_state("folder-git-2", "No repositories cloned yet."))
    return shell(rx.vstack(
        page_header("Repositories",
                    "Repositories the coding agents work in."),
        rx.box(
            rx.flex(
                rx.input(placeholder="git@github.com:org/repo.git",
                         value=AdminState.new_repository_url,
                         on_change=AdminState.set_new_repository_url,
                         flex="1"),
                rx.button(rx.icon("plus", size=16), "Add repository",
                          loading=AdminState.adding_repository,
                          on_click=AdminState.add_repository),
                gap="0.75rem", width="100%"),
            rx.text("Clones a bare repository into repositories/<name>/.bare "
                    "and checks out its default branch as a worktree beside it. "
                    "A first clone can take a few minutes.",
                    color=MUTED, font_size="0.82rem", margin_top="0.75rem"),
            padding="1.25rem", background=SURFACE, border=BORDER, width="100%"),
        rx.callout(
            rx.fragment(
                "The agents work better when they know what each repository "
                "is for: describe them in ",
                rx.link(AGENTS_FILE, on_click=AdminState.open_agents_editor,
                        color=ACCENT, cursor="pointer",
                        text_decoration="underline"),
                ".",
            ),
            icon="info", size="1", color_scheme="gray", width="100%"),
        listing,
        spacing="5", align="start", width="100%"))


def run_row(run: RunRecord) -> rx.Component:
    return rx.box(
        rx.flex(
            rx.vstack(rx.hstack(rx.text(run.skill_name, font_weight="600"),
                                rx.badge(run.status, color_scheme=rx.cond(run.status == "succeeded", "green", "red"))),
                      rx.text(local_datetime(run.started_at), color=MUTED,
                              font_size="0.8rem",
                              font_family="IBM Plex Mono, monospace"),
                      rx.text("Thread ID: ", run.session_id, color=MUTED,
                              font_size="0.8rem",
                              font_family="IBM Plex Mono, monospace"),
                      rx.text(run.preview, color=MUTED),
                      rx.cond(run.error != "", rx.text(
                          run.error, color="#b42318", font_size="0.85rem")),
                      spacing="2", align="start", flex="1"),
            rx.badge(run.trigger_type, variant="outline"),
            rx.cond(run.viewer_url != "", rx.link(rx.icon("external-link", size=16), href=run.viewer_url,
                                                  is_external=True, aria_label="View session", color=ACCENT)),
            gap="1rem", align="start", width="100%"),
        rx.cond((run.user_message != "") | (run.response != ""), rx.accordion.root(rx.accordion.item(
            header="Run info", content=rx.vstack(
                rx.text("User message", font_weight="600"),
                rx.text(run.user_message, white_space="pre-wrap"),
                rx.cond(run.response != "", rx.fragment(
                    rx.text("LLM response", font_weight="600",
                            margin_top="0.75rem"),
                    rx.text(run.response, white_space="pre-wrap"))),
                spacing="2", align="start", width="100%"), value=run.started_at),
            collapsible=True, width="100%")),
        padding="1rem", background=SURFACE, border=BORDER, width="100%")


def runs_page() -> rx.Component:
    listing = rx.vstack(
        rx.foreach(AdminState.runs, run_row),
        rx.cond(AdminState.runs_has_more,
                rx.button(rx.cond(AdminState.runs_loading, "Loading...",
                                  f"Load {RUNS_PAGE_SIZE} more"),
                          variant="soft", width="100%",
                          disabled=AdminState.runs_loading,
                          on_click=AdminState.load_more_runs)),
        spacing="3", width="100%")
    return shell(rx.vstack(page_header("Runs", "Recent trigger executions and outcomes."),
                           rx.cond(AdminState.runs.length() > 0, listing,
                                   empty_state("history", "No runs recorded yet.")),
                           align="start", width="100%"))


def workflow_warning(message: rx.Var) -> rx.Component:
    return rx.callout(
        message,
        icon="triangle-alert",
        color_scheme="orange",
        width="100%",
    )


def edge_menu_item(skill: rx.Var) -> rx.Component:
    return rx.button(
        rx.icon("pencil", size=15),
        rx.text("Edit skill", font_weight="500"),
        rx.text(
            skill,
            color=MUTED,
            font_family="IBM Plex Mono, monospace",
            font_size="0.8rem",
        ),
        variant="ghost",
        justify="start",
        width="100%",
        on_click=AdminState.edit_workflow_skill(skill),
    )


def edge_tooltip_reason(reason: rx.Var) -> rx.Component:
    return rx.text(reason, size="2", color="var(--codee-text)")


def workflow_edge_tooltip() -> rx.Component:
    """Why the hovered transition exists: the skill text it was read from."""
    return rx.cond(
        AdminState.edge_tooltip_reasons.length() > 0,
        rx.vstack(
            rx.text(
                "Transition reason",
                size="1",
                color=MUTED,
                font_weight="600",
                text_transform="uppercase",
                letter_spacing="0.06em",
            ),
            rx.foreach(AdminState.edge_tooltip_reasons, edge_tooltip_reason),
            position="fixed",
            left=AdminState.edge_tooltip_left,
            top=AdminState.edge_tooltip_top,
            z_index="39",
            spacing="1",
            padding="0.5rem 0.65rem",
            max_width="26rem",
            background=SURFACE,
            border=BORDER,
            border_radius="6px",
            box_shadow="0 8px 24px rgba(0, 0, 0, 0.28)",
            # The pointer has to stay on the arrow: a tooltip that catches it
            # would swallow the hover and flicker itself away.
            pointer_events="none",
        ),
    )


def workflow_edge_menu() -> rx.Component:
    """Context menu anchored to the last clicked transition arrow."""
    return rx.cond(
        AdminState.edge_menu_skills.length() > 0,
        rx.fragment(
            rx.box(
                position="fixed",
                inset="0",
                z_index="40",
                on_click=AdminState.close_edge_menu,
            ),
            rx.vstack(
                rx.foreach(AdminState.edge_menu_skills, edge_menu_item),
                position="fixed",
                left=AdminState.edge_menu_left,
                top=AdminState.edge_menu_top,
                z_index="41",
                spacing="1",
                padding="0.3rem",
                min_width="12rem",
                background=SURFACE,
                border=BORDER,
                border_radius="6px",
                box_shadow="0 8px 24px rgba(0, 0, 0, 0.28)",
            ),
        ),
    )


def workflow_section(section: WorkflowSection) -> rx.Component:
    """One work item's graph. Built per section so the page grows with Settings."""
    nodes, edges, warnings = section.nodes, section.edges, section.warnings
    return rx.vstack(
        rx.heading(section.title, size="5"),
        rx.cond(
            nodes.length() > 0,
            rx.vstack(
                rx.cond(
                    warnings.length() > 0,
                    rx.vstack(
                        rx.foreach(warnings, workflow_warning),
                        spacing="3",
                        width="100%",
                    ),
                ),
                workflow_graph(
                    nodes,
                    edges,
                    on_edge_click=AdminState.open_edge_menu,
                    on_edge_mouse_enter=AdminState.show_edge_tooltip,
                    on_edge_mouse_leave=AdminState.hide_edge_tooltip,
                    on_pane_click=AdminState.close_edge_menu,
                ),
                spacing="4",
                width="100%",
            ),
            empty_state(
                "git-branch",
                "No " + section.issue_type + " issue-trigger skills found.",
            ),
        ),
        spacing="4",
        align="start",
        width="100%",
    )


def workflow_progress_line(line: rx.Var[str]) -> rx.Component:
    return rx.text(
        line,
        size="2",
        color_scheme="gray",
        text_align="center",
        white_space="pre-wrap",
        max_width="48rem",
    )


def workflow_running_banner() -> rx.Component:
    """What a run is doing while the graph it will replace is still up.

    Without it a generation started over an existing graph is invisible, and
    the Regenerate button that starts one looks broken.
    """
    return rx.cond(
        AdminState.workflow_running,
        rx.hstack(
            rx.spinner(size="2"),
            rx.vstack(
                rx.text("Generating the workflow...", size="2",
                        font_weight="600"),
                rx.foreach(AdminState.workflow_progress,
                           workflow_running_line),
                spacing="1",
                align="start",
                width="100%",
            ),
            spacing="3",
            align="start",
            width="100%",
            padding="0.75rem",
            border=BORDER,
            border_radius="6px",
            background=SURFACE,
        ),
    )


def workflow_running_line(line: rx.Var[str]) -> rx.Component:
    return rx.text(
        line,
        size="2",
        color_scheme="gray",
        white_space="pre-wrap",
    )


def workflow_page() -> rx.Component:
    return shell(rx.vstack(
        rx.flex(
            page_header(
                "Workflow",
                "Status transitions per work item, inferred from issue-trigger skills."),
            rx.spacer(),
            rx.button(
                rx.icon("refresh-cw", size=16),
                "Regenerate",
                variant="outline",
                loading=AdminState.workflow_running,
                on_click=AdminState.load_workflow(True),
            ),
            align="start",
            width="100%",
        ),
        rx.cond(
            AdminState.workflow_loading,
            rx.center(
                rx.vstack(
                    rx.spinner(size="3"),
                    rx.foreach(AdminState.workflow_progress,
                               workflow_progress_line),
                    spacing="3",
                    align="center",
                    width="100%",
                ),
                min_height="48rem",
                width="100%",
            ),
            rx.cond(
                AdminState.workflow_error != "",
                rx.callout(
                    AdminState.workflow_error,
                    icon="triangle-alert",
                    color_scheme="red",
                    width="100%",
                ),
                rx.vstack(
                    workflow_running_banner(),
                    rx.foreach(AdminState.workflow_sections, workflow_section),
                    spacing="8",
                    width="100%",
                ),
            ),
        ),
        workflow_edge_menu(),
        workflow_edge_tooltip(),
        align="start",
        width="100%",
    ))


def sessions_page() -> rx.Component:
    return shell(rx.vstack(
        page_header(
            "Sessions", "Open the configured coding agent session viewer."),
        rx.cond(AdminState.session_viewer != "",
                rx.link(rx.button(rx.icon("external-link", size=16), "Open session viewer"),
                        href=AdminState.session_viewer, is_external=True),
                empty_state("key-round", "Set CODEE_SESSION_VIEWER_URL to enable the session viewer.")),
        align="start", width="100%"))


def azure_step(number: int, title: str, detail: rx.Component | str) -> rx.Component:
    return rx.hstack(
        rx.box(str(number), color="white", background=ACCENT_DEEP, min_width="1.4rem",
               height="1.4rem", display="grid", place_items="center",
               border_radius="50%", font_size="0.75rem", font_weight="700"),
        rx.vstack(rx.text(title, font_weight="600", font_size="0.9rem"),
                  rx.text(detail, color=MUTED, font_size="0.85rem")
                  if isinstance(detail, str) else detail,
                  spacing="1", align="start", width="100%"),
        spacing="3", align="start", width="100%")


def azure_redirect_uri_box() -> rx.Component:
    """The redirect URI to register, copyable — Entra ID matches it character for character."""
    return rx.hstack(
        rx.input(value=AdminState.azure_redirect_uri, read_only=True,
                 font_family="IBM Plex Mono, monospace", font_size="0.8rem",
                 width="100%"),
        rx.button(rx.icon("copy", size=15), variant="outline", type="button",
                  on_click=rx.set_clipboard(AdminState.azure_redirect_uri)),
        spacing="2", width="100%")


def azure_instructions() -> rx.Component:
    """Collapsed by default: needed once, when the Entra app is first created."""
    return rx.accordion.root(
        rx.accordion.item(
            header=rx.text("How to create the Azure app registration",
                           font_size="0.9rem", font_weight="600"),
            content=rx.vstack(
                azure_step(1, "Register the app",
                           "Azure portal → Microsoft Entra ID → App registrations → "
                           "New registration. Name it Codee. Use the directory that "
                           "backs your Azure DevOps organization."),
                azure_step(2, "Add a Web redirect URI",
                           rx.vstack(
                               rx.text("Platform Web — not SPA, because Codee exchanges the "
                                       "code on the server with the client secret.",
                                       color=MUTED, font_size="0.85rem"),
                               azure_redirect_uri_box(),
                               spacing="2", width="100%")),
                azure_step(3, "Grant the Azure DevOps permission",
                           "API permissions → Add a permission → Azure DevOps → Delegated "
                           "→ user_impersonation → Add. Entra publishes no read-only scope "
                           "for Azure DevOps; Codee only ever issues read calls, and you can "
                           "narrow it further by connecting with an account that has Readers "
                           "access to only the projects it should see."),
                azure_step(4, "Create a client secret",
                           "Certificates & secrets → New client secret. Copy the Value "
                           "column immediately — Azure hides it once you leave the page."),
                azure_step(5, "Copy the app identifiers",
                           "From Overview: Application (client) ID and Directory (tenant) ID."),
                azure_step(6, "Fill the fields below and connect",
                           "Connecting sends you to Microsoft to sign in. Codee stores the "
                           "resulting access and refresh tokens and renews them on its own."),
                spacing="4", padding="1rem 0.25rem", width="100%"),
        ),
        type="single", collapsible=True, variant="ghost", width="100%")


def azure_connection_status() -> rx.Component:
    return rx.cond(
        AdminState.azure_connected,
        rx.hstack(
            rx.icon("circle-check", size=17, color=ACCENT),
            rx.vstack(
                rx.text(rx.cond(AdminState.azure_account != "",
                                f"Connected as {AdminState.azure_account}",
                                "Connected to Azure DevOps"),
                        font_weight="600", font_size="0.9rem"),
                rx.text(AdminState.azure_expires_label,
                        color=MUTED, font_size="0.8rem"),
                spacing="1", align="start"),
            rx.spacer(),
            rx.button("Disconnect", variant="outline", color_scheme="red",
                      type="button", on_click=AdminState.disconnect_azure_devops),
            align="center", width="100%"),
        rx.callout("Not connected. Fill in the app details, then connect.",
                   icon="info", size="1", color_scheme="gray", width="100%"),
    )


def azure_fields() -> rx.Component:
    return rx.vstack(
        azure_instructions(),
        field("Organization URL", rx.input(value=AdminState.azure_organization_url,
                                           on_change=AdminState.set_azure_organization_url,
                                           placeholder="https://dev.azure.com/your-org",
                                           width="100%"),
              hint=rx.text("Work items are picked up from every project in this "
                           "organization the connected account can read.",
                           color=MUTED, font_size="0.8rem")),
        field("Application (client) ID", rx.input(value=AdminState.azure_client_id,
                                                  on_change=AdminState.set_azure_client_id,
                                                  width="100%")),
        field("Client secret", rx.input(value=AdminState.azure_client_secret,
                                        on_change=AdminState.set_azure_client_secret,
                                        type="password", width="100%")),
        field("Directory (tenant) ID", rx.input(value=AdminState.azure_tenant_id,
                                                on_change=AdminState.set_azure_tenant_id,
                                                width="100%"),
              hint=rx.text("Optional. Leave empty to sign in against any work or school "
                           "directory you belong to.", color=MUTED, font_size="0.8rem")),
        azure_connection_status(),
        rx.button(rx.icon("plug", size=16),
                  rx.cond(AdminState.azure_connected,
                          "Reconnect to Azure DevOps", "Connect to Azure DevOps"),
                  type="button", disabled=~AdminState.azure_can_connect,
                  on_click=AdminState.connect_azure_devops),
        spacing="4", width="100%")


def mcp_setup_button() -> rx.Component:
    """The setup button, built fresh per branch so each can sit in its own row."""
    return rx.button(
        rx.icon("file-cog", size=16), AdminState.mcp_button_label,
        type="button", variant="outline",
        disabled=~AdminState.mcp_can_setup,
        on_click=AdminState.setup_tasks_mcp)


def tasks_mcp_setup() -> rx.Component:
    """Hand the provider's credentials to the coding agent as an MCP server."""
    return rx.vstack(
        rx.cond(
            AdminState.mcp_configured,
            # Already installed: the state is the sentence, and the button that
            # rewrites it with whatever the fields now say sits beside it.
            rx.hstack(
                rx.icon("circle-check", size=16, color="var(--green-9)"),
                rx.text("MCP config already set up.", font_size="0.85rem"),
                mcp_setup_button(),
                spacing="3", align="center", width="100%"),
            rx.hstack(
                mcp_setup_button(),
                rx.cond(
                    AdminState.mcp_can_setup,
                    rx.text("Writes .mcp.json so the coding agent can read and "
                            "update tasks itself.",
                            color=MUTED, font_size="0.8rem"),
                    rx.text(AdminState.mcp_missing_hint,
                            color=MUTED, font_size="0.8rem")),
                spacing="3", align="center", width="100%")),
        rx.cond(
            AdminState.mcp_setup_message != "",
            rx.cond(
                AdminState.mcp_setup_ok,
                rx.callout(AdminState.mcp_setup_message, icon="circle-check",
                           size="1", color_scheme="green", width="100%"),
                rx.callout(AdminState.mcp_setup_message, icon="circle-alert",
                           size="1", color_scheme="red", width="100%")),
        ),
        spacing="3", width="100%")


def work_item_type_chip(index: Any, provider_type: Any) -> rx.Component:
    """One backend type a work item is polled as, with the way to drop it.

    A chip rather than another dropdown: the types on a row are a set, and a
    row of selects would ask the user to read four boxes to learn what a
    dropdown-less list says at a glance.
    """
    return rx.badge(
        provider_type,
        # A real button rather than an icon with a click handler: unmapping a
        # type is the only way back out of a pick, and a bare svg would put it
        # out of reach of the keyboard and unannounced to a screen reader.
        rx.el.button(
            rx.icon("x", size=12),
            type="button",
            aria_label="Remove work item type " + provider_type,
            title="Remove work item type",
            on_click=lambda: AdminState.remove_work_item_type(
                index, provider_type),
            style={"display": "flex", "alignItems": "center",
                   "cursor": "pointer", "background": "none",
                   "border": "none", "padding": "0", "color": "inherit"}),
        color_scheme="gray", variant="soft", size="2", flex_shrink="0")


def work_item_row(item: WorkItem, index: int) -> rx.Component:
    """One mapping: the Codee work item, and what the provider calls it.

    A work item is found one of two ways, and the control on the left of the
    second cell picks which. Pointed at backend types, each shows as a chip and
    the dropdown beside them adds one more. Given a query instead, the chips
    give way to the box it is written in. The mandatory two
    render with their name read-only and no remove button — the same row as the
    rest, minus the two things that would break the executor. Everything else
    is the user's to name, repoint, and delete.
    """
    # Each side gets its own flex box rather than a bare `width="100%"` child:
    # two flex items both asking for the full row collapse unpredictably, and
    # the name input lost every pixel of it.
    cell = {"flex": "1", "min_width": "0"}
    return rx.hstack(
        rx.box(
            rx.cond(
                item.fixed,
                # Read-only rather than disabled: a disabled input greys its
                # text, and "story" then reads as a placeholder for a name the
                # user still has to type instead of the name it already has.
                rx.input(value=item.name, read_only=True, width="100%",
                         cursor="default"),
                rx.input(value=item.name, placeholder="bug",
                         on_change=lambda value: AdminState.set_work_item_name(
                             index, value),
                         width="100%"),
            ),
            **cell),
        rx.icon("arrow-right", size=16, color=MUTED, flex_shrink="0"),
        rx.box(
            rx.hstack(
                rx.segmented_control.root(
                    rx.segmented_control.item("Types", value="types"),
                    rx.segmented_control.item(
                        AdminState.work_item_query_label, value="query"),
                    value=item.mode, size="1", flex_shrink="0",
                    on_change=lambda value: AdminState.set_work_item_mode(
                        index, value),
                ),
                rx.cond(
                    item.mode == "query",
                    # Nothing here is validated: only the backend can say
                    # whether a condition parses, and Verify connection below
                    # is how it gets asked.
                    rx.input(
                        value=item.query,
                        placeholder=AdminState.work_item_query_placeholder,
                        on_change=lambda value:
                            AdminState.set_work_item_query(index, value),
                        flex="1", min_width="0"),
                    rx.hstack(
                        rx.foreach(
                            item.provider_types,
                            lambda provider_type: work_item_type_chip(
                                index, provider_type)),
                        rx.select(
                            AdminState.work_item_type_options,
                            # Bound to nothing on purpose: picking is what adds
                            # a chip, and the box goes straight back to its
                            # placeholder so the next type can be added without
                            # clearing the last one.
                            value="",
                            placeholder=rx.cond(item.provider_types,
                                                "Add a work item type",
                                                "Select a work item type"),
                            # Inert while the fetch is in flight: the list it
                            # would offer is the thing being replaced, so a
                            # pick made now is a pick from a menu that is about
                            # to change under it.
                            disabled=AdminState.work_item_types_loading,
                            on_change=lambda value:
                                AdminState.add_work_item_type(index, value),
                        ),
                        spacing="2", align="center", wrap="wrap",
                        flex="1", min_width="0"),
                ),
                spacing="2", align="center", width="100%"),
            flex="2", min_width="0"),
        # The delete column is a fixed-width box holding the button rather
        # than the button itself. A ghost button carries a negative margin to
        # align optically, which swallows the row's gap and makes the column
        # 10px narrower than the spacer a fixed row gets — enough for the two
        # flex cells to absorb the difference and leave the rows unaligned.
        rx.box(
            rx.cond(
                item.fixed,
                rx.fragment(),
                # Icon-only, so it needs a name of its own: without one a
                # screen reader announces identical "button"s down the column.
                rx.button(rx.icon("trash-2", size=14), type="button",
                          variant="ghost", color_scheme="red",
                          aria_label="Remove work item " + item.name,
                          title="Remove work item",
                          on_click=lambda: AdminState.remove_work_item(index)),
            ),
            width="2rem", flex_shrink="0", display="flex",
            align_items="center", justify_content="center"),
        spacing="2", align="center", width="100%")


def work_items_setting() -> rx.Component:
    """Which work items Codee picks up, and what each is called in the backend.

    The names on the left are what a skill declares in ``x-codee-issue-type``;
    the types on the right are what the provider offers. Those are fetched when
    the page loads, so the dropdowns are already populated by the time anyone
    opens one. The button is for the case the page load cannot cover: the
    credentials were edited since, and the list belongs to the old ones.
    """
    return rx.vstack(
        rx.hstack(
            rx.text("Work items", weight="medium", font_size="0.85rem"),
            # The fetch can take seconds — Azure DevOps walks the whole
            # organization — so the heading says what is happening rather than
            # leaving the dropdowns looking merely unresponsive.
            rx.cond(
                AdminState.work_item_types_loading,
                rx.hstack(
                    rx.spinner(size="1"),
                    rx.text(f"Loading work item types from "
                            f"{AdminState.mcp_provider_label}…",
                            color=MUTED, font_size="0.8rem"),
                    spacing="2", align="center")),
            spacing="2", align="center", width="100%"),
        rx.foreach(AdminState.work_items, work_item_row),
        rx.hstack(
            rx.button(rx.icon("plus", size=16), "Add work item",
                      type="button", variant="outline",
                      disabled=AdminState.work_item_types_loading,
                      on_click=AdminState.add_work_item),
            rx.button(
                rx.cond(AdminState.work_item_types_loading,
                        rx.spinner(size="2"), rx.icon("refresh-cw", size=16)),
                rx.cond(AdminState.work_item_types_loading,
                        "Loading types…", "Reload types"),
                type="button", variant="outline",
                disabled=(AdminState.work_item_types_loading
                          | ~AdminState.work_item_types_can_load),
                on_click=AdminState.load_work_item_types),
            rx.text(AdminState.work_item_types_hint,
                    color=MUTED, font_size="0.8rem"),
            spacing="3", align="center", width="100%"),
        rx.cond(
            AdminState.work_item_types_error != "",
            rx.callout(AdminState.work_item_types_error, icon="circle-alert",
                       size="1", color_scheme="red", width="100%"),
        ),
        spacing="3", width="100%")


def task_filter_setting() -> rx.Component:
    """One more condition every poll is narrowed by, in the provider's own language.

    Sits under the work items because it answers the same question they do —
    what Codee picks up — and is empty out of the box, which is what keeps the
    query unchanged for everyone who never opens this. Nothing here is
    validated: the backend is the only thing that can say whether a clause
    parses, and **Verify connection** below is how it gets asked.
    """
    return field(
        AdminState.task_filter_label,
        rx.cond(
            AdminState.tasks_provider == "jira",
            rx.text_area(value=AdminState.jira_task_filter,
                         on_change=AdminState.set_jira_task_filter,
                         placeholder='labels = "codee" AND '
                                     'component != "legacy"',
                         rows="2", width="100%"),
            rx.text_area(value=AdminState.azure_task_filter,
                         on_change=AdminState.set_azure_task_filter,
                         placeholder="[System.Tags] CONTAINS 'codee'",
                         rows="2", width="100%")),
        hint=rx.text("Optional. Added to every task query as one more AND "
                     "condition, on top of the work items above.",
                     color=MUTED, font_size="0.8rem"))


def tasks_check_row(check: CheckResult) -> rx.Component:
    """One check: where it stands as an icon, its name, and what it found."""
    marker = {"flex_shrink": "0", "margin_top": "0.15rem"}
    return rx.hstack(
        rx.match(
            check.status,
            ("running", rx.spinner(size="2", **marker)),
            ("ok", rx.icon("circle-check", size=16,
                           color="var(--green-9)", **marker)),
            ("failed", rx.icon("circle-alert", size=16,
                               color="var(--red-9)", **marker)),
            # Still queued: named so the list is complete from the first frame,
            # but visibly not its turn yet.
            rx.icon("circle-dashed", size=16, color=MUTED, **marker)),
        rx.vstack(
            rx.text(check.name, font_size="0.85rem", weight="medium",
                    color=rx.cond(check.status == "waiting", MUTED, TEXT)),
            rx.cond(
                check.message != "",
                rx.text(check.message, color=MUTED, font_size="0.8rem",
                        white_space="pre-wrap", overflow_wrap="anywhere")),
            spacing="1", align="start", width="100%"),
        spacing="2", align="start", width="100%")


def tasks_verification() -> rx.Component:
    """Run the provider checks and print what each one found."""
    return rx.vstack(
        rx.hstack(
            rx.button(
                rx.cond(AdminState.tasks_verifying,
                        rx.spinner(size="2"), rx.icon("plug-zap", size=16)),
                rx.cond(AdminState.tasks_verifying,
                        "Verifying…", "Verify connection"),
                type="button", variant="outline",
                disabled=AdminState.tasks_verifying | ~AdminState.tasks_can_verify,
                on_click=AdminState.verify_tasks_connection),
            rx.cond(
                AdminState.tasks_can_verify,
                # Worth saying up front: the second check spends a coding-agent
                # run and leaves a closed task behind in the real backend.
                rx.text("Pull/modification tasks check.",
                        color=MUTED, font_size="0.8rem"),
                rx.text("Fill in every field above to check the connection.",
                        color=MUTED, font_size="0.8rem")),
            spacing="3", align="center", width="100%"),
        rx.cond(
            AdminState.tasks_checks,
            rx.vstack(rx.foreach(AdminState.tasks_checks, tasks_check_row),
                      spacing="3", width="100%",
                      padding="0.85rem", border=BORDER,
                      background=PAGE_BACKGROUND),
        ),
        spacing="3", width="100%")


def claude_account_row(account: ClaudeAccount) -> rx.Component:
    """One connected account: who it is, whether it is live, and disconnect."""
    return rx.hstack(
        rx.icon("circle-user-round", size=16, color=MUTED, flex_shrink="0"),
        rx.text(rx.cond(account.label != "", account.label,
                        "Account " + account.id.to_string()),
                font_size="0.85rem", overflow="hidden",
                text_overflow="ellipsis", white_space="nowrap"),
        rx.cond(account.subscription != "",
                rx.badge(account.subscription, color_scheme="gray",
                         variant="soft", flex_shrink="0"),
                rx.fragment()),
        rx.cond(account.in_use,
                rx.badge("in use", color_scheme="green", variant="soft",
                         flex_shrink="0"),
                rx.fragment()),
        # Said out loud rather than left to a silent skip: an account that can
        # no longer renew itself is one the user has to sign in again, and
        # nothing else on this page would ever tell them.
        rx.cond(account.needs_reconnect,
                rx.badge("sign in again", color_scheme="amber",
                         variant="soft", flex_shrink="0",
                         title="This account's session has expired. "
                               "Disconnect it and connect it again."),
                rx.fragment()),
        rx.spacer(),
        # Icon-only, so it needs a name of its own: without one a screen reader
        # announces identical "button"s down the column.
        rx.button(rx.icon("trash-2", size=14), type="button",
                  variant="ghost", color_scheme="red",
                  aria_label="Disconnect " + account.label,
                  title="Disconnect this account",
                  on_click=lambda: AdminState.disconnect_claude_code_account(
                      account.id)),
        spacing="3", align="center", width="100%",
        padding="0.45rem 0.7rem", border=BORDER, background=PAGE_BACKGROUND)


def claude_code_sign_in() -> rx.Component:
    """The box that appears while a sign-in is waiting for its code.

    Anthropic prints the code on its own page rather than redirecting anywhere
    Codee could listen, so the last step is a copy and paste. The URL is shown
    as well as opened: the machine running Codee is often a server with no
    browser, and the page has to be openable from wherever the user is.
    """
    return rx.vstack(
        rx.text("Approve the sign-in in the page that opened, then paste the "
                "code it shows back here.", font_size="0.82rem"),
        rx.text("If nothing opened, copy this link into a browser:",
                color=MUTED, font_size="0.8rem"),
        rx.code(AdminState.claude_code_authorize_url, font_size="0.72rem",
                color_scheme="gray", style={"word_break": "break-all"},
                width="100%"),
        rx.hstack(
            rx.input(value=AdminState.claude_code_auth_code,
                     on_change=AdminState.set_claude_code_auth_code,
                     placeholder="Paste the code from that page",
                     type="password", flex="1", min_width="0"),
            rx.button(
                rx.cond(AdminState.claude_code_connecting,
                        rx.spinner(size="2"), rx.icon("check", size=16)),
                rx.cond(AdminState.claude_code_connecting,
                        "Connecting\u2026", "Connect"),
                type="button", disabled=AdminState.claude_code_connecting,
                on_click=AdminState.finish_claude_code_sign_in),
            rx.button("Cancel", type="button", variant="soft",
                      disabled=AdminState.claude_code_connecting,
                      on_click=AdminState.cancel_claude_code_sign_in),
            spacing="3", align="center", width="100%"),
        spacing="3", width="100%", padding="0.85rem",
        border=BORDER, background=PAGE_BACKGROUND)


def claude_code_accounts_setting() -> rx.Component:
    """The connected accounts, and the button that connects another.

    Only drawn while the option is on: with it off Codee never touches the
    credentials file, and a list of accounts under a switched-off setting reads
    as something that is in use.
    """
    return rx.vstack(
        rx.cond(
            AdminState.claude_code_accounts,
            rx.vstack(rx.foreach(AdminState.claude_code_accounts,
                                 claude_account_row),
                      spacing="2", width="100%"),
            rx.callout("Connect at least one account, or Codee will leave "
                       "Claude Code signed in as it already is.",
                       icon="circle-alert", size="1", color_scheme="amber",
                       width="100%")),
        rx.cond(
            AdminState.claude_code_authorize_url != "",
            claude_code_sign_in(),
            rx.button(rx.icon("plus", size=16), "Connect a Claude account",
                      type="button", variant="outline",
                      on_click=AdminState.start_claude_code_sign_in)),
        spacing="3", width="100%")


def claude_code_setting() -> rx.Component:
    """Rotating Claude Code between subscriptions as each one runs out.

    Only on the page when the CLI is installed: the accounts are written into
    Claude Code's own credentials file, so on a machine running Copilot or
    Codex alone this would configure something that can never happen.
    """
    return rx.box(
        rx.heading("Claude Code", size="4", margin_bottom="1rem"),
        rx.vstack(
            rx.checkbox("Use specified accounts - Auto accounts rotate",
                        checked=AdminState.claude_code_rotate_keys,
                        on_change=AdminState.set_claude_code_rotate_keys,
                        size="2"),
            rx.cond(AdminState.claude_code_rotate_keys,
                    claude_code_accounts_setting(), rx.fragment()),
            spacing="3", align="start", width="100%"),
        padding="1.25rem", background=SURFACE, border=BORDER, width="100%")


def conversation_message(message: ConversationMessage) -> rx.Component:
    is_user = message.role == "user"
    return rx.box(
        rx.text(message.content, white_space="pre-wrap"),
        align_self=rx.cond(is_user, "end", "start"),
        background=rx.cond(is_user, ACTIVE, SURFACE),
        border=rx.cond(is_user, "none", BORDER),
        border_radius="6px",
        padding="0.65rem 0.8rem",
        max_width="85%",
    )


def agent_test_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Test conversation"),
            rx.dialog.description(
                "Chat with the selected coding agent in the Codee project.",
                color=MUTED),
            field(
                "Model",
                rx.popover.root(
                    rx.popover.trigger(
                        rx.button(
                            rx.hstack(
                                rx.text(AdminState.agent_test_model_label),
                                rx.spacer(),
                                rx.icon("chevrons-up-down", size=14),
                                align="center", width="100%"),
                            variant="surface", color_scheme="gray",
                            width="100%", type="button")),
                    rx.popover.content(
                        rx.vstack(
                            rx.input(
                                placeholder=(
                                    "Search models, or type a model code"),
                                value=AdminState.agent_test_model_query,
                                on_change=AdminState.set_agent_test_model_query,
                                auto_focus=True, width="100%"),
                            rx.cond(
                                AdminState.custom_agent_test_model_query != "",
                                model_menu_item(
                                    rx.button(
                                        rx.hstack(
                                            rx.icon("plus", size=14),
                                            rx.text("Use "),
                                            rx.code(
                                                AdminState.custom_agent_test_model_query),
                                            align="center", spacing="2"),
                                        variant="soft", width="100%",
                                        justify_content="start",
                                        padding="0.45rem 0.6rem",
                                        on_click=AdminState.choose_agent_test_model(
                                            AdminState.custom_agent_test_model_query)))),
                            rx.scroll_area(
                                rx.vstack(
                                    model_menu_item(
                                        rx.button(
                                            "Agent default", variant="ghost",
                                            color_scheme="gray", width="100%",
                                            justify_content="start",
                                            padding="0.45rem 0.6rem",
                                            on_click=AdminState.choose_agent_test_model(""))),
                                    rx.foreach(
                                        AdminState.filtered_agent_test_models,
                                        agent_test_model_option_row),
                                    rx.cond(
                                        AdminState.agent_test_models_loading,
                                        rx.text(
                                            "Loading models from the coding agent…",
                                            color=MUTED, font_size="0.8rem",
                                            padding="0.5rem")),
                                    spacing="1", width="100%"),
                                type="auto", scrollbars="vertical",
                                max_height="15rem", width="100%"),
                            spacing="2", width="100%"),
                        width="24rem", max_width="calc(100vw - 3rem)")),
                rx.text(
                    rx.cond(
                        AdminState.agent_test_model == "",
                        "Runs on whatever that agent defaults to.",
                        rx.fragment("Uses ",
                                    rx.code(AdminState.agent_test_model),
                                    " for this conversation.")),
                    color=MUTED, font_size="0.82rem")),
            rx.scroll_area(
                rx.vstack(
                    rx.cond(
                        AdminState.agent_test_messages.length() == 0,
                        rx.text("Send a message to start the conversation.",
                                color=MUTED, font_size="0.9rem",
                                align_self="center", margin_top="3rem"),
                        rx.foreach(AdminState.agent_test_messages,
                                   conversation_message)),
                    width="100%", spacing="3"),
                type="auto", scrollbars="vertical", height="22rem",
                width="100%", margin_top="1rem"),
            rx.form(
                rx.hstack(
                    rx.input(
                        value=AdminState.agent_test_input,
                        on_change=AdminState.set_agent_test_input,
                        placeholder="Message the agent",
                        disabled=AdminState.agent_test_sending,
                        auto_focus=True,
                        width="100%"),
                    rx.button(rx.icon("send", size=16), type="submit",
                              loading=AdminState.agent_test_sending,
                              disabled=AdminState.agent_test_input == ""),
                    spacing="2", width="100%"),
                on_submit=AdminState.send_agent_test_message,
                reset_on_submit=False,
                width="100%", margin_top="1rem"),
            rx.flex(
                rx.dialog.close(rx.button("Close", variant="soft",
                                          color_scheme="gray")),
                justify="end", margin_top="1rem"),
            max_width="38rem"),
        open=AdminState.agent_test_open,
        on_open_change=AdminState.set_agent_test_open,
    )


def settings_page() -> rx.Component:
    jira = TasksProvider.JIRA
    jira_fields = rx.vstack(
        field("Base URL", rx.input(value=AdminState.jira_base_url,
                                   on_change=AdminState.set_jira_base_url,
                                   placeholder="https://your-company.atlassian.net",
                                   width="100%"),
              credential_hint(jira, "base_url")),
        field("API Token Owner Email",
              rx.input(value=AdminState.jira_account_email,
                       on_change=AdminState.set_jira_account_email,
                       placeholder="agent@example.com", width="100%"),
              credential_hint(jira, "account_email")),
        field("API token", rx.input(value=AdminState.jira_api_token,
                                    on_change=AdminState.set_jira_api_token, type="password", width="100%")),
        field("Project key", rx.input(value=AdminState.jira_project,
                                      on_change=AdminState.set_jira_project,
                                      placeholder="MYPRJ", width="100%"),
              credential_hint(jira, "project")),
        spacing="4", width="100%")
    return shell(rx.vstack(
        page_header(
            "Settings", ""),
        rx.box(
            rx.heading("Coding agent", size="4", margin_bottom="1rem"),
            rx.vstack(
                field("Default agent",
                      rx.grid(
                          rx.select(
                              ["claude_code", "github_copilot", "codex"],
                              value=AdminState.coding_agent,
                              on_change=AdminState.set_coding_agent,
                              width="100%"),
                          rx.button(rx.icon("messages-square", size=16),
                                    "Test conversation", variant="outline",
                                    white_space="nowrap",
                                    on_click=AdminState.open_agent_test),
                          grid_template_columns=rx.breakpoints(
                              initial="minmax(0, 1fr)",
                              md="minmax(0, 1fr) auto"),
                          width="100%", gap="0.75rem")),
                field("Max parallel tasks",
                      rx.input(value=AdminState.max_parallel_agents,
                               on_change=AdminState.set_max_parallel_agents,
                               type="number", min=1, width="100%"),
                      rx.text("How many task agents may run at once.",
                              color=MUTED, font_size="0.82rem")),
                spacing="4", width="100%"),
            padding="1.25rem", background=SURFACE, border=BORDER, width="100%"),
        # Decided when the page is built rather than with an rx.cond, because
        # which agents a machine has is not state: there is no event that could
        # ever flip it, and nothing on the page should be able to.
        claude_code_setting() if CLAUDE_CODE_AVAILABLE else rx.fragment(),
        rx.box(
            rx.heading("Tasks provider", size="4", margin_bottom="1rem"),
            field("Provider", rx.select(["jira", "azure_devops"], value=AdminState.tasks_provider,
                                        on_change=AdminState.set_tasks_provider, width="100%")),
            rx.box(rx.cond(AdminState.tasks_provider == "jira",
                   jira_fields, azure_fields()), margin_top="1rem"),
            rx.box(work_items_setting(), margin_top="1.25rem"),
            rx.box(task_filter_setting(), margin_top="1.25rem"),
            rx.box(tasks_mcp_setup(), margin_top="1.25rem"),
            rx.box(tasks_verification(), margin_top="1.25rem"),
            padding="1.25rem", background=SURFACE, border=BORDER, width="100%"),
        rx.button(rx.icon("save", size=16), "Save settings",
                  on_click=AdminState.save_settings),
        agent_test_dialog(),
        spacing="5", align="start", width="100%"))


# The hover tooltip every status node shares: one `::after` whose text is a
# custom property the node carries, because the graph is drawn by React Flow
# from plain dicts and a node cannot bring a component of its own.
def _workflow_node_tooltip(
    content: str, border_color: str, white_space: str = "normal"
) -> dict[str, str]:
    return {
        "content": content,
        "position": "absolute",
        "bottom": "calc(100% + 8px)",
        "left": "50%",
        "transform": "translateX(-50%)",
        "background": "var(--codee-surface)",
        "border": f"1px solid {border_color}",
        "border_radius": "4px",
        "box_shadow": "0 8px 24px rgba(0, 0, 0, 0.28)",
        "color": "var(--codee-text)",
        "font_family": "IBM Plex Sans, sans-serif",
        "font_size": "0.75rem",
        "font_weight": "500",
        "line_height": "1.45",
        "padding": "0.35rem 0.55rem",
        "text_align": "left",
        "white_space": white_space,
        "width": "max-content",
        "max_width": "18rem",
        "opacity": "0",
        "pointer_events": "none",
        "transition": "opacity 0.12s ease",
        "z_index": "5",
    }


# Lighting the hovered transition up has to be done per transition rather than
# with a bare `:hover`: a long forward or a return arrow is drawn as two or
# three separate edges routed through invisible waypoints, and hovering one
# segment has to raise all of them. Each segment carries a `workflow-edge--gN`
# group class, and these rules pair each group with a `:has()` test on the
# canvas, so the whole group brightens while every other line fades back.
def _workflow_edge_hover_styles() -> dict[str, dict[str, str]]:
    styles: dict[str, dict[str, str]] = {}
    for index in range(WORKFLOW_HIGHLIGHT_GROUPS):
        group = f".workflow-edge--g{index}"
        hovered = f".react-flow:has({group}:hover)"
        styles[f"{hovered} .react-flow__edge:not({group})"] = {
            "opacity": "0.13",
        }
        styles[f"{hovered} {group} .react-flow__edge-path"] = {
            "stroke_width": "4 !important",
            "stroke_dasharray": "10 8 !important",
            "animation": "codee-edge-flow 0.6s linear infinite",
            "filter": "drop-shadow(0 0 6px currentColor)",
        }
    return styles


app = rx.App(
    style={
        "button:not(:disabled), [role='button']:not([aria-disabled='true'])": {
            "cursor": "pointer",
        },
        # Expanding halo behind the live dot on in-flight runs.
        "@keyframes codee-ping": {
            "0%": {"transform": "scale(1)", "opacity": "0.55"},
            "70%": {"transform": "scale(2.6)", "opacity": "0"},
            "100%": {"transform": "scale(2.6)", "opacity": "0"},
        },
        "@keyframes codee-breathe": {
            "0%, 100%": {"opacity": "1"},
            "50%": {"opacity": "0.55"},
        },
        "@media (prefers-reduced-motion: reduce)": {
            ".codee-live-dot > *, .codee-breathe": {
                "animation": "none !important",
            },
            # The hovered transition still thickens and the rest still fade;
            # only the marching dashes stop.
            ".react-flow__edge .react-flow__edge-path": {
                "animation": "none !important",
            },
        },
        "button:disabled, [role='button'][aria-disabled='true']": {
            "cursor": "not-allowed",
        },
        ".rt-TextFieldRoot": {
            "background": "var(--codee-surface) !important",
            "color": "var(--codee-text) !important",
            "box_shadow": "inset 0 0 0 1px var(--codee-border)",
        },
        ".rt-TextFieldInput": {"color": "var(--codee-text) !important"},
        ".rt-TextAreaRoot": {
            "background": "var(--codee-surface) !important",
            "color": "var(--codee-text) !important",
        },
        ".rt-SelectTrigger": {
            "background": "var(--codee-surface) !important",
            "color": "var(--codee-text) !important",
            "box_shadow": "inset 0 0 0 1px var(--codee-border)",
        },
        ".workflow-node": {
            "background": "var(--codee-surface)",
            "border": "1px solid var(--codee-border)",
            "border_radius": "6px",
            "color": "var(--codee-text)",
            # A column so the model line can sit under the status name. The
            # status name is an anonymous flex item the node type renders, and
            # the handles are absolutely positioned, so neither is disturbed.
            "display": "flex",
            "flex_direction": "column",
            "font_family": "IBM Plex Sans, sans-serif",
            "font_weight": "600",
            "min_width": "220px",
            "padding": "0.85rem 1rem",
        },
        ".workflow-node--disconnected": {
            "background": "var(--codee-warning-background)",
            "border": "2px solid var(--codee-warning-border)",
        },
        # No `position` here: React Flow places nodes with `position: absolute`
        # and a bare `transform`, so overriding it drops the node into normal
        # flow and shifts every sibling's static position.
        ".workflow-node--human": {
            "background": "var(--codee-human-background)",
            "border": "2px solid var(--codee-human-border)",
            "cursor": "help",
        },
        # The sentence comes from the node's own `--codee-human-action`; the
        # fallback covers a graph generated before the agent was asked for one.
        ".workflow-node--human::after": _workflow_node_tooltip(
            "var(--codee-human-action, 'A person moves this status "
            "forward: no issue-trigger skill handles it.')",
            "var(--codee-human-border)",
        ),
        ".workflow-node--human:hover::after": {"opacity": "1"},
        # A status an issue-trigger skill picks up is worked by an agent, and
        # which agent and model that is only shows on the node itself.
        ".workflow-node--agent": {"cursor": "help"},
        # The model working the status, under its name. `::after` is spoken for
        # by the tooltip, so this is `::before` ordered past the status name,
        # which as an anonymous flex item keeps the default order of 0.
        ".workflow-node--agent::before": {
            "content": "var(--codee-node-model, '')",
            "order": "1",
            "color": "var(--codee-muted)",
            "font_size": "0.75rem",
            "font_weight": "400",
            "line_height": "1.3",
            "margin_top": "0.15rem",
        },
        # Several lines, so the rule keeps the `\A` breaks the node's
        # `--codee-agent-run` carries.
        ".workflow-node--agent::after": _workflow_node_tooltip(
            "var(--codee-agent-run, 'An AI agent works this status.')",
            "var(--codee-border)",
            white_space="pre-line",
        ),
        ".workflow-node--agent:hover::after": {"opacity": "1"},
        ".react-flow__edge.workflow-edge": {
            "cursor": "pointer",
        },
        # Marching dashes along the hovered transition: with several arrows
        # crossing the same stretch of canvas, the direction of travel is the
        # thing a still line cannot show.
        "@keyframes codee-edge-flow": {
            "from": {"stroke_dashoffset": "18"},
            "to": {"stroke_dashoffset": "0"},
        },
        **_workflow_edge_hover_styles(),
        # A person's arrow names no skill, so clicking it opens nothing: only
        # the hover tooltip has anything to say about it.
        ".react-flow__edge.workflow-edge--human": {
            "cursor": "help",
        },
        ".workflow-route-node .react-flow__handle": {
            "border": "0",
            "border_radius": "0",
            "height": "2px",
            "left": "50%",
            "min_height": "2px",
            "min_width": "6px",
            "right": "auto",
            "transform": "translate(-50%, -50%)",
            "width": "6px",
        },
        ".workflow-route-node--forward .react-flow__handle": {
            "background": "#2f6fd6",
        },
        ".workflow-route-node--return .react-flow__handle": {
            "background": "#e26128",
        },
    },
    stylesheets=[
        "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap"
    ],
    head_components=[
        rx.el.link(rel="icon", type="image/svg+xml", href=FAVICON),
    ],
    # Serves the OAuth callback route on the same origin as the UI; Reflex
    # mounts itself underneath, so every other path still reaches the pages.
    api_transformer=api_app,
)
app.add_page(dashboard_page, route="/", title="Dashboard | Codee",
             on_load=AdminState.load_dashboard_page)
app.add_page(skills_page, route="/skills", title="Skills | Codee",
             on_load=[AdminState.load_skills, AdminState.load_agent_models])
app.add_page(workflow_page, route="/workflow", title="Workflow | Codee",
             on_load=AdminState.load_workflow)
app.add_page(memory_page, route="/memory", title="Memory | Codee",
             on_load=AdminState.load_memories)
app.add_page(repositories_page, route="/repositories",
             title="Repositories | Codee",
             on_load=AdminState.load_repositories)
app.add_page(runs_page, route="/runs", title="Runs | Codee",
             on_load=AdminState.load_runs)
app.add_page(sessions_page, route="/sessions",
             title="Sessions | Codee", on_load=AdminState.load_settings)
app.add_page(settings_page, route="/settings",
             title="Settings | Codee",
             on_load=AdminState.load_settings_page)
