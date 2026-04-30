"""Object storage for the company logo.

Railway containers have ephemeral filesystems, so logos must live in
S3-compatible storage. Cloudflare R2 is the suggested backend (free
tier, no egress fees, S3-compatible API). The storage layer is a thin
wrapper so we can swap in the future.

If S3_BUCKET is not configured, falls back to writing under
/tmp/vigil-uploads — useful for local dev only.
"""
from __future__ import annotations

import io
import os
import uuid
from typing import Optional

from flask import current_app


def _client():
    """Lazy boto3 client. Returns None if S3 is not configured."""
    cfg = current_app.config
    if not cfg.get("S3_BUCKET"):
        return None
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=cfg["S3_ENDPOINT_URL"] or None,
        aws_access_key_id=cfg["S3_ACCESS_KEY"],
        aws_secret_access_key=cfg["S3_SECRET_KEY"],
        region_name=cfg["S3_REGION"],
    )


def upload_logo(stream, filename: str, content_type: str) -> str:
    """Save a logo and return a publicly-fetchable URL."""
    cfg = current_app.config
    ext = os.path.splitext(filename)[1].lower() or ".png"
    key = f"logos/{uuid.uuid4().hex}{ext}"

    client = _client()
    if client is None:
        # Local dev fallback — never hit on Railway, but useful in Codespaces.
        local_dir = "/tmp/vigil-uploads"
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, key.replace("/", "_"))
        if hasattr(stream, "save"):
            stream.save(local_path)
        else:
            with open(local_path, "wb") as f:
                f.write(stream.read())
        return f"/uploads/{os.path.basename(local_path)}"

    body = stream.read() if hasattr(stream, "read") else stream
    client.put_object(
        Bucket=cfg["S3_BUCKET"],
        Key=key,
        Body=body,
        ContentType=content_type or "application/octet-stream",
        CacheControl="public, max-age=86400",
    )
    base = cfg.get("S3_PUBLIC_BASE_URL", "")
    if base:
        return f"{base.rstrip('/')}/{key}"
    return f"{cfg['S3_ENDPOINT_URL'].rstrip('/')}/{cfg['S3_BUCKET']}/{key}"


def read_local_upload(filename: str) -> Optional[bytes]:
    """Used by the dev-only /uploads/<filename> endpoint.

    Hardened against path traversal: rejects anything containing path
    separators, parent-directory tokens, or null bytes. The caller is
    expected to pass a basename only (as written by ``upload_logo``).
    """
    base_dir = "/tmp/vigil-uploads"
    # Normalize basename and reject anything dangerous
    if not filename:
        return None
    if "/" in filename or "\\" in filename or "\x00" in filename:
        return None
    if filename in (".", ".."):
        return None
    # Resolve and verify the resulting path is still inside base_dir
    candidate = os.path.realpath(os.path.join(base_dir, filename))
    base_real = os.path.realpath(base_dir)
    # commonpath raises if filesystems differ; in our case both are absolute
    try:
        if os.path.commonpath([candidate, base_real]) != base_real:
            return None
    except ValueError:
        return None
    if not os.path.isfile(candidate):
        return None
    with open(candidate, "rb") as f:
        return f.read()
