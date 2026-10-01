"""Administrative routes: staff users, audit logs and analytics."""

import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agents.llm import get_llm_usage_summary, reset_llm_usage
from app.agents.support import as_utc
from app.api.dependencies import require_admin
from app.api.schemas import (
    AI_ASSISTANT_ID,
    AuditLogResponse,
    ConfidenceHistogram,
    DashboardAnalytics,
    EscalationAnalytics,
    EscalationStatus,
    FeedbackAnalytics,
    FeedbackDetail,
    LlmUsageSummary,
    SlaBreakdown,
    SlaMetrics,
    StaffUserCreate,
    TicketStatus,
    UserResponse,
    UserStatusUpdate,
    ValueCount,
    WorkloadItem,
    add_audit_log,
    to_audit_log,
    to_user,
)
from app.core.database import get_db
from app.core.models import (
    AuditLogRecord,
    FeedbackRecord,
    TicketCommentRecord,
    TicketRecord,
    UserRecord,
    UserRole,
)
from app.security.auth import hash_password

AUDIT_LOG_RETENTION_DAYS = max(1, int(os.getenv("AUDIT_LOG_RETENTION_DAYS", "365")))

router = APIRouter()


@router.post("/admin/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
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


@router.get("/admin/users", response_model=list[UserResponse])
def list_users(
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[UserResponse]:
    query = select(UserRecord).order_by(UserRecord.created_at)
    return [to_user(record) for record in db.scalars(query).all()]


@router.get("/admin/audit-logs", response_model=list[AuditLogResponse])
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


@router.post("/admin/audit-logs/purge", response_model=PurgeAuditLogsResponse)
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


@router.get("/admin/analytics/sla", response_model=SlaMetrics)
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


def _classify_resolutions(
    db: Session, records: list[TicketRecord]
) -> tuple[set[str], set[str]]:
    """Split resolved tickets into `resolved` and `deflected` id sets.

    Deflection means the ticket was resolved without staff effort. The previous
    definition keyed off `first_response_at`, which `run_auto_response` sets on
    the AI's own reply, so every AI-resolved ticket counted as manually handled
    and the rate was structurally always 0.0.

    Staff effort is a public reply from anyone other than the AI or the ticket's
    own customer, a recorded reviewer, or an approved/rejected escalation. A
    customer's follow-up is not staff effort, so it does not disqualify
    deflection.
    """
    resolved = [record for record in records if record.resolved_at is not None]
    resolved_ids = {record.id for record in resolved}
    if not resolved_ids:
        return set(), set()

    customer_of = {record.id: record.customer_id for record in resolved}
    staff_replied = {
        comment.ticket_id
        for comment in db.scalars(
            select(TicketCommentRecord).where(
                TicketCommentRecord.ticket_id.in_(resolved_ids),
                TicketCommentRecord.is_internal.is_(False),
            )
        ).all()
        if comment.author_id != AI_ASSISTANT_ID
        and comment.author_id != customer_of.get(comment.ticket_id)
    }

    human_reviewed = {
        record.id
        for record in resolved
        if record.reviewed_by is not None
        or record.escalation_status
        in {EscalationStatus.approved.value, EscalationStatus.rejected.value}
    }
    return resolved_ids, resolved_ids - staff_replied - human_reviewed


@router.get("/admin/analytics/feedback", response_model=FeedbackAnalytics)
def feedback_analytics(
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> FeedbackAnalytics:
    records = db.scalars(select(TicketRecord)).all()
    feedback_records = db.scalars(select(FeedbackRecord)).all()

    ratings = [feedback.rating for feedback in feedback_records]
    resolved_ids, deflected_ids = _classify_resolutions(db, records)
    resolved_count = len(resolved_ids)

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
            round(len(deflected_ids) / resolved_count, 4) if resolved_count else None
        ),
    )


@router.get("/admin/analytics/feedback-details", response_model=list[FeedbackDetail])
def feedback_details(
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[FeedbackDetail]:
    """Per-ticket CSAT records for the admin drill-down, newest first."""
    feedback_records = db.scalars(
        select(FeedbackRecord).order_by(FeedbackRecord.created_at.desc()).limit(200)
    ).all()
    ticket_ids = {record.ticket_id for record in feedback_records}
    tickets: dict[str, TicketRecord] = {}
    if ticket_ids:
        tickets = {
            record.id: record
            for record in db.scalars(
                select(TicketRecord).where(TicketRecord.id.in_(ticket_ids))
            ).all()
        }
    return [
        FeedbackDetail(
            id=feedback.id,
            ticket_id=feedback.ticket_id,
            message=tickets[feedback.ticket_id].message if feedback.ticket_id in tickets else None,
            customer_id=(
                tickets[feedback.ticket_id].customer_id if feedback.ticket_id in tickets else None
            ),
            rating=feedback.rating,
            comment=feedback.comment,
            created_at=feedback.created_at,
        )
        for feedback in feedback_records
    ]


@router.get("/admin/analytics/dashboard", response_model=DashboardAnalytics)
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
    resolved_ids, deflected_ids = _classify_resolutions(db, records)
    resolved_count = len(resolved_ids)

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
                round(len(deflected_ids) / resolved_count, 4)
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


@router.get("/admin/llm/usage", response_model=LlmUsageSummary)
def llm_usage_summary(
    _: UserRecord = Depends(require_admin),
) -> LlmUsageSummary:
    return LlmUsageSummary(**get_llm_usage_summary())


@router.post("/admin/llm/usage/reset", response_model=LlmUsageSummary)
def reset_llm_usage_endpoint(
    current_admin: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
) -> LlmUsageSummary:
    reset_llm_usage()
    add_audit_log(db, current_admin.id, "llm.usage_reset", "llm", "", {})
    db.commit()
    return LlmUsageSummary(**get_llm_usage_summary())


@router.patch("/admin/users/{user_id}", response_model=UserResponse)
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
    if record.role == UserRole.admin.value:
        # delete_user refuses to remove a peer admin; deactivating one reaches
        # the same end state (no login, live tokens rejected) through another
        # door, so it takes the same check.
        raise HTTPException(status_code=400, detail="Cannot deactivate another admin")
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


@router.delete("/admin/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
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

