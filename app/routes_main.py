"""Main user-facing routes."""
from __future__ import annotations

import datetime

from flask import (
    Blueprint, Response, abort, current_app, flash, g, jsonify,
    redirect, render_template, request, url_for,
)
from sqlalchemy import desc, func, select

from . import audit, exports, rotation, scanner
from .auth import login_required
from .extensions import db
from .models import FileSnapshot, PathEntry, Scan, User, settings_dict


bp = Blueprint("main", __name__)


# ---------------------- Health check (Railway pings this) ----------------------

@bp.route("/healthz")
def healthz():
    try:
        db.session.execute(select(1))
        return jsonify(status="ok"), 200
    except Exception as e:
        current_app.logger.error("healthz: db check failed: %s", e)
        return jsonify(status="degraded", error=str(e)), 503


# ---------------------- Dashboard ----------------------

@bp.route("/")
@login_required
def dashboard():
    recent = db.session.execute(
        select(Scan, PathEntry, User)
        .join(PathEntry, PathEntry.id == Scan.path_entry_id)
        .join(User, User.id == Scan.operator_user_id)
        .order_by(desc(Scan.started_at))
        .limit(25)
    ).all()
    recent_rows = [{
        "id": s.id, "started_at": s.started_at, "status": s.status,
        "file_count": s.file_count, "total_bytes": s.total_bytes,
        "new_file_count": s.new_file_count, "grew_count": s.grew_count,
        "shrunk_count": s.shrunk_count,
        "path": p.path, "label": p.label,
        "operator_username": u.username, "operator_full_name": u.full_name,
    } for s, p, u in recent]

    totals = {
        "scans_total":   db.session.scalar(select(func.count(Scan.id)).where(Scan.status == "completed")) or 0,
        "scans_today":   db.session.scalar(select(func.count(Scan.id)).where(
                            Scan.status == "completed",
                            func.date(Scan.started_at) == datetime.date.today())) or 0,
        "active_users":  db.session.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0,
        "path_entries":  db.session.scalar(select(func.count(PathEntry.id))) or 0,
    }

    # Per-path roll-up: most recent scan per path entry
    per_path = []
    paths = db.session.scalars(select(PathEntry).order_by(desc(PathEntry.entered_at))).all()
    for pe in paths:
        last = db.session.scalar(
            select(Scan).where(Scan.path_entry_id == pe.id)
            .order_by(desc(Scan.started_at)).limit(1)
        )
        entered_by = db.session.get(User, pe.entered_by) if pe.entered_by else None
        per_path.append({
            "id": pe.id, "path": pe.path, "label": pe.label,
            "entered_by_username": entered_by.username if entered_by else None,
            "last_scan_id": last.id if last else None,
            "last_started": last.started_at if last else None,
            "last_status": last.status if last else None,
            "file_count": last.file_count if last else None,
            "total_bytes": last.total_bytes if last else None,
            "new_file_count": last.new_file_count if last else 0,
            "grew_count": last.grew_count if last else 0,
            "shrunk_count": last.shrunk_count if last else 0,
        })

    on_duty = rotation.current_assignment()
    upcoming = rotation.upcoming_assignments(weeks=4)
    coverage = rotation.weekly_coverage(weeks=8)
    current_coverage = rotation.current_week_path_coverage()
    missed_weeks = [w for w in coverage if w["is_missed"]]
    partial_weeks = [w for w in coverage if w["is_partial"]]

    return render_template(
        "dashboard.html",
        recent=recent_rows, totals=totals, per_path=per_path,
        on_duty=on_duty, upcoming=upcoming,
        missed_weeks=missed_weeks, partial_weeks=partial_weeks,
        current_coverage=current_coverage,
        format_bytes=scanner.format_bytes,
    )


# ---------------------- Scan flow ----------------------

@bp.route("/scan/new")
@login_required
def scan_new():
    """Render the scan page; the actual scan happens in the browser via JS
    and POSTs results to /api/scan/submit."""
    on_duty = rotation.current_assignment()
    recent_paths = db.session.execute(
        select(PathEntry.path, PathEntry.label, func.max(PathEntry.entered_at).label("last"))
        .group_by(PathEntry.path, PathEntry.label)
        .order_by(desc("last"))
        .limit(10)
    ).all()
    recent_paths = [{"path": r.path, "label": r.label, "last_entered": r.last} for r in recent_paths]
    return render_template("scan_new.html", on_duty=on_duty, recent_paths=recent_paths)


