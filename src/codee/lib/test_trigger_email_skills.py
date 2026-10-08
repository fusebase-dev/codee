import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

from codee_main_context.context import CodeeMainContext

from codee.lib import trigger_email_skills as module
from codee.lib.trigger_email_skills import trigger_email_skills

ADDRESS = "inbox@codee.example.com"


def _raw(sender="dev@example.com", to=ADDRESS, subject="Bug") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("Checkout fails with a 500.")
    return msg.as_bytes()


class FakeMailpit:
    """Just enough of the Mailpit API: search, raw message, delete."""

    def __init__(self, messages):
        # ID -> (Created, recipients, raw)
        self.messages = dict(messages)
        self.deleted = []
        self.queries = []

    def get(self, url, params=None, auth=None, timeout=None):
        resp = MagicMock()
        if url.endswith("/search"):
            self.queries.append((params["query"], auth))
            address = params["query"].split('"')[1]
            hits = [{"ID": i, "Created": c}
                    for i, (c, rcpts, _) in self.messages.items() if address in rcpts]
            hits.sort(key=lambda m: m["Created"], reverse=True)  # newest first
            resp.json.return_value = {"messages": hits}
        else:
            message_id = url.rsplit("/", 2)[-2]
            resp.content = self.messages[message_id][2]
        return resp

    def delete(self, url, json=None, auth=None, timeout=None):
        self.deleted.extend(json["IDs"])
        for message_id in json["IDs"]:
            self.messages.pop(message_id, None)
        return MagicMock()


class EmailTriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        skill_dir = self.tmp / "skills" / "inbox"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: inbox\n"
            "disable-model-invocation: true\n"
            "x-codee-trigger: email\n"
            f"x-codee-email-address: {ADDRESS}\n"
            "---\n"
            "Mail:\n\n{CONTENT}\n")
        self.emails_dir = self.tmp / "emails"
        self.prompts = []
        patch.object(module, "ALLOWED_SENDER_DOMAINS", ("example.com",)).start()
        patch.object(module.runs_db, "record_run").start()
        self.addCleanup(patch.stopall)

    def run_claude(self, prompt, session_id, model, agent, effort=""):
        self.prompts.append(prompt)
        return "ok"

    def tick(self, env=None):
        with patch.dict(module.os.environ, env or {}, clear=False):
            if not env:
                module.os.environ.pop("MAILPIT_API_URL", None)
            trigger_email_skills(self.run_claude, skills_dir=self.tmp / "skills",
                                 emails_dir=self.emails_dir,
                                 main_context=CodeeMainContext(data_dir=self.tmp))

    def test_directory_source_runs_skill_and_removes_file(self):
        self.emails_dir.mkdir()
        (self.emails_dir / "a.eml").write_bytes(_raw())

        self.tick()

        self.assertEqual(len(self.prompts), 1)
        self.assertIn("Checkout fails with a 500.", self.prompts[0])
        self.assertEqual(list(self.emails_dir.glob("*.eml")), [])

    def test_mailpit_source_only_touches_skill_mail_oldest_first(self):
        mailpit = FakeMailpit({
            "new": ("2026-10-08T06:22:00Z", [ADDRESS], _raw(subject="second")),
            "old": ("2026-10-08T06:17:00Z", [ADDRESS], _raw(subject="first")),
            "qa": ("2026-10-08T05:00:00Z", ["qa@codee.example.com"],
                   _raw(to="qa@codee.example.com")),
        })
        env = {"MAILPIT_API_URL": "http://mailpit/api/v1/",
               "MAILPIT_AUTH": "user:secret"}
        with patch.object(module.requests, "get", mailpit.get), \
                patch.object(module.requests, "delete", mailpit.delete):
            self.tick(env)

        self.assertEqual(mailpit.queries, [(f'addressed:"{ADDRESS}"', ("user", "secret"))])
        self.assertEqual(["Subject: first" in p for p in self.prompts], [True, False])
        self.assertEqual(mailpit.deleted, ["old", "new"])
        self.assertIn("qa", mailpit.messages)

    def test_mailpit_envelope_only_recipient_still_routes(self):
        # Bcc'd: Mailpit matched the envelope, the raw headers don't name us.
        mailpit = FakeMailpit({
            "bcc": ("2026-10-08T06:00:00Z", [ADDRESS], _raw(to="someone@example.com")),
        })
        with patch.object(module.requests, "get", mailpit.get), \
                patch.object(module.requests, "delete", mailpit.delete):
            self.tick({"MAILPIT_API_URL": "http://mailpit/api/v1"})

        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(mailpit.deleted, ["bcc"])

    def test_mailpit_failed_run_keeps_message(self):
        mailpit = FakeMailpit({"m": ("2026-10-08T06:00:00Z", [ADDRESS], _raw())})

        def boom(*args, **kwargs):
            raise RuntimeError("agent crashed")

        self.run_claude = boom
        with patch.object(module.requests, "get", mailpit.get), \
                patch.object(module.requests, "delete", mailpit.delete):
            self.tick({"MAILPIT_API_URL": "http://mailpit/api/v1"})

        self.assertEqual(mailpit.deleted, [])


if __name__ == "__main__":
    unittest.main()
