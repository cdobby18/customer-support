"""Create feedback table for CSAT ratings.

Revision ID: 0008_create_feedback_table
Revises: 0007_add_intake_metadata
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_create_feedback_table"
down_revision = "0007_add_intake_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticket_id", sa.String(36), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_feedback_ticket_id", "feedback", ["ticket_id"])
    op.create_index("ix_feedback_created_at", "feedback", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_feedback_created_at", table_name="feedback")
    op.drop_index("ix_feedback_ticket_id", table_name="feedback")
    op.drop_table("feedback")