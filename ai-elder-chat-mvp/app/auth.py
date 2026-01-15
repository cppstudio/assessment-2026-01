# app/auth.py
import secrets
from fastapi import HTTPException
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app import models

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_token() -> str:
    # 足够用于 MVP（不做过期/刷新）
    return secrets.token_urlsafe(24)


def get_user_id_from_authorization(db: Session, authorization: str | None) -> int:
    """
    解析 Authorization: Bearer <token>，查 sessions 表得到 user_id
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid token")

    sess = db.query(models.Session).filter(models.Session.token == token).first()
    if not sess:
        raise HTTPException(status_code=401, detail="Invalid or expired token (MVP: no expiry)")

    return sess.user_id
