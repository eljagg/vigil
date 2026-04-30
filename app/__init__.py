"""Flask application factory."""
from __future__ import annotations

import datetime
import getpass
import logging
import os
import sys

import click
from flask import Flask, g, session
from sqlalchemy import select

from .auth import hash_password
from .config import Config, assert_production_ready
from .extensions import csrf, db, limiter, migrate
from .models import User, settings_dict


def create_app(config_class: type = Config) -> Flask:
    app = Flask(__name__,
                static_folder="static",
                template_folder="templates")
    app.config.from_object(config_class)
    assert_production_ready(app)

    _configure_logging(app)

    # Trust X-Forwarded-* from Railway's edge proxy
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Extensions
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)

    # Per-request hooks
    from .auth import load_current_user
    app.before_request(load_current_user)

    # Generate a per-request CSP nonce. Used to allow our single inline
    # theme-bootstrap script in base.html without resorting to 'unsafe-inline'.
    @app.before_request
    def _csp_nonce():
        import secrets as _secrets
        from flask import g as _g
        _g.csp_nonce = _secrets.token_urlsafe(16)

    # Security headers
    @app.after_request
    def _security_headers(resp):
        from flask import g as _g
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        # CSP: tight defaults, nonce-based inline scripts only.
        # 'unsafe-inline' for styles is retained because the theme system
        # uses inline style attributes for some progress bars and sparklines.
        nonce = getattr(_g, "csp_nonce", None) or ""
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data: https:; "
            "style-src 'self' 'unsafe-inline'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        if app.config.get("SESSION_COOKIE_SECURE"):
            resp.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return resp

    # Template globals — branding + theme + date
    @app.context_processor
    def _inject_globals():
        try:
            settings_map = settings_dict()
        except Exception:
            settings_map = {}

        cfg = app.config
        company_name  = settings_map.get("company_name")  or cfg.get("COMPANY_NAME", "")
        tagline       = settings_map.get("tagline")       or cfg.get("TAGLINE", "Backup integrity check.")
        logo_path     = settings_map.get("logo_path")     or ""
        footer_credit = settings_map.get("footer_credit") or cfg.get("FOOTER_CREDIT", "")

        # Resolve theme: user pref > session pref > 'system'
        user = getattr(g, "user", None)
        if user and getattr(user, "theme", None) and user.theme != "system":
            theme = user.theme
        else:
            theme = session.get("theme", "system")

        now = datetime.datetime.now()
        return {
            "app_name": "Vigil",
            "company_name": company_name,
            "tagline": tagline,
            "logo_path": logo_path,
            "footer_credit": footer_credit,
            "theme": theme,
            "now": now,
            "today_long": now.strftime("%A, %B %d, %Y"),
            "current_user": user,
            "csp_nonce": getattr(g, "csp_nonce", ""),
        }

    # Blueprints
    from . import routes_admin, routes_api, routes_auth, routes_main
    app.register_blueprint(routes_auth.bp)
    app.register_blueprint(routes_main.bp)
    app.register_blueprint(routes_admin.bp, url_prefix="/admin")
    app.register_blueprint(routes_api.bp, url_prefix="/api")

    # CLI
    _register_cli(app)

    return app


def _configure_logging(app: Flask) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler]
    app.logger.setLevel(logging.INFO)


