"""SMTP server that drops each incoming email into temp-emails/ as an .eml file.

The cron tick (trigger_email_skills) picks them up and routes them to skills by
recipient address. Listens on a non-privileged port; map port 25 -> MAIL_PORT at
the infra layer (iptables/load balancer), not here.
"""
import asyncio
import os
import uuid
from pathlib import Path

from aiosmtpd.controller import Controller

EMAILS_DIR = Path(__file__).parent.parent / "temp-emails"
MAIL_HOST = os.environ.get("MAIL_HOST", "0.0.0.0")
MAIL_PORT = int(os.environ.get("MAIL_PORT", "2525"))


class FileDropHandler:
    async def handle_DATA(self, server, session, envelope):
        EMAILS_DIR.mkdir(parents=True, exist_ok=True)
        # Prepend envelope recipients so the router matches on the real RCPT TO,
        # not just the visible To/Cc headers.
        rcpt = ", ".join(envelope.rcpt_tos)
        content = f"X-Codee-Rcpt: {rcpt}\r\n".encode() + envelope.content
        # uuid keeps names unique within a tick; lexical sort ~ arrival order is
        # good enough for a 3-per-tick drain. ponytail: no timestamp needed.
        path = EMAILS_DIR / f"{uuid.uuid4().hex}.eml"
        path.write_bytes(content)
        print(f"[mail_server] Stored email -> {path.name} for {rcpt}")
        return "250 Message accepted for delivery"


def main() -> None:
    controller = Controller(FileDropHandler(), hostname=MAIL_HOST, port=MAIL_PORT)
    controller.start()
    print(f"[mail_server] Listening on {MAIL_HOST}:{MAIL_PORT}, dropping to {EMAILS_DIR}")
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        controller.stop()


if __name__ == "__main__":
    main()
