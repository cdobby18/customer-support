"""Add guardrail flag fields to tickets.

Revision ID: 0009_add_guardrail_fields
Revises: 0008_create_feedback_table
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_add_guardrail_fields"
down_revision = "0008_create_feedback_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("guardrail_status", sa.String(20), nullable=False, server_default="clean"),
    )
    op.create_index("ix_tickets_guardrail_status", "tickets", ["guardrail_status"])
    op.add_column("tickets", sa.Column("guardrail_hits", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "guardrail_hits")
    op.drop_index("ix_tickets_guardrail_status", table_name="tickets")
    op.drop_column("tickets", "guardrail_status")