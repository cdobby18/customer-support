"""Add SLA tracking fields to tickets.

Revision ID: 0006_add_sla_fields
Revises: 0005_add_escalation_fields
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_add_sla_fields"
down_revision = "0005_add_escalation_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tickets", sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tickets", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tickets_sla_due_at", "tickets", ["sla_due_at"])


def downgrade() -> None:
    op.drop_index("ix_tickets_sla_due_at", table_name="tickets")
    op.drop_column("tickets", "resolved_at")
    op.drop_column("tickets", "first_response_at")
    op.drop_column("tickets", "sla_due_at")