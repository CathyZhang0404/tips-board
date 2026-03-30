"""
Send plain-text email: prefer Resend HTTPS API (Render free tier), else SMTP.

Render free web services block outbound SMTP ports; use RESEND_API_KEY on Render.
See: https://render.com/changelog/free-web-services-will-no-longer-allow-outbound-traffic-to-smtp-ports
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import requests


def smtp_env_status() -> dict[str, object]:
    """
    Whether outbound email is configured (env only). Used by the UI before confirm/send.

    Returns keys: ready (bool), missing (list of human-readable labels), hint (str).
    """
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    if resend_key:
        from_addr = (
            os.environ.get("RESEND_FROM_EMAIL", "").strip()
            or os.environ.get("SMTP_FROM_EMAIL", "").strip()
        )
        missing: list[str] = []
        if not from_addr:
            missing.append("RESEND_FROM_EMAIL (or SMTP_FROM_EMAIL)")
        ready = len(missing) == 0
        hint = (
            "Using Resend API (HTTPS) — works on Render free tier (SMTP ports are blocked there)."
            if ready
            else "Set RESEND_FROM_EMAIL or SMTP_FROM_EMAIL for the From address."
        )
        return {"ready": ready, "missing": missing, "hint": hint}

    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    from_addr = os.environ.get("SMTP_FROM_EMAIL", "").strip() or user

    missing = []
    if not host:
        missing.append("SMTP_HOST")
    if not from_addr:
        missing.append("SMTP_FROM_EMAIL (or SMTP_USERNAME for From)")

    ready = len(missing) == 0
    hint = ""
    if not ready:
        hint = (
            "Add SMTP_* variables or set RESEND_API_KEY for HTTPS email (needed on Render free). "
            "Restart after changing .env."
        )
    elif host and not (user and password):
        hint = (
            "SMTP host is set but username/password are empty — many providers (e.g. Gmail) "
            "require SMTP_USERNAME and SMTP_PASSWORD."
        )

    if os.environ.get("RENDER", "").strip() and not resend_key and ready:
        extra = (
            " Render’s free tier blocks SMTP; email send will fail until you add RESEND_API_KEY "
            "(see README) or upgrade the service."
        )
        hint = (hint + extra) if hint else extra.strip()

    return {"ready": ready, "missing": missing, "hint": hint}


def _send_via_resend(to_addr: str, subject: str, body: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not set.")

    from_addr = (
        os.environ.get("RESEND_FROM_EMAIL", "").strip()
        or os.environ.get("SMTP_FROM_EMAIL", "").strip()
    )
    if not from_addr:
        raise RuntimeError("Set RESEND_FROM_EMAIL or SMTP_FROM_EMAIL for the From address.")

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_addr,
            "to": [to_addr],
            "subject": subject,
            "text": body,
        },
        timeout=45,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Resend API {resp.status_code}: {resp.text[:500]}")


def send_plain_email(to_addr: str, subject: str, body: str) -> None:
    """
    Send one email. Raises on failure (caller should catch).

    If RESEND_API_KEY is set, uses Resend HTTP API (works on Render free).
    Otherwise uses SMTP (SMTP_HOST, SMTP_PORT, …).
    """
    if os.environ.get("RESEND_API_KEY", "").strip():
        _send_via_resend(to_addr, subject, body)
        return

    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        raise RuntimeError(
            "SMTP_HOST is not set; cannot send email. "
            "On Render free tier use RESEND_API_KEY instead (SMTP is blocked)."
        )

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
