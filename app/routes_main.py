"""Main user-facing routes."""
from __future__ import annotations

import datetime
from collections import defaultdict

from flask import (
    Blueprint, Response, abort, current_app, flash, g, jsonify,
    redirect, render_template, request, url_for,
)
from sqlalchemy import desc, func, select

from . import audit, exports, rotation, scanner
from .auth import login_required, admin_required
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


# ---------------------- Public info pages ----------------------

# Vigil's published version. Update on every release.
VIGIL_VERSION = "2.1.2"


@bp.route("/about")
def about():
    """Public 'About Vigil' page — what the app is, who built it, version.
    Intentionally accessible without login so auditors and reviewers can see it."""
    return render_template("about.html", version=VIGIL_VERSION)


@bp.route("/data-handling")
def data_handling():
    """Public 'Data Handling' assurance page — what Vigil reads, what it does
    NOT read, where data goes, and how to verify each claim independently.
    Intentionally accessible without login."""
    return render_template("data_handling.html", version=VIGIL_VERSION)


# ---------------------- Dashboard helpers ----------------------

def _storage_trend(days: int = 14) -> list[dict]:
    """Total bytes scanned per day for the last N days, for the sparkline."""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days - 1)

    rows = db.session.execute(
        select(
            func.date(Scan.started_at).label("day"),
            func.sum(Scan.total_bytes).label("bytes"),
        )
        .where(
            Scan.status == "completed",
            Scan.is_hidden.is_(False),
            func.date(Scan.started_at) >= start,
            func.date(Scan.started_at) <= today,
        )
        .group_by("day")
    ).all()

    by_day = {r.day: int(r.bytes or 0) for r in rows}

    out = []
    for i in range(days):
        d = start + datetime.timedelta(days=i)
        out.append({"date": d, "bytes": by_day.get(d, 0)})
    return out


def _anomalies(threshold_pct: float = 20.0, limit: int = 10) -> list[dict]:
    """Files in the most recent completed scan per path with size deltas
    exceeding the threshold percent vs prior scan."""
    out = []

    # Most recent completed, non-hidden scan per path
    paths = db.session.scalars(select(PathEntry)).all()
    for pe in paths:
        scan = db.session.scalar(
            select(Scan)
            .where(Scan.path_entry_id == pe.id,
                   Scan.status == "completed",
                   Scan.is_hidden.is_(False))
            .order_by(desc(Scan.started_at))
            .limit(1)
        )
        if not scan:
            continue
        # Pull anomalous file snapshots from this scan
        snaps = db.session.scalars(
            select(FileSnapshot).where(FileSnapshot.scan_id == scan.id)
        ).all()
        for f in snaps:
            if f.prev_size_bytes is None or f.prev_size_bytes == 0:
                continue
            if f.size_delta_bytes is None:
                continue
            pct = (abs(f.size_delta_bytes) / f.prev_size_bytes) * 100.0
            if pct < threshold_pct:
                continue
            out.append({
                "scan_id": scan.id,
                "path_label": pe.label or pe.path,
                "filename": f.filename,
                "relative_path": f.relative_path,
                "prev_size": f.prev_size_bytes,
                "current_size": f.size_bytes,
                "delta_bytes": f.size_delta_bytes,
                "pct": pct,
                "direction": "grew" if f.size_delta_bytes > 0 else "shrunk",
            })

    out.sort(key=lambda x: x["pct"], reverse=True)
    return out[:limit]


