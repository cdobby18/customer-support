"""Tests that .env.example stays in line with what the code actually reads.

docs/CONFIGURATION.md calls .env.example canonical, and it is the file a new
deployment copies. Nine variables were missing from it - the entire notification
stack plus every attachment and SLA limit - so those settings were undiscoverable
without reading the source. Nothing caught that, because no test compared the two.

The check walks `os.getenv` across app/ rather than trusting a hand-kept list, so
a variable added in code and forgotten in .env.example fails here instead of
shipping.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
APP_ROOT = REPO_ROOT / "app"

GETENV_PATTERN = re.compile(r'os\.getenv\(\s*"([A-Z0-9_]+)"')
ASSIGNED_PATTERN = re.compile(r"^\s*(?:os\.environ\[[\"']([A-Z0-9_]+)[\"']\]|([A-Z0-9_]+)\s*=)")


def _env_example_text() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def _documented_names() -> set[str]:
    """Names assigned in .env.example, commented or not.

    Every documented entry sets a real default or is commented out with an
    explicit default, so both forms count as "the file explains this".
    """
    names: set[str] = set()
    for line in _env_example_text().splitlines():
        stripped = line.lstrip("#").strip()
        if not stripped or "=" not in stripped:
            continue
        names.add(stripped.split("=", 1)[0].strip())
    return names


def _names_read_by_code() -> set[str]:
    names: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        names.update(GETENV_PATTERN.findall(source))
        for first, second in ASSIGNED_PATTERN.findall(source):
            names.add(first or second)
    return names


def test_env_example_exists_and_is_not_empty() -> None:
    assert ENV_EXAMPLE.is_file()
    assert _documented_names(), ".env.example documents no variables"


def test_every_variable_the_code_reads_is_documented() -> None:
    undocumented = _names_read_by_code() - _documented_names()

    assert not undocumented, (
        "these are read by the code but absent from .env.example: "
        f"{sorted(undocumented)}"
    )


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "JWT_SECRET",
        "CHANNEL_WEBHOOK_SECRET",
        "NOTIFY_WEBHOOK_URL",
        "NOTIFY_WEBHOOK_SECRET",
        "REDIS_URL",
        "CELERY_TASK_ALWAYS_EAGER",
        "MAX_ATTACHMENT_BYTES",
        "MAX_TICKET_ATTACHMENTS",
        "UPLOAD_DIR",
        "SLA_CHANNEL_MULTIPLIERS",
        "HIGH_VALUE_REFUND_MIN_USD",
        "ALLOWED_RESPONSE_DOMAINS",
        "BLOCKED_EMAIL_DOMAINS",
    ],
)
def test_known_variable_is_documented(name: str) -> None:
    """The specific omissions, named individually so a regression is obvious.

    The sweep test above already covers these; listing them keeps the failure
    message readable and pins the ones that were actually missed.
    """
    assert name in _documented_names()


def test_secrets_are_placeholders_not_real_values() -> None:
    """A real key committed here would leak to everyone who copies the file."""
    text = _env_example_text()
    assignments: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if not re.fullmatch(r"[A-Z0-9_]+", name):
            continue
        if re.search(r"SECRET|PASSWORD|API_KEY|TOKEN", name):
            assignments.append(f"{name}={value.strip()}")

    assert assignments, "expected at least one secret entry"
    for entry in assignments:
        name, _, value = entry.partition("=")
        assert not value or "replace-with" in value or "your-" in value, (
            f".env.example must not carry a plausible real credential: {entry}"
        )
