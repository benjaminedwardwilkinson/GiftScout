"""
Deliberately simple admin auth.

One admin account. Its username and a salted password hash live in the
database (the `settings` table — see db.py), so they can be changed from
/admin/settings without redeploying the container. The ADMIN_USERNAME and
ADMIN_PASSWORD environment variables only seed that account the very first
time the app starts against an empty database; after that they're ignored
in favor of whatever's stored.
"""
import hashlib
import secrets
import sqlite3
from typing import Optional

from fastapi import Request
from fastapi.responses import RedirectResponse

from app import db

_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt_hex: Optional[str] = None) -> str:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash or "$" not in stored_hash:
        return False
    salt_hex, _ = stored_hash.split("$", 1)
    candidate = hash_password(password, salt_hex)
    return secrets.compare_digest(candidate, stored_hash)


def check_credentials(conn: sqlite3.Connection, username: str, password: str) -> bool:
    stored_username = db.get_setting(conn, "admin_username", "")
    stored_hash = db.get_setting(conn, "admin_password_hash", "")
    if not stored_hash:
        return False
    user_ok = secrets.compare_digest(username, stored_username)
    pass_ok = verify_password(password, stored_hash)
    return user_ok and pass_ok


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("logged_in"))


def login_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/login", status_code=303)
