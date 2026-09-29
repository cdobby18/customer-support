"""Tests for the trusted-host middleware.

There were no tests here, which is why a CI job could set `TRUSTED_HOSTS` in an
environment, silently drop `testserver` from the default, and turn all 124 API
tests into `400 Invalid host header` without anyone connecting the two.

The middleware is the only thing standing between a forged `Host` header and
host-header injection, so it earns a test of its own rather than being covered
incidentally.
"""

import pytest

# Imported as `harness`, not `tests.harness`, like every other test module. The
# two names load the file as two different modules, each building its own
# in-memory engine, and the second one to be imported wins the process-global
# `app.dependency_overrides[get_db]` - leaving every test pointing at an empty
# database. That is the exact failure the harness docstring describes.
from app.main import DEFAULT_TRUSTED_HOSTS, parse_trusted_hosts, trusted_hosts
from harness import client


def test_default_allows_the_test_client() -> None:
    """The test suite is unusable without this, which is why it is in the default."""
    assert "testserver" in DEFAULT_TRUSTED_HOSTS.split(",")


def test_default_covers_local_development() -> None:
    for host in ("localhost", "127.0.0.1", "testserver"):
        assert host in DEFAULT_TRUSTED_HOSTS.split(",")


def test_unset_environment_falls_back_to_the_default() -> None:
    assert parse_trusted_hosts(None) == DEFAULT_TRUSTED_HOSTS.split(",")


def test_hosts_are_stripped_and_empty_entries_dropped() -> None:
    assert parse_trusted_hosts(" a.example.com , , b.example.com ,") == [
        "a.example.com",
        "b.example.com",
    ]


def test_overriding_the_default_replaces_it_rather_than_extending_it() -> None:
    """The behaviour that caused 124 failures: `testserver` is not kept.

    This is not a bug in the parser, it is a sharp edge worth pinning, because
    an environment that sets TRUSTED_HOSTS expecting to *add* a host silently
    drops the test client instead.
    """
    assert "testserver" not in parse_trusted_hosts("support.example.com")


def test_spoofed_host_header_is_rejected() -> None:
    """A request claiming to be from an unknown host must not be served."""
    if "attacker.example.com" in trusted_hosts:
        pytest.skip("TRUSTED_HOSTS in this environment explicitly allows that host")

    response = client.get("/health", headers={"Host": "attacker.example.com"})

    assert response.status_code == 400
    assert "Invalid host header" in response.text


def test_known_host_is_accepted() -> None:
    """The counterpart, so the test above cannot pass by rejecting everything."""
    response = client.get("/health", headers={"Host": "testserver"})

    assert response.status_code == 200
