from contextlib import asynccontextmanager
from collections import Counter
from datetime import datetime, timedelta, timezone
from enum import Enum
import hmac
import json
import os
import time
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

import redis

from app.auth import create_access_token, decode_access_token, hash_password, verify_password
from app.database import get_db, init_db
from app.knowledge import KnowledgeMatch, search_knowledge
from app.models import (
    AuditLogRecord,
    FeedbackRecord,
    TicketCommentRecord,
    TicketRecord,
    UserRecord,
    UserRole,
)
from app.triage import TriageResult, classify_ticket
from app.intake import normalize_message, NormalizedMessage, enrich_customer_context
from app.guardrails import (
    evaluate,
    PolicyReport,
    Violation,
)
from app.integrations import sync_ticket_outbound
from app.llm import (
    get_llm_usage_summary,
    LLMConfigError,
    LLMError,
    reset_llm_usage,
)
from app.response_agent import DraftResult, draft_reply
from app.observability import (
    RequestContextMiddleware,
    get_app_logger,
    setup_logging,
    setup_tracing,
)
from app.workers import enqueue_notification

setup_logging()


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


class ChannelMessage(BaseModel):
    customer_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    external_id: str | None = Field(default=None, min_length=1)


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


def _integration_ticket_payload(record: TicketRecord) -> dict:
    return {
        "id": record.id,
        "subject": record.message[:120],
        "message": record.message,
        "status": record.status,
        "priority": record.priority,
        "intent": record.intent,
        "channel": record.channel,
    }


def _record_integration_sync(
    db: Session,
    actor_id: str | None,
    event: str,
    record: TicketRecord,
    sync_result: dict[str, object] | None,
) -> None:
    if sync_result is None:
        return
    details: dict[str, str | None] = {
        "event": event,
        "provider": str(sync_result.get("provider") or ""),
        "status": str(sync_result.get("status") or "synced"),
    }
    for key in ("remote_id", "remote_comment_id", "reason"):
        if sync_result.get(key):
            details[key] = str(sync_result[key])
    add_audit_log(db, actor_id, "integration.synced", "ticket", record.id, details)
    db.commit()


def validate_security_configuration() -> None:
    provider = os.getenv("SUPPORT_TOOL_PROVIDER", "").strip().lower()
    if provider and provider not in {"mock", "zendesk", "hubspot"}:
        raise RuntimeError(
            f"SUPPORT_TOOL_PROVIDER={provider!r} is invalid; expected one of: mock, zendesk, hubspot"
        )
    llm_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    if llm_provider and llm_provider not in {"mock", "openai", "azure_openai"}:
        raise RuntimeError(
            f"LLM_PROVIDER={llm_provider!r} is invalid; expected one of: mock, openai, azure_openai"
        )
    embedding_provider = os.getenv("EMBEDDING_PROVIDER", "local").strip().lower()
    if embedding_provider not in {"local", "openai"}:
        raise RuntimeError(
            f"EMBEDDING_PROVIDER={embedding_provider!r} is invalid; expected one of: local, openai"
        )
    if os.getenv("APP_ENV", "development").lower() != "production":
        return
    required_secrets = {
        "JWT_SECRET": "local-development-secret-change-me",
        "CHANNEL_WEBHOOK_SECRET": "local-webhook-secret",
    }
    for variable_name, insecure_default in required_secrets.items():
        configured_value = os.getenv(variable_name, "")
        if not configured_value or configured_value == insecure_default:
            raise RuntimeError(f"{variable_name} must be configured for production")
    if llm_provider == "mock":
        raise RuntimeError(
            "LLM_PROVIDER=mock is not allowed in production; use openai or azure_openai"
        )
    if llm_provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY must be configured when LLM_PROVIDER=openai in production"
        )
    if llm_provider == "azure_openai":
        missing = [
            variable
            for variable in (
                "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_DEPLOYMENT",
                "AZURE_OPENAI_API_KEY",
            )
            if not os.getenv(variable)
        ]
        if missing:
            required = ", ".join(missing)
            raise RuntimeError(
                f"{required} must be configured when LLM_PROVIDER=azure_openai in production"
            )
    if embedding_provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY must be configured when EMBEDDING_PROVIDER=openai in production"
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_security_configuration()
    init_db()
    setup_tracing(app)
    yield


