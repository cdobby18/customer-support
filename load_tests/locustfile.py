import os
import random
from uuid import uuid4

from locust import HttpUser, between, task

REGISTER_EMAIL_PREFIX = "load."
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

MESSAGES = [
    "I was charged twice for my subscription this month",
    "How do I reset my password?",
    "The dashboard shows an error when I open reports",
    "My order has not arrived yet, can you check the status?",
    "Can I get a refund for the unused months?",
    "The mobile app crashes when I try to upload a photo",
    "I would like to downgrade my plan to the free tier",
]


class CustomerUser(HttpUser):
    wait_time = between(1, 3)
    auth_header: dict[str, str] = {}
    user_id: str | None = None

    def on_start(self) -> None:
        email = f"{REGISTER_EMAIL_PREFIX}{uuid4().hex}@example.com"
        with self.client.post(
            "/auth/register",
            json={"email": email, "password": "loadtest-pass-1"},
            name="register",
            catch_response=True,
        ) as response:
            if response.status_code not in (201, 409):
                response.failure(f"register failed: {response.status_code}")
        response = self.client.post(
            "/auth/login",
            json={"email": email, "password": "loadtest-pass-1"},
            name="login",
        )
        if response.status_code == 200:
            body = response.json()
            self.auth_header = {
                "Authorization": f"Bearer {body['access_token']}"
            }
            self.user_id = body["user"]["id"]

    @task(4)
    def create_ticket(self) -> None:
        self.client.post(
            "/tickets",
            headers=self.auth_header,
            json={
                "customer_id": self.user_id or "customer-1",
                "message": random.choice(MESSAGES),
                "channel": "web",
            },
            name="create_ticket",
        )

    @task(3)
    def list_tickets(self) -> None:
        self.client.get("/tickets", headers=self.auth_header, name="list_tickets")

    @task(2)
    def knowledge_search(self) -> None:
        self.client.get(
            "/knowledge/search",
            params={"q": "charged twice refund"},
            headers=self.auth_header,
            name="knowledge_search",
        )

    @task(1)
    def auth_me(self) -> None:
        self.client.get("/auth/me", headers=self.auth_header, name="auth_me")


class AdminUser(HttpUser):
    wait_time = between(2, 5)
    auth_header: dict[str, str] = {}

    def on_start(self) -> None:
        if not ADMIN_EMAIL:
            return
        with self.client.post(
            "/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            name="admin_login",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                self.auth_header = {
                    "Authorization": f"Bearer {response.json()['access_token']}"
                }
            else:
                response.failure(f"admin login failed: {response.status_code}")

    @task(3)
    def analytics_dashboard(self) -> None:
        if not self.auth_header:
            return
        self.client.get(
            "/admin/analytics/dashboard",
            headers=self.auth_header,
            name="analytics_dashboard",
        )

    @task(2)
    def audit_logs(self) -> None:
        if not self.auth_header:
            return
        self.client.get(
            "/admin/audit-logs",
            headers=self.auth_header,
            params={"limit": 50},
            name="audit_logs",
        )

    @task(1)
    def sla_metrics(self) -> None:
        if not self.auth_header:
            return
        self.client.get(
            "/admin/analytics/sla", headers=self.auth_header, name="sla_metrics"
        )