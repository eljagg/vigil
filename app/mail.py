"""Email notification system.

Three backends, selected by MAIL_BACKEND env var:

- ``console`` (default): emails are written to the application log only,
  not actually sent. Useful for development and as a safe fallback.
- ``smtp``: classic SMTP — requires MAIL_SMTP_HOST, MAIL_SMTP_PORT,
  MAIL_SMTP_USER, MAIL_SMTP_PASSWORD, optionally MAIL_SMTP_USE_TLS.
- ``resend``: Resend transactional API (https://resend.com) —
  requires MAIL_RESEND_API_KEY. Cleaner setup if you don't have a
  corporate SMTP relay handy.

All backends share the same ``send_mail(to, subject, body_text)`` signature.
HTML content is intentionally not supported for now: plain text avoids
phishing-flag heuristics in corporate mail filters and keeps the system
simple to audit.

Failures are logged but do NOT raise. Missing email is a deliverability
problem, not a reason to crash the app or fail the calling job. The
calling code can inspect the return value if it cares about a specific
send.
"""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.request
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from typing import Optional

from flask import current_app


log = logging.getLogger("vigil.mail")


def _backend() -> str:
    return (current_app.config.get("MAIL_BACKEND") or "console").lower().strip()


def _from_addr() -> str:
    return (
        current_app.config.get("MAIL_FROM")
        or "vigil-noreply@example.com"
    )


def _config_status() -> dict:
    """Return a small dict describing the mail config — used by the settings UI
    to tell the admin whether mail is wired up."""
    backend = _backend()
    cfg = current_app.config
    if backend == "console":
        return {
            "backend": "console",
            "ready": True,
            "details": "Emails are logged only — not actually sent.",
        }
    if backend == "smtp":
        host = cfg.get("MAIL_SMTP_HOST")
        port = cfg.get("MAIL_SMTP_PORT")
        ready = bool(host and port and cfg.get("MAIL_FROM"))
        return {
            "backend": "smtp",
            "ready": ready,
            "details": (
                f"SMTP host {host}:{port}, sender {cfg.get('MAIL_FROM') or '(unset)'}"
                if ready else "SMTP backend selected but configuration incomplete."
            ),
        }
    if backend == "resend":
        ready = bool(cfg.get("MAIL_RESEND_API_KEY") and cfg.get("MAIL_FROM"))
        return {
            "backend": "resend",
            "ready": ready,
            "details": (
                f"Resend API, sender {cfg.get('MAIL_FROM')}"
                if ready else "Resend backend selected but MAIL_RESEND_API_KEY or MAIL_FROM is unset."
            ),
        }
    return {"backend": backend, "ready": False, "details": f"Unknown backend: {backend!r}"}


def send_mail(to: str, subject: str, body_text: str) -> dict:
    """Send a plain-text email.

    Returns a dict with at least ``ok`` (bool) and ``backend`` (str). On
    failure, includes ``error`` (str). Never raises.
    """
    if not to or "@" not in to:
        return {"ok": False, "backend": _backend(), "error": "no recipient"}

    backend = _backend()
    try:
        if backend == "console":
            return _send_console(to, subject, body_text)
        if backend == "smtp":
            return _send_smtp(to, subject, body_text)
        if backend == "resend":
            return _send_resend(to, subject, body_text)
        return {"ok": False, "backend": backend, "error": f"unknown backend {backend!r}"}
    except Exception as e:  # never raise to caller
        log.exception("mail send failed via %s: %s", backend, e)
        return {"ok": False, "backend": backend, "error": str(e)}


# --------------------------- backends ---------------------------

def _send_console(to: str, subject: str, body: str) -> dict:
    log.info(
        "[MAIL/console] to=%s from=%s subject=%r body=%s",
        to, _from_addr(), subject, body[:200] + ("…" if len(body) > 200 else ""),
    )
    return {"ok": True, "backend": "console"}


