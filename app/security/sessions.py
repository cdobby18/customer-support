"""Access-token revocation.

Access tokens are stateless JWTs, so killing one (logout, or a leaked token that
has to be cut off) needs server-side state. Every token carries a `jti`
session id; a revoked session id is recorded in `revoked_tokens` until the
token's own expiry passes, at which point the row is dead weight and is pruned.

These helpers do not commit; the caller owns the transaction.
"""

from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.models import RevokedTokenRecord, UserRecord


def is_token_revoked(db: Session, session_id: str) -> bool:
    return db.get(RevokedTokenRecord, session_id) is not None


def revoke_token(
    db: Session,
    *,
    session_id: str,
    user_id: str,
    expires_at: datetime,
) -> bool:
    """Mark a session as revoked. Returns False when it was already revoked."""
    purge_expired_revocations(db)
    if is_token_revoked(db, session_id):
        return False
    db.add(
        RevokedTokenRecord(
            session_id=session_id,
            user_id=user_id,
            expires_at=expires_at,
            revoked_at=datetime.now(timezone.utc),
        )
    )
    return True


def revoke_all_sessions(
    db: Session,
    *,
    user_id: str,
    revoked_at: datetime | None = None,
) -> datetime | None:
    """Invalidate every access token for one account.

    Access tokens are stateless, so there is no list of live `jti`s to walk;
    instead this records a cutoff on the user. `get_current_user` rejects a
    token whose `iat` is at or before that instant. Returns the cutoff, or None
    if the user does not exist. Does not commit; the caller owns the transaction.
    """
    user = db.get(UserRecord, user_id)
    if user is None:
        return None
    cutoff = revoked_at or datetime.now(timezone.utc)
    user.sessions_revoked_at = cutoff
    return cutoff


def purge_expired_revocations(db: Session) -> int:
    """Drop revocations for tokens that have already expired. Returns the count."""
    result = db.execute(
        delete(RevokedTokenRecord).where(
            RevokedTokenRecord.expires_at < datetime.now(timezone.utc)
        )
    )
    return result.rowcount or 0
