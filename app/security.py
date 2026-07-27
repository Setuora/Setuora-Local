from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os

from app.config import get_settings

MIN_PASSWORD_LENGTH = 8


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return f"pbkdf2_sha256${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, salt_b64, digest_b64 = password_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    salt = _unb64(salt_b64)
    expected = _unb64(digest_b64)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return hmac.compare_digest(actual, expected)


def create_session_token(user_id: int) -> str:
    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.session_timeout_minutes)
    payload = {"sub": user_id, "exp": int(expires_at.timestamp())}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def read_session_token(token: str | None) -> int | None:
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    settings = get_settings()
    expected = _b64(hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    try:
        expires_at = int(payload.get("exp", 0))
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        return None
    if expires_at < int(datetime.now(timezone.utc).timestamp()):
        return None
    return user_id
