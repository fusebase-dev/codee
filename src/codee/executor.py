import json
import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from codee_agent_abstract.provider import AbstractCodingAgent
from codee_agent_claude_code.provider import ClaudeCodeAgent
from codee_agent_github_copilot.provider import GitHubCopilotAgent
from codee_main_context.context import (
    CodeeMainContext, CodingAgent, Settings, data_dir,
    load_settings, project_root)
from codee_main_context.logging import configure_logging, get_logger
from codee_tasks_abstract.provider import AbstractTasksProvider

from codee.lib import runs_db
from codee.lib.trigger_aws_sqs_skills import trigger_aws_sqs_skills
from codee.lib.trigger_cron_skills import trigger_cron_skills
from codee.lib.trigger_email_skills import trigger_email_skills
from codee.lib.trigger_issue_skills import (
    find_issue_triggered_skills, issue_statuses, match_issue_skill)
from codee.tasks_providers import build_tasks_provider

log = get_logger(__name__)

context = CodeeMainContext(data_dir=data_dir())
context.settings = load_settings(context.data_dir)

# Concrete coding agents, keyed by the agent selected in settings. Each agent
# initializes itself from the settings, so nothing here is agent-specific.
_CODING_AGENTS: dict[CodingAgent, type[AbstractCodingAgent]] = {
    CodingAgent.CLAUDE_CODE: ClaudeCodeAgent,
    CodingAgent.GITHUB_COPILOT: GitHubCopilotAgent,
}

POLL_INTERVAL = 60  # 1 minute

SESSIONS_FILE = context.data_dir / "sessions.json"
# The project Codee operates on — same root the trigger modules scan for
# `.claude/skills`. The coding agent is spawned with this as its cwd, so the
# `/<slug>` messages we build from those skills actually resolve.
REPO_ROOT = project_root()

# Task agents run concurrently in a bounded pool so one long agent (up to 2h)
# doesn't block the others. Cap comes from the "Max parallel tasks" admin setting.
MAX_PARALLEL_AGENTS = max(1, context.settings.max_parallel_agents)
_agent_pool = ThreadPoolExecutor(
    max_workers=MAX_PARALLEL_AGENTS, thread_name_prefix="task-agent"
)
# task_ids a worker currently owns (running OR queued). Claimed on the main
# thread at submit, released by the worker — so the next poll never launches a
# second agent for a task that's still in an "In Progress"/"CR Needed" state.
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


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


def _build_coding_agent(settings: Settings) -> AbstractCodingAgent:
    agent = _CODING_AGENTS.get(settings.coding_agent)
    if agent is None:
        raise ValueError(
            f"unsupported coding agent: {settings.coding_agent.value}")
    return agent(settings, REPO_ROOT)


tasks_provider: AbstractTasksProvider = build_tasks_provider(context.settings)

coding_agent: AbstractCodingAgent = _build_coding_agent(context.settings)


def _refresh_config() -> None:
    """Re-read settings.json and rebuild whatever it changed.

    Providers capture their credentials at construction, so without this a
    settings edit (new Azure DevOps app, rotated JIRA token, switched provider)
    only took effect after restarting the executor. Rebuilds are conditional so
    a poll that changes nothing keeps the live provider — and with it the
    Azure DevOps refresh lock — untouched.
    """
    global tasks_provider, coding_agent

    settings = load_settings(context.data_dir)
    previous = context.settings
    context.settings = settings
    log.debug("re-read settings from %s: provider=%s agent=%s",
              context.data_dir, settings.tasks_provider.value,
              settings.coding_agent.value)

    if (settings.tasks_provider != previous.tasks_provider
            or settings.credentials != previous.credentials):
        try:
            tasks_provider = build_tasks_provider(settings)
        except Exception as exc:
            # Keep polling with the provider we have; the next edit gets another go.
            log.error("Failed to apply new tasks provider settings: %s", exc)
        else:
            log.info("Reloaded tasks provider: %s", tasks_provider.describe())

    if settings.coding_agent != previous.coding_agent:
        try:
            coding_agent = _build_coding_agent(settings)
        except Exception as exc:
            log.error("Failed to apply new coding agent settings: %s", exc)
        else:
            log.info("Reloaded coding agent: %s", settings.coding_agent.value)

    if settings.max_parallel_agents != previous.max_parallel_agents:
        # The pool is sized once and may have work in flight, so this one still
        # needs a restart rather than being silently ignored.
        log.warning("Max parallel tasks changed to %s; restart the executor to "
                    "apply (still running with %s).",
                    settings.max_parallel_agents, MAX_PARALLEL_AGENTS)


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


