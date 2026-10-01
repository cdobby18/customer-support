"""Ticket lifecycle routes: intake, triage updates, comments, feedback and AI drafts."""

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import delete, func, or_, select
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
from app.api.dependencies import get_current_user, is_staff, require_staff
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
    TicketAttachment,
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
    TicketAttachmentRecord,
    TicketCommentRecord,
    TicketRecord,
    UserRecord,
    UserRole,
)
from app.core.workers import enqueue_notification


DEFAULT_TICKET_LIMIT = 50
MAX_TICKET_LIMIT = 200


def _validated_assignee_id(db: Session, assignee_id: str | None) -> str | None:
    """Resolve an assignee reference, or explain why it cannot be one.

    `assignee_id` is a bare string on the ticket, and the admin workload report
    buckets tickets by it, so an unchecked typo becomes a phantom assignee that
    shows up as a column of work owned by nobody. `None` is allowed and means
    unassigned.
    """
    if assignee_id is None:
        return None
    assignee = db.get(UserRecord, assignee_id)
    if assignee is None:
        raise HTTPException(status_code=404, detail="Assignee not found")
    if not assignee.is_active:
        raise HTTPException(status_code=400, detail="Assignee is not an active user")
    if assignee.role not in {UserRole.agent.value, UserRole.admin.value}:
        raise HTTPException(status_code=400, detail="Tickets can only be assigned to staff")
    return assignee.id


def ensure_ticket_access(ticket: TicketRecord, user: UserRecord) -> None:
    if not is_staff(user) and ticket.customer_id != user.id:
        raise HTTPException(status_code=403, detail="You do not have access to this ticket")


def build_ticket_stats(db: Session, tickets: list[TicketRecord]) -> dict[str, dict]:
    """Public (non-internal) message stats per ticket, newest first."""
    if not tickets:
        return {}
    customers = {record.id: record.customer_id for record in tickets}
    stats: dict[str, dict] = {record.id: {} for record in tickets}
    comments = db.scalars(
        select(TicketCommentRecord)
        .where(
            TicketCommentRecord.ticket_id.in_([record.id for record in tickets]),
            TicketCommentRecord.is_internal.is_(False),
        )
        .order_by(TicketCommentRecord.created_at.desc())
    ).all()
    for comment in comments:
        entry = stats[comment.ticket_id]
        entry["message_count"] = entry.get("message_count", 0) + 1
        if "last_message_at" not in entry:
            entry["last_message_at"] = comment.created_at
            entry["last_message_external"] = comment.author_id != customers[comment.ticket_id]
    return stats


router = APIRouter()

# The only channel a customer may originate; channels are otherwise supplied by
# the integration that received the message.
CUSTOMER_CHANNEL = "web"


