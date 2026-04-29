"""JSON API for browser-driven scans."""
from __future__ import annotations

from flask import Blueprint, abort, g, jsonify, request

from . import audit, rotation, scanner
from .auth import login_required
from .extensions import csrf, db
from .models import PathEntry


bp = Blueprint("api", __name__)


@bp.route("/scan/submit", methods=["POST"])
@login_required
def scan_submit():
    """Receive a browser-side scan and run the diff.

    Expected payload:
        {
          "path": "Backup Logs/Archive",
          "label": "Mars · Daily backup archive",  // optional
          "files": [
            {"relative_path": "...", "filename": "...",
             "size_bytes": 12345, "mtime": "2026-04-28T12:00:00Z"},
            ...
          ]
        }
    """
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    label = (data.get("label") or "").strip() or None
    items = data.get("files") or []

    if not path:
        return jsonify(ok=False, error="path is required"), 400
    if not isinstance(items, list):
        return jsonify(ok=False, error="files must be a list"), 400
    if len(items) > 50000:
        return jsonify(ok=False, error="too many files (max 50,000)"), 400

    files = scanner.parse_payload(items)

    # Reuse existing PathEntry if this folder has been scanned before, so the
    # diff has a reference point. Otherwise create a new one.
    from sqlalchemy import desc, select
    pe = db.session.scalar(
        select(PathEntry).where(PathEntry.path == path)
        .order_by(desc(PathEntry.entered_at)).limit(1)
    )
    if pe is None:
        pe = PathEntry(path=path, label=label, entered_by=g.user.id)
        db.session.add(pe)
        db.session.flush()
    else:
        # Update label if the operator gave a new one, and refresh metadata.
        if label and label != pe.label:
            pe.label = label
        pe.entered_by = g.user.id
        import datetime as _dt
        pe.entered_at = _dt.datetime.now(_dt.UTC)

    # Determine whose week it is
    on_duty = rotation.current_assignment()
    scheduled_user_id = on_duty["id"] if on_duty else None

    scan = scanner.ingest_scan(
        path_entry=pe, files=files,
        operator_user_id=g.user.id,
        scheduled_user_id=scheduled_user_id,
    )

    audit.record("scan_run", {
        "scan_id": scan.id, "path_entry_id": pe.id,
        "path": path, "files": scan.file_count or 0,
        "scheduled_user_id": scheduled_user_id,
    })

    return jsonify(
        ok=True,
        scan_id=scan.id,
        redirect=f"/scan/{scan.id}",
    )
