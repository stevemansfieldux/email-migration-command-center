"""Who is asking? Two ways in: a session cookie from the login page, or an
API key as `Authorization: Bearer <key>`. Both resolve to the same User."""
import hashlib
import secrets
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlmodel import select

from . import db


# ---------- credentials ----------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def check_password(plain: str, hashed: Optional[str]) -> bool:
    if not hashed:
        return False
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def new_api_key() -> tuple[str, str]:
    """Returns (key, sha256). Store the hash; show the key exactly once."""
    key = "cc_" + secrets.token_urlsafe(32)
    return key, hashlib.sha256(key.encode()).hexdigest()


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ---------- lookup ----------

def user_by_email(email: str) -> Optional[db.User]:
    with db.session() as s:
        return s.exec(select(db.User).where(db.User.email == email.strip().lower())).first()


def user_by_id(user_id: int) -> Optional[db.User]:
    with db.session() as s:
        return s.get(db.User, user_id)


def user_by_key(key: str) -> Optional[db.User]:
    with db.session() as s:
        return s.exec(select(db.User).where(db.User.api_key_hash == key_hash(key))).first()


# ---------- request -> user ----------

def resolve(request: Request) -> Optional[db.User]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return user_by_key(auth[7:].strip())
    uid = request.session.get("uid")
    if uid:
        return user_by_id(int(uid))
    return None


def current_user(request: Request) -> db.User:
    """Dependency for API routes: 401 with a JSON body, never a redirect."""
    u = resolve(request)
    if not u:
        raise HTTPException(401, "sign in, or send Authorization: Bearer <api key>")
    return u


class LoginRequired(Exception):
    """Raised by page routes; the handler in main turns it into a redirect to /login."""


def current_user_page(request: Request) -> db.User:
    u = resolve(request)
    if not u:
        raise LoginRequired()
    return u


CurrentUser = Depends(current_user)
PageUser = Depends(current_user_page)
