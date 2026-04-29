"""Scan ingestion + diff.

The browser walks the folder client-side and POSTs a JSON payload of
file metadata. This module ingests that payload, computes the delta
against the previous scan of the same path, and writes everything to
the database.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import desc, select

from .extensions import db
from .models import FileSnapshot, PathEntry, Scan


# Common archive / encrypted extensions, stripped first
_EXT_RE = re.compile(
    r'(\.bak_encrypted|\.bak|\.sql|\.gz|\.zip|\.tar|\.tgz|\.7z'
    r'|\.rar|\.dump|\.enc|\.encrypted|\.full|\.diff|\.trn)$',
    re.IGNORECASE,
)

# Generic short extension fallback (1-5 alphanumeric chars after a dot)
_GENERIC_EXT_RE = re.compile(r'\.[A-Za-z0-9]{1,5}$')

# Trailing date / timestamp suffix on a backup filename:
#   - YYYY-MM-DD / YYYY_MM_DD / YYYYMMDD
#   - 8+ digits (timestamp)
#   - bare year 19xx or 20xx
_DATE_SUFFIX_RE = re.compile(
    r'[_\-\.]'
    r'(?:'
    r'(?:19|20)\d{2}[\-_/]?\d{2}[\-_/]?\d{2}'   # full date
    r'|\d{8,14}'                                  # plain digit run
    r'|(?:19|20)\d{2}'                            # bare year
    r')'
    r'(?:[_\-\.]\d{1,6})*'                       # optional time / sequence
    r'.*$'
)


def extract_job_stem(filename: str) -> str:
    """Best-effort extraction of the backup job 'stem' from a filename.

    Strips common archive extensions and trailing dates, leaving the
    name of the backup job itself. Examples:

        Sage_Owner_2026-04-28.bak_encrypted  -> Sage_Owner
        db_full_20260428_120000.sql.gz       -> db_full
        differential.bak                     -> differential
        weekly_full_2026-04-28.bak_encrypted -> weekly_full
        sql_log_2026.bak_encrypted           -> sql_log

    Falls back to the original filename if no pattern matches.
    """
    name = filename
    # Strip known backup-style extensions first (handles nested like .sql.gz)
    for _ in range(5):
        new = _EXT_RE.sub('', name)
        if new == name:
            break
        name = new
    # Strip one generic extension (e.g. .txt, .log)
    name = _GENERIC_EXT_RE.sub('', name)
    # Strip trailing date / timestamp / year suffix
    m = _DATE_SUFFIX_RE.search(name)
    if m:
        name = name[:m.start()]
    name = name.rstrip('_-. ')
    return name or filename


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
                scheduled_user_id: Optional[int],
                workstation: Optional[str] = None,
                user_agent: Optional[str] = None) -> Scan:
    """Create a Scan + FileSnapshots, computing diff against the prior scan
    of the same path entry."""
    prev = previous_scan(path_entry.id)
    prev_sizes = previous_sizes(prev.id) if prev else {}

    scan = Scan(
        path_entry_id=path_entry.id,
        operator_user_id=operator_user_id,
        scheduled_user_id=scheduled_user_id,
        status="running",
        workstation=workstation,
        user_agent=user_agent,
    )
    db.session.add(scan)
    db.session.flush()  # need scan.id for snapshots

    file_count = total_bytes = encrypted_count = plain_count = 0
    new_count = grew = shrunk = 0
    new_bytes = grew_bytes = shrunk_bytes = 0

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
                grew_bytes += delta
            elif delta < 0:
                shrunk += 1
                shrunk_bytes += -delta  # store as positive magnitude
        else:
            new_count += 1
            new_bytes += f.size_bytes

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
    scan.new_bytes = new_bytes
    scan.grew_bytes = grew_bytes
    scan.shrunk_bytes = shrunk_bytes

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
