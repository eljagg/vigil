"""Admin routes: users, rotation, settings, audit log, branding."""
from __future__ import annotations

import datetime

from flask import (
    Blueprint, current_app, flash, g, redirect, render_template, request,
    url_for,
)
from sqlalchemy import desc, select
from werkzeug.utils import secure_filename

from . import audit, rotation
from .auth import admin_required, hash_password
from .extensions import db
from .models import AuditLog, User, settings_dict, upsert_setting
from .storage import upload_logo


bp = Blueprint("admin", __name__)


# ---------------------- Users ----------------------

@bp.route("/users")
@admin_required
def users():
    users = db.session.scalars(select(User).order_by(User.username)).all()
    return render_template("admin/users.html", users=users)


@bp.route("/users/new", methods=["POST"])
@admin_required
def users_new():
    username = (request.form.get("username") or "").strip()
    full_name = (request.form.get("full_name") or "").strip()
    email = (request.form.get("email") or "").strip() or None
    role = request.form.get("role", "operator")
    auth_source = request.form.get("auth_source", "local")
    password = request.form.get("password") or ""

    if not username or not full_name or role not in ("admin", "operator"):
        flash("Username, full name, and a valid role are required.", "danger")
        return redirect(url_for("admin.users"))
    if auth_source == "local" and len(password) < 12:
        flash("Local-account passwords must be at least 12 characters.", "danger")
        return redirect(url_for("admin.users"))

    try:
        u = User(
            username=username, full_name=full_name, email=email,
            role=role, auth_source=auth_source,
            password_hash=hash_password(password) if auth_source == "local" else None,
        )
        db.session.add(u)
        db.session.commit()
        audit.record("user_created", {
            "new_username": username, "role": role, "auth_source": auth_source,
        })
        flash(f"User {username} created.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Could not create user: {e}", "danger")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def users_toggle(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("admin.users"))
    if user_id == g.user.id:
        flash("You cannot deactivate your own account.", "warning")
        return redirect(url_for("admin.users"))
    user.is_active = not user.is_active
    db.session.commit()
    audit.record("user_toggle_active", {
        "target_username": user.username, "is_active": user.is_active,
    })
    flash(f"User {user.username} {'activated' if user.is_active else 'deactivated'}.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def users_reset_pw(user_id: int):
    new_pw = request.form.get("new_password") or ""
    if len(new_pw) < 12:
        flash("New password must be at least 12 characters.", "danger")
        return redirect(url_for("admin.users"))
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("admin.users"))
    if user.auth_source != "local":
        flash("LDAP users cannot have local passwords reset.", "warning")
        return redirect(url_for("admin.users"))
    user.password_hash = hash_password(new_pw)
    db.session.commit()
    audit.record("user_password_reset", {"target_username": user.username})
    flash(f"Password reset for {user.username}.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/edit", methods=["POST"])
@admin_required
def users_edit(user_id: int):
    """Edit a user's full_name and/or email. Username is intentionally
    immutable — it's referenced by audit logs and (potentially) by external
    systems, and renaming creates more risk than benefit."""
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("admin.users"))

    new_full_name = (request.form.get("full_name") or "").strip()
    new_email = (request.form.get("email") or "").strip() or None

    if not new_full_name:
        flash("Full name cannot be empty.", "danger")
        return redirect(url_for("admin.users"))
    if len(new_full_name) > 160:
        flash("Full name must be 160 characters or fewer.", "danger")
        return redirect(url_for("admin.users"))
    if new_email and len(new_email) > 160:
        flash("Email must be 160 characters or fewer.", "danger")
        return redirect(url_for("admin.users"))
    if new_email and "@" not in new_email:
        flash("Email is not a valid address.", "danger")
        return redirect(url_for("admin.users"))

    changes = {}
    if new_full_name != user.full_name:
        changes["full_name"] = {"from": user.full_name, "to": new_full_name}
        user.full_name = new_full_name
    if new_email != user.email:
        changes["email"] = {"from": user.email, "to": new_email}
        user.email = new_email

    if not changes:
        flash("No changes to save.", "info")
        return redirect(url_for("admin.users"))

    db.session.commit()
    audit.record("user_edit", {
        "target_username": user.username,
        "changes": changes,
    })
    flash(f"Updated {user.username}.", "success")
    return redirect(url_for("admin.users"))


# ---------------------- Rotation ----------------------

