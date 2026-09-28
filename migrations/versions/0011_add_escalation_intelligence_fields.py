"""Add escalation intelligence fields (risk score, route, summary).

Revision ID: 0011_add_escalation_intelligence_fields
Revises: 0010_add_channel_threading_fields
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_add_escalation_intelligence_fields"
down_revision = "0010_add_channel_threading_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("risk_score", sa.Float, nullable=True))
    op.add_column("tickets", sa.Column("risk_level", sa.String(20), nullable=True))
    op.create_index("ix_tickets_risk_level", "tickets", ["risk_level"])
    op.add_column("tickets", sa.Column("escalation_summary", sa.Text, nullable=True))
    op.add_column("tickets", sa.Column("escalation_route", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "escalation_route")
    op.drop_column("tickets", "escalation_summary")
    op.drop_index("ix_tickets_risk_level", table_name="tickets")
    op.drop_column("tickets", "risk_level")
    op.drop_column("tickets", "risk_score")