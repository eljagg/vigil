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

    # Security headers
    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        # CSP allows inline styles (theme switcher) and a tiny bootstrap script
        # to set the theme before render to avoid flash-of-wrong-theme.
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data: https:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'"
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