def _register_cli(app: Flask) -> None:
    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    @click.option("--full-name", prompt="Full name")
    @click.option("--email", prompt="Email (optional)", default="", show_default=False)
    @click.option("--password", default=None, help="Skip prompt by passing this")
    def create_admin(username, full_name, email, password):
        """Create the first admin user (or any admin)."""
        username = username.strip()
        full_name = full_name.strip()
        email = (email or "").strip() or None

        existing = db.session.scalar(select(User).where(User.username == username))
        if existing:
            click.echo(f"User '{username}' already exists.", err=True)
            sys.exit(1)

        if password is None:
            while True:
                pw = getpass.getpass("Password (min 12 chars): ")
                if len(pw) < 12:
                    click.echo("Too short. Try again.", err=True)
                    continue
                cf = getpass.getpass("Confirm: ")
                if pw != cf:
                    click.echo("Mismatch. Try again.", err=True)
                    continue
                password = pw
                break

        u = User(
            username=username, full_name=full_name, email=email,
            role="admin", auth_source="local",
            password_hash=hash_password(password),
        )
        db.session.add(u)
        db.session.commit()
        click.echo(f"Admin user '{username}' created.")

    @app.cli.command("reset-password")
    @click.argument("username")
    @click.option("--password", default=None)
    def reset_password(username, password):
        """Reset a local user's password."""
        user = db.session.scalar(select(User).where(User.username == username))
        if not user:
            click.echo(f"User '{username}' not found.", err=True)
            sys.exit(1)
        if user.auth_source != "local":
            click.echo(f"User '{username}' is LDAP-authenticated.", err=True)
            sys.exit(1)
        if password is None:
            while True:
                pw = getpass.getpass("New password (min 12 chars): ")
                if len(pw) < 12:
                    click.echo("Too short.", err=True); continue
                cf = getpass.getpass("Confirm: ")
                if pw != cf:
                    click.echo("Mismatch.", err=True); continue
                password = pw; break
        user.password_hash = hash_password(password)
        db.session.commit()
        click.echo(f"Password updated for '{username}'.")

    @app.cli.command("send-test-email")
    @click.argument("username")
    def send_test_email(username: str) -> None:
        """Send a test email to USERNAME using the current MAIL_BACKEND.

        Useful for verifying SMTP / Resend config after setting up env vars.
        """
        from .extensions import db as _db
        from .models import User as _User
        from .mail import send_mail, compose_test_email, _config_status

        status = _config_status()
        click.echo(f"Mail backend: {status['backend']} (ready={status['ready']})")
        click.echo(f"  {status['details']}")

        user = _db.session.scalar(_db.select(_User).where(_User.username == username))
        if not user:
            click.echo(f"User '{username}' not found.", err=True)
            raise SystemExit(1)
        if not user.email:
            click.echo(f"User '{username}' has no email address on file.", err=True)
            raise SystemExit(1)

        subject, body = compose_test_email(recipient_full_name=user.full_name)
        result = send_mail(user.email, subject, body)
        if result.get("ok"):
            click.echo(f"OK — sent via {result['backend']} to {user.email}")
        else:
            click.echo(f"FAILED via {result.get('backend')}: {result.get('error')}", err=True)
            raise SystemExit(2)

    @app.cli.command("send-weekly-reminders")
    @click.option("--dry-run", is_flag=True, help="Show what would be sent, don't actually send.")
    def send_weekly_reminders(dry_run: bool) -> None:
        """Send the weekly duty-reminder email to the on-duty operator.

        Intended to run on a schedule (Monday morning). Safe to run multiple
        times — each run sends an updated snapshot of this week's coverage.
        """
        import datetime as _dt
        from .extensions import db as _db
        from .models import PathEntry as _PathEntry, Scan as _Scan, settings_dict
        from . import rotation as _rotation
        from .mail import send_mail, compose_weekly_reminder, _config_status
        from sqlalchemy import func, select as _select

        status = _config_status()
        click.echo(f"Mail backend: {status['backend']}, ready={status['ready']}")

        on_duty = _rotation.current_assignment()
        if not on_duty:
            click.echo("No rotation assignment for this week — nothing to send.")
            return
        if not on_duty.get("email"):
            click.echo(
                f"On-duty user '{on_duty.get('username')}' has no email on file — "
                f"set one in /admin/users.",
                err=True,
            )
            raise SystemExit(1)

        # Compute coverage for the current week. When any path is marked
        # is_expected, scope the reminder to those — they're the configured
        # checklist. If none are marked, fall back to all known paths.
        today = _dt.date.today()
        monday = today - _dt.timedelta(days=today.weekday())
        sunday = monday + _dt.timedelta(days=6)

        expected = _db.session.scalars(
            _select(_PathEntry).where(_PathEntry.is_expected.is_(True))
        ).all()
        if expected:
            relevant = expected
            click.echo(f"Using {len(expected)} expected path(s) as the checklist.")
        else:
            relevant = _db.session.scalars(_select(_PathEntry)).all()
            click.echo(f"No expected paths defined — falling back to all {len(relevant)} known paths.")

        scanned_path_ids = set(_db.session.scalars(
            _select(_Scan.path_entry_id).where(
                _Scan.status == "completed",
                _Scan.is_hidden.is_(False),
                func.date(_Scan.started_at) >= monday,
                func.date(_Scan.started_at) <= sunday,
            ).distinct()
        ).all())
        missed = [pe.label or pe.path for pe in relevant if pe.id not in scanned_path_ids]

        company = (settings_dict() or {}).get("company_name", "")

        subject, body = compose_weekly_reminder(
            recipient_full_name=on_duty["full_name"],
            week_start=str(monday), week_end=str(sunday),
            paths_total=len(relevant),
            paths_scanned=len(relevant) - len(missed),
            missed_paths=missed,
            company_name=company,
        )

        click.echo(f"Recipient: {on_duty['full_name']} <{on_duty['email']}>")
        click.echo(f"Subject:   {subject}")
        click.echo(f"Body preview:\n---\n{body}\n---")

        if dry_run:
            click.echo("(--dry-run) not sent.")
            return

        result = send_mail(on_duty["email"], subject, body)
        if result.get("ok"):
            click.echo(f"OK — sent via {result['backend']}")
        else:
            click.echo(f"FAILED via {result.get('backend')}: {result.get('error')}", err=True)
            raise SystemExit(2)
