"""Add intake_metadata column for normalized channel payloads.

Revision ID: 0007_add_intake_metadata
Revises: 0006_add_sla_fields
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_add_intake_metadata"
down_revision = "0006_add_sla_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("intake_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "intake_metadata")