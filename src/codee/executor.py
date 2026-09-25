import json
import os
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

from codee_agent_abstract.provider import AbstractCodingAgent
from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_agent_codex.provider import CodexAgent
from codee_agent_github_copilot.provider import (
    COPILOT_DEBUG_ENV_VAR, GitHubCopilotAgent)
from codee_agent_opencode.provider import OpenCodeAgent
from codee_main_context.context import (
    CodeeMainContext, CodingAgent, Settings, codee_issue_types, data_dir,
    load_settings, project_root)
from codee_main_context.logging import DEBUG_ENV_VAR, configure_logging, get_logger
from codee_tasks_abstract.provider import AbstractTasksProvider

from codee.coding_agents import resolve_agent_code
from codee.lib import claude_key_rotation, runs_db
from codee.lib.runtime_control import is_paused
from codee.lib.trigger_aws_sqs_skills import trigger_aws_sqs_skills
from codee.lib.trigger_cron_skills import trigger_cron_skills
from codee.lib.trigger_email_skills import trigger_email_skills
from codee.lib.trigger_issue_skills import (
    find_issue_triggered_skills, issue_statuses, match_issue_skill)
from codee.tasks_providers import build_tasks_provider

log = get_logger(__name__)

context = CodeeMainContext(data_dir=data_dir())
context.settings = load_settings(context.data_dir)

# Concrete coding agents, keyed by the agent a skill names or Settings
# defaults to. Each agent initializes itself from the settings, so nothing here
# is agent-specific.
_CODING_AGENTS: dict[CodingAgent, type[AbstractCodingAgent]] = {
    CodingAgent.CLAUDE_CODE: ClaudeCodeAgent,
    CodingAgent.GITHUB_COPILOT: GitHubCopilotAgent,
    CodingAgent.CODEX: CodexAgent,
    CodingAgent.OPENCODE: OpenCodeAgent,
}

POLL_INTERVAL = 60  # 1 minute

SESSIONS_FILE = context.data_dir / "sessions.json"
# The project Codee operates on — same root the trigger modules scan for
# `.claude/skills`. The coding agent is spawned with this as its cwd, so the
# invocations we build from those skills — a `/<slug>` command, or a path to the
# skill file for agents that don't resolve one — actually resolve.
REPO_ROOT = project_root()

# Task agents run concurrently, one thread each, so one long agent (up to 2h)
# doesn't block the others. The cap comes from the "Max parallel tasks" admin
# setting and is enforced at launch rather than by a fixed-size pool, so an edit
# applies from the next poll on — see _max_parallel_agents().
# task_ids a worker currently owns. Claimed on the main thread at launch,
# released by the worker — so the next poll never launches a second agent for a
# task that's still in an "In Progress"/"CR Needed" state.
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def _max_parallel_agents() -> int:
    """How many task agents may run at once, per the settings this poll read.

    Read fresh every time instead of being captured at import, so changing "Max
    parallel tasks" in Settings takes effect on the next poll without a restart.
    Agents already running are never interrupted by a lowered cap; the executor
    simply launches nothing new until it is back under the limit.
    """
    return max(1, context.settings.max_parallel_agents)


def _log_debug_environment() -> None:
    if os.environ.get(COPILOT_DEBUG_ENV_VAR, "").strip().lower() == "true":
        log.info("COPILOT_DEBUG=true (Copilot CLI debug logs enabled)")
    if os.environ.get(DEBUG_ENV_VAR, "").strip() == "1":
        log.info("CODEE_DEBUG=1 (Codee debug logging enabled)")


def _load_sessions() -> dict[str, str]:
    """Load task_id -> session_id mapping from disk."""
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_sessions(sessions: dict[str, str]) -> None:
    """Persist task_id -> session_id mapping to disk."""
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


def _get_or_create_session(sessions: dict[str, str], task_id: str) -> str:
    """Get existing session ID for a task or create a new one."""
    if task_id not in sessions:
        sessions[task_id] = str(uuid.uuid4())
        _save_sessions(sessions)
    return sessions[task_id]


def _build_coding_agent(settings: Settings,
                        agent: CodingAgent | None = None) -> AbstractCodingAgent:
    """One agent instance, defaulting to the one Settings selects.

    ``agent`` is what a skill asked for in ``x-codee-agent``. Building is cheap
    — an agent holds its settings and its working directory and spawns its CLI
    per run — so a skill that names one gets a fresh instance rather than the
    long-lived default.
    """
    implementation = _CODING_AGENTS.get(agent or settings.coding_agent)
    if implementation is None:
        raise ValueError(
            f"unsupported coding agent: {(agent or settings.coding_agent).value}")
    return implementation(settings, REPO_ROOT)


