from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.knowledge import MIN_MATCH_SCORE, KnowledgeMatch, search_knowledge
from app.agents.triage import TriageIntent, TriagePriority, TriageSentiment, classify_ticket
from app.api.routers import webhooks as webhooks
from app.api.routers.webhooks import _rate_limiter_memory
from app.core.config_validation import validate_security_configuration
from app.core.models import AuditLogRecord, UserRecord, UserRole
from app.security.auth import create_access_token
from harness import ADMIN_ID, client, drop_schema, reset_schema, seed_admin, test_engine


@pytest.fixture(autouse=True)
def reset_database():
    reset_schema()
    seed_admin()
    yield
    drop_schema()


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(ADMIN_ID)}"}


def test_health_check() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_billing_ticket_requires_human_review() -> None:
    response = client.post(
        "/tickets",
        headers=admin_headers(),
        json={
            "customer_id": "customer-1",
            "message": "I was charged twice for my subscription",
            "channel": "web",
        },
    )

    assert response.status_code == 201
    assert response.json()["intent"] == "billing"
    assert response.json()["requires_human_review"] is True
    assert response.json()["sentiment"] == "neutral"
    assert response.json()["recommended_team"] == "billing"
    assert response.json()["confidence"] > 0.9


def test_ticket_can_be_retrieved() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-2", "message": "My application crashes when I try to export a report"},
    ).json()

    response = client.get(f"/tickets/{created['id']}", headers=admin_headers())

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_ticket_can_be_assigned_and_moved_through_lifecycle() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-3", "message": "The app is not loading"},
    ).json()

    response = client.patch(
        f"/tickets/{created['id']}",
        headers=admin_headers(),
        json={"status": "in_progress", "assignee_id": "agent-1"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"
    assert response.json()["assignee_id"] == "agent-1"


def test_ticket_comments_are_stored_and_listed() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-4", "message": "I need help"},
    ).json()

    created_comment = client.post(
        f"/tickets/{created['id']}/comments",
        headers=admin_headers(),
        json={"author_id": "agent-2", "body": "I am looking into this", "is_internal": True},
    )
    listed_comments = client.get(f"/tickets/{created['id']}/comments", headers=admin_headers())

    assert created_comment.status_code == 201
    assert listed_comments.status_code == 200
    assert listed_comments.json()[0]["body"] == "I am looking into this"
    assert listed_comments.json()[0]["is_internal"] is True


def test_ticket_list_carries_public_message_stats() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-6", "message": "My printer is offline"},
    ).json()
    client.post(
        f"/tickets/{created['id']}/comments",
        headers=admin_headers(),
        json={"author_id": "agent-9", "body": "We are looking into it", "is_internal": False},
    )
    client.post(
        f"/tickets/{created['id']}/comments",
        headers=admin_headers(),
        json={"author_id": "agent-9", "body": "Internal only", "is_internal": True},
    )

    listed = client.get("/tickets", headers=admin_headers()).json()
    row = next(ticket for ticket in listed if ticket["id"] == created["id"])

    assert row["message_count"] == 1
    assert row["last_message_external"] is True
    assert row["last_message_at"] is not None

    single = client.get(f"/tickets/{created['id']}", headers=admin_headers()).json()
    assert single["message_count"] == 1
    assert single["last_message_external"] is True


def test_last_message_external_is_false_when_customer_replies_last() -> None:
    customer_id, headers = make_customer("notify@example.com")
    created = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "Order status please"},
    ).json()
    client.post(
        f"/tickets/{created['id']}/comments",
        headers=admin_headers(),
        json={"author_id": "admin-1", "body": "We are checking with the warehouse", "is_internal": False},
    )
    client.post(
        f"/tickets/{created['id']}/comments",
        headers=headers,
        json={"body": "Any update?"},
    )

    row = client.get("/tickets", headers=headers).json()[0]

    assert row["id"] == created["id"]
    assert row["message_count"] == 2
    assert row["last_message_external"] is False
    assert row["last_message_at"] is not None


def test_tickets_can_be_filtered_by_status() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-5", "message": "Please check my account"},
    ).json()
    client.patch(
        f"/tickets/{created['id']}",
        headers=admin_headers(),
        json={"status": "pending"},
    )

    response = client.get("/tickets", headers=admin_headers(), params={"status": "pending"})

    assert response.status_code == 200
    assert all(ticket["status"] == "pending" for ticket in response.json())


def test_user_roles_are_stored_with_authentication_ready_fields() -> None:
    user = UserRecord(
        id="user-1",
        email="agent@example.com",
        password_hash="hashed-password",
        role=UserRole.agent.value,
        is_active=True,
        created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )

    with Session(test_engine) as session:
        session.add(user)
        session.commit()
        saved_user = session.get(UserRecord, "user-1")

    assert saved_user is not None
    assert saved_user.role == UserRole.agent.value
    assert saved_user.password_hash == "hashed-password"


def test_customer_can_register_without_exposing_password_hash() -> None:
    response = client.post(
        "/auth/register",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    )

    assert response.status_code == 201
    assert response.json()["email"] == "customer@example.com"
    assert response.json()["role"] == "customer"
    assert "password_hash" not in response.json()


def test_registered_user_can_log_in_with_correct_password() -> None:
    client.post(
        "/auth/register",
        json={"email": "agent@example.com", "password": "correct horse battery"},
    )

    response = client.post(
        "/auth/login",
        json={"email": "AGENT@example.com", "password": "correct horse battery"},
    )

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert response.json()["user"]["email"] == "agent@example.com"
    assert response.json()["token_type"] == "bearer"
    assert response.json()["access_token"]


def test_access_token_identifies_current_user() -> None:
    client.post(
        "/auth/register",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    )
    login_response = client.post(
        "/auth/login",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    )

    response = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {login_response.json()['access_token']}"},
    )

    assert response.status_code == 200
    assert response.json()["email"] == "customer@example.com"


