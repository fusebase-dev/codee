import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from codee_main_context.context import CodeeMainContext

from codee.lib import runs_db
from codee_main_context.context import project_root, skills_dir as default_skills_dir

REPO_ROOT = project_root()
SKILLS_DIR = default_skills_dir(REPO_ROOT)

# How far back to look for a missed scheduled fire. A tick can be delayed for
# hours because a single run_once may block on long-running Claude jobs (each up
# to 2h). Without catch-up the exact cron minute is skipped and the job waits a
# whole period (e.g. a week for "0 0 * * 1"). 24h covers realistic backlogs
# while still refusing to run occurrences that are absurdly stale.
DEFAULT_CATCHUP = timedelta(hours=24)


RunClaude = Callable[[str, str], str]


@dataclass(frozen=True)
class CronField:
    values: set[int]
    is_wildcard: bool


@dataclass(frozen=True)
class ScheduledSkill:
    key: str
    name: str
    path: Path
    cron: str
    body: str


def _get_force_file_path(main_context: CodeeMainContext) -> Path:
    return main_context.data_dir / "cron_skill_force.json"


def trigger_cron_skills(
    run_claude: RunClaude,
    *,
    now: datetime | None = None,
    skills_dir: Path = SKILLS_DIR,
    state_file: Path = None,
    force_file: Path = None,
    catchup_window: timedelta = DEFAULT_CATCHUP,
    main_context: CodeeMainContext
) -> None:
    """Scan skills with cron frontmatter and run due jobs.

    A job is considered due if its most recent scheduled fire at or before
    ``now`` (within ``catchup_window``) has not been recorded yet. This lets a
    delayed tick still run a job whose exact cron minute was missed while the
    process was busy, instead of skipping it until the next period.
    """

    if state_file is None:
        state_file = main_context.data_dir / "cron_skill_runs.json"
    if force_file is None:
        force_file = _get_force_file_path(main_context)

    tick = (now or datetime.now()).replace(second=0, microsecond=0)
    state = _load_state(state_file)
    forced = _load_force(force_file)
    changed = False
    force_changed = False

    scheduled_skills = _find_scheduled_skills(skills_dir)
    if not scheduled_skills:
        print("[cron_skills] No scheduled skills found.")
        return

    for skill in scheduled_skills:
        is_forced = skill.key in forced

        if is_forced:
            # Admin UI asked for a one-off run on this tick; ignore the schedule.
            print(
                f"[cron_skills] Force-running {skill.name} on this tick (manual schedule).")
        else:
            try:
                due = _latest_due(skill.cron, tick, catchup_window)
            except ValueError as exc:
                print(f"[cron_skills] Invalid cron for {skill.name}: {exc}")
                continue

            if due is None:
                continue

            due_slot = due.isoformat(timespec="minutes")
            if state.get(skill.key) == due_slot:
                continue

            if skill.key not in state and due != tick:
                # First time we observe this skill and the only due fire is in the
                # past (before the process/skill existed). Don't back-run it; seed a
                # baseline so genuine future misses are still caught up.
                state[skill.key] = due_slot
                changed = True
                continue

            if due != tick:
                print(
                    f"[cron_skills] {skill.name} ({skill.cron}) scheduled at {due_slot} "
                    f"was missed; catching up at {tick.isoformat(timespec='minutes')}."
                )
            print(
                f"[cron_skills] Running {skill.name} ({skill.cron}) from {skill.path}")
        session_id = str(uuid.uuid4())
        try:
            response = run_claude(skill.body, session_id)
            print(
                f"[cron_skills] Claude response for {skill.name} ({len(response)} chars)")
            runs_db.record_run(skill.name, "cron", session_id,
                               "succeeded", message=skill.body,
                               main_context=main_context)
        except Exception as exc:
            # Don't advance the state slot: leave the job "due" so a later tick
            # (within the catch-up window) retries it instead of skipping the
            # whole period. Over-limit runs raise here and get retried.
            print(
                f"[cron_skills] Failed to run {skill.name}, will retry: {exc}")
            runs_db.record_run(skill.name, "cron", session_id, "failed",
                               error=str(exc)[:500], message=skill.body,
                               main_context=main_context)
            continue
        if is_forced:
            # One-shot off-schedule run: clear the flag but leave the cron slot
            # untracked, so the next tick still catches up a genuinely-due fire.
            forced.discard(skill.key)
            force_changed = True
        else:
            state[skill.key] = due_slot
            changed = True

    if changed:
        _save_state(state_file, state)
    if force_changed:
        _save_force(force_file, forced)