def _agent_for_skill(agent_code: str) -> AbstractCodingAgent:
    """The agent a skill's ``x-codee-agent`` names, or the configured default.

    A skill that names no agent is run by the default one Settings selects —
    the instance the executor already holds. So is a skill that names an agent
    this build cannot run: the name is reported once per run and the work still
    gets done, which beats failing every poll over a typo in frontmatter.
    """
    if not agent_code:
        return coding_agent
    agent = resolve_agent_code(agent_code)
    if agent is None:
        log.warning("x-codee-agent: %r names no agent Codee can run; using the "
                    "default agent (%s).", agent_code,
                    context.settings.coding_agent.value)
        return coding_agent
    implementation = _CODING_AGENTS.get(agent)
    if implementation is not None and isinstance(coding_agent, implementation):
        # It named the default agent, so the instance we already hold is it.
        return coding_agent
    return _build_coding_agent(context.settings, agent)


tasks_provider: AbstractTasksProvider = build_tasks_provider(context.settings)

# The default agent, kept for the life of the process. A skill that names its
# own through ``x-codee-agent`` is run by one built for that run instead.
coding_agent: AbstractCodingAgent = _build_coding_agent(context.settings)


def _refresh_config() -> None:
    """Re-read settings.json and rebuild whatever it changed.

    Providers capture their credentials at construction, so without this a
    settings edit (new Azure DevOps app, rotated JIRA token, switched provider,
    remapped work item, edited task filter) only took effect after restarting
    the executor.
    Rebuilds are conditional so a poll that changes nothing keeps the live
    provider — and with it the Azure DevOps refresh lock — untouched.
    """
    global tasks_provider, coding_agent

    settings = load_settings(context.data_dir)
    previous = context.settings
    context.settings = settings
    log.debug("re-read settings from %s: provider=%s agent=%s",
              context.data_dir, settings.tasks_provider.value,
              settings.coding_agent.value)

    if (settings.tasks_provider != previous.tasks_provider
            or settings.credentials != previous.credentials
            or settings.work_item_types != previous.work_item_types
            or settings.work_item_queries != previous.work_item_queries
            or settings.task_filters != previous.task_filters):
        try:
            tasks_provider = build_tasks_provider(settings)
        except Exception as exc:
            # Keep polling with the provider we have; the next edit gets another go.
            log.error("Failed to apply new tasks provider settings: %s", exc)
        else:
            log.info("Reloaded tasks provider: %s", tasks_provider.describe())

    coding_agent_changed = settings.coding_agent != previous.coding_agent
    copilot_settings_changed = (
        settings.coding_agent == CodingAgent.GITHUB_COPILOT
        and settings.github_copilot_prompt_prefix
        != previous.github_copilot_prompt_prefix
    )
    if coding_agent_changed or copilot_settings_changed:
        try:
            coding_agent = _build_coding_agent(settings)
        except Exception as exc:
            log.error("Failed to apply new coding agent settings: %s", exc)
        else:
            log.info("Reloaded default coding agent: %s",
                     settings.coding_agent.value)

    if settings.max_parallel_agents != previous.max_parallel_agents:
        # Nothing to rebuild: the cap is read per launch. Agents already running
        # under the old cap finish; this tick just launches by the new one.
        log.info("Max parallel tasks changed from %s to %s; applied from this "
                 "poll on.", previous.max_parallel_agents,
                 settings.max_parallel_agents)


