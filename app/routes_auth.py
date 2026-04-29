"""Login, logout, theme, 2FA enrollment."""
from __future__ import annotations

import io

from flask import (
    Blueprint, current_app, flash, g, jsonify, redirect, render_template,
    request, session, url_for,
)
import qrcode

from . import audit
from .auth import (
    authenticate, generate_totp_secret, hash_password, login_required,
    login_user, logout_user, totp_provisioning_uri, verify_password, verify_totp,
)
from .extensions import csrf, db, limiter
from .models import User


bp = Blueprint("auth", __name__)


def _login_rate_limit():
    return current_app.config.get("LOGIN_RATE_LIMIT", "5 per minute")


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit(_login_rate_limit, methods=["POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        next_url = request.form.get("next") or url_for("main.dashboard")

        user = authenticate(username, password)
        if not user:
            audit.record("login_failed", {"username": username})
            flash("Invalid username or password.", "danger")
            return render_template("login.html", next_url=next_url)

        if user.totp_enabled:
            # Stash pending login for the 2FA step.
            session["pending_user_id"] = user.id
            session["pending_next"] = next_url
            return redirect(url_for("auth.login_2fa"))

        login_user(user)
        g.user = user
        user.last_login_at = db.func.now()
        db.session.commit()
        audit.record("login", {"username": username, "auth_source": user.auth_source})
        return redirect(next_url)

    return render_template("login.html", next_url=request.args.get("next", ""))


@bp.route("/login/2fa", methods=["GET", "POST"])
@limiter.limit(_login_rate_limit, methods=["POST"])
def login_2fa():
    pending_id = session.get("pending_user_id")
    if not pending_id:
        return redirect(url_for("auth.login"))
    user = db.session.get(User, pending_id)
    if not user or not user.totp_enabled:
        session.pop("pending_user_id", None)
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        if verify_totp(user.totp_secret, code):
            next_url = session.pop("pending_next", None) or url_for("main.dashboard")
            session.pop("pending_user_id", None)
            login_user(user)
            g.user = user
            user.last_login_at = db.func.now()
            db.session.commit()
            audit.record("login_2fa", {"username": user.username})
            return redirect(next_url)
        audit.record("login_2fa_failed", {"username": user.username})
        flash("Invalid 2FA code.", "danger")

    return render_template("login_2fa.html")


@bp.route("/logout", methods=["POST"])
def logout():
    if getattr(g, "user", None):
        audit.record("logout")
    logout_user()
    flash("Signed out.", "success")
    return redirect(url_for("auth.login"))


# ---------------------- Profile / theme / 2FA enrollment ----------------------

@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = g.user
    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "change_password":
            if user.auth_source != "local":
                flash("LDAP-authenticated users can't change passwords here.", "warning")
                return redirect(url_for("auth.profile"))
            current_pw = request.form.get("current_password") or ""
            new_pw = request.form.get("new_password") or ""
            confirm_pw = request.form.get("confirm_password") or ""
            if not verify_password(user.password_hash, current_pw):
                flash("Current password is incorrect.", "danger")
            elif len(new_pw) < 12:
                flash("New password must be at least 12 characters.", "danger")
            elif new_pw != confirm_pw:
                flash("New password and confirmation don't match.", "danger")
            else:
                user.password_hash = hash_password(new_pw)
                db.session.commit()
                audit.record("password_changed_self")
                flash("Password updated.", "success")

        elif action == "set_theme":
            theme = request.form.get("theme", "system")
            if theme in ("system", "light", "dark"):
                user.theme = theme
                db.session.commit()
                # Mirror in the session for instant pickup
                session["theme"] = theme

        return redirect(url_for("auth.profile"))

    return render_template("profile.html", user=user)


@bp.route("/profile/2fa/begin", methods=["POST"])
@login_required
def begin_2fa():
    user = g.user
    secret = generate_totp_secret()
    session["pending_totp_secret"] = secret
    return redirect(url_for("auth.show_2fa"))


@bp.route("/profile/2fa/setup")
@login_required
def show_2fa():
    secret = session.get("pending_totp_secret")
    if not secret:
        return redirect(url_for("auth.profile"))
    uri = totp_provisioning_uri(secret, g.user.username,
                                issuer=current_app.config.get("COMPANY_NAME", "Vigil"))
    return render_template("profile_2fa.html", secret=secret, uri=uri)


@bp.route("/profile/2fa/qrcode.png")
@login_required
def show_2fa_qr():
    secret = session.get("pending_totp_secret")
    if not secret:
        return ("", 404)
    uri = totp_provisioning_uri(secret, g.user.username,
                                issuer=current_app.config.get("COMPANY_NAME", "Vigil"))
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return (buf.getvalue(), 200, {"Content-Type": "image/png", "Cache-Control": "no-store"})


@bp.route("/profile/2fa/confirm", methods=["POST"])
@login_required
def confirm_2fa():
    secret = session.get("pending_totp_secret")
    code = (request.form.get("code") or "").strip()
    if not secret or not verify_totp(secret, code):
        flash("Invalid code. Try again.", "danger")
        return redirect(url_for("auth.show_2fa"))

    g.user.totp_secret = secret
    g.user.totp_enabled = True
    db.session.commit()
    session.pop("pending_totp_secret", None)
    audit.record("2fa_enabled")
    flash("Two-factor authentication is now enabled.", "success")
    return redirect(url_for("auth.profile"))


@bp.route("/profile/2fa/disable", methods=["POST"])
@login_required
def disable_2fa():
    g.user.totp_secret = None
    g.user.totp_enabled = False
    db.session.commit()
    audit.record("2fa_disabled")
    flash("Two-factor authentication disabled.", "success")
    return redirect(url_for("auth.profile"))


# ---------------------- Theme toggle (no-login fallback) ----------------------

@bp.route("/theme", methods=["POST"])
@csrf.exempt
def set_theme_anon():
    """Anonymous theme switch (login screen) — stored in session only."""
    data = request.get_json(silent=True) or {}
    theme = data.get("theme", "system")
    if theme in ("system", "light", "dark"):
        session["theme"] = theme
    return jsonify(ok=True, theme=theme)