@bp.route("/scan/<int:scan_id>")
@login_required
def scan_view(scan_id: int):
    scan = db.session.get(Scan, scan_id)
    if not scan:
        abort(404)
    pe = db.session.get(PathEntry, scan.path_entry_id)
    op = db.session.get(User, scan.operator_user_id)
    sched = db.session.get(User, scan.scheduled_user_id) if scan.scheduled_user_id else None
    files = db.session.scalars(
        select(FileSnapshot).where(FileSnapshot.scan_id == scan_id)
    ).all()

    files_data = []
    for f in files:
        day = f.mtime.strftime("%A") if f.mtime else ""
        human = f.mtime.strftime("%a, %b %d %Y · %H:%M") if f.mtime else ""
        files_data.append({
            "filename": f.filename, "relative_path": f.relative_path,
            "size_bytes": f.size_bytes,
            "prev_size_bytes": f.prev_size_bytes,
            "size_delta_bytes": f.size_delta_bytes,
            "is_new": f.is_new, "is_encrypted_named": f.is_encrypted_named,
            "mtime": f.mtime, "mtime_day": day, "mtime_human": human,
        })

    by_size = sorted(files_data, key=lambda f: f["size_bytes"], reverse=True)
    by_mtime = sorted(files_data, key=lambda f: f["mtime"] or datetime.datetime.min)
    plain = [f for f in files_data if not f["is_encrypted_named"]]
    encrypted = [f for f in files_data if f["is_encrypted_named"]]

    return render_template(
        "scan_view.html",
        scan=scan, path_entry=pe,
        operator={"id": op.id, "username": op.username, "full_name": op.full_name} if op else None,
        scheduled={"id": sched.id, "username": sched.username, "full_name": sched.full_name} if sched else None,
        by_size=by_size, by_mtime=by_mtime, plain=plain, encrypted=encrypted,
        format_bytes=scanner.format_bytes, format_delta=scanner.format_delta,
    )


@bp.route("/scans")
@login_required
def scan_list():
    rows = db.session.execute(
        select(Scan, PathEntry, User)
        .join(PathEntry, PathEntry.id == Scan.path_entry_id)
        .join(User, User.id == Scan.operator_user_id)
        .order_by(desc(Scan.started_at))
        .limit(200)
    ).all()
    scans = [{
        "id": s.id, "started_at": s.started_at, "status": s.status,
        "file_count": s.file_count, "total_bytes": s.total_bytes,
        "new_file_count": s.new_file_count, "grew_count": s.grew_count,
        "shrunk_count": s.shrunk_count,
        "path": p.path, "label": p.label,
        "operator_username": u.username, "operator_full_name": u.full_name,
    } for s, p, u in rows]
    return render_template("scan_list.html", scans=scans,
                           format_bytes=scanner.format_bytes)


# ---------------------- Exports ----------------------

@bp.route("/scan/<int:scan_id>/export.csv")
@login_required
def scan_export_csv(scan_id: int):
    data = exports.export_csv(scan_id)
    audit.record("export_csv", {"scan_id": scan_id})
    return Response(
        data, mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="vigil-scan-{scan_id}.csv"'},
    )


@bp.route("/scan/<int:scan_id>/export.pdf")
@login_required
def scan_export_pdf(scan_id: int):
    settings = settings_dict()
    cfg = current_app.config
    data = exports.export_pdf(
        scan_id,
        company_name=settings.get("company_name") or cfg.get("COMPANY_NAME", ""),
        footer_credit=settings.get("footer_credit") or cfg.get("FOOTER_CREDIT", ""),
        tagline=settings.get("tagline") or cfg.get("TAGLINE", "Backup integrity check."),
    )
    audit.record("export_pdf", {"scan_id": scan_id})
    return Response(
        data, mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="vigil-scan-{scan_id}.pdf"'},
    )


# ---------------------- Local dev fallback for logo serving ----------------------

@bp.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename: str):
    """Used only when S3 is not configured (local dev / Codespaces)."""
    from .storage import read_local_upload
    data = read_local_upload(filename)
    if data is None:
        abort(404)
    # Detect mime type roughly from extension
    ext = filename.rsplit(".", 1)[-1].lower()
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return Response(data, mimetype=mime, headers={"Cache-Control": "max-age=3600"})