def test_login_rejects_an_incorrect_password() -> None:
    client.post(
        "/auth/register",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    )

    response = client.post(
        "/auth/login",
        json={"email": "customer@example.com", "password": "wrong password"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


def test_current_user_requires_a_valid_access_token() -> None:
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required"


def test_customer_can_only_see_own_tickets() -> None:
    owner = client.post(
        "/auth/register",
        json={"email": "owner@example.com", "password": "correct horse battery"},
    ).json()
    other = client.post(
        "/auth/register",
        json={"email": "other@example.com", "password": "correct horse battery"},
    ).json()
    owner_token = client.post(
        "/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery"},
    ).json()["access_token"]
    other_token = client.post(
        "/auth/login",
        json={"email": "other@example.com", "password": "correct horse battery"},
    ).json()["access_token"]
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    created = client.post(
        "/tickets",
        headers=owner_headers,
        json={"customer_id": owner["id"], "message": "My account is blocked"},
    ).json()

    own_response = client.get(f"/tickets/{created['id']}", headers=owner_headers)
    other_response = client.get(f"/tickets/{created['id']}", headers=other_headers)

    assert own_response.status_code == 200
    assert other_response.status_code == 403


def test_customer_cannot_update_tickets() -> None:
    customer = client.post(
        "/auth/register",
        json={"email": "owner@example.com", "password": "correct horse battery"},
    ).json()
    token = client.post(
        "/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer["id"], "message": "My account is blocked"},
    ).json()

    response = client.patch(
        f"/tickets/{created['id']}",
        headers=headers,
        json={"status": "closed"},
    )

    assert response.status_code == 403


def test_admin_can_create_staff_user() -> None:
    response = client.post(
        "/admin/users",
        headers=admin_headers(),
        json={
            "email": "agent@example.com",
            "password": "correct horse battery",
            "role": "agent",
        },
    )

    assert response.status_code == 201
    assert response.json()["role"] == "agent"
    assert "password_hash" not in response.json()

    login_response = client.post(
        "/auth/login",
        json={"email": "agent@example.com", "password": "correct horse battery"},
    )
    assert login_response.status_code == 200


def test_customer_cannot_create_staff_user() -> None:
    client.post(
        "/auth/register",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    )
    customer_token = client.post(
        "/auth/login",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    ).json()["access_token"]

    response = client.post(
        "/admin/users",
        headers={"Authorization": f"Bearer {customer_token}"},
        json={
            "email": "agent@example.com",
            "password": "correct horse battery",
            "role": "agent",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Administrator access required"


def test_staff_user_creation_rejects_duplicate_email() -> None:
    payload = {
        "email": "agent@example.com",
        "password": "correct horse battery",
        "role": "agent",
    }
    client.post("/admin/users", headers=admin_headers(), json=payload)

    response = client.post("/admin/users", headers=admin_headers(), json=payload)

    assert response.status_code == 409


def test_admin_can_list_and_deactivate_staff_users() -> None:
    created = client.post(
        "/admin/users",
        headers=admin_headers(),
        json={
            "email": "agent@example.com",
            "password": "correct horse battery",
            "role": "agent",
        },
    ).json()

    listed = client.get("/admin/users", headers=admin_headers())
    deactivated = client.patch(
        f"/admin/users/{created['id']}",
        headers=admin_headers(),
        json={"is_active": False},
    )
    login = client.post(
        "/auth/login",
        json={"email": "agent@example.com", "password": "correct horse battery"},
    )

    assert listed.status_code == 200
    assert any(user["id"] == created["id"] for user in listed.json())
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False
    assert login.status_code == 401


def test_customer_cannot_list_users() -> None:
    client.post(
        "/auth/register",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    )
    token = client.post(
        "/auth/login",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    ).json()["access_token"]

    response = client.get(
        "/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


def test_admin_cannot_deactivate_themselves() -> None:
    response = client.patch(
        "/admin/users/00000000-0000-0000-0000-000000000001",
        headers=admin_headers(),
        json={"is_active": False},
    )

    assert response.status_code == 400


def test_audit_log_stores_actor_action_and_details() -> None:
    audit_log = AuditLogRecord(
        id="00000000-0000-0000-0000-000000000010",
        actor_id="00000000-0000-0000-0000-000000000001",
        action="ticket.status_changed",
        entity_type="ticket",
        entity_id="00000000-0000-0000-0000-000000000020",
        details={"from": "open", "to": "in_progress"},
        created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )

    with Session(test_engine) as session:
        session.add(audit_log)
        session.commit()
        saved_log = session.get(AuditLogRecord, audit_log.id)

    assert saved_log is not None
    assert saved_log.action == "ticket.status_changed"
    assert saved_log.details == {"from": "open", "to": "in_progress"}


def test_ticket_and_user_mutations_create_audit_events() -> None:
    staff = client.post(
        "/admin/users",
        headers=admin_headers(),
        json={
            "email": "agent@example.com",
            "password": "correct horse battery",
            "role": "agent",
        },
    ).json()
    ticket = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-1", "message": "The app is not loading"},
    ).json()
    client.patch(
        f"/tickets/{ticket['id']}",
        headers=admin_headers(),
        json={"status": "in_progress", "assignee_id": staff["id"]},
    )
    client.post(
        f"/tickets/{ticket['id']}/comments",
        headers=admin_headers(),
        json={"author_id": "ignored", "body": "Investigating now", "is_internal": True},
    )

    with Session(test_engine) as session:
        actions = session.scalars(select(AuditLogRecord.action)).all()

    assert "user.created" in actions
    assert "ticket.created" in actions
    assert "ticket.updated" in actions
    assert "ticket.comment_added" in actions


def test_admin_can_filter_and_paginate_audit_logs() -> None:
    client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-1", "message": "The app is not loading"},
    )
    client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-2", "message": "I was charged twice"},
    )

    response = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "ticket.created", "entity_type": "ticket", "limit": 1},
    )

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["action"] == "ticket.created"
    assert response.json()[0]["details"]["intent"] in {"general_support", "billing"}


def test_non_admin_cannot_read_audit_logs() -> None:
    client.post(
        "/auth/register",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    )
    token = client.post(
        "/auth/login",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    ).json()["access_token"]

    response = client.get(
        "/admin/audit-logs",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


def test_structured_triage_routes_sensitive_billing_issue() -> None:
    result = classify_ticket("I am frustrated because I was charged twice for my subscription")

    assert result.intent == TriageIntent.billing
    assert result.priority == TriagePriority.high
    assert result.sentiment == TriageSentiment.frustrated
    assert result.recommended_team == "billing"
    assert result.requires_human_review is True
    assert 0 <= result.confidence <= 1


def test_structured_triage_routes_routine_technical_issue() -> None:
    result = classify_ticket("The dashboard shows an error when I upload a file")

    assert result.intent == TriageIntent.technical_support
    assert result.priority == TriagePriority.normal
    assert result.recommended_team == "engineering_support"
    assert result.requires_human_review is False


def test_knowledge_search_returns_grounded_billing_excerpt() -> None:
    matches = search_knowledge("charged twice refund")

    assert matches
    assert matches[0].document_id == "billing-and-refunds"
    assert "human approval" in matches[0].excerpt.lower()


def test_knowledge_search_drops_matches_below_relevance_floor() -> None:
    assert search_knowledge("how do I bake a cake") == []


def test_knowledge_search_floor_is_overridable() -> None:
    loose = search_knowledge("shipping", min_score=0.0)

    assert loose
    assert search_knowledge("shipping", min_score=MIN_MATCH_SCORE) == []


def test_authenticated_knowledge_search_endpoint_returns_sources() -> None:
    response = client.get(
        "/knowledge/search",
        headers=admin_headers(),
        params={"q": "password reset"},
    )

    assert response.status_code == 200
    assert response.json()[0]["source"] == "account-access.md"


def test_sensitive_ticket_enters_human_review_and_can_be_approved() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-1", "message": "This payment is fraud and unauthorized"},
    ).json()

    assert created["requires_human_review"] is True
    assert created["escalation_status"] == "pending"

    reviewed = client.patch(
        f"/tickets/{created['id']}/escalation",
        headers=admin_headers(),
        json={"status": "approved", "reason": "Verified by support lead"},
    )

    assert reviewed.status_code == 200
    assert reviewed.json()["escalation_status"] == "approved"
    assert reviewed.json()["reviewed_by"]


