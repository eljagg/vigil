"""Configuration loaded from environment variables.

Railway exposes settings as env vars; locally they come from .env via
python-dotenv. Defaults are conservative — anything security-relevant
that's missing fails loudly.
"""
from __future__ import annotations

import os
import secrets
from datetime import timedelta

from dotenv import load_dotenv


load_dotenv()  # no-op in production (no .env file exists there)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _normalize_database_url(url: str) -> str:
    """Railway and Heroku give us 'postgresql://...' or 'postgres://...';
    SQLAlchemy 2.x with psycopg 3 wants 'postgresql+psycopg://...'."""
    if not url:
        return url
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Config:
    """Application configuration."""

    # Core ----------------------------------------------------------------
    SECRET_KEY: str = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    SQLALCHEMY_DATABASE_URI: str = _normalize_database_url(
        os.environ.get("DATABASE_URL", "")
    )
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,    # tolerate Railway's idle-connection drops
        "pool_recycle": 280,
    }
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Sessions ------------------------------------------------------------
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Secure cookies: on by default if running on Railway (RAILWAY_ENVIRONMENT
    # is auto-set), or if SESSION_COOKIE_SECURE=true is set explicitly.
    SESSION_COOKIE_SECURE: bool = _bool(
        "SESSION_COOKIE_SECURE",
        bool(os.environ.get("RAILWAY_ENVIRONMENT")),
    )
    PERMANENT_SESSION_LIFETIME = timedelta(
        minutes=int(os.environ.get("SESSION_TIMEOUT_MINUTES", "60"))
    )

    # File handling -------------------------------------------------------
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32 MiB cap on uploads (logos, scans)

    # Object storage (S3 / R2) for the company logo ----------------------
    S3_ENDPOINT_URL: str = os.environ.get("S3_ENDPOINT_URL", "")
    S3_BUCKET: str = os.environ.get("S3_BUCKET", "")
    S3_ACCESS_KEY: str = os.environ.get("S3_ACCESS_KEY", "")
    S3_SECRET_KEY: str = os.environ.get("S3_SECRET_KEY", "")
    S3_REGION: str = os.environ.get("S3_REGION", "auto")
    S3_PUBLIC_BASE_URL: str = os.environ.get("S3_PUBLIC_BASE_URL", "")

    # Branding ------------------------------------------------------------
    COMPANY_NAME: str = os.environ.get("COMPANY_NAME", "Your Company")
    TAGLINE: str = os.environ.get("TAGLINE", "Backup integrity check.")
    FOOTER_CREDIT: str = os.environ.get(
        "FOOTER_CREDIT", "Designed by Omar McLeod - 2026"
    )

    # Auth ----------------------------------------------------------------
    LDAP_ENABLED: bool = _bool("LDAP_ENABLED", False)
    LDAP_SERVER: str = os.environ.get("LDAP_SERVER", "")
    LDAP_BASE_DN: str = os.environ.get("LDAP_BASE_DN", "")
    LDAP_BIND_USER_TEMPLATE: str = os.environ.get(
        "LDAP_BIND_USER_TEMPLATE", "{username}@example.com"
    )
    LOGIN_RATE_LIMIT: str = os.environ.get("LOGIN_RATE_LIMIT", "5 per minute")

    # Misc ----------------------------------------------------------------
    PREFERRED_URL_SCHEME = "https" if SESSION_COOKIE_SECURE else "http"


def assert_production_ready(app) -> None:
    """Sanity-check critical settings when running on Railway."""
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError(
            "DATABASE_URL is not set. On Railway, attach a Postgres plugin to "
            "the service so DATABASE_URL is injected automatically."
        )
    if not os.environ.get("SECRET_KEY"):
        app.logger.warning(
            "SECRET_KEY not set in environment — using an ephemeral key. "
            "Sessions will not survive restarts. Set SECRET_KEY in Railway."
        )
