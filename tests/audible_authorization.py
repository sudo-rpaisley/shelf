"""Disposable configuration for MOCK technical tests, never a real grant.

Only tests that stub/deny network operations should request this fixture. Its
reference is deliberately fictional and supplies no permission for live calls.
"""

import pytest

from app.services import audible_permission, manual_cover


@pytest.fixture
def mock_audible_authorization(monkeypatch):
    monkeypatch.setenv(audible_permission.ENABLE_ENV, "1")
    monkeypatch.setenv(audible_permission.GRANT_ENV, "TEST-ONLY-MOCK-NOT-A-PROVIDER-GRANT")

    async def no_live_dns(*args, **kwargs):
        raise AssertionError("Mock authorisation does not permit live DNS/network access")

    def no_live_transport(*args, **kwargs):
        raise AssertionError("Mock authorisation requires an explicit mock HTTP transport")

    monkeypatch.setattr(manual_cover, "_resolve_addresses", no_live_dns)
    monkeypatch.setattr(manual_cover, "_PinnedIPTransport", no_live_transport)