def test_customer_cannot_review_escalation() -> None:
    customer = client.post(
        "/auth/register",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    ).json()
    token = client.post(
        "/auth/login",
        json={"email": "customer@example.com", "password": "correct horse battery"},
    ).json()["access_token"]
    created = client.post(
        "/tickets",
        headers={"Authorization": f"Bearer {token}"},
        json={"customer_id": customer["id"], "message": "I think this is fraud"},
    ).json()

    response = client.patch(
        f"/tickets/{created['id']}/escalation",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "rejected"},
    )

    assert response.status_code == 403


def test_ticket_has_sla_deadline_and_metrics_track_resolution() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-1", "message": "The app shows an error"},
    ).json()
    assert created["sla_due_at"] is not None
    assert created["first_response_at"] is None

    comment = client.post(
        f"/tickets/{created['id']}/comments",
        headers=admin_headers(),
        json={"author_id": "ignored", "body": "We are investigating"},
    )
    resolved = client.patch(
        f"/tickets/{created['id']}",
        headers=admin_headers(),
        json={"status": "resolved"},
    )
    metrics = client.get("/admin/analytics/sla", headers=admin_headers())

    assert comment.status_code == 201
    assert resolved.status_code == 200
    assert resolved.json()["first_response_at"] is not None
    assert resolved.json()["resolved_at"] is not None
    assert metrics.status_code == 200
    assert metrics.json()["resolved_tickets"] == 1
    assert metrics.json()["average_resolution_hours"] is not None


def test_channel_webhook_normalizes_email_into_a_ticket() -> None:
    response = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={
            "customer_id": "customer-email-1",
            "external_id": "email-123",
            "message": "The dashboard shows an error",
        },
    )

    assert response.status_code == 201
    assert response.json()["channel"] == "email"
    assert response.json()["intent"] == "technical_support"


def test_channel_webhook_rejects_missing_or_invalid_secret() -> None:
    missing_secret = client.post(
        "/webhooks/chat",
        json={"customer_id": "customer-chat-1", "message": "I need help"},
    )
    invalid_secret = client.post(
        "/webhooks/chat",
        headers={"X-Webhook-Secret": "wrong-secret"},
        json={"customer_id": "customer-chat-1", "message": "I need help"},
    )

    assert missing_secret.status_code == 401
    assert invalid_secret.status_code == 401


def test_channel_webhook_rejects_a_payload_with_no_usable_message() -> None:
    # Every adapter reads vendor-specific keys; a payload carrying none of them
    # normalizes to an empty message, which pydantic rejects. That is the
    # sender's bad request, so it must be a 400 and not an unhandled 500.
    empty = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={},
    )
    blank_text = client.post(
        "/webhooks/slack",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={"event": {"user": "U1", "text": ""}},
    )
    no_envelope = client.post(
        "/webhooks/whatsapp",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={"entry": [{}]},
    )

    assert empty.status_code == 400
    assert blank_text.status_code == 400
    assert no_envelope.status_code == 400


def test_channel_webhook_creates_escalation_for_sensitive_message() -> None:
    response = client.post(
        "/webhooks/slack",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={"customer_id": "customer-slack-1", "message": "This payment is fraud"},
    )

    assert response.status_code == 201
    assert response.json()["requires_human_review"] is True
    assert response.json()["escalation_status"] == "pending"


def test_production_rejects_default_security_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("CHANNEL_WEBHOOK_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        validate_security_configuration()


def test_production_accepts_configured_security_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "a-long-production-jwt-secret")
    monkeypatch.setenv("CHANNEL_WEBHOOK_SECRET", "a-long-production-webhook-secret")

    validate_security_configuration()


def test_webhook_rate_limit_rejects_excess_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_PER_MINUTE", "1")
    # The limiter falls back to an in-memory store only when Redis is
    # unreachable. With a real Redis reachable, every webhook test in the suite
    # shares the same 60-second window, so a request made here is already inside
    # a window another test consumed and the first post would be 429. This test
    # is about the ceiling logic, not Redis persistence, so force the memory
    # path and clear it regardless of where the suite runs.
    monkeypatch.setattr(webhooks, "_rate_limiter_redis", None)
    monkeypatch.setattr(webhooks, "_get_redis", lambda: None)
    _rate_limiter_memory.clear()
    response = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={"customer_id": "customer-rate-1", "message": "first"},
    )
    limited = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={"customer_id": "customer-rate-2", "message": "second"},
    )

    assert response.status_code == 201
    assert limited.status_code == 429


def make_customer(email: str = "feedback@example.com") -> tuple[str, dict[str, str]]:
    customer = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery"},
    ).json()
    token = client.post(
        "/auth/login",
        json={"email": email, "password": "correct horse battery"},
    ).json()["access_token"]
    return customer["id"], {"Authorization": f"Bearer {token}"}


def test_customer_can_submit_feedback_on_resolved_ticket() -> None:
    customer_id, headers = make_customer()
    created = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "The app shows an error"},
    ).json()
    client.patch(f"/tickets/{created['id']}", headers=admin_headers(), json={"status": "resolved"})

    response = client.post(
        f"/tickets/{created['id']}/feedback",
        headers=headers,
        json={"rating": 5, "comment": "quick fix"},
    )
    audit = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "ticket.feedback_submitted"},
    )

    assert response.status_code == 201
    assert response.json()["rating"] == 5
    assert response.json()["comment"] == "quick fix"
    assert response.json()["ticket_id"] == created["id"]
    assert audit.status_code == 200
    assert audit.json()[0]["action"] == "ticket.feedback_submitted"


def test_feedback_rejects_rating_outside_valid_range() -> None:
    customer_id, headers = make_customer()
    created = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "The app shows an error"},
    ).json()
    client.patch(f"/tickets/{created['id']}", headers=admin_headers(), json={"status": "resolved"})

    response = client.post(
        f"/tickets/{created['id']}/feedback",
        headers=headers,
        json={"rating": 6},
    )

    assert response.status_code == 422


