import os
import re
from enum import Enum
from typing import Callable, NamedTuple, Pattern

from pydantic import BaseModel, Field


class GuardrailType(str, Enum):
    pii = "pii"
    compliance = "compliance"
    policy = "policy"
    response = "response"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Violation(BaseModel):
    rule_id: str
    rule_type: GuardrailType
    category: str
    severity: Severity
    matched: str = Field(max_length=200)
    description: str


class PolicyReport(BaseModel):
    violations: list[Violation] = Field(default_factory=list)
    is_risky: bool = False
    has_pii: bool = False
    redacted_text: str | None = None


def _luhn_valid(number: str) -> bool:
    digits = [int(char) for char in number if char.isdigit()]
    if len(digits) < 13:
        return False
    checksum = 0
    reversed_digits = digits[::-1]
    for index, digit in enumerate(reversed_digits):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


class _PiiRule(NamedTuple):
    rule_id: str
    category: str
    severity: Severity
    pattern: Pattern[str]
    validator: Callable[[str], bool] | None = None


_PII_RULES = [
    _PiiRule(
        "pii.email",
        "email_address",
        Severity.medium,
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ),
    _PiiRule(
        "pii.phone",
        "phone_number",
        Severity.medium,
        re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"),
    ),
    _PiiRule(
        "pii.ssn",
        "social_security_number",
        Severity.high,
        re.compile(r"\b(?!000|666|9\d{2})\d{3}[- ]?\d{2}[- ]?\d{4}\b"),
    ),
    _PiiRule(
        "pii.credit_card",
        "credit_card_number",
        Severity.high,
        re.compile(r"\b(?:\d[ -]?){13,19}\b"),
        _luhn_valid,
    ),
    _PiiRule(
        "pii.ipv4",
        "ip_address",
        Severity.low,
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"),
    ),
]


class _KeywordRule(NamedTuple):
    rule_id: str
    category: str
    severity: Severity
    keywords: tuple[str, ...]
    description: str


_COMPLIANCE_RULES = [
    _KeywordRule(
        "compliance.refund",
        "refund_request",
        Severity.medium,
        ("refund", "reimbursement", "money back", "credit my account"),
        "Refund or reimbursement request detected",
    ),
    _KeywordRule(
        "compliance.legal",
        "legal_action",
        Severity.high,
        ("lawsuit", "attorney", "lawyer", "sue", "legal action", "cease and desist", "litigation"),
        "Legal action or legal counsel reference detected",
    ),
    _KeywordRule(
        "compliance.fraud",
        "fraud",
        Severity.high,
        ("fraud", "unauthorized charge", "stolen card", "identity theft", "scammed"),
        "Fraud-related complaint detected",
    ),
    _KeywordRule(
        "compliance.dispute",
        "charge_dispute",
        Severity.medium,
        ("chargeback", "dispute", "charge dispute"),
        "Payment dispute reference detected",
    ),
    _KeywordRule(
        "compliance.privacy",
        "data_privacy",
        Severity.high,
        ("gdpr", "privacy", "personal data", "data breach", "ccpa"),
        "Data privacy or personal data reference detected",
    ),
    _KeywordRule(
        "compliance.regulator",
        "regulator",
        Severity.high,
        ("attorney general", "regulator", "ombudsman", "complaint filed"),
        "Regulator or enforcement body reference detected",
    ),
]


_POLICY_RULES = [
    _KeywordRule(
        "policy.high_value_refund",
        "high_value_refund",
        Severity.high,
        ("refund", "reimburse", "money back", "cash back"),
        "High-value refund context detected",
    ),
]


def _blocked_domains() -> set[str]:
    configured = os.getenv("BLOCKED_EMAIL_DOMAINS", "")
    return {domain.strip().lower().lstrip("@") for domain in configured.split(",") if domain.strip()}


def _matches_any(rule: _KeywordRule, normalized: str) -> bool:
    return any(keyword in normalized for keyword in rule.keywords)


def screen_pii(text: str) -> list[Violation]:
    violations: list[Violation] = []
    for rule in _PII_RULES:
        for match in rule.pattern.finditer(text):
            value = match.group(0)
            if rule.validator is not None and not rule.validator(value):
                continue
            violations.append(
                Violation(
                    rule_id=rule.rule_id,
                    rule_type=GuardrailType.pii,
                    category=rule.category,
                    severity=rule.severity,
                    matched=value,
                    description=f"PII detected: {rule.category}",
                )
            )
    return violations


