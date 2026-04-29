"""Audit logging helper.

Writes an immutable record to the audit_log table AND to the standard
logger (which goes to Railway's log stream / journald in dev).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from flask import g, request

from .extensions import db
from .models import AuditLog


logger = logging.getLogger("vigil.audit")


def record(action: str, details: Optional[dict[str, Any]] = None) -> None:
    user = getattr(g, "user", None)
    user_id = user.id if user else None
    username = user.username if user else None
    ip = request.remote_addr if request else None
    detail_json = json.dumps(details, default=str) if details else None

    db.session.add(AuditLog(
        user_id=user_id, username=username,
        action=action, details=detail_json, ip_address=ip,
    ))
    db.session.commit()

    logger.info(
        "audit action=%s user=%s ip=%s details=%s",
        action, username or "-", ip or "-", detail_json or "-",
    )