def test_feedback_requires_resolved_ticket() -> None:
    customer_id, headers = make_customer()
    created = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "The app shows an error"},
    ).json()

    response = client.post(
        f"/tickets/{created['id']}/feedback",
        headers=headers,
        json={"rating": 4},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Feedback can only be submitted for resolved tickets"


def test_customer_cannot_submit_feedback_on_another_ticket() -> None:
    other_id, _ = make_customer("other@example.com")
    owner_id, owner_headers = make_customer()
    created = client.post(
        "/tickets",
        headers=owner_headers,
        json={"customer_id": owner_id, "message": "The app shows an error"},
    ).json()
    client.patch(f"/tickets/{created['id']}", headers=admin_headers(), json={"status": "resolved"})

    response = client.post(
        f"/tickets/{created['id']}/feedback",
        headers={"Authorization": f"Bearer {create_access_token(other_id)}"},
        json={"rating": 4},
    )

    assert response.status_code == 403


def test_feedback_rejects_duplicate_submission() -> None:
    customer_id, headers = make_customer()
    created = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "The app shows an error"},
    ).json()
    client.patch(f"/tickets/{created['id']}", headers=admin_headers(), json={"status": "resolved"})
    first = client.post(
        f"/tickets/{created['id']}/feedback",
        headers=headers,
        json={"rating": 5},
    )
    duplicate = client.post(
        f"/tickets/{created['id']}/feedback",
        headers=headers,
        json={"rating": 1},
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Feedback for this ticket already exists"


def test_feedback_analytics_reports_rating_response_and_deflection_rates() -> None:
    customer_id, headers = make_customer()

    deflected = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "The app shows an error"},
    ).json()
    client.patch(f"/tickets/{deflected['id']}", headers=admin_headers(), json={"status": "resolved"})

    assisted = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "The app shows an error"},
    ).json()
    client.post(
        f"/tickets/{assisted['id']}/comments",
        headers=admin_headers(),
        json={"author_id": "ignored", "body": "We are investigating"},
    )
    client.patch(f"/tickets/{assisted['id']}", headers=admin_headers(), json={"status": "resolved"})

    escalated = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "This payment is fraud"},
    ).json()
    client.patch(
        f"/tickets/{escalated['id']}/escalation",
        headers=admin_headers(),
        json={"status": "approved", "reason": "Verified by support lead"},
    )
    client.patch(f"/tickets/{escalated['id']}", headers=admin_headers(), json={"status": "resolved"})

    client.post(
        f"/tickets/{deflected['id']}/feedback",
        headers=headers,
        json={"rating": 5},
    )
    client.post(
        f"/tickets/{assisted['id']}/feedback",
        headers=headers,
        json={"rating": 3, "comment": "slow"},
    )

    analytics = client.get("/admin/analytics/feedback", headers=admin_headers())

    assert analytics.status_code == 200
    assert analytics.json()["total_feedback"] == 2
    assert analytics.json()["average_rating"] == 4.0
    assert analytics.json()["rating_distribution"] == {"1": 0, "2": 0, "3": 1, "4": 0, "5": 1}
    assert analytics.json()["response_rate"] == 0.6667
    assert analytics.json()["deflection_rate"] == 0.3333


def test_ai_resolved_tickets_count_as_deflected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: `run_auto_response` sets `first_response_at` on its own reply.

    The old metric treated any ticket with `first_response_at` as manually
    handled, so a fully AI-resolved ticket reported a 0.0 deflection rate and
    the KPI could never be anything but zero.
    """
    _fixed_kb(monkeypatch, [_kb_match()])
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/auto-respond",
        headers=admin_headers(),
    )
    assert response.status_code == 200
    assert response.json()["action"] == "auto_sent"

    ticket = client.get(f"/tickets/{created['id']}", headers=admin_headers()).json()
    assert ticket["resolved_at"] is not None
    assert ticket["first_response_at"] is not None

    analytics = client.get("/admin/analytics/feedback", headers=admin_headers())
    assert analytics.status_code == 200
    assert analytics.json()["deflection_rate"] == 1.0

    dashboard = client.get("/admin/analytics/dashboard", headers=admin_headers())
    assert dashboard.status_code == 200
    assert dashboard.json()["csat"]["deflection_rate"] == 1.0


def test_ai_resolved_draft_is_grounded_not_the_customer_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto-response posts the draft publicly, so it must not be an echo."""
    _fixed_kb(monkeypatch, [_kb_match()])
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/auto-respond",
        headers=admin_headers(),
    )
    assert response.status_code == 200
    draft = response.json()["draft"]["draft"]

    assert draft
    assert draft != created["message"]
    assert created["message"] not in draft
    assert response.json()["draft"]["citations"]


def test_non_admin_cannot_read_feedback_analytics() -> None:
    _, headers = make_customer()

    response = client.get("/admin/analytics/feedback", headers=headers)

    assert response.status_code == 403


def test_ticket_with_credit_card_is_flagged_and_escalated() -> None:
    response = client.post(
        "/tickets",
        headers=admin_headers(),
        json={
            "customer_id": "customer-1",
            "message": "Please look at this card 4111-1111-1111-1111",
            "channel": "web",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["guardrail_status"] == "flagged"
    assert body["guardrail_hits"]
    assert body["requires_human_review"] is True
    assert body["escalation_status"] == "pending"
    assert "Guardrail" in body["escalation_reason"]
    assert any(hit["rule_id"] == "pii.credit_card" for hit in body["guardrail_hits"])


def test_ticket_with_credit_card_is_audited() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={
            "customer_id": "customer-1",
            "message": "Please look at this card 4111-1111-1111-1111",
            "channel": "web",
        },
    ).json()

    audit = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "ticket.guardrail_flagged"},
    )

    assert audit.status_code == 200
    assert audit.json()[0]["entity_id"] == created["id"]
    assert audit.json()[0]["details"]["violation_count"] == len(created["guardrail_hits"])


def test_normal_ticket_is_guardrail_clean() -> None:
    response = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-1", "message": "My application crashes when I try to export a report"},
    )

    assert response.status_code == 201
    assert response.json()["guardrail_status"] == "clean"
    assert response.json()["guardrail_hits"] is None


def test_webhook_with_phone_pii_is_flagged_but_not_escalated() -> None:
    response = client.post(
        "/webhooks/chat",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={"customer_id": "customer-w-1", "message": "Contact me at 555-123-4567 please"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["guardrail_status"] == "flagged"
    assert any(hit["rule_id"] == "pii.phone" for hit in body["guardrail_hits"])
    assert body["requires_human_review"] is False


def test_blocked_domain_ticket_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOCKED_EMAIL_DOMAINS", "blocked.example")
    customer_id, headers = make_customer("bad@blocked.example")

    response = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "Please help me with my account"},
    )

    assert response.status_code == 201
    assert response.json()["guardrail_status"] == "flagged"
    assert any(
        hit["rule_id"] == "policy.blocked_domain" for hit in response.json()["guardrail_hits"]
    )


def test_ticket_creation_enqueues_notification_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched: list[tuple] = []
    monkeypatch.setattr("app.api.routers.tickets.enqueue_notification", lambda *args: dispatched.append(args))
    customer_id, headers = make_customer()

    response = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "The app crashes on login"},
    )

    assert response.status_code == 201
    assert len(dispatched) == 1
    event, ticket_id, payload = dispatched[0]
    assert event == "ticket.created"
    assert ticket_id == response.json()["id"]
    assert payload["customer_id"] == customer_id
    assert payload["intent"] == "account_access"


def test_channel_webhook_enqueues_notification_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched: list[tuple] = []
    monkeypatch.setattr("app.api.routers.webhooks.enqueue_notification", lambda *args: dispatched.append(args))

    response = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={"customer_id": "customer-q-1", "external_id": "email-q-1", "message": "Refund please"},
    )

    assert response.status_code == 201
    assert len(dispatched) == 1
    event, ticket_id, payload = dispatched[0]
    assert event == "ticket.received"
    assert ticket_id == response.json()["id"]
    assert payload["channel"] == "email"
    assert payload["external_id"] == "email-q-1"
    assert payload["customer_id"] == "customer-q-1"