@bp.route("/rotation")
@admin_required
def rotation_view():
    upcoming = rotation.upcoming_assignments(weeks=12)
    history = rotation.all_assignments(limit=52)
    operators = db.session.scalars(
        select(User).where(User.is_active.is_(True),
                           User.role.in_(["admin", "operator"]))
        .order_by(User.username)
    ).all()
    today = datetime.date.today()
    monday, _ = rotation.week_bounds(today)
    return render_template(
        "admin/rotation.html",
        upcoming=upcoming, history=history, operators=operators,
        next_monday=monday + datetime.timedelta(days=7),
    )


@bp.route("/rotation/assign", methods=["POST"])
@admin_required
def rotation_assign():
    user_id = int(request.form.get("user_id", 0))
    week_start_str = request.form.get("week_start") or ""
    try:
        week_start = datetime.date.fromisoformat(week_start_str)
    except ValueError:
        flash("Invalid week start date.", "danger")
        return redirect(url_for("admin.rotation_view"))
    rotation.assign_week(user_id, week_start, created_by=g.user.id)
    audit.record("rotation_assigned",
                 {"user_id": user_id, "week_start": week_start_str})
    flash("Assignment saved.", "success")
    return redirect(url_for("admin.rotation_view"))


@bp.route("/rotation/autogenerate", methods=["POST"])
@admin_required
def rotation_autogen():
    weeks = int(request.form.get("weeks", 12))
    weeks = max(1, min(weeks, 52))
    created = rotation.autogenerate(weeks=weeks, created_by=g.user.id)
    audit.record("rotation_autogenerated", {"weeks": weeks, "created": created})
    flash(f"Generated {created} new weekly assignments.", "success")
    return redirect(url_for("admin.rotation_view"))


# ---------------------- Settings & branding ----------------------

@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        keys = ("company_name", "tagline", "footer_credit",
                "ldap_enabled", "ldap_server", "ldap_base_dn",
                "ldap_bind_user_template")
        for k in keys:
            upsert_setting(k, request.form.get(k, ""), g.user.id)
        db.session.commit()
        audit.record("settings_updated", {"keys": list(keys)})
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))
    from .mail import _config_status as _mail_status
    return render_template(
        "admin/settings.html",
        settings=settings_dict(),
        mail_status=_mail_status(),
    )


@bp.route("/settings/test-email", methods=["POST"])
@admin_required
def settings_test_email():
    """Send a test email to the currently signed-in admin's address using
    whichever MAIL_BACKEND is configured. Helpful for verifying SMTP /
    Resend wiring after deploy."""
    from .mail import send_mail, compose_test_email
    user = g.user
    if not user.email:
        flash(
            "Your admin account has no email on file. "
            "Edit your user in /admin/users first.",
            "danger",
        )
        return redirect(url_for("admin.settings"))
    subject, body = compose_test_email(recipient_full_name=user.full_name)
    result = send_mail(user.email, subject, body)
    audit.record("mail_test", {
        "recipient": user.email, "ok": result.get("ok"),
        "backend": result.get("backend"), "error": result.get("error"),
    })
    if result.get("ok"):
        flash(
            f"Test email sent to {user.email} via {result['backend']} backend. "
            f"Check your inbox.",
            "success",
        )
    else:
        flash(
            f"Test email failed via {result.get('backend')} backend: "
            f"{result.get('error')}",
            "danger",
        )
    return redirect(url_for("admin.settings"))


@bp.route("/settings/logo", methods=["POST"])
@admin_required
def settings_logo():
    f = request.files.get("logo")
    if not f or not f.filename:
        flash("Choose a logo file to upload.", "danger")
        return redirect(url_for("admin.settings"))
    fname = secure_filename(f.filename)
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in ("png", "jpg", "jpeg", "gif", "svg", "webp"):
        flash("Logo must be PNG, JPG, GIF, SVG, or WEBP.", "danger")
        return redirect(url_for("admin.settings"))
    try:
        url = upload_logo(f, fname, f.mimetype or "application/octet-stream")
    except Exception as exc:
        current_app.logger.exception("Logo upload failed: %s", exc)
        flash("Logo upload failed. Check the storage configuration.", "danger")
        return redirect(url_for("admin.settings"))
    upsert_setting("logo_path", url, g.user.id)
    db.session.commit()
    audit.record("logo_uploaded", {"url": url})
    flash("Logo uploaded.", "success")
    return redirect(url_for("admin.settings"))


# ---------------------- Audit log ----------------------

@bp.route("/audit")
@admin_required
def audit_log():
    entries = db.session.scalars(
        select(AuditLog).order_by(desc(AuditLog.timestamp)).limit(500)
    ).all()
    return render_template("admin/audit.html", entries=entries)
