from __future__ import annotations
"""JWT authentication artifact generator for Crucible web interfaces.

When enabled, this feature writes a Flask authentication blueprint with JWT
issuance, token verification, logout session invalidation, and password hashing
fallbacks that work without PyJWT or bcrypt.
"""

import json
import os
import secrets
import time
from typing import Any, Dict

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


WEBUI_AUTH_SCRIPT = r'''from __future__ import annotations
"""Flask authentication blueprint for Crucible Web UI."""

import base64
import functools
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request

try:
    import jwt  # type: ignore
except ImportError:
    jwt = None  # type: ignore[assignment]

try:
    import bcrypt  # type: ignore
except ImportError:
    bcrypt = None  # type: ignore[assignment]


auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock: threading.Lock = threading.Lock()


def _base_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _secret() -> str:
    path = os.path.join(_base_dir(), "jwt_secret.txt")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value
    except OSError:
        pass
    value = os.environ.get("JWT_SECRET", "")
    if value:
        return value
    return "change-this-secret-before-deploying"


def _users() -> Dict[str, Any]:
    path = os.path.join(_base_dir(), "users.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
        return data if isinstance(data, dict) else {"users": []}
    except (OSError, json.JSONDecodeError, TypeError):
        return {"users": []}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _manual_encode(payload: Dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _manual_decode(token: str, secret: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid token format")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    actual = _b64url_decode(parts[2])
    if not hmac.compare_digest(expected, actual):
        raise ValueError("invalid token signature")
    payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("token expired")
    return payload


def _encode(payload: Dict[str, Any]) -> str:
    secret = _secret()
    if jwt is not None:
        return jwt.encode(payload, secret, algorithm="HS256")
    return _manual_encode(payload, secret)


def _decode(token: str) -> Dict[str, Any]:
    secret = _secret()
    if jwt is not None:
        return jwt.decode(token, secret, algorithms=["HS256"])
    return _manual_decode(token, secret)


def _verify_password(password: str, stored: str, salt: str = "") -> bool:
    if not password or not stored:
        return False
    if bcrypt is not None and stored.startswith("$2"):
        try:
            return bool(bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8")))
        except ValueError:
            return False
    if not salt:
        return hmac.compare_digest(hashlib.sha256(password.encode("utf-8")).hexdigest(), stored)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 210000)
    return hmac.compare_digest(base64.b64encode(derived).decode("ascii"), stored)


def _find_user(username: str) -> Optional[Dict[str, Any]]:
    for user in _users().get("users", []):
        if isinstance(user, dict) and str(user.get("username")) == username:
            return user
    return None


@auth_bp.post("/login")
def login() -> Tuple[Any, int]:
    body = request.get_json(silent=True) or {}
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    user = _find_user(username)
    if not user or not _verify_password(password, str(user.get("password_hash") or ""), str(user.get("salt") or "")):
        return jsonify({"error": "invalid credentials"}), 401
    try:
        ttl_hours = int(os.environ.get("AUTH_TOKEN_TTL_HOURS", "8"))
    except (ValueError, TypeError):
        ttl_hours = 8
    now = int(time.time())
    session_id = secrets.token_urlsafe(24)
    payload = {"sub": username, "sid": session_id, "iat": now, "exp": now + max(ttl_hours, 1) * 3600}
    token = _encode(payload)
    with _sessions_lock:
        _sessions[session_id] = {"username": username, "created_at": now, "expires_at": payload["exp"]}
    return jsonify({"token": token, "token_type": "bearer", "expires_at": payload["exp"]}), 200


def token_required(func: Any) -> Any:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "missing bearer token"}), 401
        token = header.split(" ", 1)[1].strip()
        try:
            payload = _decode(token)
        except Exception as exc:
            return jsonify({"error": "invalid token", "detail": str(exc)}), 401
        session_id = str(payload.get("sid") or "")
        with _sessions_lock:
            if session_id not in _sessions:
                return jsonify({"error": "session revoked"}), 401
        request.auth = payload  # type: ignore[attr-defined]
        return func(*args, **kwargs)
    return wrapper


@auth_bp.post("/logout")
@token_required
def logout() -> Tuple[Any, int]:
    payload = getattr(request, "auth", {})
    session_id = str(payload.get("sid") or "")
    with _sessions_lock:
        _sessions.pop(session_id, None)
    return jsonify({"ok": True}), 200
'''


