"""Weekly rotation logic.

Each user covers a calendar week (Monday→Sunday). Rotation is auto-
generated round-robin from active operators, with manual overrides.
"""
from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy import and_, desc, func, select

from .extensions import db
from .models import PathEntry, RotationAssignment, Scan, User


def week_bounds(d: datetime.date) -> tuple[datetime.date, datetime.date]:
    monday = d - datetime.timedelta(days=d.weekday())
    sunday = monday + datetime.timedelta(days=6)
    return monday, sunday


def current_assignment() -> Optional[dict]:
    today = datetime.date.today()
    row = db.session.execute(
        select(RotationAssignment, User)
        .join(User, User.id == RotationAssignment.user_id)
        .where(RotationAssignment.week_start <= today,
               RotationAssignment.week_end >= today)
        .order_by(desc(RotationAssignment.week_start))
        .limit(1)
    ).first()
    if not row:
        return None
    ra, user = row
    return {
        "id": user.id, "username": user.username,
        "full_name": user.full_name, "email": user.email,
        "week_start": ra.week_start, "week_end": ra.week_end,
    }


def upcoming_assignments(weeks: int = 8) -> list[dict]:
    today = datetime.date.today()
    rows = db.session.execute(
        select(RotationAssignment, User)
        .join(User, User.id == RotationAssignment.user_id)
        .where(RotationAssignment.week_end >= today)
        .order_by(RotationAssignment.week_start)
        .limit(weeks)
    ).all()
    return [{
        "id": ra.id, "week_start": ra.week_start, "week_end": ra.week_end,
        "username": u.username, "full_name": u.full_name,
    } for ra, u in rows]


def all_assignments(limit: int = 52) -> list[dict]:
    rows = db.session.execute(
        select(RotationAssignment, User)
        .join(User, User.id == RotationAssignment.user_id)
        .order_by(desc(RotationAssignment.week_start))
        .limit(limit)
    ).all()
    return [{
        "id": ra.id, "week_start": ra.week_start, "week_end": ra.week_end,
        "username": u.username, "full_name": u.full_name,
    } for ra, u in rows]


def assign_week(user_id: int, week_start: datetime.date,
                created_by: int) -> None:
    monday, sunday = week_bounds(week_start)
    existing = db.session.scalar(
        select(RotationAssignment).where(RotationAssignment.week_start == monday)
    )
    if existing:
        existing.user_id = user_id
        existing.week_end = sunday
        existing.created_by = created_by
    else:
        db.session.add(RotationAssignment(
            user_id=user_id, week_start=monday, week_end=sunday,
            created_by=created_by,
        ))
    db.session.commit()


def autogenerate(weeks: int, created_by: int,
                 start_from: Optional[datetime.date] = None) -> int:
    operators = db.session.scalars(
        select(User).where(User.is_active.is_(True),
                           User.role.in_(["admin", "operator"]))
        .order_by(User.id)
    ).all()
    if not operators:
        return 0
    op_ids = [u.id for u in operators]

    last = db.session.scalar(
        select(RotationAssignment).order_by(desc(RotationAssignment.week_start))
    )
    if last and last.user_id in op_ids:
        idx = (op_ids.index(last.user_id) + 1) % len(op_ids)
    else:
        idx = 0

    monday = start_from or datetime.date.today()
    monday, _ = week_bounds(monday)

    created = 0
    for _ in range(weeks):
        existing = db.session.scalar(
            select(RotationAssignment).where(RotationAssignment.week_start == monday)
        )
        if not existing:
            sunday = monday + datetime.timedelta(days=6)
            db.session.add(RotationAssignment(
                user_id=op_ids[idx], week_start=monday, week_end=sunday,
                created_by=created_by,
            ))
            created += 1
            idx = (idx + 1) % len(op_ids)
        monday += datetime.timedelta(days=7)

    db.session.commit()
    return created


def weekly_coverage(weeks: int = 8) -> list[dict]:
    """For each recent week return assignment + scan counts so the
    dashboard can flag missed weeks."""
    today = datetime.date.today()
    rows = db.session.execute(
        select(RotationAssignment, User)
        .join(User, User.id == RotationAssignment.user_id)
        .where(RotationAssignment.week_start <= today)
        .order_by(desc(RotationAssignment.week_start))
        .limit(weeks)
    ).all()

    out = []
    for ra, user in rows:
        scan_count = db.session.scalar(
            select(func.count(Scan.id)).where(
                Scan.status == "completed",
                func.date(Scan.started_at) >= ra.week_start,
                func.date(Scan.started_at) <= ra.week_end,
            )
        ) or 0
        scans_by_assigned = db.session.scalar(
            select(func.count(Scan.id)).where(
                Scan.status == "completed",
                Scan.operator_user_id == ra.user_id,
                func.date(Scan.started_at) >= ra.week_start,
                func.date(Scan.started_at) <= ra.week_end,
            )
        ) or 0
        is_current = ra.week_start <= today <= ra.week_end
        is_past = ra.week_end < today
        out.append({
            "week_start": ra.week_start, "week_end": ra.week_end,
            "assigned_username": user.username,
            "assigned_full_name": user.full_name,
            "scan_count": scan_count,
            "scans_by_assigned": scans_by_assigned,
            "is_current": is_current,
            "is_past": is_past,
            "is_missed": is_past and scan_count == 0,
            "is_partial": is_past and scan_count > 0 and scans_by_assigned == 0,
        })
    return out


def current_week_path_coverage() -> dict:
    today = datetime.date.today()
    monday, sunday = week_bounds(today)

    # Match dashboard counter logic: when expected paths exist, scope to
    # those; else fall back to all known paths.
    expected_count = db.session.scalar(
        select(func.count(PathEntry.id)).where(PathEntry.is_expected.is_(True))
    ) or 0

    if expected_count > 0:
        total = expected_count
        scanned = db.session.scalar(
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
        uncovered_q = select(PathEntry).where(
            PathEntry.is_expected.is_(True),
            ~PathEntry.id.in_(
                select(Scan.path_entry_id).where(
                    Scan.status == "completed",
                    Scan.is_hidden.is_(False),
                    func.date(Scan.started_at) >= monday,
                    func.date(Scan.started_at) <= sunday,
                )
            )
        ).order_by(desc(PathEntry.entered_at))
    else:
        total = db.session.scalar(select(func.count(PathEntry.id))) or 0
        scanned = db.session.scalar(
            select(func.count(func.distinct(Scan.path_entry_id))).where(
                Scan.status == "completed",
                Scan.is_hidden.is_(False),
                func.date(Scan.started_at) >= monday,
                func.date(Scan.started_at) <= sunday,
            )
        ) or 0
        uncovered_q = select(PathEntry).where(
            ~PathEntry.id.in_(
                select(Scan.path_entry_id).where(
                    Scan.status == "completed",
                    Scan.is_hidden.is_(False),
                    func.date(Scan.started_at) >= monday,
                    func.date(Scan.started_at) <= sunday,
                )
            )
        ).order_by(desc(PathEntry.entered_at))

    uncovered = db.session.execute(uncovered_q).scalars().all()

    return {
        "total": total, "scanned": scanned, "uncovered": total - scanned,
        "uncovered_paths": [{"id": p.id, "path": p.path, "label": p.label} for p in uncovered],
        "week_start": monday, "week_end": sunday,
    }
