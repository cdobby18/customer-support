import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from pwdlib import PasswordHash

password_hash = PasswordHash.recommended()

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))
DEFAULT_JWT_SECRET = "local-development-secret-change-me"
ACCESS_TOKEN_TYPE = "access"


def jwt_secret() -> str:
    """Read the signing secret at call time rather than at import.

    Snapshotting it at import meant a rotated JWT_SECRET only took effect after
    every process was redeployed from scratch, which defeats the point of
    rotating a leaked secret. Both signing and verification go through here, so
    they cannot disagree about which key is current.
    """
    return os.getenv("JWT_SECRET", DEFAULT_JWT_SECRET)


def jwt_issuer() -> str:
    return os.getenv("JWT_ISSUER", "ai-customer-support")


def jwt_audience() -> str:
    return os.getenv("JWT_AUDIENCE", "ai-customer-support-api")


# Backwards-compatible snapshot: config validation and a few tests still read
# the name at import. New code must call jwt_secret() so a rotated value takes
# effect without a restart.
JWT_SECRET = jwt_secret()


@dataclass(frozen=True)
class TokenClaims:
    """What an access token carries: who it is for, which session it belongs to
    (what makes it revocable), when it was minted (what a per-user "revoke all
    sessions" compares against) and when it stops being usable."""

    user_id: str
    session_id: str
    issued_at: datetime
    expires_at: datetime


def hash_password(plain_password: str) -> str:
    return password_hash.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return password_hash.verify(plain_password, hashed_password)


def create_access_token(user_id: str) -> str:
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {
        "sub": user_id,
        "jti": str(uuid4()),
        "iat": issued_at,
        "exp": expires_at,
        # `iss`/`aud` stop a token minted for another service that happens to
        # share the secret from being replayed here, and `typ` is what lets a
        # refresh token (a future addition) be rejected by access-token
        # verification instead of being accepted as one.
        "iss": jwt_issuer(),
        "aud": jwt_audience(),
        "typ": ACCESS_TOKEN_TYPE,
    }
    return jwt.encode(payload, jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> TokenClaims:
    payload = jwt.decode(
        token,
        jwt_secret(),
        algorithms=[JWT_ALGORITHM],
        issuer=jwt_issuer(),
        audience=jwt_audience(),
        options={"require": ["exp", "iat", "sub", "jti", "iss", "aud"]},
    )
    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("Token is not an access token")
    user_id = payload.get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise jwt.InvalidTokenError("Token subject is missing")
    session_id = payload.get("jti")
    if not isinstance(session_id, str) or not session_id:
        # Tokens minted before revocation shipped have no `jti`, so there is no
        # key to revoke them under. Reject rather than accept an unkillable token.
        raise jwt.InvalidTokenError("Token session id is missing")
    issued_at = _as_datetime(payload.get("iat"))
    if issued_at is None:
        raise jwt.InvalidTokenError("Token issue time is missing")
    expires_at = _as_datetime(payload.get("exp"))
    if expires_at is None:
        raise jwt.InvalidTokenError("Token expiry is missing")
    return TokenClaims(
        user_id=user_id,
        session_id=session_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, datetime):
        return value
    return None
