import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from pwdlib import PasswordHash


password_hash = PasswordHash.recommended()
JWT_SECRET = os.getenv("JWT_SECRET", "local-development-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))


@dataclass(frozen=True)
class TokenClaims:
    """What an access token carries: who it is for, which session it belongs to
    (what makes it revocable), and when it stops being usable."""

    user_id: str
    session_id: str
    expires_at: datetime


def hash_password(plain_password: str) -> str:
    return password_hash.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return password_hash.verify(plain_password, hashed_password)


def create_access_token(user_id: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {"sub": user_id, "jti": str(uuid4()), "exp": expires_at}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> TokenClaims:
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    user_id = payload.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise jwt.InvalidTokenError("Token subject is missing")
    session_id = payload.get("jti")
    if not isinstance(session_id, str) or not session_id:
        # Tokens minted before revocation shipped have no `jti`, so there is no
        # key to revoke them under. Reject rather than accept an unkillable token.
        raise jwt.InvalidTokenError("Token session id is missing")
    expires_at = payload.get("exp")
    if isinstance(expires_at, int | float):
        expires_at = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    if not isinstance(expires_at, datetime):
        raise jwt.InvalidTokenError("Token expiry is missing")
    return TokenClaims(user_id=user_id, session_id=session_id, expires_at=expires_at)