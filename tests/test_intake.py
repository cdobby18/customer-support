"""Channel intake adapters.

`app/agents/intake.py` is the only code in the system that reads a third party's
payload verbatim: five adapters, each mapping a different vendor's field names
onto `NormalizedMessage`. Everything the automation later reasons about depends
on that mapping, and the fallback chains (`text` or `body` or `html`) only earn
their keep when a key is missing — which is exactly the case no other test
exercised before this file.

Two things are pinned deliberately. The provider shapes are contractual: a
vendor renaming `from_email` or `thread_ts` should fail a test, not a customer
request. And the fallbacks are defensive: a payload missing the preferred key
must still produce a usable message rather than an empty one.

`enrich_customer_context` is included because its tier and tag thresholds are
policy, and policy that is never exercised drifts.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.agents.intake import (
    ChannelAdapter,
    ChatIntakeAdapter,
    CRMIntakeAdapter,
    EmailIntakeAdapter,
    SlackIntakeAdapter,
    WhatsAppIntakeAdapter,
    enrich_customer_context,
    get_adapter,
    normalize_message,
)
from app.core.models import TicketRecord, UserRecord
from harness import drop_schema, reset_schema, seed_admin, test_engine


@pytest.fixture(autouse=True)
def clean_state():
    reset_schema()
    seed_admin()
    yield
    drop_schema()


def seed_user(user_id: str, role: str = "customer") -> None:
    with Session(test_engine) as session:
        session.add(
            UserRecord(
                id=user_id,
                email=f"{user_id}@example.com",
                password_hash="unused",
                role=role,
                is_active=True,
                created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
            )
        )
        session.commit()


def seed_tickets(customer_id: str, count: int, open_count: int = 0) -> None:
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with Session(test_engine) as session:
        for index in range(count):
            session.add(
                TicketRecord(
                    id=str(uuid4()),
                    customer_id=customer_id,
                    message=f"ticket {index}",
                    channel="web",
                    intent="technical",
                    priority="normal",
                    status="open" if index < open_count else "resolved",
                    created_at=base + timedelta(minutes=index),
                    updated_at=base + timedelta(minutes=index),
                )
            )
        session.commit()


# --- base class defaults ----------------------------------------------------


def test_base_adapter_normalize_is_abstract() -> None:
    with pytest.raises(NotImplementedError):
        ChannelAdapter().normalize({})


def test_base_adapter_attachments_default_missing_fields() -> None:
    attachments = ChannelAdapter().extract_attachments(
        {
            "attachments": [
                {
                    "id": "a1",
                    "filename": "invoice.pdf",
                    "content_type": "application/pdf",
                    "size": 2048,
                    "url": "https://files.example/invoice.pdf",
                },
                {},
            ]
        }
    )

    assert attachments[0].id == "a1"
    assert attachments[0].size == 2048
    # A vendor omitting the id/filename must not raise; the values fall back.
    assert attachments[1].id == ""
    assert attachments[1].filename == "unknown"
    assert attachments[1].content_type is None


def test_base_adapter_has_no_thread_or_metadata() -> None:
    adapter = ChannelAdapter()

    assert adapter.extract_attachments({}) == []
    assert adapter.extract_thread_id({"thread_ts": "1.0"}) is None
    assert adapter.extract_channel_metadata({"subject": "ignored"}) == {}


# --- email ------------------------------------------------------------------


def test_email_legacy_shape_is_marked() -> None:
    message = EmailIntakeAdapter().normalize(
        {"customer_id": "c1", "message": "hello", "external_id": "e1", "thread_id": "t1"}
    )

    assert message.customer_id == "c1"
    assert message.message == "hello"
    assert message.channel == "email"
    assert message.external_id == "e1"
    assert message.thread_id == "t1"
    assert message.attachments == []
    assert message.channel_metadata == {"legacy_format": True}


def test_email_provider_shape_maps_headers_and_attachments() -> None:
    message = EmailIntakeAdapter().normalize(
        {
            "from_email": "person@example.com",
            "text": "My invoice is wrong",
            "message_id": "msg-1",
            "in_reply_to": "msg-0",
            "subject": "Invoice",
            "to": ["support@example.com"],
            "cc": ["cc@example.com"],
            "bcc": ["bcc@example.com"],
            "headers": {"X-Mailer": "vendor"},
            "attachments": [{"id": "f1", "filename": "invoice.pdf", "size": 10}],
        }
    )

    assert message.customer_id == "person@example.com"
    assert message.message == "My invoice is wrong"
    assert message.external_id == "msg-1"
    assert message.thread_id == "msg-0"
    assert message.channel_metadata["subject"] == "Invoice"
    assert message.channel_metadata["cc"] == ["cc@example.com"]
    assert message.channel_metadata["bcc"] == ["bcc@example.com"]
    assert message.channel_metadata["headers"] == {"X-Mailer": "vendor"}
    assert [a.filename for a in message.attachments] == ["invoice.pdf"]


def test_email_falls_back_across_sender_body_and_thread_keys() -> None:
    adapter = EmailIntakeAdapter()

    assert adapter.normalize({"sender": "s@example.com", "body": "b"}).customer_id == "s@example.com"
    assert adapter.normalize({"from_email": "f@example.com", "html": "<p>h</p>"}).message == "<p>h</p>"
    assert adapter.normalize({"text": "t", "id": "i"}).external_id == "i"
    assert adapter.normalize({"text": "t", "references": "ref-1"}).thread_id == "ref-1"
    # in_reply_to wins over the references header when both are present.
    assert (
        adapter.normalize({"text": "t", "in_reply_to": "a", "references": "b"}).thread_id == "a"
    )


def test_email_without_any_known_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EmailIntakeAdapter().normalize({})


def test_email_uses_unknown_sender_and_defaults() -> None:
    message = EmailIntakeAdapter().normalize({"text": "hello"})

    assert message.customer_id == "unknown"
    assert message.channel_metadata["subject"] is None
    assert message.channel_metadata["to"] is None
    assert message.channel_metadata["headers"] == {}


# --- slack ------------------------------------------------------------------


def test_slack_legacy_shape_is_marked() -> None:
    message = SlackIntakeAdapter().normalize(
        {"customer_id": "c1", "message": "hi", "thread_id": "t1"}
    )

    assert message.channel == "slack"
    assert message.channel_metadata == {"legacy_format": True}


def test_slack_event_shape_maps_files_and_blocks() -> None:
    message = SlackIntakeAdapter().normalize(
        {
            "team_id": "T1",
            "event": {
                "user": "U1",
                "text": "the API is down",
                "ts": "1700000000.000100",
                "thread_ts": "1700000000.000100",
                "channel": "C1",
                "channel_type": "im",
                "blocks": [{"type": "section"}],
                "reactions": [{"name": "eyes"}],
                "files": [
                    {
                        "id": "F1",
                        "name": "screenshot.png",
                        "mimetype": "image/png",
                        "size": 512,
                        "url_private": "https://files.slack.com/screenshot.png",
                    }
                ],
            },
        }
    )

    assert message.customer_id == "U1"
    assert message.message == "the API is down"
    assert message.external_id == "1700000000.000100"
    assert message.thread_id == "1700000000.000100"
    assert message.channel_metadata == {
        "channel_id": "C1",
        "channel_type": "im",
        "team_id": "T1",
        "blocks": [{"type": "section"}],
        "reactions": [{"name": "eyes"}],
    }
    attachment = message.attachments[0]
    assert attachment.id == "F1"
    assert attachment.filename == "screenshot.png"
    assert attachment.content_type == "image/png"
    assert attachment.url == "https://files.slack.com/screenshot.png"


def test_slack_accepts_a_flat_payload_without_the_event_envelope() -> None:
    message = SlackIntakeAdapter().normalize(
        {"user_id": "U2", "text": "flat", "parent_ts": "1700000000.000001", "event_ts": "e-1"}
    )

    assert message.customer_id == "U2"
    assert message.external_id == "e-1"
    assert message.thread_id == "1700000000.000001"
    assert message.channel_metadata["team_id"] is None
    assert message.channel_metadata["reactions"] == []
    assert message.attachments == []


def test_slack_file_without_optional_keys_still_parses() -> None:
    message = SlackIntakeAdapter().normalize({"event": {"user": "U3", "text": "f", "files": [{}]}})

    assert message.attachments[0].id == ""
    assert message.attachments[0].filename == "unknown"
    assert message.attachments[0].content_type is None


def test_slack_without_any_known_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SlackIntakeAdapter().normalize({})


# --- whatsapp ---------------------------------------------------------------


def whatsapp_payload(message: dict) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "phone_number_id": "PN1",
                                "display_phone_number": "+15550000000",
                            },
                            "contacts": [
                                {"profile": {"name": "Dana"}, "wa_id": "15551234567"}
                            ],
                            "messages": [message],
                        }
                    }
                ]
            }
        ]
    }


def test_whatsapp_text_message_maps_conversation_and_contact() -> None:
    message = WhatsAppIntakeAdapter().normalize(
        whatsapp_payload(
            {
                "from": "15551234567",
                "id": "wamid.1",
                "type": "text",
                "text": {"body": "where is my order"},
                "conversation": {"id": "conv-1"},
            }
        )
    )

    assert message.customer_id == "15551234567"
    assert message.message == "where is my order"
    assert message.external_id == "wamid.1"
    assert message.thread_id == "conv-1"
    assert message.channel_metadata == {
        "messaging_product": "whatsapp",
        "phone_number_id": "PN1",
        "display_phone_number": "+15550000000",
        "contact_name": "Dana",
        "contact_wa_id": "15551234567",
    }


def test_whatsapp_media_message_becomes_a_placeholder_with_an_attachment() -> None:
    message = WhatsAppIntakeAdapter().normalize(
        whatsapp_payload(
            {
                "from": "15551234567",
                "id": "wamid.2",
                "type": "image",
                "image": {
                    "id": "media-1",
                    "mime_type": "image/jpeg",
                    "file_size": 2048,
                    "url": "https://lookaside.fb.com/media-1",
                },
            }
        )
    )

    assert message.message == "[Image message]"
    assert message.thread_id is None
    attachment = message.attachments[0]
    assert attachment.id == "media-1"
    assert attachment.filename == "image.bin"
    assert attachment.content_type == "image/jpeg"
    assert attachment.size == 2048


def test_whatsapp_media_filename_is_used_when_present() -> None:
    message = WhatsAppIntakeAdapter().normalize(
        whatsapp_payload(
            {
                "from": "1",
                "id": "wamid.3",
                "type": "document",
                "document": {"id": "d1", "filename": "receipt.pdf"},
            }
        )
    )

    assert message.message == "[Document message]"
    assert message.attachments[0].filename == "receipt.pdf"
    assert message.attachments[0].content_type is None


@pytest.mark.parametrize("media_type", ["audio", "video", "sticker"])
def test_whatsapp_other_media_types(media_type: str) -> None:
    message = WhatsAppIntakeAdapter().normalize(
        whatsapp_payload(
            {"from": "1", "id": "wamid.4", "type": media_type, media_type: {"id": f"{media_type}-1"}}
        )
    )

    assert message.message == f"[{media_type.capitalize()} message]"
    assert message.attachments[0].id == f"{media_type}-1"


def test_whatsapp_interactive_button_and_list_replies() -> None:
    adapter = WhatsAppIntakeAdapter()

    button = adapter.normalize(
        whatsapp_payload(
            {
                "from": "1",
                "id": "w5",
                "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"title": "Refund"}},
            }
        )
    )
    listed = adapter.normalize(
        whatsapp_payload(
            {
                "from": "1",
                "id": "w6",
                "type": "interactive",
                "interactive": {"type": "list_reply", "list_reply": {"title": "Order status"}},
            }
        )
    )
    unknown = adapter.normalize(
        whatsapp_payload(
            {
                "from": "1",
                "id": "w7",
                "type": "interactive",
                "interactive": {"type": "nfm_reply"},
            }
        )
    )

    assert button.message == "[Button: Refund]"
    assert listed.message == "[List: Order status]"
    # An interactive type this code has never seen still yields a message rather
    # than dropping the inbound text entirely.
    assert unknown.message == "[interactive message]"


def test_whatsapp_unrecognised_message_type_is_labelled() -> None:
    message = WhatsAppIntakeAdapter().normalize(
        whatsapp_payload({"from": "1", "id": "w8", "type": "location", "location": {}})
    )

    assert message.message == "[location message]"


def test_whatsapp_missing_type_is_treated_as_text() -> None:
    message = WhatsAppIntakeAdapter().normalize(
        whatsapp_payload({"from": "1", "id": "w9", "text": {"body": "no type key"}})
    )

    assert message.message == "no type key"


def test_whatsapp_empty_and_absent_envelopes_are_rejected() -> None:
    adapter = WhatsAppIntakeAdapter()

    # entry/changes/value all default, and an empty value leaves no message.
    with pytest.raises(ValidationError):
        adapter.normalize({})
    with pytest.raises(ValidationError):
        adapter.normalize({"entry": [{"changes": [{"value": {"metadata": {}}}]}]})
    with pytest.raises(ValidationError):
        adapter.normalize({"entry": [{}]})


def test_whatsapp_without_contacts_still_normalizes() -> None:
    message = WhatsAppIntakeAdapter().normalize(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "15551234567",
                                        "id": "wamid.10",
                                        "type": "text",
                                        "text": {"body": "hi"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )

    assert message.customer_id == "15551234567"
    assert message.channel_metadata["contact_name"] is None
    assert message.channel_metadata["contact_wa_id"] is None
    assert message.channel_metadata["phone_number_id"] is None


def test_whatsapp_legacy_shape_is_marked() -> None:
    message = WhatsAppIntakeAdapter().normalize(
        {"customer_id": "c1", "message": "hi", "external_id": "e1"}
    )

    assert message.channel == "whatsapp"
    assert message.channel_metadata == {"legacy_format": True}


# --- chat -------------------------------------------------------------------


def test_chat_widget_shape_maps_session_and_visitor() -> None:
    message = ChatIntakeAdapter().normalize(
        {
            "visitor_id": "v1",
            "message": "my charge looks wrong",
            "message_id": "m1",
            "session_id": "s1",
            "visitor_info": {"browser": "chrome"},
            "page_url": "https://app.example/billing",
            "referrer": "https://google.com",
            "user_agent": "test-agent",
            "attachments": [{"id": "a1", "filename": "receipt.png"}],
        }
    )

    assert message.customer_id == "v1"
    assert message.message == "my charge looks wrong"
    assert message.external_id == "m1"
    assert message.thread_id == "s1"
    assert message.attachments[0].filename == "receipt.png"
    assert message.channel_metadata == {
        "session_id": "s1",
        "visitor_info": {"browser": "chrome"},
        "page_url": "https://app.example/billing",
        "referrer": "https://google.com",
        "user_agent": "test-agent",
    }


def test_chat_falls_back_across_identity_and_thread_keys() -> None:
    adapter = ChatIntakeAdapter()

    assert adapter.normalize({"user_id": "u1", "text": "hello"}).customer_id == "u1"
    assert adapter.normalize({"visitor_id": "v1", "text": "hi"}).message == "hi"
    assert adapter.normalize({"visitor_id": "v1", "text": "hi", "id": "i1"}).external_id == "i1"
    assert (
        adapter.normalize({"visitor_id": "v1", "text": "hi", "conversation_id": "c1"}).thread_id
        == "c1"
    )
    assert adapter.normalize({"visitor_id": "v1", "text": "hi"}).channel_metadata == {
        "session_id": None,
        "visitor_info": {},
        "page_url": None,
        "referrer": None,
        "user_agent": None,
    }


def test_chat_without_any_known_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ChatIntakeAdapter().normalize({})


# --- crm --------------------------------------------------------------------


def test_crm_provider_shape_maps_deal_fields() -> None:
    message = CRMIntakeAdapter().normalize(
        {
            "contact_id": "contact-1",
            "description": "Enterprise user cannot upgrade",
            "ticket_id": "case-99",
            "conversation_id": "conv-9",
            "source": "salesforce",
            "deal_id": "deal-7",
            "pipeline_stage": "negotiation",
            "priority": "high",
            "tags": ["enterprise", "renewal"],
            "custom_fields": {"seats": 250},
            "attachments": [{"id": "att-1", "filename": "sow.pdf"}],
        }
    )

    assert message.customer_id == "contact-1"
    assert message.message == "Enterprise user cannot upgrade"
    assert message.external_id == "case-99"
    assert message.thread_id == "conv-9"
    assert message.attachments[0].filename == "sow.pdf"
    assert message.channel_metadata == {
        "source": "salesforce",
        "deal_id": "deal-7",
        "pipeline_stage": "negotiation",
        "priority": "high",
        "tags": ["enterprise", "renewal"],
        "custom_fields": {"seats": 250},
    }


def test_crm_falls_back_across_body_and_id_keys() -> None:
    adapter = CRMIntakeAdapter()

    assert adapter.normalize({"contact_id": "c1", "notes": "from notes"}).message == "from notes"
    assert adapter.normalize({"contact_id": "c1", "notes": "n", "case_id": "case-1"}).external_id == "case-1"
    assert adapter.normalize({"contact_id": "c1", "notes": "n", "id": "raw-1"}).external_id == "raw-1"
    assert adapter.normalize({"contact_id": "c1", "notes": "n", "thread_id": "t1"}).thread_id == "t1"
    assert adapter.normalize({"contact_id": "c1", "notes": "n"}).channel_metadata == {
        "source": None,
        "deal_id": None,
        "pipeline_stage": None,
        "priority": None,
        "tags": [],
        "custom_fields": {},
    }


def test_crm_legacy_shape_is_marked() -> None:
    message = CRMIntakeAdapter().normalize({"customer_id": "c1", "message": "hi"})

    assert message.channel == "crm"
    assert message.channel_metadata == {"legacy_format": True}


def test_crm_without_any_known_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CRMIntakeAdapter().normalize({})


# --- registry ---------------------------------------------------------------


@pytest.mark.parametrize("channel", ["email", "slack", "whatsapp", "chat", "crm"])
def test_every_advertised_channel_has_an_adapter(channel: str) -> None:
    adapter = get_adapter(channel)

    assert adapter is not None
    assert adapter.channel_name == channel
    assert normalize_message(channel, {"customer_id": "c1", "message": "hi"}).channel == channel


def test_unknown_channel_is_not_registered() -> None:
    assert get_adapter("carrier-pigeon") is None
    assert normalize_message("carrier-pigeon", {"message": "hi"}) is None


# --- customer context enrichment --------------------------------------------


def test_enrichment_of_an_unknown_customer_is_all_zeros() -> None:
    with Session(test_engine) as session:
        context = enrich_customer_context(session, "nobody")

    assert context.tier == "standard"
    assert context.total_tickets_count == 0
    assert context.open_tickets_count == 0
    assert context.last_contact_at is None
    assert context.tags == []


def test_enrichment_counts_tickets_and_finds_the_newest() -> None:
    seed_user("cust-1")
    seed_tickets("cust-1", count=3, open_count=2)

    with Session(test_engine) as session:
        context = enrich_customer_context(session, "cust-1")

    assert context.total_tickets_count == 3
    assert context.open_tickets_count == 2
    # SQLite does not preserve the offset on a DateTime(timezone=True) column,
    # so the value comes back naive there while PostgreSQL returns it aware.
    # Compare instants, not tzinfo.
    last_contact = context.last_contact_at
    assert last_contact is not None
    if last_contact.tzinfo is None:
        last_contact = last_contact.replace(tzinfo=timezone.utc)
    assert last_contact == datetime(2026, 9, 1, 0, 2, tzinfo=timezone.utc)
    assert context.tier == "standard"
    assert context.tags == []


def test_admin_senders_are_vip_regardless_of_volume() -> None:
    seed_user("admin-1", role="admin")
    seed_tickets("admin-1", count=60, open_count=10)

    with Session(test_engine) as session:
        context = enrich_customer_context(session, "admin-1")

    assert context.tier == "vip"
    assert context.tags == ["frequent_contactor", "multiple_open_tickets"]


def test_high_volume_sender_becomes_premium_and_tagged() -> None:
    seed_user("cust-2")
    seed_tickets("cust-2", count=51, open_count=6)

    with Session(test_engine) as session:
        context = enrich_customer_context(session, "cust-2")

    assert context.tier == "premium"
    assert context.tags == ["frequent_contactor", "multiple_open_tickets"]


def test_tier_thresholds_are_strictly_greater_than() -> None:
    # 50 tickets is not enough for premium (which needs > 50) and 5 open is not
    # enough for the second tag (which needs > 5), so neither boundary may creep
    # down. The first tag is expected: 50 is already over the > 20 mark.
    seed_user("cust-3")
    seed_tickets("cust-3", count=50, open_count=5)

    with Session(test_engine) as session:
        context = enrich_customer_context(session, "cust-3")

    assert context.tier == "standard"
    assert context.tags == ["frequent_contactor"]


def test_volume_tier_requires_a_user_row() -> None:
    # Tickets attributed to a deleted account do not upgrade the tier: the
    # volume check is nested under `if user`, so an unknown id stays standard.
    seed_tickets("ghost", count=60, open_count=10)

    with Session(test_engine) as session:
        context = enrich_customer_context(session, "ghost")

    assert context.total_tickets_count == 60
    assert context.tier == "standard"
