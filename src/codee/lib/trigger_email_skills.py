import email
import os
import tempfile
import uuid
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Callable

import requests

from codee_main_context.context import CodeeMainContext

from codee.lib import runs_db
from codee.lib.trigger_aws_sqs_skills import _parse_skill_file, _skill_key
from codee_main_context.context import project_root, skills_dir as default_skills_dir

REPO_ROOT = project_root()
SKILLS_DIR = default_skills_dir(REPO_ROOT)
EMAILS_DIR = REPO_ROOT / "temp-emails"

# Max emails handled per cron tick (across all email-triggered skills).
MAX_EMAILS_PER_TICK = 3

# Comma-separated sender domains. When unset, email-triggered skills remain disabled.
ALLOWED_SENDER_DOMAINS = tuple(
    domain.strip().lower()
    for domain in os.environ.get("CODEE_ALLOWED_SENDER_DOMAINS", "").split(",")
    if domain.strip()
)

RunClaude = Callable[..., str]


@dataclass(frozen=True)
class QueuedEmail:
    """One message waiting to be routed, wherever it is stored."""
    name: str
    read: Callable[[], bytes]
    consume: Callable[[], None]
    # Skill address the source already matched on, for recipients the raw
    # headers don't show (Mailpit also matches the SMTP envelope, e.g. Bcc).
    address: str = ""


@dataclass(frozen=True)
class EmailTriggeredSkill:
    key: str
    name: str
    path: Path
    address: str
    body: str
    model: str
    agent: str = ""
    effort: str = ""


def trigger_email_skills(
    run_claude: RunClaude,
    *,
    skills_dir: Path = SKILLS_DIR,
    emails_dir: Path = EMAILS_DIR,
    main_context: CodeeMainContext
) -> None:
    """Route up to MAX_EMAILS_PER_TICK queued emails to skills by recipient address.

    Emails come from Mailpit when MAILPIT_API_URL is set, otherwise from .eml
    files in emails_dir.
    """
    skills_by_address = find_email_triggered_skills(skills_dir)
    if not skills_by_address:
        return

    if _mailpit_api():
        try:
            queued = _mailpit_emails(skills_by_address)
        except Exception as exc:
            print(f"[email_skills] Failed to list Mailpit messages: {exc}")
            return
    else:
        queued = _directory_emails(emails_dir)

    for item in queued[:MAX_EMAILS_PER_TICK]:
        try:
            message = email.message_from_bytes(item.read())
        except Exception as exc:
            print(f"[email_skills] Failed to read {item.name}: {exc}")
            continue

        if not _sender_allowed(message):
            print(
                f"[email_skills] Sender of {item.name} not in allowed domains; dropping.")
            item.consume()
            continue

        skill = (_match_skill(message, skills_by_address)
                 or skills_by_address.get(item.address))
        if skill is None:
            print(
                f"[email_skills] No skill matches recipients of {item.name}; dropping.")
            item.consume()
            continue

        print(
            f"[email_skills] Running {skill.name} for email {item.name} -> {skill.address}")
        session_id = str(uuid.uuid4())
        prompt = render_email_prompt(skill.body, message)
        try:
            response = run_claude(prompt, session_id, skill.model, skill.agent,
                                  effort=skill.effort)
            print(
                f"[email_skills] Claude response for {skill.name} ({len(response)} chars)")
            item.consume()
            runs_db.record_run(skill.name, "email", session_id,
                               "succeeded", message=prompt,
                               user_message=prompt, response=response,
                               main_context=main_context)
        except runs_db.JobKilled as exc:
            # Stopped on purpose: consume the email so the next tick doesn't
            # start the same run again.
            print(f"[email_skills] {skill.name} for {item.name} was killed "
                  "from the dashboard.")
            item.consume()
            runs_db.record_run(skill.name, "email", session_id, "killed",
                               error=str(exc), message=prompt,
                               user_message=prompt,
                               main_context=main_context)
        except Exception as exc:
            print(
                f"[email_skills] Failed to run {skill.name} for {item.name}: {exc}")
            runs_db.record_run(skill.name, "email", session_id, "failed",
                               error=str(exc)[:500], message=prompt,
                               user_message=prompt,
                               main_context=main_context)


def _directory_emails(emails_dir: Path) -> list[QueuedEmail]:
    """.eml files dropped by mail_server (or anything else), oldest name first."""
    emails_dir.mkdir(parents=True, exist_ok=True)
    return [
        QueuedEmail(path.name, path.read_bytes,
                    lambda path=path: path.unlink(missing_ok=True))
        for path in sorted(p for p in emails_dir.glob("*.eml") if p.is_file())
    ]


def _mailpit_api() -> str:
    return os.environ.get("MAILPIT_API_URL", "").strip().rstrip("/")


def _mailpit_auth() -> tuple[str, str] | None:
    """MAILPIT_AUTH=user:password (basic auth), optional."""
    user, _, password = os.environ.get("MAILPIT_AUTH", "").partition(":")
    return (user, password) if user else None


