"""Shared test fixtures.

The login and registration throttles keep their attempt windows in a
process-local dict that is never cleared on success (registration is an
unauthenticated write path, so successes must count toward the budget). Across a
full test run every request shares one client host, so without a reset the
registration budget of 20 would be spent partway through the suite and later
tests would see unrelated 429s. Reset the windows before each test so throttle
behaviour is verified per-test rather than accumulating.
"""

import pytest

from app.security.login_throttle import reset_login_throttle


@pytest.fixture(autouse=True)
def _reset_process_local_throttles():
    reset_login_throttle()
    yield
    reset_login_throttle()
