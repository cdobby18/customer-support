"""Ticket lifecycle routes: intake, triage updates, comments, feedback and AI drafts."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agents.auto_response import (
    _maybe_auto_respond,
    run_auto_response,
)
from app.agents.guardrails import evaluate
from app.agents.intake import enrich_customer_context
from app.agents.integrations import sync_ticket_outbound
from app.agents.llm import LLMConfigError, LLMError
from app.agents.response_agent import draft_reply
from app.agents.support import (
    _integration_ticket_payload,
    _record_integration_sync,
    build_escalation_intelligence,
    sla_deadline,
    triage,
)
from app.api.dependencies import get_current_user
from app.api.schemas import (
    AutoRespondResponse,
    CommentCreate,
    DraftDecisionRequest,
    DraftDecisionResponse,
    EscalationStatus,
    EscalationUpdate,
    FeedbackCreate,
    FeedbackResponse,
    ResponseDraft,
    Ticket,
    TicketComment,
    TicketCreate,
    TicketStatus,
    TicketUpdate,
    add_audit_log,
    to_comment,
    to_feedback,
    to_ticket,
)
from app.core.database import get_db
from app.core.models import (
    FeedbackRecord,
    TicketCommentRecord,
    TicketRecord,
    UserRecord,
    UserRole,
)
from app.core.workers import enqueue_notification


def ensure_ticket_access(ticket: TicketRecord, user: UserRecord) -> None:
    if user.role == UserRole.customer.value and ticket.customer_id != user.id:
        raise HTTPException(status_code=403, detail="You do not have access to this ticket")


router = APIRouter()


@router.post("/tickets", response_model=Ticket, status_code=status.HTTP_201_CREATED)
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
    customer_context = enrich_customer_context(db, ticket_data.customer_id)
    escalation_fields = build_escalation_intelligence(
        message=ticket_data.message,
        intent=triage_result.intent.value,
        priority=triage_result.priority.value,
        sentiment=triage_result.sentiment.value,
        recommended_team=triage_result.recommended_team,
        tier=customer_context.tier,
        open_tickets_count=customer_context.open_tickets_count,
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
        created_at=now,
        updated_at=now,
        intake_metadata={"customer_context": customer_context.model_dump(mode="json")},
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
    if requires_review:
        add_audit_log(
            db,
            current_user.id,
            "escalation.auto_routed",
            "ticket",
            record.id,
            {"level": record.risk_level, "route": record.escalation_route},
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
        sync_ticket_outbound(record, "ticket.created", ticket_payload=_integration_ticket_payload(record)),
    )
    return to_ticket(record)



@router.patch("/tickets/{ticket_id}/escalation", response_model=Ticket)
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



@router.get("/tickets", response_model=list[Ticket])
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



@router.get("/tickets/{ticket_id}", response_model=Ticket)
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



@router.patch("/tickets/{ticket_id}", response_model=Ticket)
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
                    record, "ticket.updated", ticket_payload=_integration_ticket_payload(record)
                ),
        )
    return to_ticket(record)



@router.delete("/tickets/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
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



@router.post(
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



@router.get("/tickets/{ticket_id}/comments", response_model=list[TicketComment])
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



@router.post(
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



@router.post("/tickets/{ticket_id}/response-draft", response_model=ResponseDraft)
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




@router.post("/tickets/{ticket_id}/auto-respond", response_model=AutoRespondResponse)
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



@router.post("/tickets/{ticket_id}/draft-decision", response_model=DraftDecisionResponse)
def decide_draft(
    ticket_id: UUID,
    decision_data: DraftDecisionRequest,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DraftDecisionResponse:
    if current_user.role not in {UserRole.agent.value, UserRole.admin.value}:
        raise HTTPException(status_code=403, detail="Support staff access required")
    record = db.get(TicketRecord, str(ticket_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found")

    now = datetime.now(timezone.utc)

    if decision_data.decision == "approve":
        if not decision_data.body:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Draft body is required to approve",
            )
        comment = TicketCommentRecord(
            id=str(uuid4()),
            ticket_id=record.id,
            author_id=current_user.id,
            body=decision_data.body,
            is_internal=False,
            created_at=now,
        )
        db.add(comment)
        record.first_response_at = record.first_response_at or now
        resolved = decision_data.resolve
        if resolved:
            record.status = TicketStatus.resolved.value
            record.requires_human_review = False
            record.escalation_status = EscalationStatus.none.value
            record.escalation_reason = None
            record.resolved_at = now
        record.updated_at = now
        add_audit_log(
            db,
            current_user.id,
            "response.draft_approved",
            "ticket",
            record.id,
            {"comment_id": comment.id, "resolve": resolved, "is_internal": False},
        )
        db.commit()
        _record_integration_sync(
            db,
            current_user.id,
            "response.draft_approved",
            record,
            sync_ticket_outbound(
                    record,
                    "response.draft_approved",
                comment_payload={
                    "id": comment.id,
                    "body": comment.body,
                    "is_internal": False,
                    "author_id": comment.author_id,
                },
            ),
        )
        return DraftDecisionResponse(
            ticket_id=record.id,
            decision="approve",
            resolved=resolved,
            comment_id=comment.id,
        )

    note_id = None
    if decision_data.note:
        note = TicketCommentRecord(
            id=str(uuid4()),
            ticket_id=record.id,
            author_id=current_user.id,
            body=f"AI draft rejected: {decision_data.note}",
            is_internal=True,
            created_at=now,
        )
        db.add(note)
        note_id = note.id
        record.updated_at = now
    add_audit_log(
        db,
        current_user.id,
        "response.draft_rejected",
        "ticket",
        record.id,
        {"note_id": note_id, "is_internal": True},
    )
    db.commit()
    return DraftDecisionResponse(
        ticket_id=record.id,
        decision="reject",
        resolved=False,
        note_id=note_id,
    )



