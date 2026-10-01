"""Request authentication and role dependencies."""

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.agents.support import as_utc
from app.core.database import get_db
from app.core.models import UserRecord, UserRole
from app.security.auth import TokenClaims, decode_access_token
from app.security.sessions import is_token_revoked

bearer_scheme = HTTPBearer(auto_error=False)

STAFF_ROLES = frozenset({UserRole.agent.value, UserRole.admin.value})


def is_staff(user: UserRecord) -> bool:
    """True for the roles that see the whole queue rather than their own tickets."""
    return user.role in STAFF_ROLES


def read_token_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> TokenClaims:
    """Verify the bearer token's signature and shape, without touching the DB."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        return decode_access_token(credentials.credentials)
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None


def get_current_user(
    claims: TokenClaims = Depends(read_token_claims),
    db: Session = Depends(get_db),
) -> UserRecord:
    if is_token_revoked(db, claims.session_id):
        raise HTTPException(status_code=401, detail="Token has been revoked")
    user = db.get(UserRecord, claims.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.sessions_revoked_at is not None and claims.issued_at <= as_utc(
        user.sessions_revoked_at
    ):
        # "Revoke all sessions" sets a cutoff: every token minted at or before
        # it is dead, including ones this process never saw revoked.
        raise HTTPException(status_code=401, detail="Token has been revoked")
    return user


def require_admin(current_user: UserRecord = Depends(get_current_user)) -> UserRecord:
    if current_user.role != UserRole.admin.value:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return current_user


def require_staff(current_user: UserRecord = Depends(get_current_user)) -> UserRecord:
    if not is_staff(current_user):
        raise HTTPException(status_code=403, detail="Support staff access required")
    return current_user
