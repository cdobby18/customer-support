"""Authorization boundaries: which role may reach which endpoint, and which
customer may reach which ticket.

Covers the four classes of defect the endpoint-by-endpoint review turned up:
internal notes visible to customers, customers able to set staff-only flags,
channel webhook threads crossing customer boundaries, and role checks that let
one admin disable another.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from types import SimpleNamespace

from app.core.models import AuditLogRecord, TicketCommentRecord, TicketRecord, UserRecord
from harness import ADMIN_ID, client, drop_schema, reset_schema, seed_admin, test_engine

PASSWORD = "correct horse battery"
OTHER_CUSTOMER_ID = "00000000-0000-0000-0000-0000000000ff"
WEBHOOK_SECRET = {"X-Webhook-Secret": "local-webhook-secret"}
User = SimpleNamespace


@pytest.fixture(autouse=True)
def clean_state():
    reset_schema()
    seed_admin()
    yield
    drop_schema()


def register(email: str, role: str) -> str:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD, "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def login(email: str) -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def admin_headers() -> dict[str, str]:
    """The seeded admin has no real password hash; mint its token directly."""
    from app.security.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(ADMIN_ID)}"}


def make_user(email: str, role: str, password: str = PASSWORD) -> User:
    """Create a user directly, with a usable password hash, and log in.

    Returns a plain namespace rather than the ORM row: the session that made
    it is closed by the time the tests use it.
    """
    from app.security.auth import hash_password

    user_id = str(uuid4())
    with Session(test_engine) as session:
        session.add(
            UserRecord(
                id=user_id,
                email=email,
                password_hash=hash_password(password),
                role=role,
                is_active=True,
                created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
            )
        )
        session.commit()
    return User(id=user_id, email=email, role=role)


def seed_other_customer() -> UserRecord:
    return make_user("other@example.com", "customer")


def insert_ticket(customer_id: str, **overrides) -> str:
    """Create a ticket through the API as staff, then stamp internal columns."""
    response = client.post(
        "/tickets",
        headers=admin_headers(),
        json={
            "customer_id": customer_id,
            "message": overrides.pop("message", "My export crashes every time I run it"),
            "channel": overrides.pop("channel", "web"),
        },
    )
    assert response.status_code == 201, response.text
    ticket_id = response.json()["id"]
    if overrides:
        with Session(test_engine) as session:
            record = session.get(TicketRecord, ticket_id)
            for key, value in overrides.items():
                setattr(record, key, value)
            session.commit()
    return ticket_id


def insert_comment(ticket_id: str, author_id: str, body: str, is_internal: bool) -> str:
    comment_id = str(uuid4())
    with Session(test_engine) as session:
        session.add(
            TicketCommentRecord(
                id=comment_id,
                ticket_id=ticket_id,
                author_id=author_id,
                body=body,
                is_internal=is_internal,
                created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
            )
        )
        session.commit()
    return comment_id


# --- internal notes are not readable by the ticket's customer ---------------


def test_customer_cannot_read_internal_comments_on_own_ticket() -> None:
    customer = make_user("owner@example.com", "customer")
    ticket_id = insert_ticket(customer.id)
    insert_comment(ticket_id, ADMIN_ID, "Escalate to billing team", is_internal=True)
    insert_comment(ticket_id, customer.id, "Any update?", is_internal=False)

    response = client.get(f"/tickets/{ticket_id}/comments", headers=login(customer.email))

    assert response.status_code == 200
    bodies = [comment["body"] for comment in response.json()]
    assert bodies == ["Any update?"]


def test_staff_reads_internal_and_public_comments_together() -> None:
    customer = make_user("owner2@example.com", "customer")
    ticket_id = insert_ticket(customer.id)
    insert_comment(ticket_id, ADMIN_ID, "Escalate to billing team", is_internal=True)
    insert_comment(ticket_id, customer.id, "Any update?", is_internal=False)

    response = client.get(f"/tickets/{ticket_id}/comments", headers=admin_headers())

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_customer_comment_ignores_requested_internal_flag() -> None:
    customer = make_user("sneaky@example.com", "customer")
    ticket_id = insert_ticket(customer.id)

    response = client.post(
        f"/tickets/{ticket_id}/comments",
        headers=login(customer.email),
        json={"body": "pretend this is internal", "is_internal": True},
    )

    assert response.status_code == 201
    assert response.json()["is_internal"] is False
    with Session(test_engine) as session:
        stored = session.scalars(
            select(TicketCommentRecord).where(TicketCommentRecord.ticket_id == ticket_id)
        ).one()
    assert stored.is_internal is False


def test_customer_cannot_comment_on_another_customers_ticket() -> None:
    owner = make_user("owner3@example.com", "customer")
    intruder = make_user("intruder@example.com", "customer")
    ticket_id = insert_ticket(owner.id)

    listed = client.get(f"/tickets/{ticket_id}/comments", headers=login(intruder.email))
    added = client.post(
        f"/tickets/{ticket_id}/comments",
        headers=login(intruder.email),
        json={"body": "let me in"},
    )

    assert listed.status_code == 403
    assert added.status_code == 403


# --- customer ticket projection hides internal triage signals ---------------


def test_customer_ticket_response_redacts_internal_triage_fields() -> None:
    customer = make_user("proj@example.com", "customer")
    ticket_id = insert_ticket(
        customer.id,
        risk_score=0.91,
        risk_level="critical",
        escalation_summary="Route to leadership",
        escalation_route="leadership",
        intake_metadata={"customer_context": {"tier": "enterprise"}},
        guardrail_hits=[{"rule_id": "pii.email", "matched": "a@b.com", "severity": "high"}],
    )

    response = client.get(f"/tickets/{ticket_id}", headers=login(customer.email))

    assert response.status_code == 200
    body = response.json()
    assert body["risk_score"] is None
    assert body["risk_level"] is None
    assert body["escalation_summary"] is None
    assert body["escalation_route"] is None
    assert body["intake_metadata"] is None
    assert body["guardrail_hits"] == [{"rule_id": "pii.email", "severity": "high"}]


def test_staff_ticket_response_keeps_internal_triage_fields() -> None:
    customer = make_user("proj2@example.com", "customer")
    ticket_id = insert_ticket(
        customer.id,
        risk_score=0.91,
        risk_level="critical",
        intake_metadata={"customer_context": {"tier": "enterprise"}},
        guardrail_hits=[{"rule_id": "pii.email", "matched": "a@b.com", "severity": "high"}],
    )

    body = client.get(f"/tickets/{ticket_id}", headers=admin_headers()).json()

    assert body["risk_score"] == pytest.approx(0.91)
    assert body["risk_level"] == "critical"
    assert body["intake_metadata"] == {"customer_context": {"tier": "enterprise"}}
    assert body["guardrail_hits"][0]["matched"] == "a@b.com"


def test_customer_ticket_list_is_scoped_and_redacted() -> None:
    customer = make_user("proj3@example.com", "customer")
    other = seed_other_customer()
    mine = insert_ticket(customer.id, risk_score=0.5)
    insert_ticket(other.id)

    body = client.get("/tickets", headers=login(customer.email)).json()

    assert [ticket["id"] for ticket in body] == [mine]
    assert body[0]["risk_score"] is None


# --- customers cannot choose their own channel or SLA ------------------------


def test_customer_cannot_open_ticket_on_a_provider_channel() -> None:
    customer = make_user("channel@example.com", "customer")

    response = client.post(
        "/tickets",
        headers=login(customer.email),
        json={"customer_id": customer.id, "message": "hello", "channel": "slack"},
    )

    assert response.status_code == 403


def test_customer_still_opens_web_tickets() -> None:
    customer = make_user("web@example.com", "customer")

    response = client.post(
        "/tickets",
        headers=login(customer.email),
        json={"customer_id": customer.id, "message": "hello", "channel": "web"},
    )

    assert response.status_code == 201
    assert response.json()["channel"] == "web"


def test_staff_may_open_tickets_for_a_customer_on_any_channel() -> None:
    other = seed_other_customer()

    response = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": other.id, "message": "hello", "channel": "slack"},
    )

    assert response.status_code == 201
    assert response.json()["channel"] == "slack"


# --- staff-only ticket operations -------------------------------------------


def test_customer_cannot_reach_staff_only_ticket_endpoints() -> None:
    customer = make_user("staffops@example.com", "customer")
    ticket_id = insert_ticket(customer.id)
    headers = login(customer.email)

    attempts = {
        "patch": client.patch(f"/tickets/{ticket_id}", headers=headers, json={"status": "resolved"}),
        "delete": client.delete(f"/tickets/{ticket_id}", headers=headers),
        "escalation": client.patch(
            f"/tickets/{ticket_id}/escalation", headers=headers, json={"status": "acknowledged"}
        ),
        "draft": client.post(f"/tickets/{ticket_id}/response-draft", headers=headers),
        "auto_respond": client.post(f"/tickets/{ticket_id}/auto-respond", headers=headers),
        "draft_decision": client.post(
            f"/tickets/{ticket_id}/draft-decision",
            headers=headers,
            json={"decision": "approve", "final_body": "We fixed it", "internal_note": "x"},
        ),
        "agent_assist": client.get(f"/tickets/{ticket_id}/agent-assist", headers=headers),
    }

    for name, response in attempts.items():
        assert response.status_code == 403, f"{name} returned {response.status_code}"


def test_agent_reaches_staff_only_endpoints() -> None:
    agent = make_user("agent@example.com", "agent")
    ticket_id = insert_ticket(seed_other_customer().id)
    headers = login(agent.email)

    assert client.get(f"/tickets/{ticket_id}/agent-assist", headers=headers).status_code == 200
    # The role gate passes; 503 is the LLM gateway being absent in tests.
    assert client.post(f"/tickets/{ticket_id}/response-draft", headers=headers).status_code == 503


def test_customer_cannot_read_another_customers_ticket() -> None:
    owner = make_user("owner4@example.com", "customer")
    intruder = make_user("intruder2@example.com", "customer")

    assert client.get(f"/tickets/{insert_ticket(owner.id)}", headers=login(intruder.email)).status_code == 403
    assert client.get("/tickets", headers=login(intruder.email)).json() == []


# --- channel webhooks cannot cross customer boundaries ----------------------


def test_webhook_thread_reply_from_another_customer_opens_a_new_ticket() -> None:
    owner = make_user("threadowner@example.com", "customer")
    attacker = make_user("threadattacker@example.com", "customer")
    ticket_id = insert_ticket(owner.id, channel="chat", thread_id="thread-1")

    injected = client.post(
        "/webhooks/chat",
        headers=WEBHOOK_SECRET,
        json={
            "customer_id": attacker.id,
            "message": "posting into someone else's thread",
            "thread_id": "thread-1",
        },
    )

    assert injected.status_code == 201
    assert injected.json()["id"] != ticket_id
    assert injected.json()["customer_id"] == attacker.id
    assert client.get(
        f"/tickets/{ticket_id}/comments", headers=login(owner.email)
    ).json() == []


def test_webhook_thread_reply_from_same_customer_joins_the_ticket() -> None:
    owner = make_user("threadowner2@example.com", "customer")
    ticket_id = insert_ticket(owner.id, channel="chat", thread_id="thread-2")

    reply = client.post(
        "/webhooks/chat",
        headers=WEBHOOK_SECRET,
        json={"customer_id": owner.id, "message": "follow-up question", "thread_id": "thread-2"},
    )

    assert reply.status_code == 200
    assert reply.json()["id"] == ticket_id
    bodies = [c["body"] for c in client.get(f"/tickets/{ticket_id}/comments", headers=login(owner.email)).json()]
    assert "follow-up question" in bodies


def test_webhook_thread_mismatch_is_audited() -> None:
    owner = make_user("threadowner3@example.com", "customer")
    attacker = make_user("threadattacker2@example.com", "customer")
    ticket_id = insert_ticket(owner.id, channel="chat", thread_id="thread-3")

    client.post(
        "/webhooks/chat",
        headers=WEBHOOK_SECRET,
        json={"customer_id": attacker.id, "message": "hijack", "thread_id": "thread-3"},
    )

    with Session(test_engine) as session:
        entries = list(
            session.scalars(
                select(AuditLogRecord).where(
                    AuditLogRecord.action == "channel.thread_customer_mismatch"
                )
            ).all()
        )
    assert len(entries) == 1
    assert entries[0].entity_id == ticket_id
    assert entries[0].details["claimed_customer_id"] == attacker.id
    assert entries[0].details["ticket_customer_id"] == owner.id


# --- admin boundary ---------------------------------------------------------


def test_admin_cannot_deactivate_another_admin() -> None:
    other_admin = make_user("admin2@example.com", "admin")

    response = client.patch(
        f"/admin/users/{other_admin.id}",
        headers=admin_headers(),
        json={"is_active": False},
    )

    assert response.status_code == 400
    with Session(test_engine) as session:
        assert session.get(UserRecord, other_admin.id).is_active is True


def test_admin_may_deactivate_a_customer() -> None:
    customer = make_user("demoted@example.com", "customer")

    response = client.patch(
        f"/admin/users/{customer.id}",
        headers=admin_headers(),
        json={"is_active": False},
    )

    assert response.status_code == 200
    with Session(test_engine) as session:
        assert session.get(UserRecord, customer.id).is_active is False


def test_agent_cannot_reach_admin_routes() -> None:
    agent = make_user("noadmin@example.com", "agent")
    headers = login(agent.email)

    assert client.get("/admin/users", headers=headers).status_code == 403
    assert client.get("/admin/audit-logs", headers=headers).status_code == 403
    assert client.get("/admin/analytics/dashboard", headers=headers).status_code == 403


def test_customer_cannot_reach_admin_routes_but_may_search_knowledge() -> None:
    customer = make_user("plain@example.com", "customer")
    headers = login(customer.email)

    assert client.get("/admin/users", headers=headers).status_code == 403
    assert client.get("/admin/audit-logs", headers=headers).status_code == 403
    assert client.get("/knowledge/search", params={"q": "refund"}, headers=headers).status_code == 200


def test_knowledge_search_requires_authentication() -> None:
    assert client.get("/knowledge/search", params={"query": "refund"}).status_code == 401
