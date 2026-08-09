import contextlib
import io
import json
import tempfile
import unittest
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from codee_main_context.context import CodeeMainContext

from codee.lib.trigger_aws_sqs_skills import (
    AwsSqsMessage,
    find_aws_sqs_triggered_skills,
    render_aws_sqs_prompt,
    trigger_aws_sqs_skills,
)
from codee.lib.trigger_cron_skills import (
    _cron_matches,
    _find_scheduled_skills,
    _latest_due,
    _parse_skill_file,
    request_force_run,
    trigger_cron_skills,
)


def _ctx(data_dir: Path) -> CodeeMainContext:
    return CodeeMainContext(data_dir=data_dir)


class FakeSqsMessageSource:
    def __init__(self, messages):
        self.messages = list(messages)
        self.deleted = []
        self.received_skills = []

    def receive(self, skill):
        self.received_skills.append(skill)
        if not self.messages:
            return None
        return self.messages.pop(0)

    def delete(self, message):
        self.deleted.append(message)


class CronSkillTests(unittest.TestCase):
    def test_parse_skill_file_strips_frontmatter(self):
        metadata, body = _parse_skill_file(
            "---\n"
            "name: Check stale feature flags\n"
            "description: Runs every day.\n"
            "cron: 0 0 * * *\n"
            "---\n"
            "\n"
            "# Skill Body\n"
            "Do the scheduled work.\n"
        )

        self.assertEqual(metadata["cron"], "0 0 * * *")
        self.assertEqual(metadata["name"], "Check stale feature flags")
        self.assertEqual(body, "# Skill Body\nDo the scheduled work.")

    def test_cron_matches_steps_and_lists(self):
        tick = datetime(2026, 6, 7, 10, 15)

        self.assertTrue(_cron_matches("*/5 10 * * *", tick))
        self.assertTrue(_cron_matches("10,15 9-11 * * *", tick))
        self.assertFalse(_cron_matches("*/10 10 * * *", tick))

    def test_find_scheduled_skills_reads_skill_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_dir = Path(temp_dir)
            skill_dir = skills_dir / "daily-check"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: Daily Check\n"
                "disable-model-invocation: true\n"
                "cron: 0 0 * * *\n"
                "---\n\n"
                "Run the daily check.\n"
            )

            skills = _find_scheduled_skills(skills_dir)

        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "Daily Check")
        self.assertEqual(skills[0].body, "Run the daily check.")

    def test_find_scheduled_skills_requires_disable_model_invocation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_dir = Path(temp_dir)
            skill_dir = skills_dir / "daily-check"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Daily Check\ncron: 0 0 * * *\n---\n\nRun the daily check.\n"
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                skills = _find_scheduled_skills(skills_dir)

        self.assertEqual(skills, [])
        self.assertIn("ERROR", output.getvalue())
        self.assertIn("disable-model-invocation: true", output.getvalue())

    def test_find_aws_sqs_triggered_skills_reads_frontmatter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_dir = Path(temp_dir)
            skill_dir = skills_dir / "sqs-check"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: SQS Check\n"
                "disable-model-invocation: true\n"
                "x-codee-trigger: aws-sqs\n"
                "x-codee-aws-sqs-queue: codee-queue\n"
                "---\n\n"
                "Process this content: {CONTENT}\n"
            )

            skills = find_aws_sqs_triggered_skills(skills_dir)

        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "SQS Check")
        self.assertEqual(skills[0].queue, "codee-queue")
        self.assertEqual(skills[0].body, "Process this content: {CONTENT}")

    def test_find_aws_sqs_triggered_skills_requires_queue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_dir = Path(temp_dir)
            skill_dir = skills_dir / "sqs-check"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: SQS Check\n"
                "disable-model-invocation: true\n"
                "x-codee-trigger: aws-sqs\n"
                "---\n\n"
                "Process one SQS message.\n"
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                skills = find_aws_sqs_triggered_skills(skills_dir)

        self.assertEqual(skills, [])
        self.assertIn("x-codee-aws-sqs-queue", output.getvalue())

    def test_render_aws_sqs_prompt_replaces_content_placeholder(self):
        self.assertEqual(
            render_aws_sqs_prompt("Handle this:\n{CONTENT}", "message body"),
            "Handle this:\nmessage body",
        )

    def test_render_aws_sqs_prompt_appends_content_without_placeholder(self):
        self.assertEqual(
            render_aws_sqs_prompt("Handle this message.", "message body"),
            "Handle this message.\n\nmessage body",
        )

    def test_reconcile_runs_due_skill_once_per_minute(self):
        calls = []

        def run_claude(message: str, session_id: str, model: str = "") -> str:
            calls.append((message, session_id))
            return "done"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            state_file = root / "state.json"
            skill_dir = skills_dir / "daily-check"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: Daily Check\n"
                "disable-model-invocation: true\n"
                "cron: 15 10 * * *\n"
                "---\n\n"
                "Run the daily check.\n"
            )
            tick = datetime(2026, 6, 7, 10, 15, 30)

            trigger_cron_skills(run_claude, now=tick, skills_dir=skills_dir,
                                state_file=state_file, main_context=_ctx(root))
            trigger_cron_skills(run_claude, now=tick, skills_dir=skills_dir,
                                state_file=state_file, main_context=_ctx(root))

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "Run the daily check.")
        self.assertTrue(calls[0][1])

    def test_reconcile_retries_when_run_fails(self):
        # A failing run (e.g. over usage limit) must NOT mark the slot done, so
        # the next tick within the catch-up window runs it again.
        calls = []

        def run_claude(message: str, session_id: str, model: str = "") -> str:
            calls.append(session_id)
            if len(calls) == 1:
                raise RuntimeError("over limit")
            return "done"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            state_file = root / "state.json"
            skill_dir = skills_dir / "daily-check"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: Daily Check\n"
                "disable-model-invocation: true\n"
                "cron: 15 10 * * *\n"
                "---\n\n"
                "Run the daily check.\n"
            )
            tick = datetime(2026, 6, 7, 10, 15, 30)

            trigger_cron_skills(run_claude, now=tick, skills_dir=skills_dir,
                                state_file=state_file, main_context=_ctx(root))
            # nothing recorded yet -> retried on the next tick same minute
            self.assertFalse(state_file.exists())
            trigger_cron_skills(run_claude, now=tick, skills_dir=skills_dir,
                                state_file=state_file, main_context=_ctx(root))

            self.assertEqual(len(calls), 2)
            self.assertTrue(state_file.exists())

    def test_latest_due_finds_missed_fire_within_window(self):
        # cron fires at 12:05; a tick lands at 12:10 (exact minute was skipped)
        cron = "5 12 * * *"
        tick = datetime(2026, 6, 7, 12, 10)
        due = _latest_due(cron, tick, timedelta(hours=24))
        self.assertEqual(due, datetime(2026, 6, 7, 12, 5))

    def test_latest_due_returns_none_outside_window(self):
        # weekly Monday-midnight cron, tick is days later, window only 24h
        cron = "0 0 * * 1"
        tick = datetime(2026, 6, 7, 12, 10)  # a Sunday
        self.assertIsNone(_latest_due(cron, tick, timedelta(hours=24)))

    def test_reconcile_catches_up_missed_minute(self):
        calls = []

        def run_claude(message: str, session_id: str, model: str = "") -> str:
            calls.append((message, session_id))
            return "done"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            state_file = root / "state.json"
            skill_dir = skills_dir / "daily-check"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: Daily Check\n"
                "disable-model-invocation: true\n"
                "cron: 5 12 * * *\n"
                "---\n\n"
                "Run the daily check.\n"
            )

            # 12:00 tick: not yet due, nothing runs.
            trigger_cron_skills(
                run_claude,
                now=datetime(2026, 6, 7, 12, 0),
                skills_dir=skills_dir,
                state_file=state_file,
                main_context=_ctx(root),
            )
            self.assertEqual(calls, [])

            # 12:10 tick: the 12:05 fire was missed while busy -> catch up now.
            trigger_cron_skills(
                run_claude,
                now=datetime(2026, 6, 7, 12, 10),
                skills_dir=skills_dir,
                state_file=state_file,
                main_context=_ctx(root),
            )
            self.assertEqual(len(calls), 1)

            # A later tick the same day must not re-run the same occurrence.
            trigger_cron_skills(
                run_claude,
                now=datetime(2026, 6, 7, 12, 20),
                skills_dir=skills_dir,
                state_file=state_file,
                main_context=_ctx(root),
            )
            self.assertEqual(len(calls), 1)

            state = json.loads(state_file.read_text())
            self.assertEqual(list(state.values()), ["2026-06-07T12:05"])

    def test_reconcile_does_not_backrun_on_first_sight(self):
        # Starting the listener after a past fire must not retroactively run it;
        # it should only seed a baseline so future misses are caught.
        calls = []

        def run_claude(message: str, session_id: str, model: str = "") -> str:
            calls.append((message, session_id))
            return "done"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            state_file = root / "state.json"
            skill_dir = skills_dir / "daily-check"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: Daily Check\n"
                "disable-model-invocation: true\n"
                "cron: 0 0 * * *\n"
                "---\n\n"
                "Run the daily check.\n"
            )

            # First ever tick lands at 23:00, hours after the 00:00 fire.
            trigger_cron_skills(
                run_claude,
                now=datetime(2026, 6, 7, 23, 0),
                skills_dir=skills_dir,
                state_file=state_file,
                main_context=_ctx(root),
            )
            self.assertEqual(calls, [])

            state = json.loads(state_file.read_text())
            self.assertEqual(list(state.values()), ["2026-06-07T00:00"])

    def test_force_run_fires_off_schedule_then_clears(self):
        calls = []

        def run_claude(message: str, session_id: str, model: str = "") -> str:
            calls.append(session_id)
            return "done"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            state_file = root / "state.json"
            # request_force_run derives this path from main_context.data_dir.
            force_file = root / "cron_skill_force.json"
            skill_dir = skills_dir / "daily-check"
            skill_dir.mkdir(parents=True)
            skill_path = skill_dir / "SKILL.md"
            skill_path.write_text(
                "---\n"
                "name: Daily Check\n"
                "disable-model-invocation: true\n"
                "cron: 0 0 * * *\n"
                "---\n\n"
                "Run the daily check.\n"
            )

            # 15:30 is nowhere near the 00:00 cron, but a force request must run it.
            request_force_run(skill_path, _ctx(root))
            off_schedule = datetime(2026, 6, 7, 15, 30)
            trigger_cron_skills(run_claude, now=off_schedule, skills_dir=skills_dir,
                                state_file=state_file, force_file=force_file,
                                main_context=_ctx(root))
            self.assertEqual(len(calls), 1)

            # Force is one-shot: a later off-schedule tick must not re-run it.
            trigger_cron_skills(run_claude, now=datetime(2026, 6, 7, 15, 31),
                                skills_dir=skills_dir, state_file=state_file,
                                force_file=force_file, main_context=_ctx(root))
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(force_file.read_text()), [])

    def test_force_run_stays_queued_when_run_fails(self):
        calls = []

        def run_claude(message: str, session_id: str, model: str = "") -> str:
            calls.append(session_id)
            raise RuntimeError("over limit")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            state_file = root / "state.json"
            # request_force_run derives this path from main_context.data_dir.
            force_file = root / "cron_skill_force.json"
            skill_dir = skills_dir / "daily-check"
            skill_dir.mkdir(parents=True)
            skill_path = skill_dir / "SKILL.md"
            skill_path.write_text(
                "---\nname: Daily Check\ndisable-model-invocation: true\n"
                "cron: 0 0 * * *\n---\n\nRun the daily check.\n"
            )

            request_force_run(skill_path, _ctx(root))
            trigger_cron_skills(run_claude, now=datetime(2026, 6, 7, 15, 30),
                                skills_dir=skills_dir, state_file=state_file,
                                force_file=force_file, main_context=_ctx(root))

            # Still queued so the next tick retries it.
            key = json.loads(force_file.read_text())
            self.assertEqual(len(key), 1)
            self.assertEqual(len(calls), 1)

    def test_reconcile_passes_the_skill_model_to_the_agent(self):
        calls = []

        def run_claude(message: str, session_id: str, model: str = "") -> str:
            calls.append(model)
            return "done"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            for slug, model_line in (("with-model", "model: claude-opus-5\n"),
                                     ("without-model", "")):
                skill_dir = skills_dir / slug
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\n"
                    f"name: {slug}\n"
                    "disable-model-invocation: true\n"
                    "cron: 15 10 * * *\n"
                    f"{model_line}"
                    "---\n\n"
                    "Run it.\n"
                )
            tick = datetime(2026, 6, 7, 10, 15)

            trigger_cron_skills(run_claude, now=tick, skills_dir=skills_dir,
                                state_file=root / "state.json",
                                main_context=_ctx(root))

        # Sorted by directory name, so the skill declaring a model comes first.
        self.assertEqual(calls, ["claude-opus-5", ""])

    def test_reconcile_runs_one_aws_sqs_message_per_tick(self):
        calls = []
        message = AwsSqsMessage(
            content="payload",
            queue_url="https://sqs.example/queue",
            receipt_handle="receipt",
        )
        sqs_source = FakeSqsMessageSource([message])

        def run_claude(user_message: str, session_id: str, model: str = "") -> str:
            calls.append((user_message, session_id))
            return "done"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            skill_dir = skills_dir / "sqs-check"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: SQS Check\n"
                "disable-model-invocation: true\n"
                "x-codee-trigger: aws-sqs\n"
                "x-codee-aws-sqs-queue: codee-queue\n"
                "---\n\n"
                "Process this content: {CONTENT}\n"
            )

            trigger_aws_sqs_skills(
                run_claude,
                skills_dir=skills_dir,
                sqs_message_source=sqs_source,
                main_context=_ctx(root),
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "Process this content: payload")
        self.assertTrue(calls[0][1])
        self.assertEqual(sqs_source.deleted, [message])


if __name__ == "__main__":
    unittest.main()