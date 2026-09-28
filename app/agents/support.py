"""Shared helpers used by more than one route module.

Extracted from app/main.py: SLA maths, escalation intelligence construction,
customer context, audit logging and outbound-integration bookkeeping.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.agents.escalation import (
    EscalationRiskLevel,
    route_for_risk,
    score_escalation,
    summarize_escalation,
)
from app.agents.triage import TriageResult, classify_ticket
from app.api.schemas import add_audit_log
from app.core.models import TicketRecord


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


def build_escalation_intelligence(
    *,
    message: str,
    intent: str,
    priority: str,
    sentiment: str,
    recommended_team: str | None,
    tier: str | None = None,
    open_tickets_count: int = 0,
    sla_breached: bool = False,
    guardrail_flagged: bool = False,
    guardrail_hits: list[str] | None = None,
    always_summary: bool = False,
) -> dict[str, object]:
    risk = score_escalation(
        intent=intent,
        priority=priority,
        sentiment=sentiment,
        tier=tier,
        open_tickets_count=open_tickets_count,
        sla_breached=sla_breached,
        guardrail_flagged=guardrail_flagged,
    )
    route = route_for_risk(intent, recommended_team, risk.level)
    summary = None
    if always_summary or risk.level in {EscalationRiskLevel.high, EscalationRiskLevel.critical}:
        summary = summarize_escalation(
            message,
            intent=intent,
            priority=priority,
            sentiment=sentiment,
            tier=tier,
            open_tickets_count=open_tickets_count,
            sla_breached=sla_breached,
            guardrail_hits=guardrail_hits,
            route=route,
        )
    return {
        "risk_score": risk.score,
        "risk_level": risk.level.value,
        "escalation_route": route,
        "escalation_summary": summary.text if summary else None,
    }


def customer_context_from_record(record: TicketRecord) -> tuple[str | None, int]:
    metadata = record.intake_metadata if isinstance(record.intake_metadata, dict) else {}
    context = metadata.get("customer_context")
    if not isinstance(context, dict):
        return None, 0
    tier = context.get("tier")
    try:
        open_tickets = int(context.get("open_tickets_count", 0) or 0)
    except (TypeError, ValueError):
        open_tickets = 0
    return (str(tier) if tier else None), open_tickets



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
