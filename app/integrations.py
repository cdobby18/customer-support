"""Provider-agnostic helpdesk / CRM outbound integration layer.

Provider selection is driven by the ``SUPPORT_TOOL_PROVIDER`` environment
variable. When unset or ``none`` no outbound sync happens at all. ``mock``
provides a credential-free provider for development and tests; ``zendesk`` and
``hubspot`` are gated behind API credentials and raise ``IntegrationError``
until configured.
"""

from datetime import datetime, timezone
import base64
import json
import os
from typing import Any, Callable
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session


class IntegrationError(Exception):
    """Raised when a support-tool provider is missing or cannot execute."""


def _default_http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=15) as response:
        raw = response.read()
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return response.status, parsed


class HelpdeskProvider:
    name = "helpdesk"

    def __init__(self, http_request: Callable[..., Any] | None = None) -> None:
        self._http_request = http_request or _default_http_request

    def validate_config(self) -> None:
        raise IntegrationError(f"{self.name} provider is not configured")

    def create_or_update_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def push_comment(self, remote_ticket_id: str, comment: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class MockHelpdeskProvider(HelpdeskProvider):
    name = "mock"

    def validate_config(self) -> None:
        return None

    def create_or_update_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": self.name,
            "remote_id": f"mock-{ticket['id']}",
            "status": "synced",
        }

    def push_comment(self, remote_ticket_id: str, comment: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": self.name,
            "remote_id": remote_ticket_id,
            "remote_comment_id": f"mock-comment-{comment['id']}",
            "status": "synced",
        }


class ZendeskProvider(HelpdeskProvider):
    name = "zendesk"

    def validate_config(self) -> None:
        if not os.getenv("ZENDESK_SUBDOMAIN") or not os.getenv("ZENDESK_API_EMAIL") or not os.getenv(
            "ZENDESK_API_TOKEN"
        ):
            raise IntegrationError(
                "Zendesk provider requires ZENDESK_SUBDOMAIN, ZENDESK_API_EMAIL and ZENDESK_API_TOKEN"
            )

    def _base_url(self) -> str:
        return f"https://{os.getenv('ZENDESK_SUBDOMAIN').strip()}.zendesk.com/api/v2"

    def _headers(self) -> dict[str, str]:
        credentials = f"{os.getenv('ZENDESK_API_EMAIL')}/token:{os.getenv('ZENDESK_API_TOKEN')}"
        token = base64.b64encode(credentials.encode()).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def create_or_update_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        status, body = self._http_request(
            "POST",
            f"{self._base_url()}/tickets.json",
            self._headers(),
            {
                "ticket": {
                    "subject": ticket.get("subject", "Support request"),
                    "comment": {"body": ticket.get("message", ""), "public": True},
                    "external_id": ticket["id"],
                    "priority": ticket.get("priority"),
                    "status": ticket.get("status"),
                }
            },
        )
        remote_id = body.get("ticket", {}).get("id")
        return {
            "provider": self.name,
            "remote_id": str(remote_id) if remote_id is not None else None,
            "status": "synced" if status < 300 else "failed",
        }

    def push_comment(self, remote_ticket_id: str, comment: dict[str, Any]) -> dict[str, Any]:
        status, body = self._http_request(
            "POST",
            f"{self._base_url()}/tickets/{remote_ticket_id}/comments.json",
            self._headers(),
            {
                "ticket": {
                    "comment": {
                        "body": comment.get("body", ""),
                        "public": not comment.get("is_internal", False),
                    }
                }
            },
        )
        return {
            "provider": self.name,
            "remote_id": remote_ticket_id,
            "remote_comment_id": str(body.get("id", comment.get("id", ""))),
            "status": "synced" if status < 300 else "failed",
        }


