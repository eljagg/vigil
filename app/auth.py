"""Authentication: argon2 local + optional TOTP 2FA + optional LDAP."""
from __future__ import annotations

import functools
import re
from typing import Any, Callable, Optional

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from flask import current_app, flash, g, redirect, request, session, url_for
from sqlalchemy import select

from .extensions import db
from .models import User


_hasher = PasswordHasher()

# A pre-computed argon2 hash used as a decoy when the username doesn't exist.
# Verifying against this takes the same wall-clock time as verifying a real
# user's hash, preventing timing-based username enumeration on /login.
# The plaintext hashed here is irrelevant — verification will always fail.
_DECOY_HASH = _hasher.hash("decoy-password-for-timing-stability")


# ----------------------------- passwords -----------------------------

def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(stored: str, plaintext: str) -> bool:
    if not stored:
        return False
    try:
        _hasher.verify(stored, plaintext)
        return True
    except (VerifyMismatchError, InvalidHash):
        return False


def _consume_time_like_verify() -> None:
    """Burn argon2-equivalent CPU when the username doesn't exist, so an
    attacker can't tell 'no such user' from 'wrong password' by timing."""
    try:
        _hasher.verify(_DECOY_HASH, "wrong-password")
    except (VerifyMismatchError, InvalidHash):
        pass


# ----------------------------- 2FA -----------------------------

def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, username: str, issuer: str = "Vigil") -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


# ----------------------------- authentication -----------------------------

def authenticate_local(username: str, password: str) -> Optional[User]:
    user = db.session.scalar(
        select(User).where(
            User.username == username,
            User.is_active.is_(True),
            User.auth_source == "local",
        )
    )
    if user is None:
        # Do an equivalent-cost hash verification against a decoy so the
        # response time doesn't reveal whether this username exists.
        _consume_time_like_verify()
        return None
    if verify_password(user.password_hash, password):
        return user
    return None


def authenticate_ldap(username: str, password: str) -> Optional[User]:
    cfg = current_app.config
    if not cfg.get("LDAP_ENABLED") or not cfg.get("LDAP_SERVER"):
        return None
    # Whitelist usernames before injecting into LDAP bind string. Without this,
    # a username like 'admin)(uid=*' could manipulate the LDAP filter/DN.
    # The accepted character set covers all common AD/LDAP username styles.
    if not re.fullmatch(r"[A-Za-z0-9._\-]{1,64}", username):
        current_app.logger.info("LDAP auth rejected: invalid username chars")
        return None
    user = db.session.scalar(
        select(User).where(
            User.username == username,
            User.is_active.is_(True),
            User.auth_source == "ldap",
        )
    )
    if not user:
        return None
    try:
        from ldap3 import ALL, Connection, Server
    except ImportError:
        current_app.logger.error("ldap3 not installed; LDAP auth unavailable")
        return None

    bind_user = cfg["LDAP_BIND_USER_TEMPLATE"].format(username=username)
    try:
        server = Server(cfg["LDAP_SERVER"], get_info=ALL)
        conn = Connection(server, user=bind_user, password=password, auto_bind=True)
        conn.unbind()
        return user
    except Exception as exc:  # pragma: no cover
        current_app.logger.warning("LDAP auth failed for %s: %s", username, exc)
        return None


def authenticate(username: str, password: str) -> Optional[User]:
    user = authenticate_local(username, password)
    if user:
        return user
    if current_app.config.get("LDAP_ENABLED"):
        return authenticate_ldap(username, password)
    return None


# ----------------------------- sessions -----------------------------

def login_user(user: User) -> None:
    session.clear()
    session["user_id"] = user.id
    session.permanent = True


def logout_user() -> None:
    session.clear()


def load_current_user() -> None:
    g.user = None
    user_id = session.get("user_id")
    if user_id is None:
        return
    user = db.session.get(User, user_id)
    if user and user.is_active:
        g.user = user


# ----------------------------- decorators -----------------------------

def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "user", None):
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "user", None):
            return redirect(url_for("auth.login", next=request.path))
        if g.user.role != "admin":
            flash("Administrator access required.", "danger")
            return redirect(url_for("main.dashboard"))
        return view(*args, **kwargs)
    return wrapped