def _write_text(path: str, content: str, *, overwrite: bool = True) -> bool:
    try:
        if not overwrite and os.path.exists(path):
            return False
        try:
            from .._atomic_io import atomic_write_text
        except ImportError:  # flat-launcher mode
            from _atomic_io import atomic_write_text  # type: ignore[no-redef]
        # v1.1.11: delegate to the shared atomic writer (parent-dir fsync,
        # CLAUDE.md §13.1).  newline="\n" preserves the prior LF-only output
        # (the shared helper accepts a newline passthrough) so generated source
        # files stay byte-identical on Windows and POSIX.
        atomic_write_text(path, content, newline="\n")
        return True
    except OSError as exc:
        raise RuntimeError(f"cannot write {path}: {exc}") from exc


def _write_json(path: str, payload: Dict[str, Any], *, overwrite: bool = True) -> bool:
    return _write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n", overwrite=overwrite)


def _guide(admin_user: str) -> str:
    return f"""# Crucible Auth Setup Guide

Generated files:

- `webui_auth.py`: Flask blueprint with `/auth/login` and `/auth/logout`.
- `users.json`: user store initialized with an empty users array.
- `jwt_secret.txt`: local signing secret generated with `secrets.token_hex(32)`.

Create a user entry in `users.json` before enabling login. For production,
store password hashes generated with bcrypt or PBKDF2 and protect this run
directory with filesystem permissions. The default administrative username
reserved by configuration is `{admin_user}`.
"""


@register("auth_manager")
class AuthManagerFeature(BaseFeature):
    name = "auth_manager"
    label = "JWT Auth Manager"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        start = time.monotonic()
        # Strict whitelist: AUTH_ENABLED is security-critical and default-off.
        # The previous `in falsy_set` pattern would silently enable the feature
        # for any unrecognised value (typos, leading whitespace remnants, etc.)
        # which is the wrong side to fail to for an auth flag.
        _auth_raw = os.environ.get("AUTH_ENABLED", "0").strip().lower()
        if _auth_raw in ("1", "true", "yes", "on"):
            _auth_enabled = True
        else:
            # Includes the explicit falsy set ("0", "false", "no", "off") and
            # any unrecognised value: fail closed.
            _auth_enabled = False
        if not _auth_enabled:
            return FeatureResult(feature=self.name, success=True, summary="disabled", skipped=True, skip_reason="disabled")
        try:
            webui_auth_path = os.path.join(run_dir, "webui_auth.py")
            users_path = os.path.join(run_dir, "users.json")
            secret_path = os.path.join(run_dir, "jwt_secret.txt")
            guide_path = os.path.join(run_dir, "auth_setup_guide.md")
            admin_user = os.environ.get("AUTH_ADMIN_USER", "admin")
            _write_text(webui_auth_path, WEBUI_AUTH_SCRIPT)
            users_written = _write_json(users_path, {"users": []}, overwrite=False)
            secret_written = _write_text(secret_path, secrets.token_hex(32) + "\n", overwrite=False)
            _write_text(guide_path, _guide(admin_user))
            details = {"webui_auth_path": webui_auth_path, "users_path": users_path, "jwt_secret_path": secret_path, "guide_path": guide_path, "users_written": users_written, "jwt_secret_written": secret_written, "token_ttl_hours": os.environ.get("AUTH_TOKEN_TTL_HOURS", "8"), "admin_user": admin_user}
            return FeatureResult(feature=self.name, success=True, summary="Auth artifacts generated", details=details, duration_seconds=time.monotonic() - start)
        except Exception as exc:
            return FeatureResult(feature=self.name, success=False, summary="Auth artifact generation failed", error=str(exc), duration_seconds=time.monotonic() - start)