def _send_smtp(to: str, subject: str, body: str) -> dict:
    cfg = current_app.config
    host = cfg.get("MAIL_SMTP_HOST")
    port = int(cfg.get("MAIL_SMTP_PORT") or 0)
    user = cfg.get("MAIL_SMTP_USER")
    password = cfg.get("MAIL_SMTP_PASSWORD")
    use_tls = bool(cfg.get("MAIL_SMTP_USE_TLS", True))
    if not host or not port:
        return {"ok": False, "backend": "smtp", "error": "MAIL_SMTP_HOST/PORT not configured"}

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = _from_addr()
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="vigil")

    # Auto-pick STARTTLS vs implicit TLS based on common port conventions:
    # 465 = implicit TLS (SMTPS), 587 = STARTTLS, 25 = plain (rare).
    if port == 465:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as s:
            if user and password:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            if use_tls:
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            if user and password:
                s.login(user, password)
            s.send_message(msg)

    log.info("[MAIL/smtp] sent to=%s subject=%r", to, subject)
    return {"ok": True, "backend": "smtp"}


def _send_resend(to: str, subject: str, body: str) -> dict:
    cfg = current_app.config
    api_key = cfg.get("MAIL_RESEND_API_KEY")
    if not api_key:
        return {"ok": False, "backend": "resend", "error": "MAIL_RESEND_API_KEY not configured"}

    payload = json.dumps({
        "from": _from_addr(),
        "to": [to],
        "subject": subject,
        "text": body,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body_resp = resp.read().decode("utf-8", errors="replace")
            log.info("[MAIL/resend] sent to=%s subject=%r resp=%s", to, subject, body_resp[:200])
            return {"ok": True, "backend": "resend"}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "backend": "resend", "error": f"HTTP {e.code}: {err_body[:300]}"}


# --------------------------- compose helpers ---------------------------

def _base_url() -> str:
    """Canonical base URL for links in emails. Falls back to '' so the email
    still makes sense even without a configured base URL."""
    return (current_app.config.get("MAIL_BASE_URL") or "").rstrip("/")


def compose_weekly_reminder(
    *, recipient_full_name: str, week_start: str, week_end: str,
    paths_total: int, paths_scanned: int, missed_paths: list[str],
    company_name: str = "",
) -> tuple[str, str]:
    """Compose the (subject, body) for a weekly duty reminder.

    Tone: factual, short, action-oriented. No marketing speak. Plain text
    so it renders identically in every mail client and avoids HTML
    rendering quirks in corporate filters.
    """
    name_prefix = (company_name + " · ") if company_name else ""
    if paths_scanned >= paths_total and paths_total > 0:
        subject = f"{name_prefix}Vigil — backup checks complete for {week_start} → {week_end}"
    else:
        subject = f"{name_prefix}Vigil — backup integrity check due ({paths_scanned}/{paths_total} done)"

    base = _base_url()
    link = f"{base}/scan/new" if base else "/scan/new"

    lines = [
        f"Hi {recipient_full_name},",
        "",
        f"This is your Vigil weekly reminder for {week_start} → {week_end}.",
        "",
        f"Status: {paths_scanned} of {paths_total} configured path"
        f"{'s' if paths_total != 1 else ''} scanned this week.",
    ]
    if missed_paths:
        lines += ["", "Paths not yet scanned this week:"]
        for p in missed_paths[:20]:
            lines.append(f"  • {p}")
        if len(missed_paths) > 20:
            lines.append(f"  …and {len(missed_paths) - 20} more.")
    else:
        lines += ["", "All configured paths have been covered this week. Thank you."]

    lines += [
        "",
        f"Run a new scan: {link}",
        "",
        "— Vigil",
    ]
    return subject, "\n".join(lines)


def compose_test_email(*, recipient_full_name: str) -> tuple[str, str]:
    """Quick sanity-check email used by `flask send-test-email` and the
    settings 'Send test email' button."""
    base = _base_url()
    body = (
        f"Hi {recipient_full_name},\n\n"
        f"This is a test email from Vigil. Mail backend is configured "
        f"correctly — emails will deliver.\n\n"
        f"Vigil instance: {base or '(no MAIL_BASE_URL set)'}\n\n"
        f"— Vigil"
    )
    return "Vigil — test email", body
