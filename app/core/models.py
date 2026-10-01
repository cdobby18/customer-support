from datetime import datetime
from enum import Enum

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UserRole(str, Enum):
    customer = "customer"
    agent = "agent"
    admin = "admin"


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default=UserRole.customer.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLogRecord(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(50), index=True)
    entity_id: Mapped[str] = mapped_column(String(255), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TicketRecord(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(255), index=True)
    message: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(50))
    intent: Mapped[str] = mapped_column(String(100))
    priority: Mapped[str] = mapped_column(String(50))
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    sentiment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    recommended_team: Mapped[str | None] = mapped_column(String(100), nullable=True)
    triage_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalation_status: Mapped[str] = mapped_column(String(20), default="none", index=True)
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(50), index=True)
    assignee_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    intake_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    guardrail_status: Mapped[str] = mapped_column(String(20), default="clean", index=True)
    guardrail_hits: Mapped[list | None] = mapped_column(JSON, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    escalation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalation_route: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Composite rather than per-column: external-id dedup and thread continuation
    # both filter on channel *and* the id together (see
    # `find_ticket_by_external_id` / `find_open_thread_ticket` in
    # app/api/routers/webhooks.py), so a single-column index cannot serve either.
    # Names must match revision 0010 so `create_all` and `alembic upgrade head`
    # produce the same schema.
    __table_args__ = (
        Index("ix_tickets_channel_external_id", "channel", "external_id"),
        Index("ix_tickets_channel_thread_id", "channel", "thread_id"),
    )


class TicketCommentRecord(Base):
    __tablename__ = "ticket_comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(36), index=True)
    author_id: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Comment dedup (`find_comment_by_external_id`) filters on external_id alone,
    # so this index is single-column. The name comes from revision 0010 and is
    # misleading, but renaming it would need a migration to keep both schemas
    # in step.
    __table_args__ = (
        Index("ix_ticket_comments_channel_external_id", "external_id"),
    )


class FeedbackRecord(Base):
    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(36), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TicketAttachmentRecord(Base):
    __tablename__ = "ticket_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(36), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String(512))
    uploaded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RevokedTokenRecord(Base):
    """One row per access token that has been revoked. The primary key is the
    token's `jti`; rows are only needed until the token's own `exp` passes."""

    __tablename__ = "revoked_tokens"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)