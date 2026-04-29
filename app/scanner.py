"""Scan ingestion + diff.

The browser walks the folder client-side and POSTs a JSON payload of
file metadata. This module ingests that payload, computes the delta
against the previous scan of the same path, and writes everything to
the database.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import desc, select

from .extensions import db
from .models import FileSnapshot, PathEntry, Scan


@dataclass
class IncomingFile:
    """One file in the browser's scan payload."""
    relative_path: str
    filename: str
    size_bytes: int
    mtime: datetime.datetime

    @property
    def is_encrypted_named(self) -> bool:
        return "_encrypted" in self.filename.lower()


def parse_payload(items: list[dict]) -> list[IncomingFile]:
    """Validate the browser payload and convert to IncomingFile."""
    out = []
    for it in items:
        rel = (it.get("relative_path") or "").strip()
        name = (it.get("filename") or "").strip()
        size = it.get("size_bytes")
        mtime_raw = it.get("mtime")  # ISO 8601 from JS Date.toISOString()

        if not rel or not name or size is None or mtime_raw is None:
            continue
        try:
            size_int = int(size)
        except (TypeError, ValueError):
            continue
        try:
            # JS toISOString() produces "...Z"; fromisoformat handles it on 3.11+
            mtime_dt = datetime.datetime.fromisoformat(mtime_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue

        out.append(IncomingFile(
            relative_path=rel, filename=name,
            size_bytes=size_int, mtime=mtime_dt,
        ))
    return out


def previous_scan(path_entry_id: int) -> Optional[Scan]:
    return db.session.scalar(
        select(Scan)
        .where(Scan.path_entry_id == path_entry_id, Scan.status == "completed")
        .order_by(desc(Scan.completed_at))
        .limit(1)
    )


def previous_sizes(scan_id: int) -> dict[str, int]:
    rows = db.session.execute(
        select(FileSnapshot.relative_path, FileSnapshot.size_bytes)
        .where(FileSnapshot.scan_id == scan_id)
    ).all()
    return {r[0]: r[1] for r in rows}


def ingest_scan(path_entry: PathEntry, files: list[IncomingFile],
                operator_user_id: int,
                scheduled_user_id: Optional[int]) -> Scan:
    """Create a Scan + FileSnapshots, computing diff against the prior scan
    of the same path entry."""
    prev = previous_scan(path_entry.id)
    prev_sizes = previous_sizes(prev.id) if prev else {}

    scan = Scan(
        path_entry_id=path_entry.id,
        operator_user_id=operator_user_id,
        scheduled_user_id=scheduled_user_id,
        status="running",
    )
    db.session.add(scan)
    db.session.flush()  # need scan.id for snapshots

    file_count = total_bytes = encrypted_count = plain_count = 0
    new_count = grew = shrunk = 0

    for f in files:
        file_count += 1
        total_bytes += f.size_bytes
        if f.is_encrypted_named:
            encrypted_count += 1
        else:
            plain_count += 1

        prev_size = prev_sizes.get(f.relative_path)
        is_new = prev_size is None
        delta: Optional[int] = None
        if not is_new:
            delta = f.size_bytes - prev_size
            if delta > 0:
                grew += 1
            elif delta < 0:
                shrunk += 1
        else:
            new_count += 1

        db.session.add(FileSnapshot(
            scan_id=scan.id,
            relative_path=f.relative_path,
            filename=f.filename,
            size_bytes=f.size_bytes,
            mtime=f.mtime,
            is_encrypted_named=f.is_encrypted_named,
            prev_size_bytes=prev_size,
            size_delta_bytes=delta,
            is_new=is_new,
        ))

    scan.completed_at = datetime.datetime.now(datetime.UTC)
    scan.status = "completed"
    scan.file_count = file_count
    scan.total_bytes = total_bytes
    scan.encrypted_count = encrypted_count
    scan.plain_count = plain_count
    scan.new_file_count = new_count
    scan.grew_count = grew
    scan.shrunk_count = shrunk

    db.session.commit()
    return scan


# ----------------------------- formatting helpers -----------------------------

def format_bytes(size: Optional[int]) -> str:
    if size is None:
        return "—"
    s = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(s) < 1024.0:
            return f"{s:.2f} {unit}"
        s /= 1024.0
    return f"{s:.2f} EB"


def format_delta(delta: Optional[int]) -> str:
    if delta is None:
        return "—"
    sign = "+" if delta > 0 else ("−" if delta < 0 else "±")
    return f"{sign}{format_bytes(abs(delta))}"
