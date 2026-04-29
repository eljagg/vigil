"""Flask extension instances.

Defined here so they're importable everywhere without circular imports.
Initialized against the app inside the factory.
"""
from __future__ import annotations

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect


db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],         # no global limit; route-specific only
    storage_uri="memory://",   # in-memory is fine for a single-instance Railway deploy
)