def test_thread_reply_joins_existing_ticket() -> None:
    first = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={
            "customer_id": "customer-thread-1",
            "external_id": "email-t-1",
            "thread_id": "thread-alpha",
            "message": "The dashboard shows an error",
        },
    )
    reply = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={
            "customer_id": "customer-thread-1",
            "external_id": "email-t-2",
            "thread_id": "thread-alpha",
            "message": "It still fails after refresh",
        },
    )

    assert first.status_code == 201
    assert reply.status_code == 200
    assert reply.json()["id"] == first.json()["id"]
    comments = client.get(
        f"/tickets/{first.json()['id']}/comments",
        headers=admin_headers(),
    ).json()
    assert len(comments) == 1
    assert comments[0]["body"] == "It still fails after refresh"


def test_thread_reply_to_resolved_ticket_creates_new_ticket() -> None:
    first = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={
            "customer_id": "customer-thread-2",
            "external_id": "email-t-5",
            "thread_id": "thread-beta",
            "message": "The dashboard shows an error",
        },
    ).json()
    client.patch(
        f"/tickets/{first['id']}",
        headers=admin_headers(),
        json={"status": "resolved"},
    )

    reopened = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={
            "customer_id": "customer-thread-2",
            "external_id": "email-t-6",
            "thread_id": "thread-beta",
            "message": "Actually still broken",
        },
    )

    assert reopened.status_code == 201
    reopened_json = reopened.json()
    assert reopened_json["id"] != first["id"]
    assert reopened_json["thread_id"] == "thread-beta"
    assert reopened_json["status"] == "open"


def test_webhook_dedups_same_external_id() -> None:
    first = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={
            "customer_id": "customer-dedup-1",
            "external_id": "email-dd-1",
            "message": "The dashboard shows an error",
        },
    )
    repeat = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={
            "customer_id": "customer-dedup-1",
            "external_id": "email-dd-1",
            "message": "The dashboard shows an error",
        },
    )

    assert first.status_code == 201
    assert repeat.status_code == 200
    assert repeat.json()["id"] == first.json()["id"]
    tickets = client.get("/tickets", headers=admin_headers()).json()
    assert len(tickets) == 1
    assert tickets[0]["id"] == first.json()["id"]


def test_thread_reply_with_risky_content_escalates_ticket() -> None:
    first = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={
            "customer_id": "customer-thread-3",
            "external_id": "email-t-7",
            "thread_id": "thread-gamma",
            "message": "The dashboard shows an error",
        },
    ).json()
    assert first["requires_human_review"] is False

    reply = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={
            "customer_id": "customer-thread-3",
            "external_id": "email-t-8",
            "thread_id": "thread-gamma",
            "message": "I want a $1000 refund please",
        },
    )

    assert reply.status_code == 200
    reply_json = reply.json()
    assert reply_json["requires_human_review"] is True
    assert reply_json["escalation_status"] == "pending"
    assert "Guardrail" in (reply_json["escalation_reason"] or "")


def test_chat_channel_has_faster_sla_than_email() -> None:
    email = client.post(
        "/webhooks/email",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={"customer_id": "customer-sla-1", "message": "The dashboard shows an error"},
    ).json()
    chat = client.post(
        "/webhooks/chat",
        headers={"X-Webhook-Secret": "local-webhook-secret"},
        json={"customer_id": "customer-sla-2", "message": "The dashboard shows an error"},
    ).json()

    email_due = datetime.fromisoformat(email["sla_due_at"])
    chat_due = datetime.fromisoformat(chat["sla_due_at"])
    assert chat_due < email_due


def test_analytics_dashboard_aggregates_metrics() -> None:
    customer_id, headers = make_customer("analytics@example.com")
    created = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "The app shows an error"},
    ).json()
    client.patch(
        f"/tickets/{created['id']}",
        headers=admin_headers(),
        json={"status": "resolved"},
    )
    client.post(
        f"/tickets/{created['id']}/feedback",
        headers=headers,
        json={"rating": 5},
    )

    response = client.get("/admin/analytics/dashboard", headers=admin_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["sla"]["total_tickets"] == 1
    assert payload["sla"]["resolved_tickets"] == 1
    assert payload["csat"]["total_feedback"] == 1
    assert payload["csat"]["average_rating"] == 5.0
    assert payload["csat"]["rating_distribution"] == {"1": 0, "2": 0, "3": 0, "4": 0, "5": 1}
    assert payload["escalation"]["total_escalated"] == 0
    assert payload["workload"][0]["assignee_id"] is None
    assert payload["workload"][0]["assigned_tickets"] == 1
    assert payload["channels"] == [{"value": "web", "count": 1}]
    assert payload["intents"][0]["value"] == "technical_support"
    assert payload["confidence"]["low"] == 0


def test_analytics_dashboard_requires_admin() -> None:
    _, headers = make_customer("analytics-forbidden@example.com")
    response = client.get("/admin/analytics/dashboard", headers=headers)

    assert response.status_code == 403


def test_outbound_sync_skips_when_provider_not_configured() -> None:
    response = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-1", "message": "The dashboard shows an error"},
    )

    assert response.status_code == 201
    assert "integration" not in response.json()["intake_metadata"]
    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "integration.synced"},
    ).json()
    assert logs == []


def test_outbound_sync_to_mock_provider_records_integration_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPPORT_TOOL_PROVIDER", "mock")
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-1", "message": "The dashboard shows an error"},
    ).json()
    integration = created["intake_metadata"]["integration"]
    assert integration["provider"] == "mock"
    assert integration["status"] == "synced"
    assert integration["remote_id"] == f"mock-{created['id']}"

    comment = client.post(
        f"/tickets/{created['id']}/comments",
        headers=admin_headers(),
        json={"author_id": "customer-1", "body": "Thanks for looking into this"},
    )
    assert comment.status_code == 201
    ticket = client.get(f"/tickets/{created['id']}", headers=admin_headers()).json()
    assert (
        ticket["intake_metadata"]["integration"]["remote_comment_id"]
        == f"mock-comment-{comment.json()['id']}"
    )

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "integration.synced"},
    ).json()
    assert sorted(log["details"]["event"] for log in logs) == ["ticket.comment_added", "ticket.created"]


def test_internal_comments_are_not_synced_outbound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_TOOL_PROVIDER", "mock")
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-1", "message": "The dashboard shows an error"},
    ).json()
    client.post(
        f"/tickets/{created['id']}/comments",
        headers=admin_headers(),
        json={"author_id": "agent-1", "body": "Internal note only", "is_internal": True},
    )

    ticket = client.get(f"/tickets/{created['id']}", headers=admin_headers()).json()
    integration = ticket["intake_metadata"]["integration"]
    assert "remote_comment_id" not in integration
    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "integration.synced"},
    ).json()
    assert len(logs) == 1
    assert logs[0]["details"]["event"] == "ticket.created"


def test_comment_sync_skips_without_remote_ticket_id(monkeypatch: pytest.MonkeyPatch) -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-1", "message": "The dashboard shows an error"},
    ).json()
    monkeypatch.setenv("SUPPORT_TOOL_PROVIDER", "mock")
    client.post(
        f"/tickets/{created['id']}/comments",
        headers=admin_headers(),
        json={"author_id": "customer-1", "body": "Following up"},
    )

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "integration.synced"},
    ).json()
    assert len(logs) == 1
    assert logs[0]["details"]["status"] == "skipped"
    assert logs[0]["details"]["reason"] == "no_remote_ticket_id"


