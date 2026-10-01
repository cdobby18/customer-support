"""Add users.sessions_revoked_at for per-user session revocation.

Logout revokes a single token by `jti`. There was no way to kill every session
for one account short of deactivating the account (which also blocks login).
This column records a cutoff: every access token whose `iat` is at or before it
is rejected by `get_current_user`, which is how an admin can cut off a
compromised user without disabling them.

Nullable, so existing rows and tokens minted before the cutoff column existed
are unaffected until a revoke-all actually runs.

Revision ID: 0014_add_sessions_revoked_at
Revises: 0013_create_ticket_attachments
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_add_sessions_revoked_at"
down_revision = "0013_create_ticket_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table so this is a plain ALTER on PostgreSQL and a
    # copy-and-rebuild on SQLite, which has no ALTER TABLE ... ADD COLUMN support.
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("sessions_revoked_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("sessions_revoked_at")
