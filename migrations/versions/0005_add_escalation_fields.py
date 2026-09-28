"""Add escalation workflow fields to tickets.

Revision ID: 0005_add_escalation_fields
Revises: 0004_add_structured_triage_fields
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_add_escalation_fields"
down_revision = "0004_add_structured_triage_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("escalation_status", sa.String(length=20), nullable=True))
    op.add_column("tickets", sa.Column("escalation_reason", sa.Text(), nullable=True))
    op.add_column("tickets", sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tickets", sa.Column("reviewed_by", sa.String(length=36), nullable=True))
    op.execute("UPDATE tickets SET escalation_status = 'none' WHERE escalation_status IS NULL")
    op.create_index("ix_tickets_escalation_status", "tickets", ["escalation_status"])


def downgrade() -> None:
    op.drop_index("ix_tickets_escalation_status", table_name="tickets")
    op.drop_column("tickets", "reviewed_by")
    op.drop_column("tickets", "escalated_at")
    op.drop_column("tickets", "escalation_reason")
    op.drop_column("tickets", "escalation_status")