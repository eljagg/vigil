"""Database models.

Schema parity with Vigil 1.x SQLite, but rewritten for Postgres + SQLAlchemy.
"""
from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey,
    Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .extensions import db


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class User(db.Model):
    __tablename__ = "users"

    id:            Mapped[int]   = mapped_column(Integer, primary_key=True)
    username:      Mapped[str]   = mapped_column(String(64), unique=True, nullable=False)
    full_name:     Mapped[str]   = mapped_column(String(160), nullable=False)
    email:         Mapped[Optional[str]] = mapped_column(String(160))
    password_hash: Mapped[Optional[str]] = mapped_column(String(255))
    role:          Mapped[str]   = mapped_column(String(16), nullable=False, default="operator")
    auth_source:   Mapped[str]   = mapped_column(String(16), nullable=False, default="local")
    is_active:     Mapped[bool]  = mapped_column(Boolean, nullable=False, default=True)
    totp_secret:   Mapped[Optional[str]] = mapped_column(String(64))      # base32, set when 2FA enrolled
    totp_enabled:  Mapped[bool]  = mapped_column(Boolean, nullable=False, default=False)
    theme:         Mapped[str]   = mapped_column(String(8), nullable=False, default="system")
    created_at:    Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("role IN ('admin','operator')", name="ck_users_role"),
        CheckConstraint("auth_source IN ('local','ldap')", name="ck_users_auth_source"),
        CheckConstraint("theme IN ('system','light','dark')", name="ck_users_theme"),
    )


class RotationAssignment(db.Model):
    __tablename__ = "rotation_assignments"

    id:           Mapped[int]   = mapped_column(Integer, primary_key=True)
    user_id:      Mapped[int]   = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    week_start:   Mapped[datetime.date] = mapped_column(Date, nullable=False, unique=True)
    week_end:     Mapped[datetime.date] = mapped_column(Date, nullable=False)
    created_at:   Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_by:   Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))

    user = relationship("User", foreign_keys=[user_id])


class PathEntry(db.Model):
    """A path the operator selected via the browser folder picker.

    The 'path' is whatever string the browser hands us (folder name +
    relative subpath the user navigated into). It's just a label —
    file metadata is what actually drives the diff.
    """
    __tablename__ = "path_entries"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    path:       Mapped[str] = mapped_column(String(512), nullable=False)
    label:      Mapped[Optional[str]] = mapped_column(String(160))
    entered_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    entered_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # v2.0.6: expected-paths + notes
    is_expected:     Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expected_cadence: Mapped[Optional[str]] = mapped_column(String(16))  # 'daily','weekly','fortnightly'
    notes:           Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("ix_path_entries_path", "path"),
        CheckConstraint(
            "expected_cadence IS NULL OR expected_cadence IN ('daily','weekly','fortnightly')",
            name="ck_path_entries_cadence",
        ),
    )


class Scan(db.Model):
    __tablename__ = "scans"

    id:                 Mapped[int] = mapped_column(Integer, primary_key=True)
    path_entry_id:      Mapped[int] = mapped_column(ForeignKey("path_entries.id"), nullable=False)
    operator_user_id:   Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    scheduled_user_id:  Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    started_at:         Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at:       Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))
    status:             Mapped[str] = mapped_column(String(16), nullable=False)
    file_count:         Mapped[Optional[int]] = mapped_column(Integer)
    total_bytes:        Mapped[Optional[int]] = mapped_column(BigInteger)
    encrypted_count:    Mapped[Optional[int]] = mapped_column(Integer)
    plain_count:        Mapped[Optional[int]] = mapped_column(Integer)
    new_file_count:     Mapped[Optional[int]] = mapped_column(Integer)
    grew_count:         Mapped[Optional[int]] = mapped_column(Integer)
    shrunk_count:       Mapped[Optional[int]] = mapped_column(Integer)
    # v2.0.7: cumulative byte magnitudes per category (always non-negative).
    # new_bytes    = sum of size_bytes across files where is_new
    # grew_bytes   = sum of (size_bytes - prev_size_bytes) where size_delta_bytes > 0
    # shrunk_bytes = sum of (prev_size_bytes - size_bytes) where size_delta_bytes < 0
    new_bytes:          Mapped[Optional[int]] = mapped_column(BigInteger)
    grew_bytes:         Mapped[Optional[int]] = mapped_column(BigInteger)
    shrunk_bytes:       Mapped[Optional[int]] = mapped_column(BigInteger)
    error_message:      Mapped[Optional[str]] = mapped_column(Text)
    is_hidden:          Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    workstation:        Mapped[Optional[str]] = mapped_column(String(120))
    user_agent:         Mapped[Optional[str]] = mapped_column(String(255))

    path_entry = relationship("PathEntry")
    operator   = relationship("User", foreign_keys=[operator_user_id])
    scheduled  = relationship("User", foreign_keys=[scheduled_user_id])

    __table_args__ = (
        CheckConstraint("status IN ('running','completed','failed')", name="ck_scans_status"),
        Index("ix_scans_path_completed", "path_entry_id", "completed_at"),
    )


class FileSnapshot(db.Model):
    __tablename__ = "file_snapshots"

    id:                 Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id:            Mapped[int] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), nullable=False)
    relative_path:      Mapped[str] = mapped_column(String(1024), nullable=False)
    filename:           Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes:         Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime:              Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_encrypted_named: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prev_size_bytes:    Mapped[Optional[int]] = mapped_column(BigInteger)
    size_delta_bytes:   Mapped[Optional[int]] = mapped_column(BigInteger)
    is_new:             Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_snapshots_scan", "scan_id"),
        Index("ix_snapshots_scan_relpath", "scan_id", "relative_path"),
    )


class Setting(db.Model):
    __tablename__ = "settings"

    key:        Mapped[str] = mapped_column(String(64), primary_key=True)
    value:      Mapped[Optional[str]] = mapped_column(Text)
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp:  Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    user_id:    Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    username:   Mapped[Optional[str]] = mapped_column(String(64))
    action:     Mapped[str] = mapped_column(String(64), nullable=False)
    details:    Mapped[Optional[str]] = mapped_column(Text)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))


def settings_dict() -> dict:
    """Return all settings as {key: value}. Used by the template context."""
    return {s.key: s.value for s in db.session.query(Setting).all()}


def upsert_setting(key: str, value: str, user_id: Optional[int]) -> None:
    s = db.session.get(Setting, key)
    if s:
        s.value = value
        s.updated_by = user_id
        s.updated_at = _now()
    else:
        db.session.add(Setting(key=key, value=value, updated_by=user_id))
