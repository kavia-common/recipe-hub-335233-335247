from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from src.api.db import get_db
from src.api.models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


def _get_jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        # Intentionally strict: auth shouldn't silently work with a default secret.
        raise RuntimeError("Missing required environment variable JWT_SECRET")
    return secret


def _get_jwt_issuer() -> str:
    return os.environ.get("JWT_ISSUER", "recipe-hub-backend")


def _get_access_token_exp_minutes() -> int:
    try:
        return int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
    except ValueError:
        return 60


# PUBLIC_INTERFACE
def hash_password(password: str) -> str:
    """Hash a plaintext password.

    Contract:
      - Input: plaintext password (non-empty)
      - Output: password hash suitable for storage
    """
    return pwd_context.hash(password)


# PUBLIC_INTERFACE
def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a stored hash."""
    return pwd_context.verify(password, password_hash)


# PUBLIC_INTERFACE
def create_access_token(*, subject: str, user_id: int, is_admin: bool) -> str:
    """Create a signed JWT access token.

    Contract:
      Inputs:
        - subject: typically the user email
        - user_id: numeric user id
        - is_admin: whether token should include admin claim
      Output:
        - JWT string
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=_get_access_token_exp_minutes())
    payload = {
        "sub": subject,
        "uid": user_id,
        "adm": bool(is_admin),
        "iss": _get_jwt_issuer(),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm="HS256")


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, _get_jwt_secret(), algorithms=["HS256"], issuer=_get_jwt_issuer())
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from e


# PUBLIC_INTERFACE
def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the currently authenticated user from Authorization: Bearer token.

    Errors:
      - 401 if missing/invalid token
      - 401 if token refers to missing user
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = _decode_token(credentials.credentials)
    user_id = payload.get("uid")
    if not isinstance(user_id, int):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    user = db.query(User).filter(User.id == user_id).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return user


# PUBLIC_INTERFACE
def require_admin(user: User = Depends(get_current_user)) -> User:
    """Dependency that ensures the current user is an admin."""
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user