app = FastAPI(title="AI Customer Support Automation", lifespan=lifespan)
trusted_hosts = [
    host.strip()
    for host in os.getenv("TRUSTED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
    if host.strip()
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware, logger=get_app_logger())
bearer_scheme = HTTPBearer(auto_error=False)


def triage(message: str) -> TriageResult:
    return classify_ticket(message)


def sla_deadline(priority: str, created_at: datetime, channel: str = "web") -> datetime:
    hours_by_priority = {"urgent": 4, "high": 8, "normal": 24}
    return created_at + timedelta(hours=hours_by_priority.get(priority, 24) * sla_channel_multiplier(channel))


def sla_channel_multiplier(channel: str) -> float:
    default_multipliers = {
        "email": 1.0,
        "web": 1.0,
        "crm": 1.0,
        "slack": 0.75,
        "whatsapp": 0.5,
        "chat": 0.5,
    }
    configured: dict[str, float] = {}
    raw = os.getenv("SLA_CHANNEL_MULTIPLIERS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                configured = {str(key): float(value) for key, value in parsed.items()}
        except (json.JSONDecodeError, TypeError, ValueError):
            configured = {}
    multiplier = configured.get(channel, default_multipliers.get(channel, 1.0))
    try:
        return float(multiplier)
    except (TypeError, ValueError):
        return 1.0


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def webhook_secret_is_valid(provided_secret: str | None) -> bool:
    expected_secret = os.getenv("CHANNEL_WEBHOOK_SECRET", "local-webhook-secret")
    return provided_secret is not None and hmac.compare_digest(provided_secret, expected_secret)


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
AUDIT_LOG_RETENTION_DAYS = max(1, int(os.getenv("AUDIT_LOG_RETENTION_DAYS", "365")))
_rate_limiter_redis: redis.Redis | None = None
_rate_limiter_memory: dict[str, list[float]] = {}


def _get_redis() -> redis.Redis | None:
    global _rate_limiter_redis
    if _rate_limiter_redis is None:
        try:
            _rate_limiter_redis = redis.from_url(REDIS_URL, decode_responses=True)
            _rate_limiter_redis.ping()
        except Exception:
            _rate_limiter_redis = None
    return _rate_limiter_redis


def enforce_webhook_rate_limit(request: Request) -> None:
    now = time.monotonic()
    window_seconds = 60
    window_start = now - window_seconds
    client_host = request.client.host if request.client else "unknown"
    limit = int(os.getenv("WEBHOOK_RATE_LIMIT_PER_MINUTE", "60"))

    redis_client = _get_redis()
    if redis_client is not None:
        key = f"webhook_ratelimit:{client_host}"
        pipe = redis_client.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, window_seconds + 1)
        results = pipe.execute()
        current_count = results[1]
        if current_count >= limit:
            raise HTTPException(status_code=429, detail="Webhook rate limit exceeded")
    else:
        request_times = [
            timestamp for timestamp in _rate_limiter_memory.get(client_host, []) if timestamp > window_start
        ]
        if len(request_times) >= limit:
            raise HTTPException(status_code=429, detail="Webhook rate limit exceeded")
        request_times.append(now)
        _rate_limiter_memory[client_host] = request_times


def find_ticket_by_external_id(db: Session, channel: str, external_id: str) -> TicketRecord | None:
    return db.scalar(
        select(TicketRecord).where(
            TicketRecord.channel == channel,
            TicketRecord.external_id == external_id,
        )
    )


def find_comment_by_external_id(db: Session, external_id: str) -> TicketCommentRecord | None:
    return db.scalar(
        select(TicketCommentRecord).where(TicketCommentRecord.external_id == external_id)
    )


def find_open_thread_ticket(db: Session, channel: str, thread_id: str) -> TicketRecord | None:
    return db.scalar(
        select(TicketRecord)
        .where(
            TicketRecord.channel == channel,
            TicketRecord.thread_id == thread_id,
            TicketRecord.status.in_(
                {
                    TicketStatus.open.value,
                    TicketStatus.in_progress.value,
                    TicketStatus.pending.value,
                }
            ),
        )
        .order_by(TicketRecord.created_at.desc())
    )


def handle_thread_reply(
    db: Session,
    ticket: TicketRecord,
    normalized: NormalizedMessage,
    channel: str,
    customer_email: str | None,
) -> TicketRecord:
    guardrail_report = evaluate(normalized.message, customer_email=customer_email)
    now = datetime.now(timezone.utc)
    db.add(
        TicketCommentRecord(
            id=str(uuid4()),
            ticket_id=ticket.id,
            author_id=normalized.customer_id,
            body=normalized.message,
            is_internal=False,
            external_id=normalized.external_id,
            created_at=now,
        )
    )
    status_changed = []
    if ticket.status == TicketStatus.pending.value:
        ticket.status = TicketStatus.open.value
        status_changed.append(TicketStatus.pending.value)
    ticket.updated_at = now
    if guardrail_report.is_risky:
        ticket.requires_human_review = True
        if ticket.escalation_status == EscalationStatus.none.value:
            ticket.escalation_status = EscalationStatus.pending.value
            ticket.escalated_at = now
            guardrail_categories = sorted({violation.category for violation in guardrail_report.violations})
            ticket.escalation_reason = "Guardrail: " + ", ".join(guardrail_categories)
        if guardrail_report.violations:
            ticket.guardrail_status = "flagged"
            ticket.guardrail_hits = [
                violation.model_dump() for violation in guardrail_report.violations
            ]
    add_audit_log(
        db,
        None,
        "channel.thread_reply",
        "ticket",
        ticket.id,
        {
            "channel": channel,
            "external_id": normalized.external_id,
            "thread_id": normalized.thread_id,
            "status_changed": status_changed,
        },
    )
    if guardrail_report.violations:
        add_audit_log(
            db,
            None,
            "ticket.guardrail_flagged",
            "ticket",
            ticket.id,
            {
                "status": ticket.guardrail_status,
                "violation_count": len(guardrail_report.violations),
                "types": sorted({violation.rule_type.value for violation in guardrail_report.violations}),
            },
        )
    db.commit()
    db.refresh(ticket)
    return ticket


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> UserRecord:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        user_id = decode_access_token(credentials.credentials)
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None

    user = db.get(UserRecord, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


@app.get("/knowledge/search", response_model=list[KnowledgeMatch])
def knowledge_search(
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=5, ge=1, le=10),
    _: UserRecord = Depends(get_current_user),
) -> list[KnowledgeMatch]:
    return search_knowledge(q, limit=limit)


def require_admin(current_user: UserRecord = Depends(get_current_user)) -> UserRecord:
    if current_user.role != UserRole.admin.value:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return current_user


@app.post("/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register_user(user_data: RegisterRequest, db: Session = Depends(get_db)) -> UserResponse:
    normalized_email = str(user_data.email).lower()
    existing_user = db.scalar(select(UserRecord).where(UserRecord.email == normalized_email))
    if existing_user is not None:
        raise HTTPException(status_code=409, detail="Email is already registered")

    record = UserRecord(
        id=str(uuid4()),
        email=normalized_email,
        password_hash=hash_password(user_data.password),
        role=UserRole.customer.value,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(record)
    add_audit_log(db, None, "user.created", "user", record.id, {"role": record.role})
    db.commit()
    db.refresh(record)
    return to_user(record)


@app.post("/admin/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_staff_user(
    user_data: StaffUserCreate,
    current_admin: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> UserResponse:
    normalized_email = str(user_data.email).lower()
    existing_user = db.scalar(select(UserRecord).where(UserRecord.email == normalized_email))
    if existing_user is not None:
        raise HTTPException(status_code=409, detail="Email is already registered")

    record = UserRecord(
        id=str(uuid4()),
        email=normalized_email,
        password_hash=hash_password(user_data.password),
        role=user_data.role.value,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(record)
    add_audit_log(
        db,
        current_admin.id,
        "user.created",
        "user",
        record.id,
        {"role": record.role},
    )
    db.commit()
    db.refresh(record)
    return to_user(record)


@app.get("/admin/users", response_model=list[UserResponse])
def list_users(
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[UserResponse]:
    query = select(UserRecord).order_by(UserRecord.created_at)
    return [to_user(record) for record in db.scalars(query).all()]


@app.get("/admin/audit-logs", response_model=list[AuditLogResponse])
def list_audit_logs(
    action: str | None = None,
    entity_type: str | None = None,
    actor_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[AuditLogResponse]:
    query = select(AuditLogRecord).order_by(AuditLogRecord.created_at.desc())
    if action is not None:
        query = query.where(AuditLogRecord.action == action)
    if entity_type is not None:
        query = query.where(AuditLogRecord.entity_type == entity_type)
    if actor_id is not None:
        query = query.where(AuditLogRecord.actor_id == actor_id)
    query = query.offset(offset).limit(limit)
    return [to_audit_log(record) for record in db.scalars(query).all()]


class PurgeAuditLogsResponse(BaseModel):
    deleted: int
    retention_days: int


@app.post("/admin/audit-logs/purge", response_model=PurgeAuditLogsResponse)
def purge_audit_logs(
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> PurgeAuditLogsResponse:
    cutoff = datetime.now(timezone.utc) - timedelta(days=AUDIT_LOG_RETENTION_DAYS)
    deleted = db.execute(
        delete(AuditLogRecord).where(AuditLogRecord.created_at < cutoff)
    ).rowcount
    db.commit()
    return PurgeAuditLogsResponse(deleted=deleted, retention_days=AUDIT_LOG_RETENTION_DAYS)


@app.get("/admin/analytics/sla", response_model=SlaMetrics)
def sla_metrics(
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> SlaMetrics:
    records = db.scalars(select(TicketRecord)).all()
    now = datetime.now(timezone.utc)
    resolved_records = [record for record in records if record.resolved_at is not None]
    resolution_hours = [
        (as_utc(record.resolved_at) - as_utc(record.created_at)).total_seconds() / 3600
        for record in resolved_records
    ]
    return SlaMetrics(
        total_tickets=len(records),
        open_tickets=sum(
            record.status
            in {TicketStatus.open.value, TicketStatus.in_progress.value, TicketStatus.pending.value}
            for record in records
        ),
        overdue_tickets=sum(
            record.sla_due_at is not None
            and as_utc(record.sla_due_at) < now
            and record.status not in {TicketStatus.resolved.value, TicketStatus.closed.value}
            for record in records
        ),
        resolved_tickets=len(resolved_records),
        average_resolution_hours=(
            round(sum(resolution_hours) / len(resolution_hours), 2)
            if resolution_hours
            else None
        ),
    )


@app.get("/admin/analytics/feedback", response_model=FeedbackAnalytics)
def feedback_analytics(
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> FeedbackAnalytics:
    records = db.scalars(select(TicketRecord)).all()
    feedback_records = db.scalars(select(FeedbackRecord)).all()

    ratings = [feedback.rating for feedback in feedback_records]
    resolved_tickets = [record for record in records if record.resolved_at is not None]
    resolved_ids = {record.id for record in resolved_tickets}

    manually_handled_ids = {
        record.id
        for record in records
        if record.reviewed_by is not None
        or record.escalation_status
        in {EscalationStatus.approved.value, EscalationStatus.rejected.value}
        or record.first_response_at is not None
    }
    resolved_count = len(resolved_tickets)

    return FeedbackAnalytics(
        total_feedback=len(feedback_records),
        average_rating=(
            round(sum(ratings) / len(ratings), 2) if ratings else None
        ),
        rating_distribution={star: ratings.count(star) for star in range(1, 6)},
        response_rate=(
            round(len(feedback_records) / resolved_count, 4) if resolved_count else None
        ),
        deflection_rate=(
            round((resolved_count - len(resolved_ids & manually_handled_ids)) / resolved_count, 4)
            if resolved_count
            else None
        ),
    )


@app.get("/admin/analytics/dashboard", response_model=DashboardAnalytics)
def analytics_dashboard(
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> DashboardAnalytics:
    records = db.scalars(select(TicketRecord)).all()
    feedback_records = db.scalars(select(FeedbackRecord)).all()
    now = datetime.now(timezone.utc)

    open_statuses = {TicketStatus.open.value, TicketStatus.in_progress.value, TicketStatus.pending.value}
    resolved_statuses = {TicketStatus.resolved.value, TicketStatus.closed.value}
    resolved_records = [record for record in records if record.resolved_at is not None]
    resolution_hours = [
        (as_utc(record.resolved_at) - as_utc(record.created_at)).total_seconds() / 3600
        for record in resolved_records
    ]

    ratings = [feedback.rating for feedback in feedback_records]
    resolved_ids = {record.id for record in resolved_records}
    manually_handled_ids = {
        record.id
        for record in records
        if record.reviewed_by is not None
        or record.escalation_status
        in {EscalationStatus.approved.value, EscalationStatus.rejected.value}
        or record.first_response_at is not None
    }
    resolved_count = len(resolved_records)

    escalated_records = [
        record for record in records if record.escalation_status != EscalationStatus.none.value
    ]
    pending_review_count = sum(
        record.escalation_status == EscalationStatus.pending.value for record in escalated_records
    )
    approved_count = sum(
        record.escalation_status == EscalationStatus.approved.value for record in escalated_records
    )
    rejected_count = sum(
        record.escalation_status == EscalationStatus.rejected.value for record in escalated_records
    )

    workload_by_assignee: dict[str, list[int]] = {}
    for record in records:
        assignee = record.assignee_id or "unassigned"
        entry = workload_by_assignee.setdefault(assignee, [0, 0])
        entry[0] += 1
        if record.status in resolved_statuses:
            entry[1] += 1
    workload = [
        WorkloadItem(
            assignee_id=None if assignee == "unassigned" else assignee,
            assigned_tickets=counts[0],
            resolved_tickets=counts[1],
        )
        for assignee, counts in sorted(
            workload_by_assignee.items(), key=lambda item: item[1][0], reverse=True
        )
    ]

    total_records = len(records)
    confidence_values = [record.confidence for record in records if record.confidence is not None]
    confidence_bins = {
        "low": sum(1 for value in confidence_values if value < 0.7),
        "medium": sum(1 for value in confidence_values if 0.7 <= value < 0.8),
        "high": sum(1 for value in confidence_values if 0.8 <= value < 0.9),
        "very_high": sum(1 for value in confidence_values if value >= 0.9),
    }

    def value_counts(field: str) -> list[ValueCount]:
        counter = Counter(getattr(record, field) for record in records)
        return [ValueCount(value=value, count=count) for value, count in counter.most_common()]

    return DashboardAnalytics(
        sla=SlaBreakdown(
            total_tickets=total_records,
            open_tickets=sum(record.status in open_statuses for record in records),
            in_progress_tickets=sum(record.status == TicketStatus.in_progress.value for record in records),
            pending_tickets=sum(record.status == TicketStatus.pending.value for record in records),
            overdue_tickets=sum(
                record.sla_due_at is not None
                and as_utc(record.sla_due_at) < now
                and record.status not in resolved_statuses
                for record in records
            ),
            resolved_tickets=resolved_count,
            average_resolution_hours=(
                round(sum(resolution_hours) / len(resolution_hours), 2)
                if resolution_hours
                else None
            ),
        ),
        csat=FeedbackAnalytics(
            total_feedback=len(feedback_records),
            average_rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
            rating_distribution={star: ratings.count(star) for star in range(1, 6)},
            response_rate=(
                round(len(feedback_records) / resolved_count, 4) if resolved_count else None
            ),
            deflection_rate=(
                round((resolved_count - len(resolved_ids & manually_handled_ids)) / resolved_count, 4)
                if resolved_count
                else None
            ),
        ),
        escalation=EscalationAnalytics(
            total_escalated=len(escalated_records),
            pending_review=pending_review_count,
            approved=approved_count,
            rejected=rejected_count,
            escalation_rate=(
                round(len(escalated_records) / total_records, 4) if total_records else None
            ),
        ),
        workload=workload,
        channels=value_counts("channel"),
        priorities=value_counts("priority"),
        intents=value_counts("intent"),
        sentiments=value_counts("sentiment"),
        confidence=ConfidenceHistogram(
            low=confidence_bins["low"],
            medium=confidence_bins["medium"],
            high=confidence_bins["high"],
            very_high=confidence_bins["very_high"],
            average=(
                round(sum(confidence_values) / len(confidence_values), 4)
                if confidence_values
                else None
            ),
        ),
    )


@app.get("/admin/llm/usage", response_model=LlmUsageSummary)
def llm_usage_summary(
    _: UserRecord = Depends(require_admin),
) -> LlmUsageSummary:
    return LlmUsageSummary(**get_llm_usage_summary())


@app.post("/admin/llm/usage/reset", response_model=LlmUsageSummary)
def reset_llm_usage_endpoint(
    current_admin: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> LlmUsageSummary:
    reset_llm_usage()
    add_audit_log(db, current_admin.id, "llm.usage_reset", "llm", "", {})
    db.commit()
    return LlmUsageSummary(**get_llm_usage_summary())


@app.patch("/admin/users/{user_id}", response_model=UserResponse)
def update_user_status(
    user_id: UUID,
    user_update: UserStatusUpdate,
    current_admin: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> UserResponse:
    if str(user_id) == current_admin.id:
        raise HTTPException(status_code=400, detail="Administrators cannot deactivate themselves")

    record = db.get(UserRecord, str(user_id))
    if record is None:
        raise HTTPException(status_code=404, detail="User not found")
    previous_status = record.is_active
    record.is_active = user_update.is_active
    add_audit_log(
        db,
        current_admin.id,
        "user.status_changed",
        "user",
        record.id,
        {"from": previous_status, "to": record.is_active},
    )
    db.commit()
    db.refresh(record)
    return to_user(record)


@app.delete("/admin/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: UUID,
    current_admin: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> None:
    if str(user_id) == current_admin.id:
        raise HTTPException(status_code=400, detail="Administrators cannot delete themselves")
    record = db.get(UserRecord, str(user_id))
    if record is None:
        raise HTTPException(status_code=404, detail="User not found")
    if record.role == UserRole.admin.value:
        raise HTTPException(status_code=400, detail="Cannot delete another admin")
    add_audit_log(
        db,
        current_admin.id,
        "user.deleted",
        "user",
        record.id,
        {"email": record.email, "role": record.role},
    )
    db.delete(record)
    db.commit()


@app.post("/auth/login", response_model=LoginResponse)
def login_user(user_data: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    normalized_email = str(user_data.email).lower()
    record = db.scalar(select(UserRecord).where(UserRecord.email == normalized_email))
    if record is None or not record.is_active or not verify_password(
        user_data.password, record.password_hash
    ):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return LoginResponse(
        authenticated=True,
        user=to_user(record),
        access_token=create_access_token(record.id),
    )


@app.get("/auth/me", response_model=UserResponse)
def get_authenticated_user(current_user: UserRecord = Depends(get_current_user)) -> UserResponse:
    return to_user(current_user)


def ensure_ticket_access(ticket: TicketRecord, user: UserRecord) -> None:
    if user.role == UserRole.customer.value and ticket.customer_id != user.id:
        raise HTTPException(status_code=403, detail="You do not have access to this ticket")


@app.post("/tickets", response_model=Ticket, status_code=status.HTTP_201_CREATED)
def create_ticket(
    ticket_data: TicketCreate,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Ticket:
    if current_user.role == UserRole.customer.value and ticket_data.customer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Customers can only create their own tickets")
    triage_result = triage(ticket_data.message)
    guardrail_report = evaluate(ticket_data.message, customer_email=current_user.email)
    now = datetime.now(timezone.utc)
    requires_review = triage_result.requires_human_review or guardrail_report.is_risky
    escalation_status = EscalationStatus.pending if requires_review else EscalationStatus.none
    if triage_result.requires_human_review:
        escalation_reason = f"Sensitive {triage_result.intent.value} issue"
    elif guardrail_report.is_risky:
        guardrail_categories = sorted({violation.category for violation in guardrail_report.violations})
        escalation_reason = "Guardrail: " + ", ".join(guardrail_categories)
    else:
        escalation_reason = None
    record = TicketRecord(
        id=str(uuid4()),
        intent=triage_result.intent.value,
        priority=triage_result.priority.value,
        requires_human_review=requires_review,
        sentiment=triage_result.sentiment.value,
        confidence=triage_result.confidence,
        recommended_team=triage_result.recommended_team,
        triage_summary=triage_result.summary,
        escalation_status=escalation_status.value,
        escalation_reason=escalation_reason,
        escalated_at=now if requires_review else None,
        reviewed_by=None,
        sla_due_at=sla_deadline(triage_result.priority.value, now, ticket_data.channel),
        first_response_at=None,
        resolved_at=None,
        status=TicketStatus.open.value,
        assignee_id=None,
        guardrail_status="flagged" if guardrail_report.violations else "clean",
        guardrail_hits=(
            [violation.model_dump() for violation in guardrail_report.violations]
            if guardrail_report.violations
            else None
        ),
        created_at=now,
        updated_at=now,
        **ticket_data.model_dump(),
    )
    db.add(record)
    add_audit_log(
        db,
        current_user.id,
        "ticket.created",
        "ticket",
        record.id,
        {"intent": record.intent, "priority": record.priority},
    )
    if guardrail_report.violations:
        add_audit_log(
            db,
            current_user.id,
            "ticket.guardrail_flagged",
            "ticket",
            record.id,
            {
                "status": record.guardrail_status,
                "violation_count": len(guardrail_report.violations),
                "types": sorted({violation.rule_type.value for violation in guardrail_report.violations}),
            },
        )
    db.commit()
    db.refresh(record)
    enqueue_notification(
        "ticket.created",
        record.id,
        {
            "channel": record.channel,
            "customer_id": record.customer_id,
            "intent": record.intent,
            "priority": record.priority,
            "summary": record.message[:200],
        },
    )
    _maybe_auto_respond(db, record)
    _record_integration_sync(
        db,
        current_user.id,
        "ticket.created",
        record,
        sync_ticket_outbound(db, record, "ticket.created", ticket_payload=_integration_ticket_payload(record)),
    )
    return to_ticket(record)


@app.post("/webhooks/{channel}", response_model=Ticket, status_code=status.HTTP_201_CREATED)
async def receive_channel_message(
    channel: str,
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
    _: None = Depends(enforce_webhook_rate_limit),
    db: Session = Depends(get_db),
) -> Ticket:
    supported_channels = {"email", "chat", "slack", "whatsapp", "crm"}
    if channel not in supported_channels:
        raise HTTPException(status_code=404, detail="Unsupported channel")
    if not webhook_secret_is_valid(x_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    raw_payload = await request.json()

    normalized = normalize_message(channel, raw_payload)
    if normalized is None:
        raise HTTPException(status_code=400, detail=f"No adapter for channel: {channel}")

    customer_context = enrich_customer_context(db, normalized.customer_id)
    normalized.customer_context = customer_context

    customer_record = db.get(UserRecord, normalized.customer_id)
    customer_email = customer_record.email if customer_record else None

    if normalized.external_id:
        existing_ticket = find_ticket_by_external_id(db, channel, normalized.external_id)
        if existing_ticket is not None:
            return JSONResponse(
                content=to_ticket(existing_ticket).model_dump(mode="json"),
                status_code=200,
            )
        existing_comment = find_comment_by_external_id(db, normalized.external_id)
        if existing_comment is not None:
            parent_ticket = db.get(TicketRecord, existing_comment.ticket_id)
            if parent_ticket is not None:
                return JSONResponse(
                    content=to_ticket(parent_ticket).model_dump(mode="json"),
                    status_code=200,
                )

    if normalized.thread_id:
        open_thread = find_open_thread_ticket(db, channel, normalized.thread_id)
        if open_thread is not None:
            thread_reply = handle_thread_reply(
                db,
                open_thread,
                normalized,
                channel,
                customer_email,
            )
            enqueue_notification(
                "thread.reply",
                open_thread.id,
                {
                    "channel": channel,
                    "external_id": normalized.external_id,
                    "thread_id": normalized.thread_id,
                    "summary": normalized.message[:200],
                },
            )
            return JSONResponse(
                content=to_ticket(thread_reply).model_dump(mode="json"),
                status_code=200,
            )

    guardrail_report = evaluate(
        normalized.message,
        customer_email=customer_email,
    )
    triage_result = triage(normalized.message)
    now = datetime.now(timezone.utc)
    requires_review = triage_result.requires_human_review or guardrail_report.is_risky
    if triage_result.requires_human_review:
        escalation_reason = f"Sensitive {triage_result.intent.value} issue"
    elif guardrail_report.is_risky:
        guardrail_categories = sorted({violation.category for violation in guardrail_report.violations})
        escalation_reason = "Guardrail: " + ", ".join(guardrail_categories)
    else:
        escalation_reason = None
    record = TicketRecord(
        id=str(uuid4()),
        customer_id=normalized.customer_id,
        message=normalized.message,
        channel=channel,
        intent=triage_result.intent.value,
        priority=triage_result.priority.value,
        requires_human_review=requires_review,
        sentiment=triage_result.sentiment.value,
        confidence=triage_result.confidence,
        recommended_team=triage_result.recommended_team,
        triage_summary=triage_result.summary,
        escalation_status=EscalationStatus.pending.value if requires_review else EscalationStatus.none.value,
        escalation_reason=escalation_reason,
        escalated_at=now if requires_review else None,
        reviewed_by=None,
        sla_due_at=sla_deadline(triage_result.priority.value, now, channel),
        first_response_at=None,
        resolved_at=None,
        status=TicketStatus.open.value,
        assignee_id=None,
        guardrail_status="flagged" if guardrail_report.violations else "clean",
        guardrail_hits=(
            [violation.model_dump() for violation in guardrail_report.violations]
            if guardrail_report.violations
            else None
        ),
        external_id=normalized.external_id,
        thread_id=normalized.thread_id,
        created_at=now,
        updated_at=now,
        intake_metadata={
            "external_id": normalized.external_id,
            "thread_id": normalized.thread_id,
            "attachments": [att.model_dump() for att in normalized.attachments],
            "channel_metadata": normalized.channel_metadata,
            "received_at": normalized.received_at.isoformat(),
            "customer_context": customer_context.model_dump(mode="json") if customer_context else None,
        },
    )
    db.add(record)
    add_audit_log(
        db,
        None,
        "channel.ticket_created",
        "ticket",
        record.id,
        {
            "channel": channel,
            "external_id": normalized.external_id,
            "thread_id": normalized.thread_id,
            "attachment_count": len(normalized.attachments),
        },
    )
    if guardrail_report.violations:
        add_audit_log(
            db,
            None,
            "ticket.guardrail_flagged",
            "ticket",
            record.id,
            {
                "status": record.guardrail_status,
                "violation_count": len(guardrail_report.violations),
                "types": sorted({violation.rule_type.value for violation in guardrail_report.violations}),
            },
        )
    db.commit()
    db.refresh(record)
    enqueue_notification(
        "ticket.received",
        record.id,
        {
            "channel": channel,
            "customer_id": record.customer_id,
            "intent": record.intent,
            "priority": record.priority,
            "summary": record.message[:200],
            "external_id": normalized.external_id,
        },
    )
    _maybe_auto_respond(db, record)
    _record_integration_sync(
        db,
        None,
        "ticket.received",
        record,
        sync_ticket_outbound(db, record, "ticket.received", ticket_payload=_integration_ticket_payload(record)),
    )
    return to_ticket(record)


@app.patch("/tickets/{ticket_id}/escalation", response_model=Ticket)
def update_escalation(
    ticket_id: UUID,
    escalation_update: EscalationUpdate,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Ticket:
    if current_user.role not in {UserRole.agent.value, UserRole.admin.value}:
        raise HTTPException(status_code=403, detail="Support staff access required")

    record = db.get(TicketRecord, str(ticket_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if not record.requires_human_review:
        raise HTTPException(status_code=400, detail="Ticket does not require human review")

    previous_status = record.escalation_status
    record.escalation_status = escalation_update.status.value
    record.escalation_reason = escalation_update.reason or record.escalation_reason
    record.reviewed_by = current_user.id
    add_audit_log(
        db,
        current_user.id,
        "ticket.escalation_reviewed",
        "ticket",
        record.id,
        {
            "from": previous_status,
            "to": record.escalation_status,
            "reason": record.escalation_reason,
        },
    )
    db.commit()
    db.refresh(record)
    return to_ticket(record)


@app.get("/tickets", response_model=list[Ticket])
def list_tickets(
    ticket_status: TicketStatus | None = Query(default=None, alias="status"),
    customer_id: str | None = None,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Ticket]:
    query = select(TicketRecord).order_by(TicketRecord.created_at)
    if current_user.role == UserRole.customer.value:
        customer_id = current_user.id
    if ticket_status is not None:
        query = query.where(TicketRecord.status == ticket_status.value)
    if customer_id is not None:
        query = query.where(TicketRecord.customer_id == customer_id)
    return [to_ticket(record) for record in db.scalars(query).all()]


@app.get("/tickets/{ticket_id}", response_model=Ticket)
def get_ticket(
    ticket_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Ticket:
    record = db.get(TicketRecord, str(ticket_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ensure_ticket_access(record, current_user)
    return to_ticket(record)


@app.patch("/tickets/{ticket_id}", response_model=Ticket)
def update_ticket(
    ticket_id: UUID,
    ticket_update: TicketUpdate,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Ticket:
    if current_user.role == UserRole.customer.value:
        raise HTTPException(status_code=403, detail="Customers cannot update tickets")
    record = db.get(TicketRecord, str(ticket_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ensure_ticket_access(record, current_user)

    updated_fields = ticket_update.model_dump(exclude_unset=True)
    previous_values = {
        field: getattr(record, field)
        for field in updated_fields
    }
    if "status" in updated_fields:
        record.status = updated_fields["status"].value
        if record.status in {TicketStatus.resolved.value, TicketStatus.closed.value}:
            record.resolved_at = datetime.now(timezone.utc)
    if "assignee_id" in updated_fields:
        record.assignee_id = updated_fields["assignee_id"]
    add_audit_log(
        db,
        current_user.id,
        "ticket.updated",
        "ticket",
        record.id,
        {"from": previous_values, "to": updated_fields},
    )
    record.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)
    if "status" in updated_fields or "assignee_id" in updated_fields:
        _record_integration_sync(
            db,
            current_user.id,
            "ticket.updated",
            record,
            sync_ticket_outbound(
                db, record, "ticket.updated", ticket_payload=_integration_ticket_payload(record)
            ),
        )
    return to_ticket(record)


@app.delete("/tickets/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ticket(
    ticket_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    if current_user.role == UserRole.customer.value:
        raise HTTPException(status_code=403, detail="Customers cannot delete tickets")
    record = db.get(TicketRecord, str(ticket_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ensure_ticket_access(record, current_user)
    comment_count = len(
        db.scalars(
            select(TicketCommentRecord.id).where(TicketCommentRecord.ticket_id == str(ticket_id))
        ).all()
    )
    add_audit_log(
        db,
        current_user.id,
        "ticket.deleted",
        "ticket",
        record.id,
        {"message": record.message, "comments_removed": comment_count},
    )
    db.execute(delete(TicketCommentRecord).where(TicketCommentRecord.ticket_id == str(ticket_id)))
    db.delete(record)
    db.commit()


@app.post(
    "/tickets/{ticket_id}/comments",
    response_model=TicketComment,
    status_code=status.HTTP_201_CREATED,
)
def add_comment(
    ticket_id: UUID,
    comment_data: CommentCreate,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TicketComment:
    ticket = db.get(TicketRecord, str(ticket_id))
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ensure_ticket_access(ticket, current_user)

    record = TicketCommentRecord(
        id=str(uuid4()),
        ticket_id=str(ticket_id),
        author_id=current_user.id,
        created_at=datetime.now(timezone.utc),
        body=comment_data.body,
        is_internal=comment_data.is_internal,
    )
    if (
        current_user.role in {UserRole.agent.value, UserRole.admin.value}
        and ticket.first_response_at is None
    ):
        ticket.first_response_at = record.created_at
    db.add(record)
    add_audit_log(
        db,
        current_user.id,
        "ticket.comment_added",
        "ticket",
        ticket.id,
        {"comment_id": record.id, "is_internal": record.is_internal},
    )
    db.commit()
    db.refresh(record)
    if not record.is_internal:
        _record_integration_sync(
            db,
            current_user.id,
            "ticket.comment_added",
            ticket,
            sync_ticket_outbound(
                db,
                ticket,
                "ticket.comment_added",
                comment_payload={
                    "id": record.id,
                    "body": record.body,
                    "is_internal": record.is_internal,
                    "author_id": record.author_id,
                },
            ),
        )
    return to_comment(record)


@app.get("/tickets/{ticket_id}/comments", response_model=list[TicketComment])
def list_comments(
    ticket_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TicketComment]:
    ticket = db.get(TicketRecord, str(ticket_id))
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ensure_ticket_access(ticket, current_user)
    query = (
        select(TicketCommentRecord)
        .where(TicketCommentRecord.ticket_id == str(ticket_id))
        .order_by(TicketCommentRecord.created_at)
    )
    return [to_comment(record) for record in db.scalars(query).all()]


@app.post(
    "/tickets/{ticket_id}/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_feedback(
    ticket_id: UUID,
    feedback: FeedbackCreate,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FeedbackResponse:
    ticket = db.get(TicketRecord, str(ticket_id))
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ensure_ticket_access(ticket, current_user)
    if ticket.status not in {TicketStatus.resolved.value, TicketStatus.closed.value}:
        raise HTTPException(
            status_code=400,
            detail="Feedback can only be submitted for resolved tickets",
        )
    existing = db.scalar(
        select(FeedbackRecord).where(FeedbackRecord.ticket_id == str(ticket_id))
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="Feedback for this ticket already exists",
        )

    record = FeedbackRecord(
        id=str(uuid4()),
        ticket_id=str(ticket_id),
        rating=feedback.rating,
        comment=feedback.comment,
        created_at=datetime.now(timezone.utc),
    )
    db.add(record)
    add_audit_log(
        db,
        current_user.id,
        "ticket.feedback_submitted",
        "ticket",
        ticket.id,
        {"rating": feedback.rating, "feedback_id": record.id},
    )
    db.commit()
    db.refresh(record)
    return to_feedback(record)


@app.post("/tickets/{ticket_id}/response-draft", response_model=ResponseDraft)
def draft_ticket_response(
    ticket_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ResponseDraft:
    if current_user.role not in {UserRole.agent.value, UserRole.admin.value}:
        raise HTTPException(status_code=403, detail="Support staff access required")
    record = db.get(TicketRecord, str(ticket_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found")

    try:
        result = draft_reply(record.message)
    except LLMConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"LLM gateway is not configured: {exc}",
        ) from exc
    except LLMError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Response agent failed: {exc}",
        ) from exc

    add_audit_log(
        db,
        current_user.id,
        "response.draft_generated",
        "ticket",
        record.id,
        {
            "needs_review": result.needs_review,
            "confidence": result.confidence,
            "provider": result.provider,
            "model": result.model,
            "reasons": result.reasons,
        },
    )
    db.commit()
    return ResponseDraft(ticket_id=str(ticket_id), **result.model_dump())


def auto_respond_enabled() -> bool:
    return os.getenv("AUTO_RESPOND_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _auto_skip_result(ticket_id: str, reason: str) -> AutoRespondResponse:
    return AutoRespondResponse(ticket_id=ticket_id, action="skipped", reason=reason)


def run_auto_response(
    db: Session,
    ticket: TicketRecord,
    actor_id: str = AI_ASSISTANT_ID,
) -> AutoRespondResponse:
    now = datetime.now(timezone.utc)
    if ticket.status != TicketStatus.open.value:
        return _auto_skip_result(ticket.id, "ticket_not_open")
    if ticket.requires_human_review:
        return _auto_skip_result(ticket.id, "already_flagged_for_review")

    try:
        result = draft_reply(ticket.message)
    except LLMConfigError as exc:
        add_audit_log(
            db,
            actor_id,
            "response.auto_skipped",
            "ticket",
            ticket.id,
            {"reason": "llm_not_configured", "detail": str(exc)},
        )
        db.commit()
        return _auto_skip_result(ticket.id, "llm_not_configured")
    except LLMError as exc:
        add_audit_log(
            db,
            actor_id,
            "response.auto_skipped",
            "ticket",
            ticket.id,
            {"reason": "provider_error", "detail": str(exc)},
        )
        db.commit()
        return _auto_skip_result(ticket.id, "provider_error")

    if result.needs_review:
        ticket.status = TicketStatus.pending.value
        ticket.requires_human_review = True
        ticket.escalation_status = EscalationStatus.pending.value
        ticket.escalation_reason = ticket.escalation_reason or "AI could not answer confidently"
        ticket.escalated_at = now
        ticket.updated_at = now
        add_audit_log(
            db,
            actor_id,
            "response.auto_escalated",
            "ticket",
            ticket.id,
            {
                "confidence": result.confidence,
                "reasons": result.reasons,
            },
        )
        db.commit()
        db.refresh(ticket)
        return AutoRespondResponse(ticket_id=ticket.id, action="needs_review", draft=result)

    comment = TicketCommentRecord(
        id=str(uuid4()),
        ticket_id=ticket.id,
        author_id=AI_ASSISTANT_ID,
        body=result.draft,
        is_internal=False,
        created_at=now,
    )
    db.add(comment)
    ticket.status = TicketStatus.resolved.value
    ticket.requires_human_review = False
    ticket.escalation_status = EscalationStatus.none.value
    ticket.first_response_at = ticket.first_response_at or now
    ticket.resolved_at = now
    ticket.updated_at = now
    add_audit_log(
        db,
        actor_id,
        "response.auto_sent",
        "ticket",
        ticket.id,
        {
            "confidence": result.confidence,
            "reasons": result.reasons,
            "citations": [citation.document_id for citation in result.citations],
        },
    )
    db.commit()
    return AutoRespondResponse(
        ticket_id=ticket.id,
        action="auto_sent",
        comment_id=comment.id,
        draft=result,
    )


def _maybe_auto_respond(db: Session, record: TicketRecord) -> None:
    if not auto_respond_enabled():
        return
    try:
        run_auto_response(db, record, actor_id=AI_ASSISTANT_ID)
    except Exception as exc:
        add_audit_log(
            db,
            AI_ASSISTANT_ID,
            "response.auto_skipped",
            "ticket",
            record.id,
            {"reason": "unexpected_error", "detail": str(exc)},
        )
        db.commit()


@app.post("/tickets/{ticket_id}/auto-respond", response_model=AutoRespondResponse)
def auto_respond_ticket(
    ticket_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AutoRespondResponse:
    if current_user.role not in {UserRole.agent.value, UserRole.admin.value}:
        raise HTTPException(status_code=403, detail="Support staff access required")
    record = db.get(TicketRecord, str(ticket_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return run_auto_response(db, record, actor_id=current_user.id)