def _mailpit_emails(
    skills_by_address: dict[str, EmailTriggeredSkill],
) -> list[QueuedEmail]:
    """Mailpit messages addressed to a skill, oldest first.

    Searching by address leaves every other message in Mailpit untouched.
    """
    api, auth = _mailpit_api(), _mailpit_auth()
    found: dict[str, tuple[str, str]] = {}  # ID -> (Created, address)
    for address in skills_by_address:
        resp = requests.get(f"{api}/search",
                            params={"query": f'addressed:"{address}"', "limit": 50},
                            auth=auth, timeout=30)
        resp.raise_for_status()
        for meta in resp.json().get("messages") or []:
            found.setdefault(meta["ID"], (meta.get("Created", ""), address))

    def read(message_id: str) -> bytes:
        resp = requests.get(f"{api}/message/{message_id}/raw", auth=auth, timeout=30)
        resp.raise_for_status()
        return resp.content

    def delete(message_id: str) -> None:
        try:
            resp = requests.delete(f"{api}/messages", json={"IDs": [message_id]},
                                   auth=auth, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[email_skills] Failed to delete Mailpit message {message_id}: {exc}")

    return [
        QueuedEmail(f"mailpit:{message_id}",
                    lambda message_id=message_id: read(message_id),
                    lambda message_id=message_id: delete(message_id),
                    address=address)
        for message_id, (_, address) in sorted(found.items(), key=lambda kv: kv[1])
    ]


def find_email_triggered_skills(skills_dir: Path = SKILLS_DIR) -> dict[str, EmailTriggeredSkill]:
    """Map normalized recipient address -> skill. Duplicate addresses are skipped."""
    skills_by_address: dict[str, EmailTriggeredSkill] = {}
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            metadata, body = _parse_skill_file(path.read_text())
        except OSError as exc:
            print(f"[email_skills] Failed to read {path}: {exc}")
            continue

        if metadata.get("x-codee-trigger", "").strip().lower() != "email":
            continue

        name = metadata.get(
            "name", path.parent.name).strip() or path.parent.name
        if metadata.get("disable-model-invocation", "").strip().lower() != "true":
            print(
                f"[email_skills] ERROR: {path} declares x-codee-trigger: email but is missing "
                "disable-model-invocation: true; skipping."
            )
            continue

        address = metadata.get("x-codee-email-address", "").strip().lower()
        if not address:
            print(
                f"[email_skills] ERROR: {path} declares x-codee-trigger: email but is missing "
                "x-codee-email-address; skipping."
            )
            continue

        if address in skills_by_address:
            print(
                f"[email_skills] WARNING: duplicate x-codee-email-address {address!r} in {path}; "
                f"already claimed by {skills_by_address[address].path}. Skipping."
            )
            continue

        skills_by_address[address] = EmailTriggeredSkill(
            key=_skill_key(path),
            name=name,
            path=path,
            address=address,
            body=body.strip(),
            model=metadata.get("model", "").strip(),
            agent=metadata.get("x-codee-agent", "").strip(),
            effort=metadata.get("effort", "").strip(),
        )
    return skills_by_address


def _sender_allowed(message: Message) -> bool:
    _, addr = email.utils.parseaddr(message.get("From", ""))
    domain = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
    return domain in ALLOWED_SENDER_DOMAINS


def _match_skill(
    message: Message, skills_by_address: dict[str, EmailTriggeredSkill]
) -> EmailTriggeredSkill | None:
    for address in _recipients(message):
        skill = skills_by_address.get(address)
        if skill is not None:
            return skill
    return None


def _recipients(message: Message) -> list[str]:
    """All recipient addresses, normalized to lowercase. Envelope header wins."""
    headers = ["X-Codee-Rcpt", "Delivered-To", "To", "Cc", "Bcc"]
    raw = ", ".join(v for h in headers for v in message.get_all(h, []))
    return [addr.lower() for _, addr in email.utils.getaddresses([raw]) if addr]


def render_email_prompt(body: str, message: Message) -> str:
    content = _format_email(message)
    if "{CONTENT}" in body:
        return body.replace("{CONTENT}", content)
    return f"{body.rstrip()}\n\n{content}"


def _format_email(message: Message) -> str:
    headers = [f"{key}: {value}" for key, value in message.items()]
    parts = [*headers, "", _body_text(message)]
    attachments = _save_attachments(message)
    if attachments:
        parts.append("")
        parts.append("Attachments (saved to disk):")
        parts.extend(f"- {name}: {path}" for name, path in attachments)
    return "\n".join(parts).strip()


def _save_attachments(message: Message) -> list[tuple[str, Path]]:
    """Write every attachment to a temp dir; return (filename, path) references."""
    saved: list[tuple[str, Path]] = []
    dest: Path | None = None
    for part in message.walk():
        filename = part.get_filename()
        if not filename or part.get_content_disposition() == "inline":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        if dest is None:
            dest = Path(tempfile.mkdtemp(prefix="email-attach-"))
        safe = Path(filename).name  # strip any path components
        path = dest / safe
        path.write_bytes(payload)
        saved.append((safe, path))
    return saved


def _body_text(message: Message) -> str:
    try:
        part = message.get_body(preferencelist=("plain", "html"))
    except (AttributeError, Exception):  # noqa: BLE001 - non-EmailMessage fallback
        part = None
    if part is not None:
        return part.get_content().strip()

    if message.is_multipart():
        for sub in message.walk():
            if sub.get_content_type() == "text/plain":
                return sub.get_payload(decode=True).decode(sub.get_content_charset() or "utf-8", "replace").strip()
        return ""
    payload = message.get_payload(decode=True)
    if payload is None:
        return message.get_payload()
    return payload.decode(message.get_content_charset() or "utf-8", "replace").strip()