def _run_agent(user_message: str, session_id: str, model: str = "") -> str:
    """Run the configured coding agent and return its response text.

    ``model`` comes from the triggering skill's ``model:`` frontmatter; agents
    that can't be told which model to use ignore it. Wraps the agent run in job
    tracking; the agent itself raises on any failure so callers can retry.
    """
    job_id = runs_db.start_job(session_id, user_message, main_context=context)
    log.debug("job %s started: session=%s message=%r model=%r",
              job_id, session_id, user_message, model)
    try:
        return coding_agent.run(user_message, session_id, model)
    finally:
        log.debug("job %s finished", job_id)
        runs_db.finish_job(job_id, main_context=context)


def _run_task(task_id: str, message: str, session_id: str, skill_name: str,
              model: str = "") -> None:
    """Pool worker: run one task's coding agent, then release its in-flight slot.

    Logs the outcome to the runs table like the cron/email/sqs triggers do, so
    issue-triggered coding runs show up on the dashboard too. Stamped with the
    launch time (not the finish time) so the hourly chart buckets it where it
    actually started — an agent can run for hours.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        response = _run_agent(message, session_id, model)
        log.info("Agent response for %s (%d chars): %s",
                 task_id, len(response), response)
        runs_db.record_run(skill_name, "issue", session_id, "succeeded",
                           started_at=started_at, message=message,
                           main_context=context)
    except Exception as exc:
        # Over-limit / transient failure: leave the task in its current
        # status so the next poll retries it.
        log.warning("Failed to run %s, will retry next poll: %s", task_id, exc)
        log.debug("%s failed with:\n%s", task_id, traceback.format_exc())
        runs_db.record_run(skill_name, "issue", session_id, "failed",
                           error=str(exc)[:500], started_at=started_at,
                           message=message, main_context=context)
    finally:
        with _inflight_lock:
            _inflight.discard(task_id)


def _submit_task(task_id: str, message: str, session_id: str, skill_name: str,
                 model: str = "") -> bool:
    """Hand a task to the agent pool unless one is already in flight for it.

    Returns True if submitted, False if skipped as a duplicate. Only the main
    (polling) thread adds to _inflight and only workers remove, so claiming the
    slot here is race-free against the next tick.
    """
    with _inflight_lock:
        if task_id in _inflight:
            log.debug("%s already running; skipping duplicate launch.", task_id)
            return False
        _inflight.add(task_id)
        depth = len(_inflight)
    _agent_pool.submit(_run_task, task_id, message,
                       session_id, skill_name, model)
    log.info("Submitted %s to agent pool (%d in flight/queued, max %d).",
             task_id, depth, MAX_PARALLEL_AGENTS)
    return True


def run_once() -> None:
    """Single cron tick: reconcile scheduled skills, fetch tasks, and run Claude."""
    log.debug("tick: reconciling scheduled skills and polling for tasks")
    _refresh_config()

    if not _pull_latest_code():
        log.warning("Failed to pull from the repo, still continuing...")

    trigger_cron_skills(_run_agent, main_context=context)
    trigger_aws_sqs_skills(_run_agent, main_context=context)
    trigger_email_skills(_run_agent, main_context=context)

    if not tasks_provider.is_configured():
        log.debug("Tasks provider is not configured; skipping poll.")
        return

    log.debug("Checking %s for tasks...", tasks_provider.describe())

    with _inflight_lock:
        running = len(_inflight)
    log.debug("Agent pool: %d/%d in flight/queued.",
              running, MAX_PARALLEL_AGENTS)

    issue_skills = find_issue_triggered_skills()
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

        # Children of a Codee-owned story are driven by that story's own agent
        # run. What marks a story as Codee-owned is the provider's business.
        if issue_type != "Story" and task.is_parent_codee_story:
            log.debug("Skipping %s: parent %s is a Codee story",
                      task_id, task.parent.key)
            continue

        skill = match_issue_skill(issue_skills, status, issue_type)
        if skill is None:
            log.debug("No issue trigger matches %s (%s, %s); skipping",
                      task_id, status, issue_type)
            continue
        message = f"/{skill.slug} {task_id}"

        log.info("Processing %s (%s, %s): %s  session-id=%s",
                 task_id, status, issue_type, summary, session_id)

        _submit_task(task_id, message, session_id, skill.name, skill.model)


def main() -> None:
    # Entry point: install the handler before anything logs. Level comes from
    # CODEE_DEBUG, which `codee-start --debug` exports for this subprocess.
    configure_logging()

    if not tasks_provider.is_configured():
        log.warning("tasks provider is not configured; task polling stays "
                    "idle until it is set up in Settings (no restart needed)")

    runs_db.clear_active_jobs(context)  # purge rows left by a previous process

    log.info("Starting the main loop (ticking every %ss)...", POLL_INTERVAL)
    log.info("Tasks provider: %s", tasks_provider.describe())
    log.debug("data dir=%s repo root=%s max parallel agents=%d",
              context.data_dir, REPO_ROOT, MAX_PARALLEL_AGENTS)

    while True:
        try:
            run_once()
        except Exception as exc:
            log.error("Unhandled error: %s %s", exc, traceback.format_exc())
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