def _week_counters() -> dict:
    """Counters for the current calendar week (Mon-Sun).

    "Paths covered" is computed against the *expected* path list when
    any expected paths are configured, else falls back to all known
    paths (legacy behaviour, useful before any expected paths are set).
    """
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    sunday = monday + datetime.timedelta(days=6)

    scans_this_week = db.session.scalar(
        select(func.count(Scan.id)).where(
            Scan.status == "completed",
            Scan.is_hidden.is_(False),
            func.date(Scan.started_at) >= monday,
            func.date(Scan.started_at) <= sunday,
        )
    ) or 0

    expected_count = db.session.scalar(
        select(func.count(PathEntry.id)).where(PathEntry.is_expected.is_(True))
    ) or 0
    use_expected = expected_count > 0

    if use_expected:
        # Paths covered: distinct expected path_entry_ids scanned this week
        paths_covered = db.session.scalar(
            select(func.count(func.distinct(Scan.path_entry_id)))
            .join(PathEntry, PathEntry.id == Scan.path_entry_id)
            .where(
                PathEntry.is_expected.is_(True),
                Scan.status == "completed",
                Scan.is_hidden.is_(False),
                func.date(Scan.started_at) >= monday,
                func.date(Scan.started_at) <= sunday,
            )
        ) or 0
        paths_total = expected_count
    else:
        paths_covered = db.session.scalar(
            select(func.count(func.distinct(Scan.path_entry_id))).where(
                Scan.status == "completed",
                Scan.is_hidden.is_(False),
                func.date(Scan.started_at) >= monday,
                func.date(Scan.started_at) <= sunday,
            )
        ) or 0
        paths_total = db.session.scalar(select(func.count(PathEntry.id))) or 0

    scans_today = db.session.scalar(
        select(func.count(Scan.id)).where(
            Scan.status == "completed",
            Scan.is_hidden.is_(False),
            func.date(Scan.started_at) == today,
        )
    ) or 0

    return {
        "scans_this_week": scans_this_week,
        "paths_covered": paths_covered,
        "paths_total": paths_total,
        "scans_today": scans_today,
        "expected_only": use_expected,
    }


# Cadence → maximum tolerated gap (in days) since the last completed scan
_CADENCE_DAYS = {
    "daily":       1,
    "weekly":      7,
    "fortnightly": 14,
}


def _overdue_paths() -> list[dict]:
    """Expected paths whose last completed scan exceeds their cadence's grace
    window — or which have never been scanned at all. Sorted worst-first."""
    today = datetime.datetime.now(datetime.UTC)
    out = []
    expected = db.session.scalars(
        select(PathEntry).where(PathEntry.is_expected.is_(True))
    ).all()
    for pe in expected:
        max_days = _CADENCE_DAYS.get(pe.expected_cadence or "weekly", 7)
        last = db.session.scalar(
            select(Scan).where(
                Scan.path_entry_id == pe.id,
                Scan.status == "completed",
                Scan.is_hidden.is_(False),
            ).order_by(desc(Scan.started_at)).limit(1)
        )
        if last is None:
            out.append({
                "id": pe.id, "label": pe.label or pe.path,
                "path": pe.path, "cadence": pe.expected_cadence or "weekly",
                "last_scan_at": None,
                "days_since": None,
                "max_days": max_days,
                "severity": "never",
            })
            continue
        # Make 'last.started_at' timezone-aware for comparison
        last_at = last.started_at
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=datetime.UTC)
        delta = today - last_at
        days_since = delta.total_seconds() / 86400.0
        if days_since > max_days:
            severity = "critical" if days_since > max_days * 2 else "overdue"
            out.append({
                "id": pe.id, "label": pe.label or pe.path,
                "path": pe.path, "cadence": pe.expected_cadence or "weekly",
                "last_scan_at": last_at,
                "days_since": days_since,
                "max_days": max_days,
                "severity": severity,
            })
    # Worst-first ordering: never-scanned, then most-overdue, then by cadence severity
    severity_rank = {"never": 0, "critical": 1, "overdue": 2}
    out.sort(key=lambda r: (severity_rank[r["severity"]], -(r["days_since"] or 1e9)))
    return out


# ---------------------- Dashboard ----------------------

