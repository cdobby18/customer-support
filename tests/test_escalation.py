import pytest

from app.agents import llm, escalation


class FakeLLMProvider(llm.LLMProvider):
    name = "fake"

    def __init__(self, text: str):
        self._text = text

    def validate_config(self) -> None:
        return None

    def complete(self, messages, *, temperature=0.2, max_tokens=600, response_format=None):
        return llm.LLMResult(text=self._text, model="fake-model", provider=self.name, usage=llm.LLMUsage(total_tokens=1))


def test_baseline_risk_is_low() -> None:
    risk = escalation.score_escalation(
        intent="general_support",
        priority="normal",
        sentiment="neutral",
    )

    assert risk.score == 0.0
    assert risk.level == escalation.EscalationRiskLevel.low
    assert "no risk drivers" in risk.drivers


def test_technical_ticket_scores_low() -> None:
    risk = escalation.score_escalation(
        intent="technical_support",
        priority="normal",
        sentiment="neutral",
    )

    assert risk.score == 8.0
    assert risk.level == escalation.EscalationRiskLevel.low


def test_billing_high_priority_scores_medium() -> None:
    risk = escalation.score_escalation(
        intent="billing",
        priority="high",
        sentiment="neutral",
    )

    assert risk.score == 30.0
    assert risk.level == escalation.EscalationRiskLevel.medium


def test_fraud_is_critical() -> None:
    risk = escalation.score_escalation(
        intent="fraud",
        priority="urgent",
        sentiment="angry",
    )

    assert risk.score == 70.0
    assert risk.level == escalation.EscalationRiskLevel.critical
    for driver in ("fraud intent", "urgent priority", "angry sentiment"):
        assert driver in risk.drivers


def test_premium_tier_scales_risk() -> None:
    standard = escalation.score_escalation(intent="fraud", priority="urgent", sentiment="angry", tier="standard")
    premium = escalation.score_escalation(intent="fraud", priority="urgent", sentiment="angry", tier="premium")

    assert standard.score == 70.0
    assert premium.score == 87.5
    assert premium.level == escalation.EscalationRiskLevel.critical
    assert "premium tier" in premium.drivers


def test_sla_breach_and_open_tickets_add_risk() -> None:
    risk = escalation.score_escalation(
        intent="technical_support",
        priority="normal",
        sentiment="neutral",
        open_tickets_count=3,
        sla_breached=True,
    )

    assert risk.score == 8.0 + 15.0 + 25.0
    assert risk.level == escalation.EscalationRiskLevel.high
    assert "3 open tickets" in risk.drivers
    assert "sla breached" in risk.drivers


def test_open_ticket_penalty_is_capped() -> None:
    risk = escalation.score_escalation(
        intent="technical_support",
        priority="normal",
        sentiment="neutral",
        open_tickets_count=99,
    )

    assert risk.score == 8.0 + 25.0


def test_guardrail_flag_adds_risk() -> None:
    clean = escalation.score_escalation(intent="technical_support", priority="normal", sentiment="neutral")
    flagged = escalation.score_escalation(intent="technical_support", priority="normal", sentiment="neutral", guardrail_flagged=True)

    assert flagged.score == clean.score + 15.0
    assert "guardrail flagged" in flagged.drivers


def test_score_is_bounded_at_100() -> None:
    risk = escalation.score_escalation(
        intent="fraud",
        priority="urgent",
        sentiment="angry",
        tier="vip",
        open_tickets_count=9,
        sla_breached=True,
        guardrail_flagged=True,
    )

    assert risk.score == 100.0
    assert risk.score <= 100.0


def test_route_for_risk_prefers_leadership_on_critical() -> None:
    assert (
        escalation.route_for_risk(
            "billing",
            "billing",
            escalation.EscalationRiskLevel.critical,
        )
        == "leadership"
    )


def test_route_for_risk_uses_recommended_team() -> None:
    assert (
        escalation.route_for_risk(
            "technical_support",
            "engineering_support",
            escalation.EscalationRiskLevel.high,
        )
        == "engineering_support"
    )


def test_route_for_risk_falls_back_to_default_team() -> None:
    assert (
        escalation.route_for_risk(
            "unknown_intent",
            None,
            escalation.EscalationRiskLevel.low,
        )
        == "customer_support"
    )


def test_summary_falls_back_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    summary = escalation.summarize_escalation(
        "I was charged twice",
        intent="billing",
        priority="high",
        sentiment="frustrated",
        tier="premium",
        open_tickets_count=2,
        guardrail_hits=["refund_policy"],
        route="billing",
    )

    assert "billing" in summary.text
    assert "frustrated" in summary.text
    assert "premium" in summary.text.lower()
    assert "refund_policy" in summary.text
    assert summary.provider == ""


def test_summary_uses_llm_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        escalation,
        "generate",
        lambda *args, **kwargs: llm.LLMResult(
            text="VIP billing dispute, route to billing lead.",
            model="fake-model",
            provider="fake",
            usage=llm.LLMUsage(total_tokens=1),
        ),
    )
    summary = escalation.summarize_escalation(
        "I was charged twice",
        intent="billing",
        priority="high",
        sentiment="frustrated",
        route="billing",
    )

    assert summary.text == "VIP billing dispute, route to billing lead."
    assert summary.provider == "fake"
    assert summary.model == "fake-model"