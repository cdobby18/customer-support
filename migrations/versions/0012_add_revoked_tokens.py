"""Add revoked_tokens table and widen audit_logs.entity_id.

Access tokens are stateless JWTs, so logout needs server-side state: the
`jti` of a revoked session is recorded here until the token expires.
`audit_logs.entity_id` grows from 36 to 255 characters because brute-force
lockout rows key on the targeted account's email address, which is longer than
a UUID.

Revision ID: 0012_add_revoked_tokens
Revises: 0011_add_escalation_intelligence_fields
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_add_revoked_tokens"
down_revision = "0011_add_escalation_intelligence_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "revoked_tokens",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_revoked_tokens_user_id", "revoked_tokens", ["user_id"])
    op.create_index("ix_revoked_tokens_expires_at", "revoked_tokens", ["expires_at"])
    op.create_index("ix_revoked_tokens_revoked_at", "revoked_tokens", ["revoked_at"])
    # batch_alter_table so this is a plain ALTER on PostgreSQL and a
    # copy-and-rebuild on SQLite, which has no ALTER COLUMN ... TYPE.
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.alter_column(
            "entity_id",
            existing_type=sa.String(36),
            type_=sa.String(255),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.alter_column(
            "entity_id",
            existing_type=sa.String(255),
            type_=sa.String(36),
            existing_nullable=False,
        )
    op.drop_index("ix_revoked_tokens_revoked_at", table_name="revoked_tokens")
    op.drop_index("ix_revoked_tokens_expires_at", table_name="revoked_tokens")
    op.drop_index("ix_revoked_tokens_user_id", table_name="revoked_tokens")
    op.drop_table("revoked_tokens")