@bp.route("/")
@login_required
def dashboard():
    # Recent scans (excluding hidden)
    recent = db.session.execute(
        select(Scan, PathEntry, User)
        .join(PathEntry, PathEntry.id == Scan.path_entry_id)
        .join(User, User.id == Scan.operator_user_id)
        .where(Scan.is_hidden.is_(False))
        .order_by(desc(Scan.started_at))
        .limit(25)
    ).all()
    recent_rows = [{
        "id": s.id, "started_at": s.started_at, "status": s.status,
        "file_count": s.file_count, "total_bytes": s.total_bytes,
        "new_file_count": s.new_file_count, "grew_count": s.grew_count,
        "shrunk_count": s.shrunk_count,
        "new_bytes": s.new_bytes, "grew_bytes": s.grew_bytes,
        "shrunk_bytes": s.shrunk_bytes,
        "workstation": s.workstation,
        "path": p.path, "label": p.label,
        "operator_username": u.username, "operator_full_name": u.full_name,
    } for s, p, u in recent]

    counters = _week_counters()
    storage_trend = _storage_trend(days=14)
    anomalies = _anomalies(threshold_pct=20.0, limit=10)
    overdue = _overdue_paths()

    # Per-path roll-up: most recent non-hidden scan per path entry
    per_path = []
    paths = db.session.scalars(select(PathEntry).order_by(desc(PathEntry.entered_at))).all()
    for pe in paths:
        last = db.session.scalar(
            select(Scan).where(Scan.path_entry_id == pe.id,
                               Scan.is_hidden.is_(False))
            .order_by(desc(Scan.started_at)).limit(1)
        )
        entered_by = db.session.get(User, pe.entered_by) if pe.entered_by else None
        per_path.append({
            "id": pe.id, "path": pe.path, "label": pe.label,
            "is_expected": pe.is_expected,
            "expected_cadence": pe.expected_cadence,
            "notes": pe.notes,
            "entered_by_username": entered_by.username if entered_by else None,
            "last_scan_id": last.id if last else None,
            "last_started": last.started_at if last else None,
            "last_status": last.status if last else None,
            "last_workstation": last.workstation if last else None,
            "file_count": last.file_count if last else None,
            "total_bytes": last.total_bytes if last else None,
            "new_file_count": last.new_file_count if last else 0,
            "grew_count": last.grew_count if last else 0,
            "shrunk_count": last.shrunk_count if last else 0,
            "new_bytes":    last.new_bytes    if last else 0,
            "grew_bytes":   last.grew_bytes   if last else 0,
            "shrunk_bytes": last.shrunk_bytes if last else 0,
        })

    on_duty = rotation.current_assignment()
    upcoming = rotation.upcoming_assignments(weeks=4)
    coverage = rotation.weekly_coverage(weeks=8)
    current_coverage = rotation.current_week_path_coverage()
    missed_weeks = [w for w in coverage if w["is_missed"]]
    partial_weeks = [w for w in coverage if w["is_partial"]]

    return render_template(
        "dashboard.html",
        recent=recent_rows, counters=counters,
        storage_trend=storage_trend, anomalies=anomalies,
        overdue=overdue,
        per_path=per_path,
        on_duty=on_duty, upcoming=upcoming,
        missed_weeks=missed_weeks, partial_weeks=partial_weeks,
        current_coverage=current_coverage,
        format_bytes=scanner.format_bytes,
    )


# ---------------------- Scan flow ----------------------

@bp.route("/scan/new")
@login_required
def scan_new():
    on_duty = rotation.current_assignment()
    recent_paths = db.session.execute(
        select(PathEntry.path, PathEntry.label, func.max(PathEntry.entered_at).label("last"))
        .group_by(PathEntry.path, PathEntry.label)
        .order_by(desc("last"))
        .limit(10)
    ).all()
    recent_paths = [{"path": r.path, "label": r.label, "last_entered": r.last} for r in recent_paths]

    # Rescan pre-fill: if ?from_scan=N is given, pull label + workstation from that scan.
    prefill = {"label": "", "workstation": "", "path_hint": "",
               "from_scan_id": None, "notes": "", "path_entry_id": None}
    from_scan = request.args.get("from_scan", type=int)
    if from_scan:
        prior = db.session.get(Scan, from_scan)
        if prior:
            prior_pe = db.session.get(PathEntry, prior.path_entry_id)
            if prior_pe:
                prefill["label"] = prior_pe.label or ""
                prefill["path_hint"] = prior_pe.path or ""
                prefill["notes"] = prior_pe.notes or ""
                prefill["path_entry_id"] = prior_pe.id
            prefill["workstation"] = prior.workstation or ""
            prefill["from_scan_id"] = prior.id

    return render_template(
        "scan_new.html", on_duty=on_duty,
        recent_paths=recent_paths, prefill=prefill,
    )


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
            "job_stem": scanner.extract_job_stem(f.filename),
        })

    by_size = sorted(files_data, key=lambda f: f["size_bytes"], reverse=True)
    by_mtime = sorted(files_data, key=lambda f: f["mtime"] or datetime.datetime.min)
    plain = [f for f in files_data if not f["is_encrypted_named"]]

    # v2.0.9: count distinct subfolders to surface "walks subfolders" reassurance.
    # A file at the picked-folder root has no '/' in its relative_path; nested
    # files do. We count both the picked folder itself and any deeper folders.
    folders = {""}  # the picked folder root counts as one
    max_depth = 0
    for f in files_data:
        rel = f.get("relative_path") or ""
        if "/" in rel:
            parent = rel.rsplit("/", 1)[0]
            folders.add(parent)
            depth = parent.count("/") + 1
            if depth > max_depth:
                max_depth = depth
    folder_count = len(folders)

    # Group by backup job stem
    by_job: dict[str, list[dict]] = defaultdict(list)
    for f in files_data:
        by_job[f["job_stem"]].append(f)
    # Order each group's files by mtime desc, and order groups by total size desc
    for stem in by_job:
        by_job[stem].sort(key=lambda f: f["mtime"] or datetime.datetime.min, reverse=True)
    by_job_groups = sorted(
        [
            {
                "stem": stem,
                "files": items,
                "count": len(items),
                "total_bytes": sum(f["size_bytes"] for f in items),
                "latest_mtime": max((f["mtime"] for f in items if f["mtime"]), default=None),
                "newest_size": items[0]["size_bytes"] if items else 0,
                "any_plain": any(not f["is_encrypted_named"] for f in items),
            }
            for stem, items in by_job.items()
        ],
        key=lambda g: g["total_bytes"], reverse=True,
    )

    return render_template(
        "scan_view.html",
        scan=scan, path_entry=pe,
        operator={"id": op.id, "username": op.username, "full_name": op.full_name} if op else None,
        scheduled={"id": sched.id, "username": sched.username, "full_name": sched.full_name} if sched else None,
        by_size=by_size, by_mtime=by_mtime, plain=plain,
        by_job_groups=by_job_groups,
        folder_count=folder_count, max_depth=max_depth,
        format_bytes=scanner.format_bytes, format_delta=scanner.format_delta,
    )