class HubSpotProvider(HelpdeskProvider):
    name = "hubspot"

    def validate_config(self) -> None:
        if not os.getenv("HUBSPOT_ACCESS_TOKEN"):
            raise IntegrationError("HubSpot provider requires HUBSPOT_ACCESS_TOKEN")

    def _base_url(self) -> str:
        return "https://api.hubapi.com/crm/v3/objects"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {os.getenv('HUBSPOT_ACCESS_TOKEN')}",
            "Content-Type": "application/json",
        }

    def create_or_update_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        status, body = self._http_request(
            "POST",
            f"{self._base_url()}/tickets",
            self._headers(),
            {
                "properties": {
                    "subject": ticket.get("subject", "Support request"),
                    "content": ticket.get("message", ""),
                    "hs_ticket_priority": ticket.get("priority"),
                    "hs_ticket_status": ticket.get("status"),
                    "hs_pipeline_stage": ticket.get("intent"),
                },
                "associations": [],
            },
        )
        remote_id = body.get("id")
        return {
            "provider": self.name,
            "remote_id": str(remote_id) if remote_id is not None else None,
            "status": "synced" if status < 300 else "failed",
        }

    def push_comment(self, remote_ticket_id: str, comment: dict[str, Any]) -> dict[str, Any]:
        status, body = self._http_request(
            "POST",
            f"{self._base_url()}/notes",
            self._headers(),
            {
                "properties": {
                    "hs_timestamp": datetime.now(timezone.utc).isoformat(),
                    "hs_note_body": comment.get("body", ""),
                },
                "associations": [
                    {"types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 0}], "to": {"id": remote_ticket_id}}
                ],
            },
        )
        return {
            "provider": self.name,
            "remote_id": remote_ticket_id,
            "remote_comment_id": str(body.get("id", comment.get("id", ""))),
            "status": "synced" if status < 300 else "failed",
        }


PROVIDERS: dict[str, type[HelpdeskProvider]] = {
    "mock": MockHelpdeskProvider,
    "zendesk": ZendeskProvider,
    "hubspot": HubSpotProvider,
}


def get_provider() -> HelpdeskProvider:
    provider_name = os.getenv("SUPPORT_TOOL_PROVIDER", "").strip().lower()
    provider_class = PROVIDERS.get(provider_name)
    if provider_class is None:
        raise IntegrationError("No support tool provider configured (set SUPPORT_TOOL_PROVIDER)")
    provider = provider_class()
    provider.validate_config()
    return provider


def _integration_remote_id(ticket: Any) -> str | None:
    metadata = ticket.intake_metadata if isinstance(ticket.intake_metadata, dict) else {}
    integration = metadata.get("integration")
    if isinstance(integration, dict):
        remote_id = integration.get("remote_id")
        return str(remote_id) if remote_id else None
    return None


def sync_ticket_outbound(
    db: Session,
    ticket: Any,
    event: str,
    *,
    ticket_payload: dict[str, Any] | None = None,
    comment_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Push a ticket event to the configured helpdesk provider.

    Mutates ``ticket.intake_metadata`` with the latest integration state but
    does not commit; the caller owns the transaction and audit logging.
    Returns ``None`` when no provider is configured (sync is opt-in).
    """
    try:
        provider = get_provider()
    except IntegrationError:
        return None

    try:
        if event == "ticket.comment_added":
            remote_id = _integration_remote_id(ticket)
            if not remote_id:
                return {"status": "skipped", "reason": "no_remote_ticket_id", "provider": provider.name}
            result = provider.push_comment(remote_id, comment_payload or {})
        else:
            result = provider.create_or_update_ticket(ticket_payload or {})
    except Exception as exc:
        return {"status": "failed", "reason": str(exc), "provider": provider.name}

    state = {
        "provider": provider.name,
        "status": result.get("status", "synced"),
        "remote_id": result.get("remote_id"),
        "remote_comment_id": result.get("remote_comment_id"),
        "event": event,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "reason": result.get("reason") or (result.get("status") if result.get("status") != "synced" else None),
    }
    metadata = dict(ticket.intake_metadata or {})
    metadata["integration"] = {
        key: value for key, value in state.items() if key != "event" and value is not None
    }
    ticket.intake_metadata = metadata
    return state