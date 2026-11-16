"""Accounts: password hashing, login sessions (cookie), and login rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, Response

from . import store
from .config import APP_PASSWORD

COOKIE = "mac_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8

# scrypt parameters (about 16 MB of memory per hash)
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r),
                             p=int(p), dklen=len(h) // 2)
        return hmac.compare_digest(got.hex(), h)
    except Exception:
        return False


# A real hash to compare against when the username doesn't exist, so a wrong
# username takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def ensure_admin() -> None:
    """On first start, create the 'admin' account with APP_PASSWORD."""
    if store.count_users() == 0 and APP_PASSWORD:
        uid = store.create_user("admin", hash_password(APP_PASSWORD), is_admin=True)
        store.claim_unowned_debates(uid)


def needs_setup() -> bool:
    """True when there are no accounts and no APP_PASSWORD to create the first one."""
    return store.count_users() == 0


# ---- rate limiting: 10 failed logins per 15 minutes per IP and per username ---

_fails: dict[str, deque] = defaultdict(deque)
_WINDOW, _MAX_FAILS = 15 * 60, 10


def _client_ip(request: Request) -> str:
    return (request.headers.get("fly-client-ip")
            or (request.client.host if request.client else "unknown"))


def _too_many(key: str) -> bool:
    q = _fails[key]
    now = time.time()
    while q and q[0] < now - _WINDOW:
        q.popleft()
    return len(q) >= _MAX_FAILS


def check_rate(request: Request, username: str) -> None:
    if _too_many("ip:" + _client_ip(request)) or _too_many("user:" + username.lower()):
        raise HTTPException(429, "Too many failed attempts. Wait 15 minutes and try again.")


def record_failure(request: Request, username: str) -> None:
    now = time.time()
    _fails["ip:" + _client_ip(request)].append(now)
    _fails["user:" + username.lower()].append(now)


# ---- sessions --------------------------------------------------------------

def start_session(response: Response, request: Request, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    store.create_session(token, user_id, time.time() + SESSION_DAYS * 86400)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=secure, samesite="lax", path="/")


def end_session(response: Response, request: Request) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        store.delete_session(token)
    response.delete_cookie(COOKIE, path="/")


def current_user(request: Request) -> dict:
    """FastAPI dependency: the logged-in user, or 401."""
    token = request.cookies.get(COOKIE)
    user = store.session_user(token) if token else None
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def admin_user(request: Request) -> dict:
    user = current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Only an admin can do this.")
    return user


def validate_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Use at least {MIN_PASSWORD} characters.")


def public_user(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"], "is_admin": user["is_admin"]}
