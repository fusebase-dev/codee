"""Reflex UI for managing Codee."""
from __future__ import annotations

import asyncio
from typing import Any

import reflex as rx
from pydantic import BaseModel

from codee.admin_api import api_app
from codee.admin_service import AGENTS_FILE, AdminService, ISSUE_TYPES, SKILL_TYPES
from codee.workflow_graph import workflow_graph

SERVICE = AdminService()

RUNS_PAGE_SIZE = 20


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
    issue_status: str = ""
    issue_type: str = ""


class ModelOption(BaseModel):
    """One entry in the skill editor's model picker: code plus friendly name."""

    id: str
    name: str


class MemoryEntry(BaseModel):
    title: str
    file: str
    hook: str
    lineno: int
    raw: str
    matched: bool


class ActiveJob(BaseModel):
    message: str
    elapsed_label: str
    viewer_url: str


class RunRecord(BaseModel):
    skill_name: str
    trigger_type: str
    status: str
    error: str
    started_at: str
    message: str
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

    active_jobs: list[ActiveJob] = []
    total_runs: int = 0
    last_24h_runs: int = 0
    hourly_runs: list[dict[str, Any]] = []
    dashboard_polling: bool = False

    runs: list[RunRecord] = []
    runs_has_more: bool = False
    runs_loading: bool = False
    session_viewer: str = SERVICE.session_viewer

    story_workflow_nodes: list[dict[str, Any]] = []
    story_workflow_edges: list[dict[str, Any]] = []
    story_workflow_warnings: list[str] = []
    task_workflow_nodes: list[dict[str, Any]] = []
    task_workflow_edges: list[dict[str, Any]] = []
    task_workflow_warnings: list[str] = []
    workflow_error: str = ""
    workflow_loading: bool = False
    edge_menu_skills: list[str] = []
    edge_menu_left: str = "0px"
    edge_menu_top: str = "0px"

    tasks_provider: str = "jira"
    coding_agent: str = "claude_code"
    max_parallel_agents: str = "3"
    jira_base_url: str = ""
    jira_account_email: str = ""
    jira_api_token: str = ""
    jira_project: str = ""
    azure_organization_url: str = ""
    azure_project: str = ""
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_connected: bool = False
    azure_account: str = ""
    azure_expires_label: str = ""
    azure_redirect_uri: str = ""

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

    def set_model_query(self, value: str) -> None:
        self.model_query = value

    def choose_model(self, model_id: str) -> None:
        """Pick a model from the list, or use whatever the user typed."""
        self.skill_model = model_id.strip()
        self.model_query = ""

    @rx.event(background=True)
    async def load_agent_models(self) -> None:
        """Fetch the configured agent's catalog off the event loop.

        Asking an agent can mean spawning its CLI, so this runs in the
        background while the skill list renders; the picker still accepts a
        hand-typed model code if the list never arrives.
        """
        async with self:
            if self.models_loading:
                return
            self.models_loading = True
        try:
            models = await asyncio.to_thread(SERVICE.list_agent_models)
        except Exception:
            models = []
        async with self:
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
        self.skills = [SkillSummary(**skill)
                       for skill in SERVICE.list_skills()]

    def create_skill(self) -> Any:
        saved, pushed, message, slug = SERVICE.create_skill(
            self.new_skill_name)
        if saved:
            self.new_skill_name = ""
            self.load_skills()
            self.edit_skill(slug)
        return _save_toast(saved, pushed, message)

    def edit_skill(self, slug: str) -> None:
        skill = SERVICE.load_skill(slug)
        self.editing_agents = False
        self.selected_skill = skill["slug"]
        self.skill_name = skill["name"]
        self.skill_description = skill["description"]
        self.skill_model = skill["model"]
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

    def close_skill(self) -> None:
        self.selected_skill = ""

    def save_skill(self) -> Any:
        saved, pushed, message, slug = SERVICE.save_skill({
            "slug": self.selected_skill,
            "name": self.skill_name,
            "description": self.skill_description,
            "model": self.skill_model,
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

    def set_agents_content(self, value: str) -> None:
        self.agents_content = value

    def close_agents(self) -> None:
        self.editing_agents = False

    def save_agents(self) -> Any:
        return _save_toast(*SERVICE.save_agents(self.agents_content))

    def load_memories(self) -> None:
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

    def _refresh_dashboard(self) -> None:
        dashboard = SERVICE.dashboard()
        self.active_jobs = [
            ActiveJob(
                message=(job.get("message") or "(no prompt)")[:140],
                elapsed_label=job["elapsed_label"],
                viewer_url=(SERVICE.session_viewer.format(session_id=job["session_id"])
                            if SERVICE.session_viewer and job.get("session_id") else ""),
            )
            for job in dashboard["active"]
        ]
        self.total_runs = dashboard["counts"]["total"]
        self.last_24h_runs = dashboard["counts"]["last_24h"]
        self.hourly_runs = dashboard["hourly"]

    @rx.event(background=True)
    async def poll_dashboard(self) -> None:
        async with self:
            if self.dashboard_polling:
                return
            self.dashboard_polling = True
        while True:
            async with self:
                self._refresh_dashboard()
            await asyncio.sleep(1)

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
                message=message,
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
        async with self:
            if self.workflow_loading:
                return
            self.workflow_loading = True
            self.workflow_error = ""
            self.edge_menu_skills = []
        try:
            workflow = await asyncio.to_thread(
                SERVICE.generate_workflow, force)
        except Exception as error:
            async with self:
                self.workflow_error = str(error)
                self.story_workflow_nodes = []
                self.story_workflow_edges = []
                self.story_workflow_warnings = []
                self.task_workflow_nodes = []
                self.task_workflow_edges = []
                self.task_workflow_warnings = []
                self.workflow_loading = False
            return
        async with self:
            self.story_workflow_nodes = workflow["story"]["nodes"]
            self.story_workflow_edges = workflow["story"]["edges"]
            self.story_workflow_warnings = workflow["story"]["warnings"]
            self.task_workflow_nodes = workflow["task"]["nodes"]
            self.task_workflow_edges = workflow["task"]["edges"]
            self.task_workflow_warnings = workflow["task"]["warnings"]
            self.workflow_loading = False

    def open_edge_menu(self, skills: list[str], x: float, y: float) -> None:
        self.edge_menu_skills = skills
        self.edge_menu_left = f"{round(x)}px"
        self.edge_menu_top = f"{round(y)}px"

    def close_edge_menu(self) -> None:
        self.edge_menu_skills = []

    def edit_workflow_skill(self, label: str) -> Any:
        self.edge_menu_skills = []
        slug = SERVICE.resolve_skill_slug(label)
        if not slug:
            return rx.toast.error(f"No skill found for transition '{label}'")
        self.load_skills()
        self.edit_skill(slug)
        return rx.redirect("/skills")

    def load_settings(self) -> Any:
        settings = SERVICE.load_settings()
        self.tasks_provider = settings.tasks_provider.value
        self.coding_agent = settings.coding_agent.value
        self.max_parallel_agents = str(settings.max_parallel_agents)
        jira = settings.credentials.get("jira", {})
        azure = settings.credentials.get("azure_devops", {})
        self.jira_base_url = jira.get("base_url", "")
        self.jira_account_email = jira.get("account_email", "")
        self.jira_api_token = jira.get("api_token", "")
        self.jira_project = jira.get("project", "")
        self.azure_organization_url = azure.get("organization_url", "")
        self.azure_project = azure.get("project", "")
        self.azure_tenant_id = azure.get("tenant_id", "")
        self.azure_client_id = azure.get("client_id", "")
        self.azure_client_secret = azure.get("client_secret", "")
        self.load_azure_connection()
        return self._azure_callback_toast()

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

    def set_tasks_provider(self, value: str) -> None:
        self.tasks_provider = value

    def set_coding_agent(self, value: str) -> None:
        self.coding_agent = value

    def set_max_parallel_agents(self, value: str) -> None:
        self.max_parallel_agents = value

    def set_jira_base_url(self, value: str) -> None:
        self.jira_base_url = value

    def set_jira_account_email(self, value: str) -> None:
        self.jira_account_email = value

    def set_jira_api_token(self, value: str) -> None:
        self.jira_api_token = value

    def set_jira_project(self, value: str) -> None:
        self.jira_project = value

    def set_azure_organization_url(self, value: str) -> None:
        self.azure_organization_url = value

    def set_azure_project(self, value: str) -> None:
        self.azure_project = value

    def set_azure_tenant_id(self, value: str) -> None:
        self.azure_tenant_id = value

    def set_azure_client_id(self, value: str) -> None:
        self.azure_client_id = value

    def set_azure_client_secret(self, value: str) -> None:
        self.azure_client_secret = value

    @rx.var
    def azure_can_connect(self) -> bool:
        """Everything the authorization request and the later queries need."""
        return all(value.strip() for value in (
            self.azure_organization_url, self.azure_project,
            self.azure_client_id, self.azure_client_secret))

    def connect_azure_devops(self) -> Any:
        """Save the app registration, then hand the browser to Entra ID for consent.

        Saving first is what makes the callback work: it arrives as its own HTTP
        request and reads the client secret back off disk to exchange the code.
        """
        if not self.azure_can_connect:
            return rx.toast.error(
                "Fill in organization URL, project, client ID and client secret first.")
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
        self.load_azure_connection()
        return rx.toast.success("Disconnected from Azure DevOps")

    def _persist_settings(self) -> str:
        """Write the settings to disk. Returns an error message, or '' when saved."""
        try:
            parallel_agents = int(self.max_parallel_agents)
        except ValueError:
            return "Max parallel tasks must be a number"
        credentials = (
            {
                "base_url": self.jira_base_url,
                "account_email": self.jira_account_email,
                "api_token": self.jira_api_token,
                "project": self.jira_project,
            }
            if self.tasks_provider == "jira"
            else {
                "organization_url": self.azure_organization_url,
                "project": self.azure_project,
                "tenant_id": self.azure_tenant_id,
                "client_id": self.azure_client_id,
                "client_secret": self.azure_client_secret,
            }
        )
        SERVICE.save_settings(
            self.tasks_provider,
            self.coding_agent,
            parallel_agents,
            credentials,
        )
        return ""

    def save_settings(self) -> Any:
        error = self._persist_settings()
        return rx.toast.error(error) if error else rx.toast.success("Settings saved")


ACCENT = "var(--codee-accent)"
BORDER = "1px solid var(--codee-border)"
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
                        nav_link("Runs", "history", "/runs"),
                        rx.cond(
                            AdminState.coding_agent == "claude_code",
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
            "--codee-accent": rx.color_mode_cond("#167d5a", "#4abd85"),
            "--codee-border": rx.color_mode_cond("#dfe4df", "#354039"),
            "--codee-muted": rx.color_mode_cond("#68716b", "#a1ada5"),
            "--codee-surface": rx.color_mode_cond("#ffffff", "#18201c"),
            "--codee-page-background": rx.color_mode_cond("#f3f5f3", "#101512"),
            "--codee-nav-background": rx.color_mode_cond("#f7f9f7", "#141b17"),
            "--codee-text": rx.color_mode_cond("#202721", "#edf3ef"),
            "--codee-hover": rx.color_mode_cond("#edf5f0", "#213029"),
            "--codee-active": rx.color_mode_cond("#e2efe9", "#23372e"),
            "--codee-grid": rx.color_mode_cond("#e4e9e5", "#2e3932"),
            "--codee-subtle-icon": rx.color_mode_cond("#91a097", "#718078"),
            "--codee-warning-background": rx.color_mode_cond("#fffbeb", "#332a13"),
            "--codee-warning-border": rx.color_mode_cond("#d97706", "#f59e0b"),
            # Tint reserved for in-flight work, so a live run reads at a glance.
            "--codee-running-background": rx.color_mode_cond("#effaf4", "#15271f"),
            "--codee-running-glow": rx.color_mode_cond(
                "rgba(22, 125, 90, 0.16)", "rgba(74, 189, 133, 0.18)"),
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
        rx.box(position="absolute", inset="0", border_radius="50%", background=ACCENT),
        class_name="codee-live-dot",
        position="relative",
        width=size,
        height=size,
        flex_shrink="0",
    )


def elapsed_pill(label: rx.Var | str) -> rx.Component:
    return rx.hstack(
        rx.icon("timer", size=14, color=ACCENT),
        rx.text(label, font_family=MONO, font_size="0.85rem", font_weight="500"),
        spacing="2",
        align="center",
        flex_shrink="0",
        padding="0.2rem 0.6rem",
        background=SURFACE,
        border=BORDER,
        border_radius="999px",
    )


def active_job_row(job: ActiveJob) -> rx.Component:
    return rx.flex(
        live_dot(),
        rx.text(job.message, font_weight="600", font_family=MONO, font_size="0.9rem",
                flex="1", min_width="0", overflow="hidden", text_overflow="ellipsis",
                white_space="nowrap", custom_attrs={"title": job.message}),
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


def dashboard_page() -> rx.Component:
    return shell(rx.vstack(
        page_header(
            "Dashboard", "Run activity and live coding-agent sessions."),
        rx.grid(
            rx.box(rx.text("Total runs", color=MUTED), rx.heading(AdminState.total_runs, size="8"),
                   padding="1.25rem", background=SURFACE, border=BORDER),
            rx.box(rx.text("Last 24 hours", color=MUTED), rx.heading(AdminState.last_24h_runs, size="8"),
                   padding="1.25rem", background=SURFACE, border=BORDER),
            running_tile(),
            columns=rx.breakpoints(initial="1", sm="2", lg="3"), gap="1rem", width="100%"),
        running_panel(),
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
                      rx.badge(skill.type, color_scheme="green", variant="soft"), width="100%"),
            rx.text(skill.description, color=MUTED, font_size="0.9rem", min_height="2.7rem",
                    overflow="hidden"),
            rx.cond(
                skill.issue_status != "",
                rx.hstack(
                    rx.badge(skill.issue_type, color_scheme="green",
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


def model_option_row(option: ModelOption) -> rx.Component:
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
            on_click=AdminState.choose_model(option.id)))


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
                    "Runs on whatever the coding agent defaults to.",
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
        model_picker(),
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
                        ISSUE_TYPES, value=AdminState.skill_issue_type,
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
                      rx.text("YAML lines written into the frontmatter as they are. "
                              "Leave out the fields that already have their own control above.",
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


def memory_page() -> rx.Component:
    listing = rx.cond(AdminState.memories.length() > 0,
                      rx.vstack(rx.foreach(AdminState.memories,
                                memory_row), spacing="3", width="100%"),
                      empty_state("notebook-text", "No memories yet."))
    return shell(rx.vstack(page_header("Memory", "Review and maintain durable project context."),
                           rx.cond(AdminState.selected_memory ==
                                   "", listing, memory_editor()),
                           align="start", width="100%"))


def run_row(run: RunRecord) -> rx.Component:
    return rx.box(
        rx.flex(
            rx.vstack(rx.hstack(rx.text(run.skill_name, font_weight="600"),
                                rx.badge(run.status, color_scheme=rx.cond(run.status == "succeeded", "green", "red"))),
                      rx.text(run.started_at, color=MUTED, font_size="0.8rem",
                              font_family="IBM Plex Mono, monospace"),
                      rx.text(run.preview, color=MUTED),
                      rx.cond(run.error != "", rx.text(
                          run.error, color="#b42318", font_size="0.85rem")),
                      spacing="2", align="start", flex="1"),
            rx.badge(run.trigger_type, variant="outline"),
            rx.cond(run.viewer_url != "", rx.link(rx.icon("external-link", size=16), href=run.viewer_url,
                                                  is_external=True, aria_label="View session", color=ACCENT)),
            gap="1rem", align="start", width="100%"),
        rx.cond(run.message != "", rx.accordion.root(rx.accordion.item(
            header="Full message", content=rx.text(run.message, white_space="pre-wrap"), value=run.started_at),
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
        color_scheme="amber",
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


def workflow_section(
    title: str,
    nodes: rx.Var,
    edges: rx.Var,
    warnings: rx.Var,
) -> rx.Component:
    return rx.vstack(
        rx.heading(title, size="5"),
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
                    on_pane_click=AdminState.close_edge_menu,
                ),
                spacing="4",
                width="100%",
            ),
            empty_state(
                "git-branch",
                f"No {title.lower()} issue-trigger skills found.",
            ),
        ),
        spacing="4",
        align="start",
        width="100%",
    )


def workflow_page() -> rx.Component:
    return shell(rx.vstack(
        rx.flex(
            page_header(
                "Workflow", "Story and task transitions inferred from issue-trigger skills."),
            rx.spacer(),
            rx.button(
                rx.icon("refresh-cw", size=16),
                "Regenerate",
                variant="outline",
                loading=AdminState.workflow_loading,
                on_click=AdminState.load_workflow(True),
            ),
            align="start",
            width="100%",
        ),
        rx.cond(
            AdminState.workflow_loading,
            rx.center(rx.spinner(size="3"), min_height="48rem", width="100%"),
            rx.cond(
                AdminState.workflow_error != "",
                rx.callout(
                    AdminState.workflow_error,
                    icon="triangle-alert",
                    color_scheme="red",
                    width="100%",
                ),
                rx.vstack(
                    workflow_section(
                        "Story workflow",
                        AdminState.story_workflow_nodes,
                        AdminState.story_workflow_edges,
                        AdminState.story_workflow_warnings,
                    ),
                    workflow_section(
                        "Task workflow",
                        AdminState.task_workflow_nodes,
                        AdminState.task_workflow_edges,
                        AdminState.task_workflow_warnings,
                    ),
                    spacing="8",
                    width="100%",
                ),
            ),
        ),
        workflow_edge_menu(),
        align="start",
        width="100%",
    ))


def sessions_page() -> rx.Component:
    return shell(rx.vstack(
        page_header(
            "Sessions", "Open the configured Claude Code session viewer."),
        rx.cond(AdminState.session_viewer != "",
                rx.link(rx.button(rx.icon("external-link", size=16), "Open session viewer"),
                        href=AdminState.session_viewer, is_external=True),
                empty_state("key-round", "Set CODEE_SESSION_VIEWER_URL to enable the session viewer.")),
        align="start", width="100%"))


def azure_step(number: int, title: str, detail: rx.Component | str) -> rx.Component:
    return rx.hstack(
        rx.box(str(number), color="white", background=ACCENT, min_width="1.4rem",
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
                           "access to the project."),
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
                                           width="100%")),
        field("Project", rx.input(value=AdminState.azure_project,
                                  on_change=AdminState.set_azure_project, width="100%")),
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


def settings_page() -> rx.Component:
    jira_fields = rx.vstack(
        field("Base URL", rx.input(value=AdminState.jira_base_url,
                                   on_change=AdminState.set_jira_base_url, width="100%")),
        field("Account email", rx.input(value=AdminState.jira_account_email,
                                        on_change=AdminState.set_jira_account_email, width="100%")),
        field("API token", rx.input(value=AdminState.jira_api_token,
                                    on_change=AdminState.set_jira_api_token, type="password", width="100%")),
        field("Project key", rx.input(value=AdminState.jira_project,
                                      on_change=AdminState.set_jira_project, width="100%")),
        spacing="4", width="100%")
    return shell(rx.vstack(
        page_header(
            "Settings", "Choose providers and control executor concurrency."),
        rx.box(
            rx.heading("Coding agent", size="4", margin_bottom="1rem"),
            rx.grid(
                field("Agent", rx.select(["claude_code", "github_copilot"], value=AdminState.coding_agent,
                                         on_change=AdminState.set_coding_agent, width="100%")),
                field("Max parallel tasks", rx.input(value=AdminState.max_parallel_agents,
                                                     on_change=AdminState.set_max_parallel_agents,
                                                     type="number", min=1, width="100%")),
                columns=rx.breakpoints(initial="1", md="2"), gap="1rem", width="100%"),
            padding="1.25rem", background=SURFACE, border=BORDER, width="100%"),
        rx.box(
            rx.heading("Tasks provider", size="4", margin_bottom="1rem"),
            field("Provider", rx.select(["jira", "azure_devops"], value=AdminState.tasks_provider,
                                        on_change=AdminState.set_tasks_provider, width="100%")),
            rx.box(rx.cond(AdminState.tasks_provider == "jira",
                   jira_fields, azure_fields()), margin_top="1rem"),
            padding="1.25rem", background=SURFACE, border=BORDER, width="100%"),
        rx.button(rx.icon("save", size=16), "Save settings",
                  on_click=AdminState.save_settings),
        spacing="5", align="start", width="100%"))


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
        ".workflow-node--unhandled": {
            "background": "var(--codee-warning-background)",
            "border": "2px dashed var(--codee-warning-border)",
        },
        ".workflow-node--unhandled::after": {
            "content": "'No issue trigger skill for this status'",
            "position": "absolute",
            "bottom": "calc(100% + 8px)",
            "left": "50%",
            "transform": "translateX(-50%)",
            "background": "var(--codee-surface)",
            "border": "1px solid var(--codee-warning-border)",
            "border_radius": "4px",
            "box_shadow": "0 8px 24px rgba(0, 0, 0, 0.28)",
            "color": "var(--codee-text)",
            "font_family": "IBM Plex Sans, sans-serif",
            "font_size": "0.75rem",
            "font_weight": "500",
            "padding": "0.35rem 0.55rem",
            "white_space": "nowrap",
            "opacity": "0",
            "pointer_events": "none",
            "transition": "opacity 0.12s ease",
            "z_index": "5",
        },
        ".workflow-node--unhandled:hover::after": {"opacity": "1"},
        ".react-flow__edge.workflow-edge": {
            "cursor": "pointer",
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
            "background": "#167d5a",
        },
        ".workflow-route-node--return .react-flow__handle": {
            "background": "#d97706",
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
             on_load=AdminState.poll_dashboard)
app.add_page(skills_page, route="/skills", title="Skills | Codee",
             on_load=[AdminState.load_skills, AdminState.load_agent_models])
app.add_page(workflow_page, route="/workflow", title="Workflow | Codee",
             on_load=AdminState.load_workflow)
app.add_page(memory_page, route="/memory", title="Memory | Codee",
             on_load=AdminState.load_memories)
app.add_page(runs_page, route="/runs", title="Runs | Codee",
             on_load=AdminState.load_runs)
app.add_page(sessions_page, route="/sessions",
             title="Sessions | Codee", on_load=AdminState.load_settings)
app.add_page(settings_page, route="/settings",
             title="Settings | Codee", on_load=AdminState.load_settings)
