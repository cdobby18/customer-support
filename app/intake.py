from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class Attachment(BaseModel):
    id: str
    filename: str
    content_type: str | None = None
    size: int | None = None
    url: str | None = None


class CustomerContext(BaseModel):
    tier: str | None = None
    open_tickets_count: int = 0
    total_tickets_count: int = 0
    last_contact_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class NormalizedMessage(BaseModel):
    customer_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    external_id: str | None = None
    thread_id: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    customer_context: CustomerContext | None = None
    channel_metadata: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=lambda: datetime.now())


class ChannelAdapter:
    channel_name: str

    def normalize(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        raise NotImplementedError

    def extract_attachments(self, raw_payload: dict[str, Any]) -> list[Attachment]:
        return []

    def extract_thread_id(self, raw_payload: dict[str, Any]) -> str | None:
        return None

    def extract_channel_metadata(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        return {}


class EmailIntakeAdapter(ChannelAdapter):
    channel_name = "email"

    def normalize(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        # Legacy format (backward compatible with ChannelMessage)
        if "customer_id" in raw_payload and "message" in raw_payload:
            customer_id = raw_payload.get("customer_id") or "unknown"
            message = raw_payload.get("message", "")
            external_id = raw_payload.get("external_id")
            thread_id = raw_payload.get("thread_id")
            attachments = []
            channel_metadata = {"legacy_format": True}
        else:
            # Provider-specific format (SendGrid, Mailgun, etc.)
            customer_id = raw_payload.get("from_email") or raw_payload.get("sender") or "unknown"
            message = raw_payload.get("text") or raw_payload.get("body") or raw_payload.get("html", "")
            external_id = raw_payload.get("message_id") or raw_payload.get("id")
            thread_id = self.extract_thread_id(raw_payload)
            attachments = self.extract_attachments(raw_payload)
            channel_metadata = self.extract_channel_metadata(raw_payload)

        return NormalizedMessage(
            customer_id=customer_id,
            message=message,
            channel=self.channel_name,
            external_id=external_id,
            thread_id=thread_id,
            attachments=attachments,
            channel_metadata=channel_metadata,
        )

    def extract_thread_id(self, raw_payload: dict[str, Any]) -> str | None:
        return raw_payload.get("in_reply_to") or raw_payload.get("references")

    def extract_attachments(self, raw_payload: dict[str, Any]) -> list[Attachment]:
        attachments = []
        for att in raw_payload.get("attachments", []):
            attachments.append(
                Attachment(
                    id=att.get("id", ""),
                    filename=att.get("filename", "unknown"),
                    content_type=att.get("content_type"),
                    size=att.get("size"),
                    url=att.get("url"),
                )
            )
        return attachments

    def extract_channel_metadata(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "subject": raw_payload.get("subject"),
            "to": raw_payload.get("to"),
            "cc": raw_payload.get("cc"),
            "bcc": raw_payload.get("bcc"),
            "headers": raw_payload.get("headers", {}),
        }


class SlackIntakeAdapter(ChannelAdapter):
    channel_name = "slack"

    def normalize(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        # Legacy format (backward compatible with ChannelMessage)
        if "customer_id" in raw_payload and "message" in raw_payload:
            customer_id = raw_payload.get("customer_id") or "unknown"
            message = raw_payload.get("message", "")
            external_id = raw_payload.get("external_id")
            thread_id = raw_payload.get("thread_id")
            attachments = []
            channel_metadata = {"legacy_format": True}
        else:
            # Slack Events API format
            event = raw_payload.get("event", raw_payload)
            customer_id = event.get("user") or event.get("user_id") or "unknown"
            message = event.get("text", "")
            external_id = event.get("ts") or event.get("event_ts")
            thread_id = self.extract_thread_id(raw_payload)
            attachments = self.extract_attachments(raw_payload)
            channel_metadata = self.extract_channel_metadata(raw_payload)

        return NormalizedMessage(
            customer_id=customer_id,
            message=message,
            channel=self.channel_name,
            external_id=external_id,
            thread_id=thread_id,
            attachments=attachments,
            channel_metadata=channel_metadata,
        )

    def extract_thread_id(self, raw_payload: dict[str, Any]) -> str | None:
        event = raw_payload.get("event", raw_payload)
        return event.get("thread_ts") or event.get("parent_ts")

    def extract_attachments(self, raw_payload: dict[str, Any]) -> list[Attachment]:
        attachments = []
        event = raw_payload.get("event", raw_payload)
        for file in event.get("files", []):
            attachments.append(
                Attachment(
                    id=file.get("id", ""),
                    filename=file.get("name", "unknown"),
                    content_type=file.get("mimetype"),
                    size=file.get("size"),
                    url=file.get("url_private"),
                )
            )
        return attachments

    def extract_channel_metadata(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        event = raw_payload.get("event", raw_payload)
        return {
            "channel_id": event.get("channel"),
            "channel_type": event.get("channel_type"),
            "team_id": raw_payload.get("team_id"),
            "blocks": event.get("blocks"),
            "reactions": event.get("reactions", []),
        }


class WhatsAppIntakeAdapter(ChannelAdapter):
    channel_name = "whatsapp"

    def normalize(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        # Legacy format (backward compatible with ChannelMessage)
        if "customer_id" in raw_payload and "message" in raw_payload:
            customer_id = raw_payload.get("customer_id") or "unknown"
            message = raw_payload.get("message", "")
            external_id = raw_payload.get("external_id")
            thread_id = raw_payload.get("thread_id")
            attachments = []
            channel_metadata = {"legacy_format": True}
        else:
            # WhatsApp Cloud API format
            entry = raw_payload.get("entry", [{}])[0]
            changes = entry.get("changes", [{}])[0]
            value = changes.get("value", {})
            messages = value.get("messages", [])
            msg = messages[0] if messages else {}

            customer_id = msg.get("from") or "unknown"
            message = self._extract_message_text(msg)
            external_id = msg.get("id")
            thread_id = msg.get("conversation", {}).get("id") if msg.get("conversation") else None
            attachments = self.extract_attachments(msg)
            channel_metadata = self.extract_channel_metadata(raw_payload)

        return NormalizedMessage(
            customer_id=customer_id,
            message=message,
            channel=self.channel_name,
            external_id=external_id,
            thread_id=thread_id,
            attachments=attachments,
            channel_metadata=channel_metadata,
        )

    def _extract_message_text(self, msg: dict[str, Any]) -> str:
        msg_type = msg.get("type", "text")
        if msg_type == "text":
            return msg.get("text", {}).get("body", "")
        elif msg_type in ("image", "document", "audio", "video", "sticker"):
            return f"[{msg_type.capitalize()} message]"
        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                return f"[Button: {interactive.get('button_reply', {}).get('title', '')}]"
            elif interactive.get("type") == "list_reply":
                return f"[List: {interactive.get('list_reply', {}).get('title', '')}]"
        return f"[{msg_type} message]"

    def extract_attachments(self, raw_payload: dict[str, Any]) -> list[Attachment]:
        attachments = []
        msg = raw_payload if "type" in raw_payload else {}
        for media_type in ("image", "document", "audio", "video", "sticker"):
            media = msg.get(media_type)
            if media:
                attachments.append(
                    Attachment(
                        id=media.get("id", ""),
                        filename=media.get("filename", f"{media_type}.bin"),
                        content_type=media.get("mime_type"),
                        size=media.get("file_size"),
                        url=media.get("url"),
                    )
                )
        return attachments

    def extract_channel_metadata(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        entry = raw_payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        contacts = value.get("contacts", [])
        contact = contacts[0] if contacts else {}
        return {
            "messaging_product": "whatsapp",
            "phone_number_id": value.get("metadata", {}).get("phone_number_id"),
            "display_phone_number": value.get("metadata", {}).get("display_phone_number"),
            "contact_name": contact.get("profile", {}).get("name"),
            "contact_wa_id": contact.get("wa_id"),
        }


class ChatIntakeAdapter(ChannelAdapter):
    channel_name = "chat"

    def normalize(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        # Legacy format (backward compatible with ChannelMessage)
        if "customer_id" in raw_payload and "message" in raw_payload:
            customer_id = raw_payload.get("customer_id") or "unknown"
            message = raw_payload.get("message", "")
            external_id = raw_payload.get("external_id")
            thread_id = raw_payload.get("thread_id")
            attachments = []
            channel_metadata = {"legacy_format": True}
        else:
            # Chat widget format (Intercom, Crisp, etc.)
            customer_id = raw_payload.get("visitor_id") or raw_payload.get("user_id") or "unknown"
            message = raw_payload.get("message") or raw_payload.get("text", "")
            external_id = raw_payload.get("message_id") or raw_payload.get("id")
            thread_id = raw_payload.get("session_id") or raw_payload.get("conversation_id")
            attachments = self.extract_attachments(raw_payload)
            channel_metadata = self.extract_channel_metadata(raw_payload)

        return NormalizedMessage(
            customer_id=customer_id,
            message=message,
            channel=self.channel_name,
            external_id=external_id,
            thread_id=thread_id,
            attachments=attachments,
            channel_metadata=channel_metadata,
        )

    def extract_attachments(self, raw_payload: dict[str, Any]) -> list[Attachment]:
        attachments = []
        for att in raw_payload.get("attachments", []):
            attachments.append(
                Attachment(
                    id=att.get("id", ""),
                    filename=att.get("filename", "unknown"),
                    content_type=att.get("content_type"),
                    size=att.get("size"),
                    url=att.get("url"),
                )
            )
        return attachments

    def extract_channel_metadata(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": raw_payload.get("session_id"),
            "visitor_info": raw_payload.get("visitor_info", {}),
            "page_url": raw_payload.get("page_url"),
            "referrer": raw_payload.get("referrer"),
            "user_agent": raw_payload.get("user_agent"),
        }


class CRMIntakeAdapter(ChannelAdapter):
    channel_name = "crm"

    def normalize(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        # Legacy format (backward compatible with ChannelMessage)
        if "customer_id" in raw_payload and "message" in raw_payload:
            customer_id = raw_payload.get("customer_id") or "unknown"
            message = raw_payload.get("message", "")
            external_id = raw_payload.get("external_id")
            thread_id = raw_payload.get("thread_id")
            attachments = []
            channel_metadata = {"legacy_format": True}
        else:
            # CRM webhook format (HubSpot, Salesforce, etc.)
            customer_id = raw_payload.get("contact_id") or raw_payload.get("customer_id") or "unknown"
            message = raw_payload.get("description") or raw_payload.get("notes") or raw_payload.get("message", "")
            external_id = raw_payload.get("ticket_id") or raw_payload.get("case_id") or raw_payload.get("id")
            thread_id = raw_payload.get("thread_id") or raw_payload.get("conversation_id")
            attachments = self.extract_attachments(raw_payload)
            channel_metadata = self.extract_channel_metadata(raw_payload)

        return NormalizedMessage(
            customer_id=customer_id,
            message=message,
            channel=self.channel_name,
            external_id=external_id,
            thread_id=thread_id,
            attachments=attachments,
            channel_metadata=channel_metadata,
        )

    def extract_attachments(self, raw_payload: dict[str, Any]) -> list[Attachment]:
        attachments = []
        for att in raw_payload.get("attachments", []):
            attachments.append(
                Attachment(
                    id=att.get("id", ""),
                    filename=att.get("filename", "unknown"),
                    content_type=att.get("content_type"),
                    size=att.get("size"),
                    url=att.get("url"),
                )
            )
        return attachments

    def extract_channel_metadata(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": raw_payload.get("source"),
            "deal_id": raw_payload.get("deal_id"),
            "pipeline_stage": raw_payload.get("pipeline_stage"),
            "priority": raw_payload.get("priority"),
            "tags": raw_payload.get("tags", []),
            "custom_fields": raw_payload.get("custom_fields", {}),
        }


ADAPTERS: dict[str, ChannelAdapter] = {
    "email": EmailIntakeAdapter(),
    "slack": SlackIntakeAdapter(),
    "whatsapp": WhatsAppIntakeAdapter(),
    "chat": ChatIntakeAdapter(),
    "crm": CRMIntakeAdapter(),
}


def get_adapter(channel: str) -> ChannelAdapter | None:
    return ADAPTERS.get(channel)


def normalize_message(channel: str, raw_payload: dict[str, Any]) -> NormalizedMessage | None:
    adapter = get_adapter(channel)
    if adapter is None:
        return None
    return adapter.normalize(raw_payload)


def enrich_customer_context(db: Session, customer_id: str) -> CustomerContext:
    from app.models import TicketRecord, UserRecord
    from sqlalchemy import select, func

    user = db.get(UserRecord, customer_id)

    ticket_count = db.scalar(
        select(func.count(TicketRecord.id)).where(TicketRecord.customer_id == customer_id)
    ) or 0

    open_ticket_count = db.scalar(
        select(func.count(TicketRecord.id)).where(
            TicketRecord.customer_id == customer_id,
            TicketRecord.status.in_(["open", "in_progress", "pending"]),
        )
    ) or 0

    last_ticket = db.scalar(
        select(TicketRecord)
        .where(TicketRecord.customer_id == customer_id)
        .order_by(TicketRecord.created_at.desc())
        .limit(1)
    )

    tier = "standard"
    if user:
        if user.role == "admin":
            tier = "vip"
        elif ticket_count > 50:
            tier = "premium"

    tags = []
    if ticket_count > 20:
        tags.append("frequent_contactor")
    if open_ticket_count > 5:
        tags.append("multiple_open_tickets")

    return CustomerContext(
        tier=tier,
        open_tickets_count=open_ticket_count,
        total_tickets_count=ticket_count,
        last_contact_at=last_ticket.created_at if last_ticket else None,
        tags=tags,
    )