def screen_compliance(text: str) -> list[Violation]:
    violations: list[Violation] = []
    normalized = text.lower()
    for rule in _COMPLIANCE_RULES:
        if _matches_any(rule, normalized):
            violations.append(
                Violation(
                    rule_id=rule.rule_id,
                    rule_type=GuardrailType.compliance,
                    category=rule.category,
                    severity=rule.severity,
                    matched=rule.keywords[0],
                    description=rule.description,
                )
            )
    return violations


def screen_policy(
    text: str,
    *,
    customer_email: str | None = None,
) -> list[Violation]:
    violations: list[Violation] = []
    normalized = text.lower()
    blocked = _blocked_domains()
    if customer_email:
        email_domain = customer_email.lower().strip().split("@")[-1]
        if email_domain and email_domain in blocked:
            violations.append(
                Violation(
                    rule_id="policy.blocked_domain",
                    rule_type=GuardrailType.policy,
                    category="blocked_domain",
                    severity=Severity.medium,
                    matched=customer_email,
                    description="Customer email domain is on the blocked list",
                )
            )
    for rule in _POLICY_RULES:
        if _matches_any(rule, normalized):
            amounts = [
                int(value)
                for match in re.finditer(r"\$\s?\s?(\d{3,})|(\d{3,})\s?dollars", normalized)
                for value in match.groups()
                if value
            ]
            amount = max(amounts) if amounts else 0
            threshold = int(os.getenv("HIGH_VALUE_REFUND_MIN_USD", "500"))
            if amount >= threshold:
                violations.append(
                    Violation(
                        rule_id=rule.rule_id,
                        rule_type=GuardrailType.policy,
                        category=rule.category,
                        severity=rule.severity,
                        matched=rule.keywords[0],
                        description=rule.description,
                    )
                )
    return violations


def _allowed_response_domains() -> set[str]:
    configured = os.getenv("ALLOWED_RESPONSE_DOMAINS", "")
    return {domain.strip().lower() for domain in configured.split(",") if domain.strip()}


def validate_response(text: str) -> list[Violation]:
    violations: list[Violation] = []
    normalized = text.lower()
    allowed_domains = _allowed_response_domains()
    for link in re.findall(r"https?://[^\s)\]]+", normalized):
        host = re.sub(r"https?://", "", link).split("/")[0]
        if host and host not in allowed_domains:
            violations.append(
                Violation(
                    rule_id="response.external_link",
                    rule_type=GuardrailType.response,
                    category="external_link",
                    severity=Severity.low,
                    matched=link,
                    description="Response contains a link to a domain outside the allowlist",
                )
            )
    for placeholder in ("lorem", "todo", "fixme", "[insert", "placeholder text"):
        if placeholder in normalized:
            violations.append(
                Violation(
                    rule_id="response.placeholder",
                    rule_type=GuardrailType.response,
                    category="unfinished_response",
                    severity=Severity.low,
                    matched=placeholder,
                    description="Response contains placeholder or unfinished text",
                )
            )
            break
    if len(text) > 4000:
        violations.append(
            Violation(
                rule_id="response.excessive_length",
                rule_type=GuardrailType.response,
                category="excessive_length",
                severity=Severity.low,
                matched=f"{len(text)} chars",
                description="Response exceeds the recommended length limit",
            )
        )
    violations.extend(screen_pii(text))
    return violations


def redact(text: str) -> str:
    redacted = text
    for rule in _PII_RULES:
        def _replace(match: re.Match[str], validator: Callable[[str], bool] | None = rule.validator) -> str:
            if validator is not None and not validator(match.group(0)):
                return match.group(0)
            return "[REDACTED]"

        redacted = rule.pattern.sub(_replace, redacted)
    return redacted


def _dedupe(violations: list[Violation]) -> list[Violation]:
    seen: set[tuple[str, str]] = set()
    unique: list[Violation] = []
    for violation in violations:
        key = (violation.rule_id, violation.matched)
        if key in seen:
            continue
        seen.add(key)
        unique.append(violation)
    return unique


def evaluate(
    text: str,
    *,
    customer_email: str | None = None,
) -> PolicyReport:
    violations = _dedupe(
        [
            *screen_pii(text),
            *screen_compliance(text),
            *screen_policy(text, customer_email=customer_email),
        ]
    )
    return PolicyReport(
        violations=violations,
        is_risky=any(violation.severity == Severity.high for violation in violations),
        has_pii=any(violation.rule_type == GuardrailType.pii for violation in violations),
        redacted_text=redact(text),
    )