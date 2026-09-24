"""Escalation Agent: risk scoring, team routing, and reviewer context summaries.

Task 6. Deterministic weighted risk scoring combines triage signals
(intent, priority, sentiment), customer tier, open-ticket volume, SLA
breach, and guardrail flags into a 0-100 score with an explainable level
(low/medium/high/critical). High-risk tickets are auto-routed to the
correct team queue (leadership override for critical risk). A best-effort
LLM context summary (``escalation.summary`` prompt template) is produced
for human reviewers, falling back to a deterministic summary when the LLM
is unavailable.
"""

import json
from enum import Enum

from pydantic import BaseModel, Field

from app.llm import generate, LLMNotConfigured, LLMError
from app.prompts import get_template


class EscalationRiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class EscalationRisk(BaseModel):
    score: float = Field(ge=0, le=100)
    level: EscalationRiskLevel
    drivers: list[str] = Field(default_factory=list)


class EscalationSummary(BaseModel):
    text: str
    provider: str = ""
    model: str = ""


_RISK_BY_INTENT = {
    "fraud": 30,
    "legal": 28,
    "billing": 20,
    "account_access": 14,
    "technical_support": 8,
    "general_support": 0,
}
_RISK_BY_PRIORITY = {"normal": 0, "high": 10, "urgent": 20}
_RISK_BY_SENTIMENT = {"neutral": 0, "frustrated": 10, "angry": 20}
_TIER_MULTIPLIER = {"standard": 1.0, "premium": 1.25, "vip": 1.5}
_OPEN_TICKET_PENALTY_MAX = 25
_SLA_BREACH_PENALTY = 25
_GUARDRAIL_PENALTY = 15
_LEVEL_TIERS = (
    (60.0, EscalationRiskLevel.critical),
    (40.0, EscalationRiskLevel.high),
    (20.0, EscalationRiskLevel.medium),
    (0.0, EscalationRiskLevel.low),
)
_DEFAULT_TEAM_BY_INTENT = {
    "fraud": "fraud",
    "legal": "leadership",
    "billing": "billing",
    "account_access": "account_support",
    "technical_support": "engineering_support",
    "general_support": "customer_support",
}


def _clamp(value: float) -> float:
    return round(min(100.0, max(0.0, value)), 1)


def _level_for(score: float) -> EscalationRiskLevel:
    for threshold, level in _LEVEL_TIERS:
        if score >= threshold:
            return level
    return EscalationRiskLevel.low


def score_escalation(
    *,
    intent: str,
    priority: str,
    sentiment: str,
    tier: str | None = None,
    open_tickets_count: int = 0,
    sla_breached: bool = False,
    guardrail_flagged: bool = False,
) -> EscalationRisk:
    """Combine triage + customer + SLA signals into an explainable risk score."""
    intent_points = _RISK_BY_INTENT.get(intent, 0)
    priority_points = _RISK_BY_PRIORITY.get(priority, 0)
    sentiment_points = _RISK_BY_SENTIMENT.get(sentiment, 0)
    base = intent_points + priority_points + sentiment_points
    scaled = base * _TIER_MULTIPLIER.get(tier, 1.0)

    open_tickets = min(max(int(open_tickets_count or 0), 0), 5)
    open_penalty = open_tickets * 5
    sla_penalty = _SLA_BREACH_PENALTY if sla_breached else 0
    guardrail_penalty = _GUARDRAIL_PENALTY if guardrail_flagged else 0

    score = _clamp(scaled + open_penalty + sla_penalty + guardrail_penalty)

    drivers: list[str] = []
    if intent_points:
        drivers.append(f"{intent.replace('_', ' ')} intent")
    if priority_points:
        drivers.append(f"{priority} priority")
    if sentiment_points:
        drivers.append(f"{sentiment} sentiment")
    if tier in {"premium", "vip"}:
        drivers.append(f"{tier} tier")
    if open_tickets:
        drivers.append(f"{open_tickets_count} open tickets")
    if sla_breached:
        drivers.append("sla breached")
    if guardrail_flagged:
        drivers.append("guardrail flagged")
    if not drivers:
        drivers.append("no risk drivers")

    return EscalationRisk(score=score, level=_level_for(score), drivers=drivers)


def route_for_risk(
    intent: str,
    recommended_team: str | None,
    level: EscalationRiskLevel,
) -> str:
    """Pick the team queue to auto-route to, escalating critical risk upward."""
    if level == EscalationRiskLevel.critical:
        return "leadership"
    if recommended_team:
        return recommended_team
    return _DEFAULT_TEAM_BY_INTENT.get(intent, "customer_support")


def _fallback_summary(details: dict) -> str:
    sentiment = str(details.get("sentiment") or "neutral").replace("_", " ")
    intent = str(details.get("intent") or "general").replace("_", " ")
    parts = [f"{sentiment} customer reporting a {intent} issue (priority {details.get('priority')})."]
    tier = details.get("tier")
    if tier and tier != "unknown":
        parts.append(f"Customer tier {tier.title()} with {details.get('open_tickets_count', 0)} open tickets.")
    if details.get("sla_breached"):
        parts.append("SLA deadline already breached.")
    guardrail_hits = details.get("guardrail_hits") or []
    if guardrail_hits:
        parts.append("Guardrail flags: " + ", ".join(sorted(set(guardrail_hits))) + ".")
    parts.append(f"Recommended route: {details.get('suggested_route', 'customer_support')}.")
    return " ".join(parts)


def summarize_escalation(
    message: str,
    *,
    intent: str,
    priority: str,
    sentiment: str,
    tier: str | None = None,
    open_tickets_count: int = 0,
    sla_breached: bool = False,
    guardrail_hits: list[str] | None = None,
    route: str = "customer_support",
) -> EscalationSummary:
    """Produce a reviewer context summary, degrading to a deterministic one."""
    details = {
        "message": message[:1000],
        "intent": intent,
        "priority": priority,
        "sentiment": sentiment,
        "tier": tier or "unknown",
        "open_tickets_count": open_tickets_count,
        "sla_breached": bool(sla_breached),
        "guardrail_hits": list(guardrail_hits or []),
        "suggested_route": route,
    }
    fallback = _fallback_summary(details)
    template = get_template("escalation.summary")
    system = template.render(details=json.dumps(details, indent=2))
    try:
        result = generate(None, message, system=system, temperature=0.2, max_tokens=160)
        text = result.text.strip()
        provider, model = result.provider, result.model
    except (LLMNotConfigured, LLMError):
        text, provider, model = "", "", ""
    return EscalationSummary(text=text or fallback, provider=provider, model=model)