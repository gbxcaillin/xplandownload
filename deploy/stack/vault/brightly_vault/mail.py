"""Optional email (SMTP, e.g. Microsoft 365 with an app mailbox). Without SMTP settings the vault
works the same; staff just copy the link themselves and nobody is emailed."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from .config import Settings

log = logging.getLogger("vault.mail")


def enabled(s: Settings) -> bool:
    return bool(s.smtp_host and s.smtp_from)


def _send(s: Settings, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password or "")
        smtp.send_message(msg)


async def send(s: Settings, to: str, subject: str, body: str) -> bool:
    if not enabled(s) or not to:
        return False
    try:
        await asyncio.to_thread(_send, s, to, subject, body)
        return True
    except Exception as exc:   # never break an upload because email failed
        log.warning("email to %s failed: %s", to, exc)
        return False
