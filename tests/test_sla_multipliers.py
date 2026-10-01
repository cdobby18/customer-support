"""Tests for SLA_CHANNEL_MULTIPLIERS parsing.

The override is a JSON blob read from the environment on every call, and every
malformed case falls back to 1.0 rather than raising. That silent fallback is
the reason these need pinning: a typo in production config silently doubles or
halves every SLA deadline on a channel, and no test would notice because nothing
was ever asserted about the parsing.

Each bad case ends up at 1.0, which is the right answer for a config mistake -
but only because it is deliberate. If a future change makes bad input raise,
these tests should fail loudly rather than be quietly updated.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.support import sla_channel_multiplier, sla_deadline

CHANNELS = ["email", "web", "crm", "slack", "whatsapp", "chat"]

DEFAULTS = {
    "email": 1.0,
    "web": 1.0,
    "crm": 1.0,
    "slack": 0.75,
    "whatsapp": 0.5,
    "chat": 0.5,
}


@pytest.mark.parametrize("channel", CHANNELS)
def test_defaults_apply_when_the_variable_is_unset(
    channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLA_CHANNEL_MULTIPLIERS", raising=False)

    assert sla_channel_multiplier(channel) == DEFAULTS[channel]


@pytest.mark.parametrize(
    ("channel", "configured", "expected"),
    [
        ("slack", '{"slack": 2.5}', 2.5),
        ("email", '{"email": 0}', 0.0),
        ("chat", '{"chat": "1.5"}', 1.5),
        ("whatsapp", '{"whatsapp": 3}', 3.0),
        ("email", '{"email": 2, "chat": 3}', 2.0),
    ],
)
def test_valid_overrides_are_applied(
    channel: str, configured: str, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLA_CHANNEL_MULTIPLIERS", configured)

    assert sla_channel_multiplier(channel) == expected


def test_an_override_for_one_channel_leaves_the_others_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The override is a merge, not a replacement: partial config is normal."""
    monkeypatch.setenv("SLA_CHANNEL_MULTIPLIERS", json.dumps({"slack": 2.0}))

    assert sla_channel_multiplier("slack") == 2.0
    assert sla_channel_multiplier("email") == 1.0
    assert sla_channel_multiplier("chat") == 0.5


@pytest.mark.parametrize(
    "configured",
    [
        "",
        "   ",
        "not json",
        "[1, 2, 3]",
        '"a string"',
        "123",
        '{"slack": "not-a-number"}',
        '{"slack": null}',
    ],
)
def test_malformed_config_falls_back_to_defaults(
    configured: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad config must not silently invent a deadline."""
    monkeypatch.setenv("SLA_CHANNEL_MULTIPLIERS", configured)

    assert sla_channel_multiplier("slack") == 0.75
    assert sla_channel_multiplier("email") == 1.0


def test_an_unknown_channel_is_treated_as_web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLA_CHANNEL_MULTIPLIERS", raising=False)

    assert sla_channel_multiplier("carrier-pigeon") == 1.0


def test_multiplier_shortens_the_sla_window_for_fast_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The multiplier exists to tighten deadlines on chat-like channels."""
    monkeypatch.delenv("SLA_CHANNEL_MULTIPLIERS", raising=False)
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)

    email = sla_deadline("urgent", created, "email")
    whatsapp = sla_deadline("urgent", created, "whatsapp")

    assert email - created == timedelta(hours=4)
    assert whatsapp - created == timedelta(hours=2)


def test_an_override_actually_moves_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through sla_deadline, not just the multiplier in isolation."""
    monkeypatch.delenv("SLA_CHANNEL_MULTIPLIERS", raising=False)
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    before = sla_deadline("high", created, "slack")

    # high priority is 8 base hours: slack's 0.75 default tightens it to 6,
    # and a 2.0 override loosens it to the full 16.
    monkeypatch.setenv("SLA_CHANNEL_MULTIPLIERS", json.dumps({"slack": 2.0}))
    after = sla_deadline("high", created, "slack")

    assert before - created == timedelta(hours=6)
    assert after - created == timedelta(hours=16)