@router.post("/tickets", response_model=Ticket, status_code=status.HTTP_201_CREATED)
def create_ticket(
    ticket_data: TicketCreate,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Ticket:
    if not is_staff(current_user) and ticket_data.customer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Customers can only create their own tickets")
    if not is_staff(current_user) and ticket_data.channel != CUSTOMER_CHANNEL:
        # The channel selects the SLA multiplier, so a customer choosing it
        # would be choosing their own deadline.
        raise HTTPException(
            status_code=403,
            detail=f"Customers can only open tickets on the {CUSTOMER_CHANNEL} channel",
        )
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
    return to_ticket(record, staff_view=is_staff(current_user))



@router.patch("/tickets/{ticket_id}/escalation", response_model=Ticket)
def update_escalation(
    ticket_id: UUID,
    escalation_update: EscalationUpdate,
    current_user: UserRecord = Depends(require_staff),
    db: Session = Depends(get_db),
) -> Ticket:
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
    response: Response,
    ticket_status: TicketStatus | None = Query(default=None, alias="status"),
    escalation_status: EscalationStatus | None = Query(default=None),
    customer_id: str | None = None,
    q: str | None = None,
    limit: int = Query(default=DEFAULT_TICKET_LIMIT, ge=1, le=MAX_TICKET_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Ticket]:
    staff_view = is_staff(current_user)
    # Newest first: the queue list is paginated, so ascending order would put
    # the oldest rows on page one and hide every new conversation until the
    # user paged all the way down. The client still re-sorts the loaded page by
    # SLA urgency.
    query = select(TicketRecord).order_by(TicketRecord.created_at.desc())
    if not staff_view:
        # A customer-supplied customer_id filter must not widen the result set.
        customer_id = current_user.id
    if ticket_status is not None:
        query = query.where(TicketRecord.status == ticket_status.value)
    if escalation_status is not None:
        # Server-side so the escalation panel can ask for exactly the rows it
        # draws. Filtering client-side meant downloading every ticket in the
        # table to display the handful awaiting review.
        query = query.where(TicketRecord.escalation_status == escalation_status.value)
    if customer_id is not None:
        query = query.where(TicketRecord.customer_id == customer_id)
    if q:
        # The queue list is paginated, so the search box has to run against the
        # whole table here; filtering only the loaded page would silently hide
        # older matches.
        pattern = f"%{q.strip()}%"
        query = query.where(
            or_(
                TicketRecord.message.ilike(pattern),
                TicketRecord.intent.ilike(pattern),
                TicketRecord.customer_id.ilike(pattern),
            )
        )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    query = query.offset(offset).limit(limit)
    records = db.scalars(query).all()
    stats = build_ticket_stats(db, records)
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Has-More"] = "true" if offset + len(records) < total else "false"
    return [
        to_ticket(record, staff_view=staff_view, **stats[record.id])
        for record in records
    ]



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
    stats = build_ticket_stats(db, [record])
    return to_ticket(record, staff_view=is_staff(current_user), **stats[record.id])



@router.patch("/tickets/{ticket_id}", response_model=Ticket)
def update_ticket(
    ticket_id: UUID,
    ticket_update: TicketUpdate,
    current_user: UserRecord = Depends(require_staff),
    db: Session = Depends(get_db),
) -> Ticket:
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
        record.assignee_id = _validated_assignee_id(db, updated_fields["assignee_id"])
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
    return to_ticket(record, staff_view=True)



@router.delete("/tickets/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ticket(
    ticket_id: UUID,
    current_user: UserRecord = Depends(require_staff),
    db: Session = Depends(get_db),
) -> None:
    record = db.get(TicketRecord, str(ticket_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ensure_ticket_access(record, current_user)
    comment_count = len(
        db.scalars(
            select(TicketCommentRecord.id).where(TicketCommentRecord.ticket_id == str(ticket_id))
        ).all()
    )
    attachments = db.scalars(
        select(TicketAttachmentRecord).where(
            TicketAttachmentRecord.ticket_id == str(ticket_id)
        )
    ).all()
    storage_paths = [row.storage_path for row in attachments]

    add_audit_log(
        db,
        current_user.id,
        "ticket.deleted",
        "ticket",
        record.id,
        {
            "message": record.message,
            "comments_removed": comment_count,
            "attachments_removed": len(attachments),
        },
    )
    db.execute(delete(TicketAttachmentRecord).where(TicketAttachmentRecord.ticket_id == str(ticket_id)))
    db.execute(delete(TicketCommentRecord).where(TicketCommentRecord.ticket_id == str(ticket_id)))
    db.delete(record)
    db.commit()
    # After the commit, so a rollback cannot leave rows pointing at deleted bytes.
    # Attachment bytes are customer PII: deleting the row without the file left
    # them on disk indefinitely, and nothing else ever reached them.
    removed_files = _remove_attachment_files(storage_paths)



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
        # An internal note is staff-only by definition; letting a customer set
        # the flag would let them write into a space they cannot read.
        is_internal=comment_data.is_internal and is_staff(current_user),
    )
    if (
        is_staff(current_user)
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
    if not is_staff(current_user):
        # Internal notes are staff-only. The ticket owner is not staff.
        query = query.where(TicketCommentRecord.is_internal.is_(False))
    return [to_comment(record) for record in db.scalars(query).all()]


def _upload_dir() -> str:
    path = os.getenv("UPLOAD_DIR", ".uploads")
    os.makedirs(path, exist_ok=True)
    return path


def _remove_attachment_files(storage_paths: list[str]) -> int:
    """Delete stored bytes, returning how many were actually removed.

    storage_path is a bare uuid4 written by this module rather than anything
    client-supplied, but the join is still resolved and checked against the
    upload directory so a corrupted row cannot delete something outside it.
    """
    root = os.path.realpath(_upload_dir())
    removed = 0
    for storage_path in storage_paths:
        candidate = os.path.realpath(os.path.join(root, storage_path))
        if os.path.dirname(candidate) != root:
            continue
        try:
            os.remove(candidate)
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            # A file we cannot delete must not fail the request: the rows are
            # already gone, and the alternative is a deleted ticket that looks
            # like it failed.
            continue
    return removed


def _safe_filename(filename: str) -> str:
    base = Path(filename).name.strip()
    base = re.sub(r"[^\w.\- ]", "_", base)
    return base[:120] or "file"


# Attachments are served back with a caller-influenced Content-Type and an
# attachment disposition, so a file that a browser would treat as active content
# is stored and replayed verbatim. Restrict uploads to types we are willing to
# serve, and confirm the bytes actually match the declared type before writing
# anything to disk.
ALLOWED_ATTACHMENT_TYPES = {
    "application/pdf": (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
    "text/plain": None,  # no magic bytes; the browser renders it as inert text
    "text/csv": None,
    "text/markdown": None,
    "application/json": None,
}

# Bytes that make a file dangerous regardless of the extension a client claims:
# an HTML tag, a script tag, or a PDF-style JavaScript action. Checked against the
# payload because content_type is attacker-supplied and easy to set to text/plain.
ACTIVE_CONTENT_MARKERS = (
    b"<script",
    b"<html",
    b"<iframe",
    b"<svg",
    b"javascript:",
    b"/javascript",
    b"<?php",
)


def _sniffed_content_type(payload: bytes) -> str | None:
    for content_type, magics in ALLOWED_ATTACHMENT_TYPES.items():
        if magics is None:
            continue
        if payload.startswith(magics):
            return content_type
    return None


def _looks_like_active_content(payload: bytes) -> bool:
    head = payload[:4096].lower()
    return any(marker in head for marker in ACTIVE_CONTENT_MARKERS)


def validate_attachment_payload(
    filename: str,
    declared_type: str | None,
    payload: bytes,
) -> str:
    """Return the content type to store, or explain why the file is refused.

    The declared type comes from the client, so it is treated as a hint and
    confirmed against the bytes. Storing the sniffed type rather than the
    declared one is what stops a `.html` upload announced as `text/plain` from
    being served back as HTML to the next person who opens the ticket.
    """
    normalized = (declared_type or "").split(";")[0].strip().lower()
    if normalized not in ALLOWED_ATTACHMENT_TYPES:
        allowed = ", ".join(sorted(ALLOWED_ATTACHMENT_TYPES))
        raise HTTPException(
            status_code=415,
            detail=f"{filename}: {normalized or 'unknown'} type is not allowed (allowed: {allowed})",
        )
    if _looks_like_active_content(payload):
        raise HTTPException(
            status_code=415,
            detail=f"{filename}: content looks like active markup and is not allowed",
        )
    sniffed = _sniffed_content_type(payload)
    if sniffed is not None:
        if normalized in {"text/plain", "text/markdown", "text/csv", "application/json"}:
            # Text-like types carry no magic bytes, so only refuse when the bytes
            # are unambiguously another allowed binary type (a PDF renamed .txt).
            pass
        elif normalized != sniffed:
            raise HTTPException(
                status_code=415,
                detail=(
                    f"{filename}: content is {sniffed}, not the declared {normalized}"
                ),
            )
        return sniffed
    return normalized


@router.post(
    "/tickets/{ticket_id}/attachments",
    response_model=list[TicketAttachment],
    status_code=status.HTTP_201_CREATED,
)
def upload_attachments(
    ticket_id: UUID,
    files: list[UploadFile] = File(...),
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TicketAttachment]:
    ticket = db.get(TicketRecord, str(ticket_id))
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ensure_ticket_access(ticket, current_user)
    max_files = int(os.getenv("MAX_TICKET_ATTACHMENTS", "5"))
    max_bytes = int(os.getenv("MAX_ATTACHMENT_BYTES", str(5 * 1024 * 1024)))
    if len(files) > max_files:
        raise HTTPException(status_code=400, detail=f"At most {max_files} files per upload")
    existing = db.scalars(
        select(TicketAttachmentRecord).where(TicketAttachmentRecord.ticket_id == str(ticket_id))
    ).all()
    if len(existing) + len(files) > max_files:
        raise HTTPException(status_code=400, detail=f"Ticket already has {len(existing)} attachments")
    created: list[TicketAttachment] = []
    staged: list[tuple[TicketAttachmentRecord, bytes]] = []
    # Validate every file before writing any of them: a rejection halfway through
    # would otherwise leave orphaned bytes on disk with no row pointing at them.
    for request_file in files:
        display_name = request_file.filename or "file"
        # Read one byte past the limit so an oversized file is detected without
        # buffering all of it into memory.
        payload = request_file.file.read(max_bytes + 1)
        request_file.file.close()
        if len(payload) > max_bytes:
            raise HTTPException(status_code=413, detail=f"{display_name} exceeds the {max_bytes // (1024 * 1024)}MB limit")
        if len(payload) == 0:
            raise HTTPException(status_code=400, detail=f"{display_name} is empty")
        stored_type = validate_attachment_payload(
            display_name, request_file.content_type, payload
        )
        record = TicketAttachmentRecord(
            id=str(uuid4()),
            ticket_id=str(ticket_id),
            filename=_safe_filename(request_file.filename or ""),
            content_type=stored_type,
            size=len(payload),
            storage_path=str(uuid4()),
            uploaded_by=current_user.id,
            created_at=datetime.now(timezone.utc),
        )
        staged.append((record, payload))
        created.append(
            TicketAttachment(
                id=UUID(record.id),
                ticket_id=UUID(record.ticket_id),
                filename=record.filename,
                content_type=record.content_type,
                size=record.size,
                uploaded_by=record.uploaded_by,
                created_at=record.created_at,
            )
        )
    for record, payload in staged:
        with open(os.path.join(_upload_dir(), record.storage_path), "wb") as handle:
            handle.write(payload)
        db.add(record)
    db.commit()
    return created


@router.get("/tickets/{ticket_id}/attachments", response_model=list[TicketAttachment])
def list_attachments(
    ticket_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TicketAttachment]:
    ticket = db.get(TicketRecord, str(ticket_id))
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ensure_ticket_access(ticket, current_user)
    records = db.scalars(
        select(TicketAttachmentRecord)
        .where(TicketAttachmentRecord.ticket_id == str(ticket_id))
        .order_by(TicketAttachmentRecord.created_at)
    ).all()
    return [
        TicketAttachment(
            id=UUID(record.id),
            ticket_id=UUID(record.ticket_id),
            filename=record.filename,
            content_type=record.content_type,
            size=record.size,
            uploaded_by=record.uploaded_by,
            created_at=record.created_at,
        )
        for record in records
    ]


@router.get("/tickets/{ticket_id}/attachments/{attachment_id}/content")
def download_attachment(
    ticket_id: UUID,
    attachment_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    ticket = db.get(TicketRecord, str(ticket_id))
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ensure_ticket_access(ticket, current_user)
    record = db.scalars(
        select(TicketAttachmentRecord).where(
            TicketAttachmentRecord.ticket_id == str(ticket_id),
            TicketAttachmentRecord.id == str(attachment_id),
        )
    ).one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    path = os.path.join(_upload_dir(), record.storage_path)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Attachment content is missing")
    with open(path, "rb") as handle:
        content = handle.read()
    return Response(
        content=content,
        media_type=record.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{record.filename}"',
            # Defence in depth: rows written before the upload allowlist existed
            # can still carry an HTML content_type, and the browser decides what
            # to do with it, not us.
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )



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
    current_user: UserRecord = Depends(require_staff),
    db: Session = Depends(get_db),
) -> ResponseDraft:
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
    current_user: UserRecord = Depends(require_staff),
    db: Session = Depends(get_db),
) -> AutoRespondResponse:
    record = db.get(TicketRecord, str(ticket_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return run_auto_response(db, record, actor_id=current_user.id)



@router.post("/tickets/{ticket_id}/draft-decision", response_model=DraftDecisionResponse)
def decide_draft(
    ticket_id: UUID,
    decision_data: DraftDecisionRequest,
    current_user: UserRecord = Depends(require_staff),
    db: Session = Depends(get_db),
) -> DraftDecisionResponse:
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



