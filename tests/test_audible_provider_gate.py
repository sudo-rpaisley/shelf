"""Default-off containment: no live network, provider approval or coverage claim."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from app import auth, config
from app.services import audible_catalog, audible_permission, audiobook_review, manual_cover, outbound, provider_result
from tests.audible_authorization import mock_audible_authorization  # noqa: F401 — pytest fixture
from tests.test_audible_catalog import ASIN, IMAGE, PNG, PRODUCT
from tests.test_audiobook_review import candidate, seed, ticket, _all_rows


@pytest.fixture(autouse=True)
def default_off(_isolated_db, monkeypatch):
    monkeypatch.delenv(audible_permission.ENABLE_ENV, raising=False)
    monkeypatch.delenv(audible_permission.GRANT_ENV, raising=False)


@pytest.fixture(autouse=True)
def mounted_component(monkeypatch):
    # Component-level proof only; parent owns actual main/feature/UI wiring.
    from app.main import app
    from app.routers.audiobook_metadata import router
    if not any(getattr(route, "path", "") == "/api/audiobooks/{item_id}/metadata" for route in app.routes):
        monkeypatch.setattr(app.router, "routes", [*app.routes])
        app.include_router(router)


@pytest.fixture
def no_egress(monkeypatch):
    dns = AsyncMock(side_effect=AssertionError("Blocked provider attempted DNS"))
    pace = AsyncMock(side_effect=AssertionError("Blocked provider reached outbound pacing"))
    http = Mock(side_effect=AssertionError("Blocked provider constructed an HTTP client"))
    monkeypatch.setattr(manual_cover, "_public_target", dns)
    monkeypatch.setattr(outbound, "acquire", pace)
    monkeypatch.setattr(audible_catalog.httpx, "AsyncClient", http)
    return dns, pace, http


@pytest.mark.parametrize("configuration", ["unset", "enable_only", "reference_only", "disabled"])
@pytest.mark.parametrize("operation", ["lookup", "artwork", "raw_http", "staging"])
@pytest.mark.asyncio
async def test_default_off_service_entries_never_reach_network(no_egress, monkeypatch, operation, configuration):
    if configuration in {"enable_only", "disabled"}:
        monkeypatch.setenv(audible_permission.ENABLE_ENV, "1" if configuration == "enable_only" else "0")
    if configuration in {"reference_only", "disabled"}:
        monkeypatch.setenv(audible_permission.GRANT_ENV, "TEST-ONLY-MOCK-NOT-A-PROVIDER-GRANT")
    with pytest.raises(audible_permission.ProviderBlocked):
        if operation == "lookup":
            await audible_catalog.lookup(ASIN, "us")
        elif operation == "artwork":
            await audible_catalog.download_artwork(1, IMAGE)
        elif operation == "raw_http":
            await audible_catalog._read_public("https://api.audible.com/1.0/catalog/products")
        else:
            audible_catalog._stage_artwork(1, PNG)
    for call in no_egress:
        call.assert_not_called()
    assert list(config.COVERS_DIR.iterdir()) == []


def test_default_off_panel_is_honestly_blocked_but_local_asin_edit_is_available(editor_client, db, no_egress):
    item_id = seed(db)
    before = _all_rows(db)
    response = editor_client.get(f"/api/audiobooks/{item_id}/metadata")
    assert response.status_code == 200 and "provider access is blocked" in response.text
    assert "Operator opt-in is not a licence" in response.text and "ASIN" in response.text
    assert f'href="/item/{item_id}/edit"' in response.text
    assert 'name="marketplace"' not in response.text and 'name="ticket"' not in response.text
    assert 'metadata/image?ticket=' not in response.text and "No permitted artwork" not in response.text
    assert _all_rows(db) == before
    for call in no_egress:
        call.assert_not_called()


def test_default_off_preview_refuses_before_lookup_or_signing(editor_client, db, monkeypatch, no_egress):
    item_id = seed(db)
    before = _all_rows(db)
    lookup = AsyncMock(side_effect=AssertionError("Blocked request reached catalogue lookup"))
    sign = Mock(side_effect=AssertionError("Blocked request signed a candidate"))
    monkeypatch.setattr(audible_catalog, "lookup", lookup)
    monkeypatch.setattr(audiobook_review, "sign", sign)
    response = editor_client.post(f"/api/audiobooks/{item_id}/metadata/preview", data={"marketplace": "us"})
    assert response.status_code == 403 and "provider access is blocked" in response.text
    assert 'name="ticket"' not in response.text and _all_rows(db) == before
    lookup.assert_not_called()
    sign.assert_not_called()


@pytest.mark.parametrize("operation", ["image", "metadata_apply", "artwork_apply"])
@pytest.mark.parametrize("missing", ["enable", "grant_reference"])
def test_cached_tickets_cannot_bypass_disabled_or_unconfigured_gate(editor_client, editor_user, db, monkeypatch, no_egress,
                                                                 mock_audible_authorization, operation, missing):
    item_id = seed(db)
    signed = ticket(db, editor_user, item_id, image=operation == "image")
    before = _all_rows(db)
    monkeypatch.delenv(audible_permission.ENABLE_ENV if missing == "enable" else audible_permission.GRANT_ENV)
    download = AsyncMock(side_effect=AssertionError("Cached ticket attempted a provider download"))
    monkeypatch.setattr(audible_catalog, "download_artwork", download)
    if operation == "image":
        response = editor_client.get(f"/api/audiobooks/{item_id}/metadata/image", params={"ticket": signed})
    else:
        response = editor_client.post(f"/api/audiobooks/{item_id}/metadata/apply", data={
            "ticket": signed, "fields": "publisher", "image": "0" if operation == "artwork_apply" else "-1",
        }, follow_redirects=False)
    assert response.status_code == 403 and "provider access is blocked" in response.text
    assert response.content != PNG and "<form" not in response.text
    assert _all_rows(db) == before and list(config.COVERS_DIR.iterdir()) == []
    download.assert_not_called()
    for call in no_egress:
        call.assert_not_called()


@pytest.mark.parametrize("value", ["", "0", "true", "on", " 1", "1\n"])
def test_enable_requires_exact_opt_in_value(monkeypatch, value):
    monkeypatch.setenv(audible_permission.ENABLE_ENV, value)
    monkeypatch.setenv(audible_permission.GRANT_ENV, "TEST-ONLY-MOCK-NOT-A-PROVIDER-GRANT")
    assert not audible_permission.enabled()


@pytest.mark.parametrize("reference", ["", "  ", "x" * 513, " " * 512 + "x", "audit\nreference", "audit\treference"])
def test_missing_or_malformed_audit_reference_keeps_gate_closed(monkeypatch, reference):
    monkeypatch.setenv(audible_permission.ENABLE_ENV, "1")
    monkeypatch.setenv(audible_permission.GRANT_ENV, reference)
    assert not audible_permission.enabled()


@pytest.mark.parametrize("boundary", ["dns", "pacing"])
@pytest.mark.asyncio
async def test_gate_is_rechecked_after_egress_waits_before_http(monkeypatch, mock_audible_authorization, boundary):
    async def dns(url):
        if boundary == "dns":
            monkeypatch.delenv(audible_permission.ENABLE_ENV)
        return "api.audible.com", "8.8.8.8"

    async def pace(host):
        monkeypatch.delenv(audible_permission.ENABLE_ENV)

    http = Mock(side_effect=AssertionError("Revoked provider gate opened HTTP"))
    paced = AsyncMock(side_effect=pace)
    monkeypatch.setattr(manual_cover, "_public_target", dns)
    monkeypatch.setattr(outbound, "acquire", paced)
    monkeypatch.setattr(audible_catalog.httpx, "AsyncClient", http)
    with pytest.raises(audible_permission.ProviderBlocked):
        await audible_catalog.lookup(ASIN, "us")
    http.assert_not_called()
    assert paced.await_count == (1 if boundary == "pacing" else 0)


@pytest.mark.parametrize("operation", ["lookup", "artwork"])
@pytest.mark.asyncio
async def test_gate_revocation_rejects_service_result_before_return_or_storage(monkeypatch, mock_audible_authorization, operation):
    async def read(*args, **kwargs):
        monkeypatch.delenv(audible_permission.GRANT_ENV)
        return 200, None, PNG if operation == "artwork" else json.dumps({"products": [PRODUCT]}).encode()

    monkeypatch.setattr(audible_catalog, "_read_public", read)
    with pytest.raises(audible_permission.ProviderBlocked):
        if operation == "lookup":
            await audible_catalog.lookup(ASIN, "us")
        else:
            await audible_catalog.download_artwork(1, IMAGE)
    assert list(config.COVERS_DIR.iterdir()) == []


@pytest.mark.parametrize("operation", ["preview", "image", "apply"])
def test_gate_revocation_during_route_await_refuses_cached_result_and_cleans_staging(editor_client, editor_user, db, monkeypatch,
                                                                                  mock_audible_authorization, operation):
    item_id = seed(db)
    signed = ticket(db, editor_user, item_id, image=operation == "image")
    before = _all_rows(db)

    async def lookup(*args):
        monkeypatch.delenv(audible_permission.ENABLE_ENV)
        return provider_result.found("audible", candidate())

    async def download(identifier, url):
        monkeypatch.delenv(audible_permission.ENABLE_ENV)
        name = f"{identifier}-revoked-provider.jpg"
        (config.COVERS_DIR / name).write_bytes(PNG)
        return "covers/" + name

    monkeypatch.setattr(audible_catalog, "lookup", lookup)
    monkeypatch.setattr(audible_catalog, "download_artwork", download)
    if operation == "preview":
        response = editor_client.post(f"/api/audiobooks/{item_id}/metadata/preview", data={"marketplace": "us"})
    elif operation == "image":
        response = editor_client.get(f"/api/audiobooks/{item_id}/metadata/image", params={"ticket": signed})
    else:
        response = editor_client.post(f"/api/audiobooks/{item_id}/metadata/apply", data={"ticket": signed, "fields": "title", "image": "0"}, follow_redirects=False)
    assert response.status_code == 403 and "provider access is blocked" in response.text
    assert response.content != PNG and 'name="ticket"' not in response.text and "<form" not in response.text
    assert _all_rows(db) == before and list(config.COVERS_DIR.iterdir()) == []


def test_direct_apply_cannot_persist_cached_observation_after_gate_closes(editor_client, editor_user, db, monkeypatch, mock_audible_authorization):
    from app.database import get_db
    item_id = seed(db)
    signed = audiobook_review.verify(ticket(db, editor_user, item_id), user_id=editor_user["id"], item_id=item_id)
    actor = {**editor_user, "token_version": 1, "expires_at": auth.decode_token(editor_client.cookies.get("access_token"))["exp"]}
    before = _all_rows(db)
    monkeypatch.delenv(audible_permission.ENABLE_ENV)
    with pytest.raises(audible_permission.ProviderBlocked):
        with get_db() as other:
            other.execute("BEGIN IMMEDIATE")
            audiobook_review.apply(other, item_id, signed, mode="selected", chosen={"publisher"}, path=None, actor=actor)
    assert _all_rows(db) == before


def test_mock_configuration_is_not_echoed_or_presented_as_a_licence(editor_client, db, no_egress, mock_audible_authorization):
    item_id = seed(db)
    response = editor_client.get(f"/api/audiobooks/{item_id}/metadata")
    assert response.status_code == 200 and 'name="marketplace"' in response.text
    assert "TEST-ONLY-MOCK-NOT-A-PROVIDER-GRANT" not in response.text
    assert "it is not a licence" in response.text and "not a regional coverage promise" in response.text
    for call in no_egress:
        call.assert_not_called()
