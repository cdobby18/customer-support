"""Request and response schemas, plus record-to-schema serializers.

Extracted from app/main.py so route modules can share them without
importing the FastAPI app (which would be a circular import)."""

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.models import (
    AuditLogRecord,
    FeedbackRecord,
    TicketCommentRecord,
    TicketRecord,
    UserRecord,
    UserRole,
)
from app.response_agent import DraftResult

class TicketStatus(str, Enum):
    open = "open"
    in_progress = "in_progress"
    pending = "pending"
    resolved = "resolved"
    closed = "closed"


class EscalationStatus(str, Enum):
    none = "none"
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class TicketCreate(BaseModel):
    customer_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    channel: str = Field(default="web", min_length=1)


class TicketUpdate(BaseModel):
    status: TicketStatus | None = None
    assignee_id: str | None = Field(default=None, min_length=1)


class EscalationUpdate(BaseModel):
    status: EscalationStatus
    reason: str | None = Field(default=None, max_length=500)


class CommentCreate(BaseModel):
    author_id: str = Field(min_length=1)
    body: str = Field(min_length=1)
    is_internal: bool = False


class TicketComment(CommentCreate):
    id: UUID
    ticket_id: UUID
    created_at: datetime


class Ticket(TicketCreate):
    id: UUID
    intent: str
    priority: str
    requires_human_review: bool
    escalation_status: EscalationStatus
    escalation_reason: str | None
    escalated_at: datetime | None
    reviewed_by: str | None
    sla_due_at: datetime | None
    first_response_at: datetime | None
    resolved_at: datetime | None
    sentiment: str | None
    confidence: float | None
    recommended_team: str | None
    triage_summary: str | None
    status: TicketStatus
    assignee_id: str | None
    risk_score: float | None
    risk_level: str | None
    escalation_summary: str | None
    escalation_route: str | None
    created_at: datetime
    updated_at: datetime
    intake_metadata: dict | None = None
    guardrail_status: str = "clean"
    guardrail_hits: list[dict] | None = None
    external_id: str | None = None
    thread_id: str | None = None


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class StaffUserCreate(RegisterRequest):
    role: UserRole


class UserStatusUpdate(BaseModel):
    is_active: bool


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: UUID
    email: EmailStr
    role: UserRole
    is_active: bool


class AuditLogResponse(BaseModel):
    id: UUID
    actor_id: str | None
    action: str
    entity_type: str
    entity_id: str
    details: dict
    created_at: datetime


class SlaMetrics(BaseModel):
    total_tickets: int
    open_tickets: int
    overdue_tickets: int
    resolved_tickets: int
    average_resolution_hours: float | None


class FeedbackCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackResponse(BaseModel):
    id: UUID
    ticket_id: UUID
    rating: int
    comment: str | None
    created_at: datetime


class FeedbackAnalytics(BaseModel):
    total_feedback: int
    average_rating: float | None
    rating_distribution: dict[int, int]
    response_rate: float | None
    deflection_rate: float | None


class SlaBreakdown(BaseModel):
    total_tickets: int
    open_tickets: int
    in_progress_tickets: int
    pending_tickets: int
    overdue_tickets: int
    resolved_tickets: int
    average_resolution_hours: float | None


class EscalationAnalytics(BaseModel):
    total_escalated: int
    pending_review: int
    approved: int
    rejected: int
    escalation_rate: float | None


class WorkloadItem(BaseModel):
    assignee_id: str | None
    assigned_tickets: int
    resolved_tickets: int


class ValueCount(BaseModel):
    value: str
    count: int


class ConfidenceHistogram(BaseModel):
    low: int = 0
    medium: int = 0
    high: int = 0
    very_high: int = 0
    average: float | None = None


class DashboardAnalytics(BaseModel):
    sla: SlaBreakdown
    csat: FeedbackAnalytics
    escalation: EscalationAnalytics
    workload: list[WorkloadItem]
    channels: list[ValueCount]
    priorities: list[ValueCount]
    intents: list[ValueCount]
    sentiments: list[ValueCount]
    confidence: ConfidenceHistogram


class ModelUsage(BaseModel):
    model: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class ResponseDraft(DraftResult):
    ticket_id: str


class AutoRespondResponse(BaseModel):
    ticket_id: str
    action: str
    reason: str | None = None
    comment_id: str | None = None
    draft: DraftResult | None = None


class DraftDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    body: str | None = Field(default=None, min_length=1, max_length=2000)
    note: str | None = Field(default=None, min_length=1, max_length=500)
    resolve: bool = True


class DraftDecisionResponse(BaseModel):
    ticket_id: str
    decision: str
    resolved: bool = False
    comment_id: str | None = None
    note_id: str | None = None


AI_ASSISTANT_ID = "ai-assistant"


class LlmUsageSummary(BaseModel):
    total_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost_usd: float
    since: str | None
    by_model: list[ModelUsage]


class LoginResponse(BaseModel):
    authenticated: bool
    user: UserResponse
    access_token: str
    token_type: str = "bearer"


def to_ticket(record: TicketRecord) -> Ticket:
    return Ticket(
        id=UUID(record.id),
        customer_id=record.customer_id,
        message=record.message,
        channel=record.channel,
        intent=record.intent,
        priority=record.priority,
        requires_human_review=record.requires_human_review,
        escalation_status=EscalationStatus(record.escalation_status),
        escalation_reason=record.escalation_reason,
        escalated_at=record.escalated_at,
        reviewed_by=record.reviewed_by,
        sla_due_at=record.sla_due_at,
        first_response_at=record.first_response_at,
        resolved_at=record.resolved_at,
        sentiment=record.sentiment,
        confidence=record.confidence,
        recommended_team=record.recommended_team,
        triage_summary=record.triage_summary,
        status=TicketStatus(record.status),
        assignee_id=record.assignee_id,
        risk_score=record.risk_score,
        risk_level=record.risk_level,
        escalation_summary=record.escalation_summary,
        escalation_route=record.escalation_route,
        created_at=record.created_at,
        updated_at=record.updated_at,
        intake_metadata=record.intake_metadata,
        guardrail_status=record.guardrail_status,
        guardrail_hits=[dict(hit) for hit in (record.guardrail_hits or [])] if record.guardrail_hits else None,
        external_id=record.external_id,
        thread_id=record.thread_id,
    )


def to_comment(record: TicketCommentRecord) -> TicketComment:
    return TicketComment(
        id=UUID(record.id),
        ticket_id=UUID(record.ticket_id),
        author_id=record.author_id,
        body=record.body,
        is_internal=record.is_internal,
        created_at=record.created_at,
    )


def to_feedback(record: FeedbackRecord) -> FeedbackResponse:
    return FeedbackResponse(
        id=UUID(record.id),
        ticket_id=UUID(record.ticket_id),
        rating=record.rating,
        comment=record.comment,
        created_at=record.created_at,
    )


def to_user(record: UserRecord) -> UserResponse:
    return UserResponse(
        id=UUID(record.id),
        email=record.email,
        role=UserRole(record.role),
        is_active=record.is_active,
    )


def to_audit_log(record: AuditLogRecord) -> AuditLogResponse:
    return AuditLogResponse(
        id=UUID(record.id),
        actor_id=record.actor_id,
        action=record.action,
        entity_type=record.entity_type,
        entity_id=record.entity_id,
        details=record.details,
        created_at=record.created_at,
    )


def add_audit_log(
    db: Session,
    actor_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict,
) -> None:
    db.add(
        AuditLogRecord(
            id=str(uuid4()),
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            created_at=datetime.now(timezone.utc),
        )
    )
