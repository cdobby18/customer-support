"""Add structured triage fields to tickets.

Revision ID: 0004_add_structured_triage_fields
Revises: 0003_create_audit_logs_table
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_add_structured_triage_fields"
down_revision = "0003_create_audit_logs_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("sentiment", sa.String(length=50), nullable=True))
    op.add_column("tickets", sa.Column("confidence", sa.Float(), nullable=True))
    op.add_column("tickets", sa.Column("recommended_team", sa.String(length=100), nullable=True))
    op.add_column("tickets", sa.Column("triage_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "triage_summary")
    op.drop_column("tickets", "recommended_team")
    op.drop_column("tickets", "confidence")
    op.drop_column("tickets", "sentiment")