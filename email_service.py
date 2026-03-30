"""
Send plain-text email via SMTP. Credentials come from environment variables only.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def smtp_env_status() -> dict[str, object]:
    """
    Whether outbound email is configured (env only). Used by the UI before confirm/send.

    Returns keys: ready (bool), missing (list of human-readable labels), hint (str).
    """
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    from_addr = os.environ.get("SMTP_FROM_EMAIL", "").strip() or user

    missing: list[str] = []
    if not host:
        missing.append("SMTP_HOST")
    if not from_addr:
        missing.append("SMTP_FROM_EMAIL (or SMTP_USERNAME for From)")

    ready = len(missing) == 0
    hint = ""
    if not ready:
        hint = (
            "Add the missing variable(s) to CLOVER_Tips/.env or tip_dashboard/.env, "
            "then restart uvicorn (Stop terminal with Ctrl+C, run it again)."
        )
    elif host and not (user and password):
        hint = (
            "SMTP host is set but username/password are empty — many providers (e.g. Gmail) "
            "require SMTP_USERNAME and SMTP_PASSWORD."
        )

    return {"ready": ready, "missing": missing, "hint": hint}


def send_plain_email(to_addr: str, subject: str, body: str) -> None:
    """
    Send one email. Raises on configuration or SMTP errors (caller should catch).

    Env:
      SMTP_HOST (required)
      SMTP_PORT (default 587)
      SMTP_USERNAME, SMTP_PASSWORD (if server requires auth)
      SMTP_FROM_EMAIL (defaults to SMTP_USERNAME)
    """
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        raise RuntimeError("SMTP_HOST is not set; cannot send email.")

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    from_addr = os.environ.get("SMTP_FROM_EMAIL", "").strip() or user
    if not from_addr:
        raise RuntimeError("Set SMTP_FROM_EMAIL or SMTP_USERNAME for the From header.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=45) as smtp:
        smtp.starttls()
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)