def _current_branch() -> str | None:
    """Return the current git branch name, or None if it can't be determined."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_ROOT,
        )
    except Exception as exc:
        log.warning("failed to determine current branch: %s", exc)
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _pull_latest_code() -> bool:
    """Update the local repo before polling for tasks.

    Only pulls when the current branch is the mainline (master or main); on any
    other branch it's a no-op so we don't disturb in-progress work.
    """
    branch = _current_branch()
    if branch not in ("master", "main"):
        log.debug("not on mainline branch (on '%s'), skipping git pull", branch)
        return True

    try:
        result = subprocess.run(
            ["git", "pull", "origin", branch],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired:
        log.error("git pull timed out after 5 minutes")
        return False
    except Exception as exc:
        log.error("git pull failed: %s", exc)
        return False

    if result.returncode != 0:
        error_output = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        log.error("git pull failed: %s", error_output)
        return False

    output = result.stdout.strip()
    if output:
        log.debug("git pull output: %s", output)
    else:
        log.debug("git pull completed")
    return True


def _run_agent(user_message: str, session_id: str, model: str = "",
               agent_code: str = "", label: str = "") -> str:
    """Run the skill's coding agent and return its response text.

    ``model`` comes from the triggering skill's ``model:`` frontmatter; agents
    that can't be told which model to use ignore it. ``agent_code`` comes from
    its ``x-codee-agent:`` frontmatter and picks which agent runs at all, empty
    for the default one. Wraps the agent run in job tracking; the agent itself
    raises on any failure so callers can retry.

    ``label`` is how the run should read on the dashboard when that differs
    from the prompt — an issue run is always shown as ``/<slug> <task id>``,
    whatever wording the agent needed. Empty means the prompt is the label.
    """
    agent = _agent_for_skill(agent_code)
    job_id = runs_db.start_job(session_id, label or user_message,
                               agent=agent.DISPLAY_NAME,
                               model=model, main_context=context)
    log.debug("job %s started: session=%s message=%r model=%r agent=%s",
              job_id, session_id, user_message, model, agent.describe())

    def opened(agent_session_id: str) -> None:
        """Record the session the agent actually opened, if it isn't ours.

        Claude Code and Copilot run under the id they were handed and this
        changes nothing. Codex names its own, and both the live dashboard row
        and the run this ends up writing should carry that name instead.
        """
        if agent_session_id == session_id:
            return
        log.debug("job %s runs under agent session %s",
                  job_id, agent_session_id)
        runs_db.note_agent_session(session_id, agent_session_id)
        runs_db.set_job_session(job_id, agent_session_id, main_context=context)

    try:
        return agent.run(user_message, session_id, model, opened)
    finally:
        log.debug("job %s finished", job_id)
        try:
            runs_db.finish_job(job_id, main_context=context)
        except Exception as exc:  # ponytail: clearing the in-flight row must never
            # replace the agent's outcome (FR-009). Raising here would discard a
            # completed run's reply and leave the cron slot un-advanced, so the
            # trigger re-runs the finished job every tick for the catch-up window.
            log.warning("failed to clear in-flight job %s: %s", job_id, exc)


def _run_task(task_id: str, message: str, session_id: str, skill_name: str,
              model: str = "", agent_code: str = "", label: str = "") -> None:
    """Pool worker: run one task's coding agent, then release its in-flight slot.

    Logs the outcome to the runs table like the cron/email/sqs triggers do, so
    issue-triggered coding runs show up on the dashboard too. Stamped with the
    launch time (not the finish time) so the hourly chart buckets it where it
    actually started — an agent can run for hours.

    ``label`` is what the dashboard and the run log show instead of ``message``,
    so every issue run reads as its ``/<slug> <task id>`` command no matter how
    the agent had to be asked. The prompt itself is in the debug log.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    shown = label or message
    try:
        response = _run_agent(message, session_id, model, agent_code, label)
        log.info("Agent response for %s (%d chars): %s",
                 task_id, len(response), response)
        runs_db.record_run(skill_name, "issue", session_id, "succeeded",
                           started_at=started_at, message=shown,
                           user_message=message, response=response,
                           main_context=context)
    except Exception as exc:
        # Over-limit / transient failure: leave the task in its current
        # status so the next poll retries it.
        log.warning("Failed to run %s, will retry next poll: %s", task_id, exc)
        log.debug("%s failed with:\n%s", task_id, traceback.format_exc())
        runs_db.record_run(skill_name, "issue", session_id, "failed",
                           error=str(exc)[:500], started_at=started_at,
                           message=shown, user_message=message,
                           main_context=context)
    finally:
        with _inflight_lock:
            _inflight.discard(task_id)


def _submit_task(task_id: str, message: str, session_id: str, skill_name: str,
                 model: str = "", agent_code: str = "", label: str = "") -> bool:
    """Start a task's agent unless one is already in flight or we're at the cap.

    Returns True if launched, False if skipped — as a duplicate, or because
    "Max parallel tasks" is already reached, in which case the task keeps its
    status and the next poll picks it up. Only the main (polling) thread adds to
    _inflight and only workers remove, so claiming the slot here is race-free
    against the next tick.
    """
    with _inflight_lock:
        if is_paused(context):
            log.debug("New agent work was paused before %s could start.", task_id)
            return False
        if task_id in _inflight:
            log.debug("%s already running; skipping duplicate launch.", task_id)
            return False
        limit = _max_parallel_agents()
        if len(_inflight) >= limit:
            log.debug("%d agent(s) running, at the cap of %d; %s waits for the "
                      "next poll.", len(_inflight), limit, task_id)
            return False
        _inflight.add(task_id)
        depth = len(_inflight)
    # Non-daemon on purpose: a shutdown waits for a running agent rather than
    # killing it mid-run, which is what the thread pool used to give us.
    threading.Thread(target=_run_task, name=f"task-agent-{task_id}",
                     args=(task_id, message, session_id, skill_name, model,
                           agent_code, label)).start()
    log.info("Started an agent for %s (%d running, max %d).",
             task_id, depth, limit)
    return True


