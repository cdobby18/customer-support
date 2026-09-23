from enum import Enum

from pydantic import BaseModel, Field


class TriageIntent(str, Enum):
    account_access = "account_access"
    billing = "billing"
    fraud = "fraud"
    legal = "legal"
    technical_support = "technical_support"
    general_support = "general_support"


class TriagePriority(str, Enum):
    normal = "normal"
    high = "high"
    urgent = "urgent"


class TriageSentiment(str, Enum):
    neutral = "neutral"
    frustrated = "frustrated"
    angry = "angry"


class TriageResult(BaseModel):
    intent: TriageIntent
    priority: TriagePriority
    sentiment: TriageSentiment
    confidence: float = Field(ge=0, le=1)
    recommended_team: str
    requires_human_review: bool
    summary: str


def classify_ticket(message: str) -> TriageResult:
    normalized_message = message.lower()
    summary = " ".join(message.strip().split())[:240]

    if any(term in normalized_message for term in ("fraud", "stolen", "unauthorized")):
        return TriageResult(
            intent=TriageIntent.fraud,
            priority=TriagePriority.urgent,
            sentiment=_sentiment(normalized_message),
            confidence=0.98,
            recommended_team="fraud",
            requires_human_review=True,
            summary=summary,
        )
    if any(term in normalized_message for term in ("legal", "lawyer", "lawsuit")):
        return TriageResult(
            intent=TriageIntent.legal,
            priority=TriagePriority.urgent,
            sentiment=_sentiment(normalized_message),
            confidence=0.97,
            recommended_team="leadership",
            requires_human_review=True,
            summary=summary,
        )
    if any(term in normalized_message for term in ("refund", "billing", "charged", "payment", "subscription")):
        return TriageResult(
            intent=TriageIntent.billing,
            priority=TriagePriority.high,
            sentiment=_sentiment(normalized_message),
            confidence=0.94,
            recommended_team="billing",
            requires_human_review=True,
            summary=summary,
        )
    if any(term in normalized_message for term in ("password", "login", "access", "locked out")):
        return TriageResult(
            intent=TriageIntent.account_access,
            priority=TriagePriority.high,
            sentiment=_sentiment(normalized_message),
            confidence=0.9,
            recommended_team="account_support",
            requires_human_review=True,
            summary=summary,
        )
    if any(term in normalized_message for term in ("error", "bug", "broken", "not working", "crash")):
        return TriageResult(
            intent=TriageIntent.technical_support,
            priority=TriagePriority.normal,
            sentiment=_sentiment(normalized_message),
            confidence=0.86,
            recommended_team="engineering_support",
            requires_human_review=False,
            summary=summary,
        )
    return TriageResult(
        intent=TriageIntent.general_support,
        priority=TriagePriority.normal,
        sentiment=_sentiment(normalized_message),
        confidence=0.65,
        recommended_team="customer_support",
        requires_human_review=False,
        summary=summary,
    )


def _sentiment(normalized_message: str) -> TriageSentiment:
    angry_terms = ("furious", "lawsuit", "scam", "unacceptable")
    frustrated_terms = ("frustrated", "annoyed", "disappointed", "again")
    if any(term in normalized_message for term in angry_terms):
        return TriageSentiment.angry
    if any(term in normalized_message for term in frustrated_terms):
        return TriageSentiment.frustrated
    return TriageSentiment.neutral