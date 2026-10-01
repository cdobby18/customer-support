"""Create the ticket_attachments table.

`TicketAttachmentRecord` (app/core/models.py) has existed since attachments
shipped, but no revision ever created its table, so the documented production
path (`alembic upgrade head`) leaves the three attachment endpoints querying a
relation that does not exist. The gap was invisible because `init_db()` calls
`create_all()` on startup, and because the test harness builds the schema from
`Base` rather than from the migration chain.

Columns mirror the model exactly, including the `index=True` columns, so the
Alembic-built and `create_all`-built schemas agree.

Revision ID: 0013_create_ticket_attachments
Revises: 0012_add_revoked_tokens
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_create_ticket_attachments"
down_revision = "0012_add_revoked_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticket_id", sa.String(36), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("uploaded_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ticket_attachments_ticket_id", "ticket_attachments", ["ticket_id"])
    op.create_index("ix_ticket_attachments_created_at", "ticket_attachments", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ticket_attachments_created_at", table_name="ticket_attachments")
    op.drop_index("ix_ticket_attachments_ticket_id", table_name="ticket_attachments")
    op.drop_table("ticket_attachments")