def test_ticket_update_syncs_outbound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_TOOL_PROVIDER", "mock")
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-1", "message": "The dashboard shows an error"},
    ).json()
    client.patch(
        f"/tickets/{created['id']}",
        headers=admin_headers(),
        json={"status": "in_progress"},
    )

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "integration.synced"},
    ).json()
    assert sorted(log["details"]["event"] for log in logs) == ["ticket.created", "ticket.updated"]


def test_security_configuration_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_TOOL_PROVIDER", "salesforce")
    with pytest.raises(RuntimeError, match="invalid"):
        validate_security_configuration()
    monkeypatch.setenv("SUPPORT_TOOL_PROVIDER", "mock")
    validate_security_configuration()


def test_security_configuration_rejects_unknown_llm_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama-server")
    with pytest.raises(RuntimeError, match="invalid"):
        validate_security_configuration()
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    validate_security_configuration()


def test_security_configuration_rejects_unknown_embedding_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "aws-bedrock")
    with pytest.raises(RuntimeError, match="invalid"):
        validate_security_configuration()
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    validate_security_configuration()


def test_production_openai_embeddings_require_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "a-long-production-jwt-secret")
    monkeypatch.setenv("CHANNEL_WEBHOOK_SECRET", "a-long-production-webhook-secret")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        validate_security_configuration()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-embed-prod")
    validate_security_configuration()


def test_production_rejects_mock_llm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "a-long-production-jwt-secret")
    monkeypatch.setenv("CHANNEL_WEBHOOK_SECRET", "a-long-production-webhook-secret")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    with pytest.raises(RuntimeError, match="mock is not allowed"):
        validate_security_configuration()


def test_production_openai_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "a-long-production-jwt-secret")
    monkeypatch.setenv("CHANNEL_WEBHOOK_SECRET", "a-long-production-webhook-secret")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        validate_security_configuration()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-prod-123")
    validate_security_configuration()


def test_production_azure_openai_requires_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "a-long-production-jwt-secret")
    monkeypatch.setenv("CHANNEL_WEBHOOK_SECRET", "a-long-production-webhook-secret")
    monkeypatch.setenv("LLM_PROVIDER", "azure_openai")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AZURE_OPENAI_ENDPOINT"):
        validate_security_configuration()
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "deploy-1")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    validate_security_configuration()


def test_admin_llm_usage_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents import llm as llm_module

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    llm_module.reset_llm_usage()
    llm_module.generate(prompt="Hello support")

    summary = client.get("/admin/llm/usage", headers=admin_headers())
    assert summary.status_code == 200
    assert summary.json()["total_calls"] == 1
    assert summary.json()["by_model"][0]["model"] == "mock-llm"

    reset = client.post("/admin/llm/usage/reset", headers=admin_headers())
    assert reset.status_code == 200
    assert reset.json()["total_calls"] == 0

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "llm.usage_reset"},
    ).json()
    assert len(logs) == 1
    assert logs[0]["details"] == {}


def test_admin_llm_usage_requires_admin() -> None:
    _, headers = make_customer("llm-forbidden@example.com")
    response = client.get("/admin/llm/usage", headers=headers)
    assert response.status_code == 403


def _create_ticket(message: str) -> dict:
    return client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-draft", "message": message},
    ).json()


def test_response_draft_endpoint_generates_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/response-draft",
        headers=admin_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ticket_id"] == created["id"]
    assert body["provider"] == "mock"
    assert body["draft"]
    assert 0.0 <= body["confidence"] <= 1.0

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "response.draft_generated"},
    ).json()
    assert len(logs) == 1
    assert logs[0]["details"]["provider"] == "mock"


def test_response_draft_requires_staff() -> None:
    _, headers = make_customer("draft-customer@example.com")
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/response-draft",
        headers=headers,
    )

    assert response.status_code == 403


def test_response_draft_unknown_ticket() -> None:
    response = client.post(
        "/tickets/00000000-0000-0000-0000-000000000099/response-draft",
        headers=admin_headers(),
    )

    assert response.status_code == 404


def test_response_draft_requires_llm_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/response-draft",
        headers=admin_headers(),
    )

    assert response.status_code == 503


def _fixed_kb(monkeypatch: pytest.MonkeyPatch, matches: list[KnowledgeMatch]) -> None:
    monkeypatch.setattr(
        "app.agents.response_agent.search_knowledge",
        lambda message, limit=5: matches,
    )
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("AUTO_RESPOND_ENABLED", raising=False)


def _kb_match() -> KnowledgeMatch:
    return KnowledgeMatch(
        document_id="account-access",
        title="Account access",
        source="account-access.md",
        excerpt="Use the password reset flow first.",
        score=0.91,
    )


def test_auto_respond_sends_when_confident(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_kb(monkeypatch, [_kb_match()])
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/auto-respond",
        headers=admin_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "auto_sent"
    assert body["comment_id"]
    assert body["draft"]["provider"] == "mock"

    ticket = client.get(f"/tickets/{created['id']}", headers=admin_headers()).json()
    assert ticket["status"] == "resolved"
    assert ticket["requires_human_review"] is False
    assert ticket["first_response_at"] is not None
    assert ticket["resolved_at"] is not None

    comments = client.get(f"/tickets/{created['id']}/comments", headers=admin_headers()).json()
    assert len(comments) == 1
    assert comments[0]["author_id"] == "ai-assistant"
    assert comments[0]["is_internal"] is False
    assert comments[0]["body"] == body["draft"]["draft"]

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "response.auto_sent"},
    ).json()
    assert len(logs) == 1


def test_auto_respond_escalates_when_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_kb(monkeypatch, [])
    created = _create_ticket("This is a completely ungrounded gibberish request zzqzq")

    response = client.post(
        f"/tickets/{created['id']}/auto-respond",
        headers=admin_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "needs_review"
    assert "no_knowledge_matches" in body["draft"]["reasons"]

    ticket = client.get(f"/tickets/{created['id']}", headers=admin_headers()).json()
    assert ticket["status"] == "pending"
    assert ticket["requires_human_review"] is True
    assert ticket["escalation_status"] == "pending"

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "response.auto_escalated"},
    ).json()
    assert len(logs) == 1


def test_auto_respond_skips_tickets_already_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_kb(monkeypatch, [_kb_match()])
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={
            "customer_id": "customer-1",
            "message": "I was charged twice for my subscription",
            "channel": "web",
        },
    ).json()
    assert created["requires_human_review"] is True

    response = client.post(
        f"/tickets/{created['id']}/auto-respond",
        headers=admin_headers(),
    )

    body = response.json()
    assert response.status_code == 200
    assert body["action"] == "skipped"
    assert body["reason"] == "already_flagged_for_review"
    assert body["draft"] is None
    assert "response.auto_escalated" not in [
        log["action"] for log in client.get("/admin/audit-logs", headers=admin_headers()).json()
    ]