@bp.route("/scan/<int:scan_id>/files-fragment")
@login_required
def scan_files_fragment(scan_id: int):
    """Returns a small HTML fragment listing files for a scan, suitable for
    inline expansion on the dashboard rollup."""
    scan = db.session.get(Scan, scan_id)
    if not scan:
        abort(404)
    files = db.session.scalars(
        select(FileSnapshot).where(FileSnapshot.scan_id == scan_id)
        .order_by(desc(FileSnapshot.size_bytes))
        .limit(50)
    ).all()
    total = db.session.scalar(
        select(func.count(FileSnapshot.id)).where(FileSnapshot.scan_id == scan_id)
    ) or 0
    return render_template(
        "_files_fragment.html",
        scan=scan, files=files, total=total,
        format_bytes=scanner.format_bytes, format_delta=scanner.format_delta,
    )


@bp.route("/scan/<int:scan_id>/hide", methods=["POST"])
@admin_required
def scan_hide(scan_id: int):
    scan = db.session.get(Scan, scan_id)
    if not scan:
        abort(404)
    scan.is_hidden = True
    db.session.commit()
    audit.record("scan_hidden", {"scan_id": scan_id})
    flash(f"Scan #{scan_id} hidden from dashboard.", "success")
    return redirect(request.referrer or url_for("main.dashboard"))


@bp.route("/scan/<int:scan_id>/unhide", methods=["POST"])
@admin_required
def scan_unhide(scan_id: int):
    scan = db.session.get(Scan, scan_id)
    if not scan:
        abort(404)
    scan.is_hidden = False
    db.session.commit()
    audit.record("scan_unhidden", {"scan_id": scan_id})
    flash(f"Scan #{scan_id} restored to dashboard.", "success")
    return redirect(request.referrer or url_for("main.scan_list"))


@bp.route("/scans")
@login_required
def scan_list():
    show_hidden = request.args.get("show_hidden") == "1"
    q = (
        select(Scan, PathEntry, User)
        .join(PathEntry, PathEntry.id == Scan.path_entry_id)
        .join(User, User.id == Scan.operator_user_id)
        .order_by(desc(Scan.started_at))
        .limit(200)
    )
    if not show_hidden:
        q = q.where(Scan.is_hidden.is_(False))
    rows = db.session.execute(q).all()
    scans = [{
        "id": s.id, "started_at": s.started_at, "status": s.status,
        "file_count": s.file_count, "total_bytes": s.total_bytes,
        "new_file_count": s.new_file_count, "grew_count": s.grew_count,
        "shrunk_count": s.shrunk_count,
        "new_bytes": s.new_bytes, "grew_bytes": s.grew_bytes,
        "shrunk_bytes": s.shrunk_bytes,
        "is_hidden": s.is_hidden,
        "workstation": s.workstation,
        "path": p.path, "label": p.label,
        "operator_username": u.username, "operator_full_name": u.full_name,
    } for s, p, u in rows]
    return render_template("scan_list.html", scans=scans, show_hidden=show_hidden,
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
    ext = filename.rsplit(".", 1)[-1].lower()
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return Response(data, mimetype=mime, headers={"Cache-Control": "max-age=3600"})
