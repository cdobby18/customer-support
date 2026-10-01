"""Tests for attachment upload validation and deletion.

Two separate problems share this file.

Validation (#16): the upload endpoint stored whatever the client called the
file. `content_type` was stored verbatim and replayed on download, so a customer
could upload HTML announced as text/plain and have it served back to the next
person who opened the ticket. There was no allowlist and no check that the bytes
matched the claim.

Deletion (#15): deleting a ticket removed its comments and the ticket row but
left the attachment rows and the files on disk. Those files are customer PII,
and nothing else in the codebase ever reached them again, so they accumulated
forever.

The negative tests (#40) matter most here: they are the paths that must NOT
succeed, and an endpoint that only has happy-path coverage is exactly how the
original gap survived.
"""

import os
from pathlib import Path

import pytest

from harness import ADMIN_ID, client, drop_schema, reset_schema, seed_admin

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Give each test its own upload directory and database.

    Several assertions here are about what is or is not left on disk, so the
    directory has to be isolated from the developer's real .uploads and from
    whatever the previous test wrote.
    """
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    reset_schema()
    seed_admin()
    yield
    drop_schema()


@pytest.fixture
def admin() -> dict[str, str]:
    from app.security.auth import create_access_token

    return {"Authorization": f"Bearer {create_access_token(ADMIN_ID)}"}


@pytest.fixture
def register_customer():
    """Create a customer account and return an Authorization header for them."""

    def _register(email: str) -> dict[str, str]:
        created = client.post(
            "/auth/register", json={"email": email, "password": PASSWORD}
        )
        assert created.status_code == 201, created.text
        login = client.post(
            "/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
        return {"Authorization": f"Bearer {login.json()['access_token']}"}

    return _register


def make_ticket(headers: dict[str, str], customer_id: str = "customer-attach") -> dict:
    response = client.post(
        "/tickets",
        headers=headers,
        json={"customer_id": customer_id, "message": "Attaching a file"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload(
    headers: dict[str, str],
    ticket_id: str,
    filename: str,
    payload: bytes,
    content_type: str,
):
    return client.post(
        f"/tickets/{ticket_id}/attachments",
        headers=headers,
        files=[("files", (filename, payload, content_type))],
    )


def upload_dir() -> Path:
    return Path(os.getenv("UPLOAD_DIR", ".uploads"))


# --- allowed content -------------------------------------------------------


def test_a_plain_text_attachment_is_accepted(admin) -> None:
    ticket = make_ticket(admin)

    response = upload(admin, ticket["id"], "notes.txt", b"just some notes", "text/plain")

    assert response.status_code == 201, response.text
    assert response.json()[0]["content_type"] == "text/plain"


@pytest.mark.parametrize(
    ("filename", "payload", "declared"),
    [
        ("receipt.pdf", PDF, "application/pdf"),
        ("shot.png", PNG, "image/png"),
        ("photo.jpg", JPEG, "image/jpeg"),
        ("data.csv", b"a,b\n1,2\n", "text/csv"),
        ("body.json", b'{"ok": true}', "application/json"),
        ("readme.md", b"# Title", "text/markdown"),
    ],
)
def test_allowed_types_are_accepted(
    admin, filename: str, payload: bytes, declared: str
) -> None:
    ticket = make_ticket(admin)

    response = upload(admin, ticket["id"], filename, payload, declared)

    assert response.status_code == 201, response.text


# --- rejected content -----------------------------------------------------


def test_a_disallowed_content_type_is_refused(admin) -> None:
    ticket = make_ticket(admin)

    response = upload(
        admin, ticket["id"], "app.exe", b"MZ\x90\x00", "application/x-msdownload"
    )

    assert response.status_code == 415


def test_html_announced_as_text_plain_is_refused(admin) -> None:
    """The attack the stored content_type enabled: serve HTML to the next reader."""
    ticket = make_ticket(admin)

    response = upload(
        admin,
        ticket["id"],
        "note.txt",
        b"<html><script>fetch('/admin')</script></html>",
        "text/plain",
    )

    assert response.status_code == 415


@pytest.mark.parametrize(
    "payload",
    [
        b"<script>alert(1)</script>",
        b"<iframe src=evil></iframe>",
        b"<svg onload=alert(1)>",
        b"javascript:alert(1)",
        b"<?php system($_GET['c']); ?>",
    ],
)
def test_active_content_is_refused_whatever_the_declared_type(
    admin, payload: bytes
) -> None:
    ticket = make_ticket(admin)

    response = upload(admin, ticket["id"], "data.txt", payload, "text/plain")

    assert response.status_code == 415


def test_content_that_contradicts_its_declared_type_is_refused(admin) -> None:
    """A PDF renamed .png must not be stored as image/png."""
    ticket = make_ticket(admin)

    response = upload(admin, ticket["id"], "fake.png", PDF, "image/png")

    assert response.status_code == 415


def test_the_sniffed_type_is_stored_not_the_declared_one(admin) -> None:
    """Text-like types have no magic bytes, so the bytes win over the claim."""
    ticket = make_ticket(admin)

    response = upload(admin, ticket["id"], "actually-png.txt", PNG, "text/plain")

    assert response.status_code == 201, response.text
    assert response.json()[0]["content_type"] == "image/png"


def test_a_rejected_file_is_not_written_to_disk(admin) -> None:
    """Validation runs before any write, so a refusal leaves no orphaned bytes."""
    ticket = make_ticket(admin)
    upload_dir().mkdir(parents=True, exist_ok=True)
    before = set(upload_dir().glob("*"))

    response = upload(
        admin, ticket["id"], "app.exe", b"MZ\x90\x00", "application/x-msdownload"
    )

    assert response.status_code == 415
    assert set(upload_dir().glob("*")) == before
    assert client.get(f"/tickets/{ticket['id']}/attachments", headers=admin).json() == []


def test_one_bad_file_rejects_the_whole_batch(admin) -> None:
    """A partially-applied batch would leave files no row points at."""
    ticket = make_ticket(admin)

    response = client.post(
        f"/tickets/{ticket['id']}/attachments",
        headers=admin,
        files=[
            ("files", ("good.txt", b"fine", "text/plain")),
            ("files", ("bad.exe", b"MZ\x90\x00", "application/x-msdownload")),
        ],
    )

    assert response.status_code == 415
    assert client.get(f"/tickets/{ticket['id']}/attachments", headers=admin).json() == []


# --- download hardening ---------------------------------------------------


def test_download_sets_nosniff_and_sandbox(admin) -> None:
    ticket = make_ticket(admin)
    stored = upload(admin, ticket["id"], "notes.txt", b"hello", "text/plain").json()[0]

    response = client.get(
        f"/tickets/{ticket['id']}/attachments/{stored['id']}/content",
        headers=admin,
    )

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "sandbox"


# --- authorization (#40) --------------------------------------------------


def test_anonymous_upload_is_refused() -> None:
    response = client.post(
        "/tickets/00000000-0000-0000-0000-000000000001/attachments",
        files=[("files", ("x.txt", b"x", "text/plain"))],
    )

    assert response.status_code == 401


def test_anonymous_listing_is_refused() -> None:
    response = client.get("/tickets/00000000-0000-0000-0000-000000000001/attachments")

    assert response.status_code == 401


def test_anonymous_download_is_refused() -> None:
    response = client.get(
        "/tickets/00000000-0000-0000-0000-000000000001/attachments/"
        "00000000-0000-0000-0000-000000000002/content"
    )

    assert response.status_code == 401


def test_download_requires_authentication(admin) -> None:
    ticket = make_ticket(admin)
    stored = upload(admin, ticket["id"], "notes.txt", b"secret", "text/plain").json()[0]

    response = client.get(
        f"/tickets/{ticket['id']}/attachments/{stored['id']}/content"
    )

    assert response.status_code == 401


def test_another_customers_attachment_is_not_readable(admin, register_customer) -> None:
    ticket = make_ticket(admin)
    stored = upload(admin, ticket["id"], "notes.txt", b"private", "text/plain").json()[0]

    intruder = register_customer("intruder@example.com")
    response = client.get(
        f"/tickets/{ticket['id']}/attachments/{stored['id']}/content",
        headers=intruder,
    )

    assert response.status_code == 403


def test_another_customers_ticket_attachments_are_not_listable(
    admin, register_customer
) -> None:
    ticket = make_ticket(admin)

    nosy = register_customer("nosy@example.com")
    response = client.get(f"/tickets/{ticket['id']}/attachments", headers=nosy)

    assert response.status_code == 403


def test_customer_cannot_delete_someone_elses_ticket_to_reach_the_files(
    admin, register_customer
) -> None:
    ticket = make_ticket(admin)
    upload(admin, ticket["id"], "notes.txt", b"data", "text/plain")

    remover = register_customer("remover@example.com")
    response = client.delete(f"/tickets/{ticket['id']}", headers=remover)

    assert response.status_code == 403


def test_upload_to_an_unknown_ticket_is_a_404_not_a_500(admin) -> None:
    response = upload(
        admin,
        "00000000-0000-0000-0000-0000000000ff",
        "x.txt",
        b"x",
        "text/plain",
    )

    assert response.status_code == 404


# --- deletion removes the files too (#15) ---------------------------------


def test_deleting_a_ticket_removes_its_attachment_files(admin) -> None:
    ticket = make_ticket(admin)
    upload(admin, ticket["id"], "notes.txt", b"pii data", "text/plain")

    assert list(upload_dir().glob("*")), "expected the file to exist first"

    response = client.delete(f"/tickets/{ticket['id']}", headers=admin)

    assert response.status_code == 204
    assert list(upload_dir().glob("*")) == [], (
        "attachment bytes are customer PII and must not outlive the ticket"
    )


def test_deleting_a_ticket_removes_the_attachment_rows(admin) -> None:
    ticket = make_ticket(admin)
    upload(admin, ticket["id"], "notes.txt", b"data", "text/plain")

    client.delete(f"/tickets/{ticket['id']}", headers=admin)

    assert (
        client.get(f"/tickets/{ticket['id']}/attachments", headers=admin).status_code
        == 404
    )


def test_deleting_a_ticket_without_attachments_still_works(admin) -> None:
    ticket = make_ticket(admin)

    response = client.delete(f"/tickets/{ticket['id']}", headers=admin)

    assert response.status_code == 204


def test_the_delete_audit_records_the_attachment_count(admin) -> None:
    ticket = make_ticket(admin)
    for index in range(2):
        upload(admin, ticket["id"], f"f{index}.txt", b"x", "text/plain")

    client.delete(f"/tickets/{ticket['id']}", headers=admin)

    logs = client.get("/admin/audit-logs", headers=admin).json()
    deleted = [row for row in logs if row["action"] == "ticket.deleted"]
    assert deleted
    assert deleted[0]["details"]["attachments_removed"] == 2


def test_file_removal_refuses_to_escape_the_upload_directory(monkeypatch, tmp_path) -> None:
    """A corrupted storage_path must not become an arbitrary file delete."""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    outside = tmp_path / "important.txt"
    outside.write_bytes(b"must survive")

    from app.api.routers.tickets import _remove_attachment_files

    removed = _remove_attachment_files(["../important.txt", "/etc/passwd"])

    assert removed == 0
    assert outside.exists()


def test_file_removal_tolerates_an_already_missing_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))

    from app.api.routers.tickets import _remove_attachment_files

    assert _remove_attachment_files(["does-not-exist"]) == 0