def test_auto_respond_requires_staff() -> None:
    _, headers = make_customer("auto-respond-customer@example.com")
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/auto-respond",
        headers=headers,
    )

    assert response.status_code == 403


def test_auto_respond_skips_when_llm_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_kb(monkeypatch, [_kb_match()])
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/auto-respond",
        headers=admin_headers(),
    )

    body = response.json()
    assert response.status_code == 200
    assert body["action"] == "skipped"
    assert body["reason"] == "llm_not_configured"

    ticket = client.get(f"/tickets/{created['id']}", headers=admin_headers()).json()
    assert ticket["status"] == "open"


def test_intake_auto_responds_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_kb(monkeypatch, [_kb_match()])
    monkeypatch.setenv("AUTO_RESPOND_ENABLED", "true")

    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={
            "customer_id": "customer-auto",
            "message": "My application crashes when I try to export a report",
            "channel": "web",
        },
    )

    assert created.status_code == 201
    ticket = created.json()
    assert ticket["status"] == "resolved"

    comments = client.get(f"/tickets/{ticket['id']}/comments", headers=admin_headers()).json()
    assert len(comments) == 1
    assert comments[0]["author_id"] == "ai-assistant"

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "response.auto_sent"},
    ).json()
    assert len(logs) == 1


def test_intake_does_not_auto_respond_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_kb(monkeypatch, [_kb_match()])
    monkeypatch.setenv("AUTO_RESPOND_ENABLED", "false")

    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={
            "customer_id": "customer-off",
            "message": "My application crashes when I try to export a report",
            "channel": "web",
        },
    )

    assert created.status_code == 201
    ticket = created.json()
    assert ticket["status"] == "open"

    comments = client.get(f"/tickets/{ticket['id']}/comments", headers=admin_headers()).json()
    assert comments == []


def test_requests_carry_request_id_and_echo_incoming() -> None:
    response = client.get("/missing-endpoint-xyz")

    assert response.status_code == 404
    assert response.headers.get("X-Request-Id")

    echoed = client.get("/missing-endpoint-xyz", headers={"X-Request-Id": "custom-id-1"})
    assert echoed.headers.get("X-Request-Id") == "custom-id-1"


def test_purge_audit_logs_respects_retention_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-1", "message": "The dashboard shows an error"},
    )
    before = client.get("/admin/audit-logs", headers=admin_headers()).json()
    assert len(before) > 0

    import app.api.routers.admin as admin_module

    monkeypatch.setattr(admin_module, "AUDIT_LOG_RETENTION_DAYS", 0)
    response = client.post("/admin/audit-logs/purge", headers=admin_headers())

    assert response.status_code == 200
    assert response.json()["deleted"] == len(before)
    remaining = client.get("/admin/audit-logs", headers=admin_headers()).json()
    assert remaining == []


def test_purge_audit_logs_requires_admin() -> None:
    _, headers = make_customer("purge-forbidden@example.com")
    response = client.post("/admin/audit-logs/purge", headers=headers)

    assert response.status_code == 403


def test_draft_approve_sends_reply_and_resolves() -> None:
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/draft-decision",
        headers=admin_headers(),
        json={"decision": "approve", "body": "Try reinstalling the export module.", "resolve": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "approve"
    assert body["resolved"] is True
    assert body["comment_id"]

    ticket = client.get(f"/tickets/{created['id']}", headers=admin_headers()).json()
    assert ticket["status"] == "resolved"
    assert ticket["first_response_at"] is not None
    assert ticket["resolved_at"] is not None
    assert ticket["requires_human_review"] is False

    comments = client.get(f"/tickets/{created['id']}/comments", headers=admin_headers()).json()
    assert len(comments) == 1
    assert comments[0]["author_id"] == "00000000-0000-0000-0000-000000000001"
    assert comments[0]["is_internal"] is False
    assert comments[0]["body"] == "Try reinstalling the export module."

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "response.draft_approved"},
    ).json()
    assert len(logs) == 1
    assert logs[0]["details"]["comment_id"] == body["comment_id"]


def test_draft_approve_leaves_ticket_open_when_not_resolving() -> None:
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/draft-decision",
        headers=admin_headers(),
        json={"decision": "approve", "body": "Here is a workaround.", "resolve": False},
    )

    assert response.status_code == 200
    assert response.json()["resolved"] is False

    ticket = client.get(f"/tickets/{created['id']}", headers=admin_headers()).json()
    assert ticket["status"] == "open"
    assert ticket["resolved_at"] is None
    assert ticket["first_response_at"] is not None


def test_draft_reject_adds_internal_note_and_keeps_ticket() -> None:
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/draft-decision",
        headers=admin_headers(),
        json={"decision": "reject", "note": "Draft cites an outdated article"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "reject"
    assert body["resolved"] is False
    assert body["note_id"]

    ticket = client.get(f"/tickets/{created['id']}", headers=admin_headers()).json()
    assert ticket["status"] == "open"
    assert ticket["resolved_at"] is None

    comments = client.get(f"/tickets/{created['id']}/comments", headers=admin_headers()).json()
    assert len(comments) == 1
    assert comments[0]["is_internal"] is True
    assert comments[0]["body"] == "AI draft rejected: Draft cites an outdated article"

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "response.draft_rejected"},
    ).json()
    assert len(logs) == 1
    assert logs[0]["details"]["note_id"] == body["note_id"]


def test_draft_reject_without_note_audits_only() -> None:
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/draft-decision",
        headers=admin_headers(),
        json={"decision": "reject"},
    )

    assert response.status_code == 200
    assert response.json()["note_id"] is None

    comments = client.get(f"/tickets/{created['id']}/comments", headers=admin_headers()).json()
    assert comments == []

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "response.draft_rejected"},
    ).json()
    assert len(logs) == 1


def test_draft_approve_requires_body() -> None:
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/draft-decision",
        headers=admin_headers(),
        json={"decision": "approve"},
    )

    assert response.status_code == 422


def test_draft_decision_requires_staff() -> None:
    _, headers = make_customer("draft-decision-customer@example.com")
    created = _create_ticket("My application crashes when I try to export a report")

    response = client.post(
        f"/tickets/{created['id']}/draft-decision",
        headers=headers,
        json={"decision": "reject"},
    )

    assert response.status_code == 403


def test_draft_decision_unknown_ticket() -> None:
    response = client.post(
        "/tickets/00000000-0000-0000-0000-000000000099/draft-decision",
        headers=admin_headers(),
        json={"decision": "reject"},
    )

    assert response.status_code == 404


def test_ticket_creation_records_risk_and_route() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-rd", "message": "I was charged twice for my subscription"},
    )

    assert created.status_code == 201
    ticket = created.json()
    assert ticket["risk_score"] > 0
    assert ticket["risk_level"] in {"medium", "high", "critical"}
    assert ticket["escalation_route"] == "billing"
    assert ticket["escalation_summary"]
    assert ticket["intake_metadata"]["customer_context"]["tier"] == "standard"

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "escalation.auto_routed"},
    ).json()
    assert len(logs) == 1
    assert logs[0]["details"]["route"] == "billing"


