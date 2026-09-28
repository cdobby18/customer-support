"""Automatic first-response behaviour for AI-handled tickets.

Kept out of app/main.py (and out of the route module) because it is business
logic rather than HTTP handling: app/routers/tickets.py and the intake/webhook
paths all trigger it.
"""

import os
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.llm import LLMConfigError, LLMError
from app.models import TicketCommentRecord, TicketRecord
from app.response_agent import draft_reply
from app.schemas import (
    AI_ASSISTANT_ID,
    AutoRespondResponse,
    EscalationStatus,
    TicketStatus,
    add_audit_log,
)
from app.support import (
    as_utc,
    build_escalation_intelligence,
    customer_context_from_record,
)


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
        tier, open_count = customer_context_from_record(ticket)
        sla_breached = ticket.sla_due_at is not None and as_utc(ticket.sla_due_at) < now
        escalation_fields = build_escalation_intelligence(
            message=ticket.message,
            intent=ticket.intent,
            priority=ticket.priority,
            sentiment=ticket.sentiment or "neutral",
            recommended_team=ticket.recommended_team,
            tier=tier,
            open_tickets_count=open_count,
            sla_breached=sla_breached,
            guardrail_flagged=ticket.guardrail_status == "flagged",
            guardrail_hits=sorted(
                {hit.get("category") for hit in (ticket.guardrail_hits or []) if isinstance(hit, dict)}
            ) or None,
            always_summary=True,
        )
        ticket.status = TicketStatus.pending.value
        ticket.requires_human_review = True
        ticket.escalation_status = EscalationStatus.pending.value
        ticket.escalation_reason = ticket.escalation_reason or "AI could not answer confidently"
        ticket.escalated_at = now
        ticket.updated_at = now
        ticket.risk_score = escalation_fields["risk_score"]
        ticket.risk_level = escalation_fields["risk_level"]
        ticket.escalation_summary = escalation_fields["escalation_summary"]
        ticket.escalation_route = escalation_fields["escalation_route"]
        add_audit_log(
            db,
            actor_id,
            "response.auto_escalated",
            "ticket",
            ticket.id,
            {
                "confidence": result.confidence,
                "reasons": result.reasons,
                "risk_level": ticket.risk_level,
                "route": ticket.escalation_route,
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