def run_once() -> None:
    """Single cron tick: reconcile scheduled skills, fetch tasks, and run Claude."""
    log.debug("tick: reconciling scheduled skills and polling for tasks")
    _refresh_config()

    if not _pull_latest_code():
        log.warning("Failed to pull from the repo, still continuing...")

    if is_paused(context):
        log.info("New agent work is paused; active agents continue running.")
        return

    trigger_cron_skills(_run_agent, main_context=context)
    trigger_aws_sqs_skills(_run_agent, main_context=context)
    trigger_email_skills(_run_agent, main_context=context)

    if not tasks_provider.is_configured():
        log.debug("Tasks provider is not configured; skipping poll.")
        return

    log.debug("Checking %s for tasks...", tasks_provider.describe())

    with _inflight_lock:
        running = len(_inflight)
    log.debug("Agents: %d/%d running.", running, _max_parallel_agents())

    # The work items come from the settings this tick already re-read, rather
    # than from a second read of the same file inside the loader.
    issue_skills = find_issue_triggered_skills(
        issue_types=codee_issue_types(context.settings))
    if not issue_skills:
        log.debug("No issue-triggered skills found.")
        return
    log.debug("Issue-triggered skills: %s",
              ", ".join(skill.slug for skill in issue_skills))

    tasks = tasks_provider.get_tasks(issue_statuses(issue_skills))
    if not tasks:
        log.debug("No tasks found.")
        return

    log.info("Found %d task(s).", len(tasks))
    sessions = _load_sessions()

    for task in tasks:
        task_id = task.key
        with _inflight_lock:
            running, limit = len(_inflight), _max_parallel_agents()
            if running >= limit:
                # Full for this tick — the rest keep their status and get
                # another go at the next poll, under whatever the cap is then.
                log.info("%d agent(s) running, at the cap of %d; the remaining "
                         "task(s) wait for the next poll.", running, limit)
                break
            if task_id in _inflight:
                continue  # a worker already owns it; don't re-fetch or re-launch
        summary = task.summary
        status = task.status
        issue_type = task.issue_type
        priority = task.priority

        # always create a new session
        session_id = str(uuid.uuid4())

        log.info("Incoming %s (%s, %s, %s): %s",
                 task_id, status, issue_type, priority, summary)

        # An item whose parent is one Codee polls in its own right is left
        # alone: the parent's own run is what decides what its children need,
        # and picking the child up here as well is two agents on one change.
        # Its type is what says so — a work item selected by a query of the
        # user's own shields nothing, since nothing in a parent says whether
        # that condition would have claimed it.
        if task.is_parent_codee_work_item:
            log.debug("Skipping %s: its parent %s is a %s, which Codee polls "
                      "in its own right",
                      task_id, task.parent.key, task.parent.work_item_type)
            continue

        skill = match_issue_skill(issue_skills, status, issue_type)
        if skill is None:
            log.debug("No issue trigger matches %s (%s, %s); skipping",
                      task_id, status, issue_type)
            continue
        # How a skill is invoked is the agent's business: Claude Code resolves
        # the slash command out of `.claude/skills`, Copilot has to be pointed
        # at the file. The command stays the label either way, so one issue run
        # reads the same on the dashboard whichever agent picked it up.
        label = f"/{skill.slug} {task_id}"
        message = _agent_for_skill(skill.agent).skill_prompt(
            skill.slug, skill.path, task_id, skill.argument_name)

        log.info("Processing %s (%s, %s): %s  session-id=%s",
                 task_id, status, issue_type, summary, session_id)

        _submit_task(task_id, message, session_id, skill.name,
                     skill.model, skill.agent, label)


def main() -> None:
    # Entry point: install the handler before anything logs. Level comes from
    # CODEE_DEBUG, which `codee-start --debug` exports for this subprocess.
    configure_logging()
    _log_debug_environment()

    if not tasks_provider.is_configured():
        log.warning("tasks provider is not configured; task polling stays "
                    "idle until it is set up in Settings (no restart needed)")

    runs_db.clear_active_jobs(context)  # purge rows left by a previous process
    # Before the first tick: the thread's own first check puts the configured
    # key into Claude Code's credentials file, so an agent launched by that
    # tick already runs on the key Codee thinks it is running on.
    claude_key_rotation.start(context)

    log.info("Starting the main loop (ticking every %ss)...", POLL_INTERVAL)
    log.info("Tasks provider: %s", tasks_provider.describe())
    log.debug("data dir=%s repo root=%s max parallel agents=%d",
              context.data_dir, REPO_ROOT, _max_parallel_agents())

    while True:
        try:
            run_once()
        except Exception as exc:
            log.error("Unhandled error: %s %s", exc, traceback.format_exc())
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
