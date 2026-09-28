"""Inbound channel webhooks (email, chat, CRM, ...)."""

import hmac
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

import redis
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auto_response import _maybe_auto_respond
from app.database import get_db
from app.guardrails import evaluate
from app.integrations import sync_ticket_outbound
from app.intake import NormalizedMessage, enrich_customer_context, normalize_message
from app.models import (
    TicketCommentRecord,
    TicketRecord,
    UserRecord,
)
from app.schemas import (
    EscalationStatus,
    Ticket,
    TicketStatus,
    add_audit_log,
    to_ticket,
)
from app.support import (
    _integration_ticket_payload,
    _record_integration_sync,
    build_escalation_intelligence,
    sla_deadline,
    triage,
)
from app.workers import enqueue_notification

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


def webhook_secret_is_valid(provided_secret: str | None) -> bool:
    expected_secret = os.getenv("CHANNEL_WEBHOOK_SECRET", "local-webhook-secret")
    return provided_secret is not None and hmac.compare_digest(provided_secret, expected_secret)


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


router = APIRouter()


@router.post("/webhooks/{channel}", response_model=Ticket, status_code=status.HTTP_201_CREATED)
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
    escalation_fields = build_escalation_intelligence(
        message=normalized.message,
        intent=triage_result.intent.value,
        priority=triage_result.priority.value,
        sentiment=triage_result.sentiment.value,
        recommended_team=triage_result.recommended_team,
        tier=customer_context.tier if customer_context else None,
        open_tickets_count=customer_context.open_tickets_count if customer_context else 0,
        guardrail_flagged=guardrail_report.is_risky,
        guardrail_hits=(
            sorted({violation.category for violation in guardrail_report.violations})
            if guardrail_report.violations
            else None
        ),
        always_summary=requires_review,
    )
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
        risk_score=escalation_fields["risk_score"],
        risk_level=escalation_fields["risk_level"],
        escalation_summary=escalation_fields["escalation_summary"],
        escalation_route=escalation_fields["escalation_route"],
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
    if requires_review:
        add_audit_log(
            db,
            None,
            "escalation.auto_routed",
            "ticket",
            record.id,
            {"level": record.risk_level, "route": record.escalation_route},
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
        sync_ticket_outbound(record, "ticket.received", ticket_payload=_integration_ticket_payload(record)),
    )
    return to_ticket(record)
