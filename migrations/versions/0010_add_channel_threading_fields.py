"""Add channel threading fields for multi-channel orchestration.

Revision ID: 0010_add_channel_threading_fields
Revises: 0009_add_guardrail_fields
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_add_channel_threading_fields"
down_revision = "0009_add_guardrail_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("external_id", sa.String(255), nullable=True))
    op.create_index("ix_tickets_channel_external_id", "tickets", ["channel", "external_id"])
    op.add_column("tickets", sa.Column("thread_id", sa.String(255), nullable=True))
    op.create_index("ix_tickets_channel_thread_id", "tickets", ["channel", "thread_id"])
    op.add_column(
        "ticket_comments", sa.Column("external_id", sa.String(255), nullable=True)
    )
    op.create_index(
        "ix_ticket_comments_channel_external_id",
        "ticket_comments",
        ["external_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ticket_comments_channel_external_id", table_name="ticket_comments")
    op.drop_column("ticket_comments", "external_id")
    op.drop_index("ix_tickets_channel_thread_id", table_name="tickets")
    op.drop_column("tickets", "thread_id")
    op.drop_index("ix_tickets_channel_external_id", table_name="tickets")
    op.drop_column("tickets", "external_id")