from __future__ import annotations

import logging
import os

import yagmail


logger = logging.getLogger(__name__)


def send_email(recipient_email: str | list[str], subject: str, body: str) -> bool:
    sender_email = os.environ.get("SENDER_EMAIL", "m25ai2043@iitj.ac.in")
    sender_password = os.environ.get("SENDER_PASSWORD", "jqzg bamx isoj kvrf")

    if not sender_email or not sender_password:
        logger.warning(
            "--- EMAIL SKIPPED (Config missing) --- To: %s | Subject: %s",
            recipient_email,
            subject,
        )
        return False

    try:
        yagmail.SMTP(sender_email, sender_password).send(
            to=recipient_email,
            subject=subject,
            contents=body,
        )
        return True
    except Exception as exc:
        logger.error("Email send failed for %s: %s", recipient_email, exc, exc_info=True)
        return False
