from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
import hmac
import os
import time
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
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
from app.models import AuditLogRecord, TicketCommentRecord, TicketRecord, UserRecord, UserRole
from app.triage import TriageResult, classify_ticket
from app.intake import normalize_message, NormalizedMessage, enrich_customer_context


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


def validate_security_configuration() -> None:
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_security_configuration()
    init_db()
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
bearer_scheme = HTTPBearer(auto_error=False)


def triage(message: str) -> TriageResult:
    return classify_ticket(message)


def sla_deadline(priority: str, created_at: datetime) -> datetime:
    hours_by_priority = {"urgent": 4, "high": 8, "normal": 24}
    return created_at + timedelta(hours=hours_by_priority.get(priority, 24))


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def webhook_secret_is_valid(provided_secret: str | None) -> bool:
    expected_secret = os.getenv("CHANNEL_WEBHOOK_SECRET", "local-webhook-secret")
    return provided_secret is not None and hmac.compare_digest(provided_secret, expected_secret)


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
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
    now = datetime.now(timezone.utc)
    escalation_status = (
        EscalationStatus.pending
        if triage_result.requires_human_review
        else EscalationStatus.none
    )
    record = TicketRecord(
        id=str(uuid4()),
        intent=triage_result.intent.value,
        priority=triage_result.priority.value,
        requires_human_review=triage_result.requires_human_review,
        sentiment=triage_result.sentiment.value,
        confidence=triage_result.confidence,
        recommended_team=triage_result.recommended_team,
        triage_summary=triage_result.summary,
        escalation_status=escalation_status.value,
        escalation_reason=(
            f"Sensitive {triage_result.intent.value} issue"
            if triage_result.requires_human_review
            else None
        ),
        escalated_at=now if triage_result.requires_human_review else None,
        reviewed_by=None,
        sla_due_at=sla_deadline(triage_result.priority.value, now),
        first_response_at=None,
        resolved_at=None,
        status=TicketStatus.open.value,
        assignee_id=None,
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
    db.commit()
    db.refresh(record)
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

    triage_result = triage(normalized.message)
    now = datetime.now(timezone.utc)
    requires_review = triage_result.requires_human_review
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
        escalation_reason=f"Sensitive {triage_result.intent.value} issue" if requires_review else None,
        escalated_at=now if requires_review else None,
        reviewed_by=None,
        sla_due_at=sla_deadline(triage_result.priority.value, now),
        first_response_at=None,
        resolved_at=None,
        status=TicketStatus.open.value,
        assignee_id=None,
        created_at=now,
        updated_at=now,
        intake_metadata={
            "external_id": normalized.external_id,
            "thread_id": normalized.thread_id,
            "attachments": [att.model_dump() for att in normalized.attachments],
            "channel_metadata": normalized.channel_metadata,
            "received_at": normalized.received_at.isoformat(),
            "customer_context": customer_context.model_dump() if customer_context else None,
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
    db.commit()
    db.refresh(record)
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