def test_critical_risk_routes_to_leadership() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={
            "customer_id": "customer-crit",
            "message": "This is unacceptable, my account was stolen and charges are unauthorized",
        },
    )

    assert created.status_code == 201
    ticket = created.json()
    assert ticket["risk_level"] == "critical"
    assert ticket["risk_score"] >= 60
    assert ticket["escalation_route"] == "leadership"
    assert ticket["escalation_summary"]
    assert ticket["escalation_status"] == "pending"


def test_low_risk_ticket_has_no_summary_or_route_audit() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-low", "message": "What are your opening hours?"},
    )

    assert created.status_code == 201
    ticket = created.json()
    assert ticket["risk_level"] == "low"
    assert ticket["risk_score"] == 0
    assert ticket["escalation_route"] == "customer_support"
    assert ticket["escalation_summary"] is None
    assert ticket["requires_human_review"] is False

    logs = client.get(
        "/admin/audit-logs",
        headers=admin_headers(),
        params={"action": "escalation.auto_routed"},
    ).json()
    assert logs == []


def test_auto_respond_escalation_recomputes_risk() -> None:
    monkeypatch = pytest.MonkeyPatch()
    _fixed_kb(monkeypatch, [])
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-ar", "message": "This is completely ungrounded gibberish zzqzq"},
    ).json()
    assert created["requires_human_review"] is False

    response = client.post(
        f"/tickets/{created['id']}/auto-respond",
        headers=admin_headers(),
    )

    assert response.status_code == 200
    assert response.json()["action"] == "needs_review"

    ticket = client.get(f"/tickets/{created['id']}", headers=admin_headers()).json()
    assert ticket["risk_level"] in {"low", "medium"}
    assert ticket["escalation_summary"]


def test_agent_assist_returns_summary_kb_and_similar_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(
        "app.agents.agent_assist.search_knowledge",
        lambda message, limit=5: [
            KnowledgeMatch(
                document_id="password-reset",
                title="Password reset",
                source="password-reset.md",
                excerpt="Use the reset flow.",
                score=0.8,
            )
        ],
    )
    previous = _create_ticket("I cannot log in to my account")
    client.patch(
        f"/tickets/{previous['id']}",
        headers=admin_headers(),
        json={"status": "resolved"},
    )
    created = _create_ticket("I cannot log in to my account anymore")

    response = client.get(
        f"/tickets/{created['id']}/agent-assist",
        headers=admin_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ticket_id"] == created["id"]
    assert body["summary"]
    assert body["template_version"] == 1
    assert [match["document_id"] for match in body["knowledge"]] == ["password-reset"]
    assert [case["ticket_id"] for case in body["similar_cases"]] == [previous["id"]]
    assert body["similar_cases"][0]["status"] == "resolved"


def test_agent_assist_limits_are_respected() -> None:
    created = _create_ticket("I cannot log in to my account")
    for _ in range(2):
        resolved = _create_ticket("I cannot log in to my account")
        client.patch(
            f"/tickets/{resolved['id']}",
            headers=admin_headers(),
            json={"status": "resolved"},
        )

    response = client.get(
        f"/tickets/{created['id']}/agent-assist",
        headers=admin_headers(),
        params={"similar_limit": 1, "kb_limit": 0},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["similar_cases"]) == 1
    assert body["knowledge"] == []


def test_agent_assist_requires_staff() -> None:
    _, headers = make_customer("assist-customer@example.com")
    created = _create_ticket("I cannot log in to my account")

    response = client.get(
        f"/tickets/{created['id']}/agent-assist",
        headers=headers,
    )

    assert response.status_code == 403


def test_agent_assist_unknown_ticket() -> None:
    response = client.get(
        "/tickets/00000000-0000-0000-0000-000000000099/agent-assist",
        headers=admin_headers(),
    )

    assert response.status_code == 404


def test_agent_assist_works_without_llm_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    created = _create_ticket("I cannot log in to my account")

    response = client.get(
        f"/tickets/{created['id']}/agent-assist",
        headers=admin_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]
    assert body["provider"] == ""
    assert body["suggested_replies"] == []


def test_ticket_attachments_upload_list_and_download() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-8", "message": "Here is the file"},
    ).json()
    upload = client.post(
        f"/tickets/{created['id']}/attachments",
        headers=admin_headers(),
        files=[("files", ("receipt.txt", b"hello attachment", "text/plain"))],
    )

    assert upload.status_code == 201
    uploaded = upload.json()
    assert uploaded[0]["filename"] == "receipt.txt"
    assert uploaded[0]["size"] == len(b"hello attachment")

    listed = client.get(f"/tickets/{created['id']}/attachments", headers=admin_headers())
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    content = client.get(
        f"/tickets/{created['id']}/attachments/{uploaded[0]['id']}/content",
        headers=admin_headers(),
)
    assert content.status_code == 200
    assert content.content == b"hello attachment"
    assert content.headers["content-type"].startswith("text/plain")


def test_customer_cannot_upload_to_someone_elses_ticket() -> None:
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-9", "message": "private"},
    ).json()
    _, headers = make_customer("other-att@example.com")

    upload = client.post(
        f"/tickets/{created['id']}/attachments",
        headers=headers,
        files=[("files", ("leak.txt", b"nope", "text/plain"))],
    )

    assert upload.status_code == 403


def test_attachment_upload_enforces_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_ATTACHMENT_BYTES", "8")
    created = client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "customer-10", "message": "big file"},
    ).json()

    upload = client.post(
        f"/tickets/{created['id']}/attachments",
        headers=admin_headers(),
        files=[("files", ("big.bin", b"x" * 64, "application/octet-stream"))],
    )

    assert upload.status_code == 413


def test_ticket_list_is_paginated_and_reports_totals() -> None:
    for index in range(5):
        client.post(
            "/tickets",
            headers=admin_headers(),
            json={"customer_id": f"pager-{index}", "message": f"Pagination probe {index}"},
        )

    first_page = client.get("/tickets?limit=2", headers=admin_headers())
    assert first_page.status_code == 200
    assert len(first_page.json()) == 2
    total = int(first_page.headers["X-Total-Count"])
    assert total >= 5
    assert first_page.headers["X-Has-More"] == "true"

    last_page = client.get(f"/tickets?limit=2&offset={total - 1}", headers=admin_headers())
    assert len(last_page.json()) == 1
    assert last_page.headers["X-Has-More"] == "false"

    ids_first = {ticket["id"] for ticket in first_page.json()}
    ids_last = {ticket["id"] for ticket in last_page.json()}
    assert not ids_first & ids_last


def test_ticket_list_search_matches_older_rows_beyond_the_first_page() -> None:
    marker = "needle-zebra-9f2"
    client.post(
        "/tickets",
        headers=admin_headers(),
        json={"customer_id": "search-owner", "message": f"older {marker} ticket"},
    )
    for index in range(3):
        client.post(
            "/tickets",
            headers=admin_headers(),
            json={"customer_id": f"filler-{index}", "message": f"unrelated filler {index}"},
        )

    response = client.get(f"/tickets?limit=1&q={marker}", headers=admin_headers())

    assert response.status_code == 200
    matches = response.json()
    assert len(matches) == 1
    assert marker in matches[0]["message"]
    assert int(response.headers["X-Total-Count"]) == 1