def _latest_due(cron: str, tick: datetime, catchup_window: timedelta) -> datetime | None:
    """Most recent minute matching ``cron`` in ``[tick - catchup_window, tick]``.

    Returns ``None`` if no scheduled fire falls within the window. Validates the
    expression on the first candidate so an invalid cron raises ``ValueError``.
    """
    max_steps = max(0, int(catchup_window.total_seconds() // 60))
    candidate = tick
    for _ in range(max_steps + 1):
        if _cron_matches(cron, candidate):
            return candidate
        candidate -= timedelta(minutes=1)
    return None


def _find_scheduled_skills(skills_dir: Path = SKILLS_DIR) -> list[ScheduledSkill]:
    skills: list[ScheduledSkill] = []
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            metadata, body = _parse_skill_file(path.read_text())
        except OSError as exc:
            print(f"[cron_skills] Failed to read {path}: {exc}")
            continue

        # New convention: x-codee-trigger: cron + x-codee-cron. Old `cron` kept as fallback.
        cron = (metadata.get("x-codee-cron")
                or metadata.get("cron", "")).strip()
        if not cron:
            continue

        name = metadata.get(
            "name", path.parent.name).strip() or path.parent.name
        if metadata.get("disable-model-invocation", "").strip().lower() != "true":
            print(
                f"[cron_skills] ERROR: {path} declares cron but is missing "
                "disable-model-invocation: true; skipping."
            )
            continue

        skills.append(
            ScheduledSkill(
                key=_skill_key(path),
                name=name,
                path=path,
                cron=cron,
                body=body.strip(),
            )
        )
    return skills


def _parse_skill_file(contents: str) -> tuple[dict[str, str], str]:
    lines = contents.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, contents

    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break

    if end_index is None:
        return {}, contents

    metadata: dict[str, str] = {}
    for line in lines[1:end_index]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        metadata[key.strip().lower()] = _strip_quotes(value.strip())

    body = "\n".join(lines[end_index + 1:]).lstrip("\n")
    return metadata, body


def _cron_matches(expression: str, tick: datetime) -> bool:
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError(
            "expected 5 fields: minute hour day-of-month month day-of-week")

    minute = _parse_cron_field(parts[0], 0, 59)
    hour = _parse_cron_field(parts[1], 0, 23)
    day_of_month = _parse_cron_field(parts[2], 1, 31)
    month = _parse_cron_field(parts[3], 1, 12)
    day_of_week = _parse_cron_field(
        parts[4], 0, 7, normalize_seven_to_zero=True)
    cron_day_of_week = (tick.weekday() + 1) % 7

    if tick.minute not in minute.values or tick.hour not in hour.values or tick.month not in month.values:
        return False

    month_day_matches = tick.day in day_of_month.values
    week_day_matches = cron_day_of_week in day_of_week.values

    if not day_of_month.is_wildcard and not day_of_week.is_wildcard:
        return month_day_matches or week_day_matches
    return month_day_matches and week_day_matches


def _parse_cron_field(
    field: str,
    minimum: int,
    maximum: int,
    *,
    normalize_seven_to_zero: bool = False,
) -> CronField:
    values: set[int] = set()
    is_wildcard = True

    for item in field.split(","):
        item = item.strip()
        if not item:
            raise ValueError(f"empty cron field item in {field!r}")

        base, slash, step_text = item.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) <= 0:
                raise ValueError(f"invalid step {step_text!r} in {field!r}")
            step = int(step_text)

        if base == "*":
            start = minimum
            end = maximum
        elif "-" in base:
            start_text, _, end_text = base.partition("-")
            start = _parse_cron_int(
                start_text, minimum, maximum, normalize_seven_to_zero)
            end = _parse_cron_int(
                end_text, minimum, maximum, normalize_seven_to_zero)
            is_wildcard = False
            if start > end:
                raise ValueError(f"invalid range {base!r} in {field!r}")
        else:
            value = _parse_cron_int(
                base, minimum, maximum, normalize_seven_to_zero)
            start = value
            end = value
            is_wildcard = False

        values.update(range(start, end + 1, step))

    if normalize_seven_to_zero and 7 in values:
        values.remove(7)
        values.add(0)

    return CronField(values=values, is_wildcard=is_wildcard)


def _parse_cron_int(value: str, minimum: int, maximum: int, normalize_seven_to_zero: bool) -> int:
    if not value.isdigit():
        raise ValueError(f"invalid integer {value!r}")
    parsed = int(value)
    if normalize_seven_to_zero and parsed == 7:
        return parsed
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"value {parsed} outside {minimum}-{maximum}")
    return parsed


def _load_state(state_file: Path) -> dict[str, str]:
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text())
            if isinstance(data, dict):
                return {str(key): str(value) for key, value in data.items()}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[cron_skills] Failed to load state from {state_file}: {exc}")
    return {}


def _save_state(state_file: Path, state: dict[str, str]) -> None:
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True))


def _load_force(force_file: Path) -> set[str]:
    try:
        if force_file.exists():
            data = json.loads(force_file.read_text())
            if isinstance(data, list):
                return {str(key) for key in data}
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"[cron_skills] Failed to load force list from {force_file}: {exc}")
    return set()


def _save_force(force_file: Path, keys: set[str]) -> None:
    force_file.write_text(json.dumps(sorted(keys), indent=2))


def request_force_run(skill_path: Path, main_context: CodeeMainContext) -> None:
    """Queue a cron skill to run on the next trigger tick, ignoring its schedule."""
    keys = _load_force(_get_force_file_path(main_context))
    keys.add(_skill_key(Path(skill_path)))
    _save_force(_get_force_file_path(main_context), keys)


def _skill_key(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
