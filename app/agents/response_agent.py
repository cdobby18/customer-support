"""Response Agent: KB-grounded draft replies with guardrails.

Given a customer message the agent retrieves top knowledge-base matches,
renders them into the ``response.draft_json`` prompt, calls the LLM gateway
for a structured draft, validates it through the deterministic guardrail
``validate_response`` layer, and decides whether the draft may be sent
directly or needs human review.

Review is required when the draft is empty, no KB matches were found, the
model confident or retrieval score falls below ``RESPONSE_CONFIDENCE_THRESHOLD``
(default 0.75), guardrail violations are present, or the model recommends
escalation.
"""

import os
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.agents.guardrails import Violation, validate_response
from app.agents.knowledge import KnowledgeMatch, search_knowledge
from app.agents.llm import (
    LLMContractError,
    LLMProvider,
    generate,
    generate_structured,
)
from app.agents.prompts import get_template

NO_KB_FALLBACK = (
    "I don't have enough information to answer that yet, so a specialist will "
    "review your request and follow up shortly."
)


class DraftCitation(BaseModel):
    document_id: str
    title: str
    source: str
    score: float = Field(ge=0)


class DraftResult(BaseModel):
    draft: str
    citations: list[DraftCitation] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    needs_review: bool
    reasons: list[str] = Field(default_factory=list)
    guardrail_violations: list[Violation] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    template_version: int


class DraftContract(BaseModel):
    """The `response.draft_json` prompt's JSON contract.

    Deliberately *total*: every field coerces, so this contract can only refuse
    a payload that is not a JSON object at all. That matches what the draft
    agent can actually do with an answer - it needs usable text or it needs to
    escalate, and anything in between is a judgement call already covered by
    the confidence threshold. A stricter contract here would report ordinary
    model vagueness as a provider failure and bury the real parse failures.
    """

    draft: str = ""
    content: str = ""
    confidence: float | None = None
    citations: list[Any] = Field(default_factory=list)
    escalate: bool = False

    @field_validator("draft", "content", mode="before")
    @classmethod
    def _text_or_empty(cls, value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    @field_validator("confidence", mode="before")
    @classmethod
    def _numeric_or_none(cls, value: Any) -> float | None:
        # A model that answers "high" instead of 0.9 is not a contract
        # violation, it is a value this agent already knows how to replace
        # with the retrieval score.
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @field_validator("citations", mode="before")
    @classmethod
    def _citations_list(cls, value: Any) -> list[Any]:
        # One unusable label must not cost the agent the whole draft.
        return value if isinstance(value, list) else []

    @field_validator("escalate", mode="before")
    @classmethod
    def _truthy_escalate(cls, value: Any) -> bool:
        return value is True or value == "true"

    @property
    def text(self) -> str:
        return self.draft or self.content


def _render_excerpts(matches: list[KnowledgeMatch]) -> tuple[str, dict[str, DraftCitation]]:
    lines: list[str] = []
    citation_map: dict[str, DraftCitation] = {}
    for index, match in enumerate(matches, start=1):
        label = f"Source {index}"
        citation_map[label] = DraftCitation(
            document_id=match.document_id,
            title=match.title,
            source=match.source,
            score=round(match.score, 3),
        )
        lines.append(f"{label} ({match.source}): {match.excerpt}")
    return "\n".join(lines), citation_map


def _match_citations(raw: list[Any], citation_map: dict[str, DraftCitation]) -> list[DraftCitation]:
    """Resolve the model's labels against the prompt's excerpt map.

    `raw` is always a list by the time it gets here - `DraftContract` coerces
    anything else - so a bad shape is a contract concern, not a matching one.
    """
    citations: list[DraftCitation] = []
    seen: set[str] = set()
    for item in raw:
        label = str(item).strip()
        citation = citation_map.get(label)
        if citation is not None and citation.document_id not in seen:
            seen.add(citation.document_id)
            citations.append(citation)
    return citations


def _parse_confidence(raw: Any, matches: list[KnowledgeMatch]) -> float:
    if isinstance(raw, (int, float)):
        value = float(raw)
        if 0.0 <= value <= 1.0:
            return round(value, 3)
    if not matches:
        return 0.0
    best = max(match.score for match in matches)
    return round(max(0.0, min(1.0, best)), 3)


def _decision(
    draft_text: str,
    matches: list[KnowledgeMatch],
    confidence: float,
    escalate: bool,
    violations: list[GuardrailViolation],
    threshold: float,
    contract_failed: bool = False,
) -> tuple[list[str], bool]:
    reasons: list[str] = []
    # Recorded ahead of the derived reasons so the review trail says *why* the
    # draft is unusable ("the provider's answer did not match the contract")
    # rather than only that it came back empty.
    if contract_failed:
        reasons.append("invalid_llm_response")
    if not matches:
        reasons.append("no_knowledge_matches")
    if not draft_text:
        reasons.append("empty_draft")
    elif confidence < threshold:
        reasons.append("low_confidence")
    if violations:
        reasons.append("guardrail_violations")
    if escalate:
        reasons.append("agent_recommended_escalation")
    if not reasons:
        reasons.append("auto_reply_ready")
    return reasons, reasons != ["auto_reply_ready"]


def draft_reply(
    message: str,
    *,
    provider: LLMProvider | str | None = None,
    limit_kb: int = 5,
    confidence_threshold: float | None = None,
) -> DraftResult:
    if confidence_threshold is not None:
        threshold = float(confidence_threshold)
    else:
        threshold = float(os.getenv("RESPONSE_CONFIDENCE_THRESHOLD", "0.75"))
    matches = search_knowledge(message, limit=limit_kb)
    template = get_template("response.draft_json")

    if not matches:
        return DraftResult(
            draft=NO_KB_FALLBACK,
            citations=[],
            confidence=0.0,
            needs_review=True,
            reasons=["no_knowledge_matches"],
            guardrail_violations=[],
            provider="",
            model="",
            template_version=template.version,
        )

    excerpts, citation_map = _render_excerpts(matches)
    system = template.render(excerpts=excerpts)

    # A contract failure degrades rather than propagates: this is the path that
    # answers a customer, so the worst outcome must be a ticket in front of a
    # human, never a 500 on ticket creation. The empty payload forces
    # `empty_draft`, which already forces review, and the refusal is reported
    # separately so the review trail distinguishes "the model gave us nothing
    # usable" from "the model gave us nothing".
    contract_failed = False
    provider_name = model_name = ""
    try:
        completion = generate_structured(
            provider,
            message,
            system=system,
            temperature=0.2,
            max_tokens=700,
            contract=DraftContract,
            contract_name="response.draft_json",
        )
        payload = completion.data
        provider_name, model_name = completion.provider, completion.model
    except LLMContractError as exc:
        payload = DraftContract()
        provider_name, model_name = exc.provider, exc.model
        contract_failed = True

    draft_text = payload.text
    citations = _match_citations(payload.citations, citation_map)
    confidence = _parse_confidence(payload.confidence, matches)
    violations = validate_response(draft_text)
    reasons, needs_review = _decision(
        draft_text,
        matches,
        confidence,
        payload.escalate,
        violations,
        threshold,
        contract_failed=contract_failed,
    )

    return DraftResult(
        draft=draft_text,
        citations=citations,
        confidence=confidence,
        needs_review=needs_review,
        reasons=reasons,
        guardrail_violations=violations,
        provider=provider_name,
        model=model_name,
        template_version=template.version,
    )