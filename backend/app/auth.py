"""Local authentication: users, PBKDF2 passwords, signed session tokens."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db, utcnow
from .models import SessionToken, User
from .security import hash_password, verify_password


def create_user(session: Session, username: str, password: str, is_admin: bool = False) -> User:
    user = User(username=username, password_hash=hash_password(password), is_admin=is_admin)
    session.add(user)
    session.flush()
    return user


def ensure_default_admin(session: Session) -> None:
    if not settings.auth_enabled:
        return
    if session.query(User).count() == 0:
        create_user(session, settings.default_admin_username, settings.default_admin_password, is_admin=True)


def login(session: Session, username: str, password: str) -> tuple[str, User]:
    user = session.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    token = _make_token()
    session.add(
        SessionToken(
            user_id=user.id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
        )
    )
    session.flush()
    return token, user


def _make_token() -> str:
    return secrets.token_urlsafe(32)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def extract_bearer(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip()


def get_current_user(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_db),
) -> User:
    if not settings.auth_enabled:
        # local single-user mode: bootstrap admin
        ensure_default_admin(session)
        return session.query(User).order_by(User.id).first()
    token = extract_bearer(authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing session token")
    row = session.query(SessionToken).filter(SessionToken.token_hash == _hash(token)).first()
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session token")
    if row.expires_at < utcnow():
        session.delete(row)
        session.flush()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    user = session.get(User, row.user_id)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user
