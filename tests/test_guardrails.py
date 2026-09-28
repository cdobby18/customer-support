import pytest

from app.guardrails import (
    GuardrailType,
    Severity,
    evaluate,
    redact,
    screen_pii,
    validate_response,
)


def test_evaluate_detects_email_pii() -> None:
    report = evaluate("Please email me at john.doe@example.com for details")

    assert any(violation.rule_id == "pii.email" for violation in report.violations)
    assert report.has_pii is True
    assert report.redacted_text is not None
    assert "john.doe@example.com" not in report.redacted_text


def test_evaluate_detects_phone_pii() -> None:
    report = evaluate("Call me on 555-123-4567")

    assert any(violation.rule_id == "pii.phone" for violation in report.violations)


def test_evaluate_detects_ssn_as_high_risk() -> None:
    report = evaluate("My tax id is 123-45-6789")

    assert any(violation.rule_id == "pii.ssn" for violation in report.violations)
    assert report.is_risky is True


def test_evaluate_detects_luhn_valid_credit_card() -> None:
    report = evaluate("I used card 4111-1111-1111-1111 to pay")

    assert any(violation.rule_id == "pii.credit_card" for violation in report.violations)
    assert report.is_risky is True


def test_evaluate_skips_non_luhn_number_as_credit_card() -> None:
    report = evaluate("Reference number is 1234567890123456")

    assert not any(violation.rule_id == "pii.credit_card" for violation in report.violations)


def test_evaluate_detects_ipv4() -> None:
    report = evaluate("My server is at 192.168.1.1")

    assert any(violation.rule_id == "pii.ipv4" for violation in report.violations)


def test_redact_masks_email_and_phone() -> None:
    redacted = redact("Contact a@b.com or 555-123-4567 immediately")

    assert "a@b.com" not in redacted
    assert "555-123-4567" not in redacted
    assert "[REDACTED]" in redacted


def test_compliance_flags_legal_action_as_high_risk() -> None:
    report = evaluate("I have contacted my lawyer about this issue")

    assert any(violation.rule_id == "compliance.legal" for violation in report.violations)
    assert report.is_risky is True


def test_compliance_flags_refund_as_medium() -> None:
    report = evaluate("I would like a refund please")

    assert any(violation.rule_id == "compliance.refund" for violation in report.violations)
    assert report.is_risky is False


def test_policy_flags_high_value_refund(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIGH_VALUE_REFUND_MIN_USD", "500")
    report = evaluate("I was charged $750 and want a refund")

    assert any(violation.rule_id == "policy.high_value_refund" for violation in report.violations)
    assert report.is_risky is True


def test_policy_flags_blocked_customer_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOCKED_EMAIL_DOMAINS", "blocked.example")
    report = evaluate("Please help me", customer_email="user@blocked.example")

    assert any(violation.rule_id == "policy.blocked_domain" for violation in report.violations)


def test_evaluate_clean_message_has_no_violations() -> None:
    report = evaluate("The dashboard shows an error when I upload a file")

    assert report.violations == []
    assert report.is_risky is False
    assert report.has_pii is False


def test_validate_response_clean() -> None:
    violations = validate_response("Thanks for reaching out. The fix is to clear your cache.")

    assert violations == []


def test_validate_response_flags_external_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_RESPONSE_DOMAINS", "support.example.com")
    violations = validate_response("See our docs at https://evil.example.com/x for help")

    assert any(violation.rule_id == "response.external_link" for violation in violations)


def test_validate_response_allows_whitelisted_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_RESPONSE_DOMAINS", "support.example.com")
    violations = validate_response("See Our https://support.example.com/x doc")

    assert not any(violation.rule_id == "response.external_link" for violation in violations)


def test_validate_response_flags_placeholder_and_excessive_length() -> None:
    violations = validate_response("Lorem ipsum dolor " * 400)

    assert any(violation.rule_id == "response.placeholder" for violation in violations)
    assert any(violation.rule_id == "response.excessive_length" for violation in violations)


def test_screen_pii_rule_types_are_exposed() -> None:
    pii = screen_pii("Reach me at a@b.com")

    assert pii[0].rule_type == GuardrailType.pii
    assert pii[0].severity == Severity.medium
    assert pii[0].matched == "a@b.com"