"""Real item/provenance writes through signed, explicit ASIN review routes.

The orchestrator owns main/features integration. Until registered there, mount
only this router on a per-test copy of app.routes; all normal middleware stays.
"""

import html
import json
import re
import sqlite3
import time
from unittest.mock import AsyncMock

import jwt
import pytest
from starlette.requests import Request

from app import auth, config
from app.config import JWT_ALGORITHM
from app.services import audible_catalog, audiobook_review as review, cover_sync_policy, item_write, music_review, provider_result
from tests.conftest import _insert_item
from tests.test_audible_catalog import ASIN, IMAGE, PNG, PRODUCT


@pytest.fixture(autouse=True)
def mount_router(_isolated_db, monkeypatch):
    from app.main import app
    from app.routers.audiobook_metadata import router
    if not any(getattr(route, "path", "") == "/api/audiobooks/{item_id}/metadata" for route in app.routes):
        monkeypatch.setattr(app.router, "routes", [*app.routes])
        app.include_router(router)


def seed(db, **values):
    fields = {"title": "My edition", "authors": "My author", "narrator": "Imported narrator", "duration_mins": 600,
              "media_type": "audiobook", "isbn": "9780441172719", "asin": ASIN, "cover_path": "covers/chosen.jpg",
              "notes": "Keep my notes", "subtitle": "Imported subtitle", **values}
    item_id = _insert_item(db, **fields)
    db.commit()
    return item_id


def candidate(**values):
    return {**audible_catalog.normalise({"products": [PRODUCT]}, ASIN, "us"), **values}


def ticket(db, user, item_id, value=None, *, image=False):
    return review.sign(value if value is not None else candidate(), user_id=user["id"], item_id=item_id,
                       fingerprint=review.fingerprint(db, item_id), image=image)


def inputs(response, name):
    return [html.unescape(value) for value in re.findall(rf'name="{name}" value="([^"]*)"', response.text)]


def stored(db, item_id):
    return dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())


def apply(client, item_id, ticket_value, **choices):
    return client.post(f"/api/audiobooks/{item_id}/metadata/apply", data={"ticket": ticket_value, **choices}, follow_redirects=False)


def test_panel_never_requests_provider_and_requires_explicit_marketplace(editor_client, db, monkeypatch):
    item_id = seed(db)
    lookup = AsyncMock(side_effect=AssertionError("GET must never look up"))
    monkeypatch.setattr(audible_catalog, "lookup", lookup)
    response = editor_client.get(f"/api/audiobooks/{item_id}/metadata")
    assert response.status_code == 200 and 'name="marketplace" required' in response.text
    assert '<option value="">Choose a marketplace</option>' in response.text
    assert "Saved ASIN" in response.text and ASIN in response.text
    assert "<script>" not in response.text
    lookup.assert_not_awaited()


def test_preview_is_nonmutating_signed_and_same_origin(editor_client, editor_user, db, monkeypatch):
    item_id = seed(db)
    before = stored(db, item_id)
    monkeypatch.setattr(audible_catalog, "lookup", AsyncMock(return_value=provider_result.found("audible", candidate())))
    response = editor_client.post(f"/api/audiobooks/{item_id}/metadata/preview", data={"marketplace": "us"}, headers={"HX-Request": "true"})
    assert response.status_code == 200 and "Wil Wheaton" in response.text and "unabridged" in response.text and "659" in response.text
    assert stored(db, item_id) == before and not db.execute("SELECT * FROM item_metadata_provenance").fetchone()
    signed = review.verify(inputs(response, "ticket")[0], user_id=editor_user["id"], item_id=item_id)
    assert signed["candidate"]["asin"] == ASIN and signed["candidate"]["marketplace"] == "us"
    assert not re.search(r'name="fields"[^>]*checked', response.text)
    assert 'name="image" value="-1" checked' in response.text
    assert '<img src="https://' not in response.text and f'src="/api/audiobooks/{item_id}/metadata/image?ticket=' in response.text
    assert response.headers["cache-control"] == "no-store"


def test_selected_fields_do_not_change_narrator_duration_or_any_identity(editor_client, editor_user, db):
    item_id = seed(db)
    before = stored(db, item_id)
    response = apply(editor_client, item_id, ticket(db, editor_user, item_id), fields=["publisher", "publish_year"])
    assert response.status_code == 303 and response.headers["location"] == f"/item/{item_id}"
    after = stored(db, item_id)
    assert after["publisher"] == "Audible Studios" and after["publish_year"] == 2020
    for key in before.keys() - {"publisher", "publish_year", "updated_at"}:
        assert after[key] == before[key], key
    snapshot = json.loads(db.execute("SELECT value_json FROM item_metadata_provenance WHERE item_id = ? AND field_name = 'narrator'", (item_id,)).fetchone()[0])
    assert snapshot == {"observed": "Wil Wheaton", "local": "Imported narrator", "overridden": True}


def test_explicit_selection_can_replace_narrator_and_duration_but_not_asin(editor_client, editor_user, db):
    item_id = seed(db)
    response = apply(editor_client, item_id, ticket(db, editor_user, item_id), fields=["narrator", "duration_mins"])
    assert response.status_code == 303
    row = stored(db, item_id)
    assert row["narrator"] == "Wil Wheaton" and row["duration_mins"] == 659 and row["asin"] == ASIN


def test_backfill_keeps_nonempty_values_and_fills_only_missing_metadata(editor_client, editor_user, db):
    item_id = seed(db, narrator=None, duration_mins=None, publisher=None)
    response = apply(editor_client, item_id, ticket(db, editor_user, item_id), mode="backfill", fields=["title"])
    assert response.status_code == 303
    row = stored(db, item_id)
    assert row["title"] == "My edition" and row["authors"] == "My author" and row["subtitle"] == "Imported subtitle"
    assert row["narrator"] == "Wil Wheaton" and row["duration_mins"] == 659 and row["publisher"] == "Audible Studios"
    assert row["asin"] == ASIN and row["isbn"] == "9780441172719"


@pytest.mark.parametrize("source_key", [f"audible:us:{ASIN}", f"audible:uk:{ASIN}", "audiobookshelf:remote-id"])
@pytest.mark.parametrize("snapshot", [{"observed": "Remote", "local": "", "overridden": True},
    {"observed": "Remote", "applied": "Remote", "local": "Remote"}, {"observed": "Remote", "local": "Original"}])
def test_backfill_respects_blank_overrides_from_any_source(editor_client, editor_user, db, source_key, snapshot):
    item_id = seed(db, narrator="")
    db.execute("INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json) VALUES (?, ?, 'narrator', ?)",
               (item_id, source_key, json.dumps(snapshot)))
    db.commit()
    response = apply(editor_client, item_id, ticket(db, editor_user, item_id), mode="backfill")
    assert response.status_code == 303 and stored(db, item_id)["narrator"] == ""


def test_selective_keep_blank_survives_repeated_and_other_market_backfills(editor_client, editor_user, db):
    item_id = seed(db, narrator="")
    assert apply(editor_client, item_id, ticket(db, editor_user, item_id), fields="publisher").status_code == 303
    for market in ("us", "uk"):
        value = audible_catalog.normalise({"products": [PRODUCT]}, ASIN, market)
        assert apply(editor_client, item_id, ticket(db, editor_user, item_id, value), mode="backfill").status_code == 303
        assert stored(db, item_id)["narrator"] == ""
    assert apply(editor_client, item_id, ticket(db, editor_user, item_id), fields="narrator").status_code == 303
    assert stored(db, item_id)["narrator"] == "Wil Wheaton"


def test_clearing_previously_backfilled_value_is_a_durable_override(editor_client, editor_user, db):
    item_id = seed(db, narrator=None)
    assert apply(editor_client, item_id, ticket(db, editor_user, item_id), mode="backfill").status_code == 303
    item_write.update_item_fields(db, item_id, {"narrator": None})
    db.commit()
    for _ in range(2):
        assert apply(editor_client, item_id, ticket(db, editor_user, item_id), mode="backfill").status_code == 303
        assert stored(db, item_id)["narrator"] is None


@pytest.mark.parametrize("local", [None, "Wil Wheaton"])
def test_explicit_local_decision_survives_connected_refresh_even_when_value_is_unchanged(editor_client, editor_user, db, local):
    from app.services import metadata_policy
    item_id = seed(db, narrator=local)
    metadata_policy.record_initial(db, item_id, "audiobookshelf:id", {"narrator": local})
    db.commit()
    before = db.execute("SELECT observed_at FROM item_metadata_provenance WHERE source_key = 'audiobookshelf:id'").fetchone()[0]
    fields = "narrator" if local else "publisher"
    assert apply(editor_client, item_id, ticket(db, editor_user, item_id), fields=fields).status_code == 303
    assert db.execute("SELECT observed_at FROM item_metadata_provenance WHERE source_key = 'audiobookshelf:id'").fetchone()[0] == before
    with db:
        updates = metadata_policy.refresh_metadata(db, item_id, "audiobookshelf:id", {"narrator": "Changed by ABS"})
    assert updates == {} and stored(db, item_id)["narrator"] == local


def test_backfill_will_not_put_series_position_onto_another_series(editor_client, editor_user, db):
    item_id = seed(db, series_name="My other series", series_position=None)
    value = candidate(fields={"title": "The Martian", "series_name": "Provider series", "series_position": 2})
    assert apply(editor_client, item_id, ticket(db, editor_user, item_id, value), mode="backfill").status_code == 303
    row = stored(db, item_id)
    assert row["series_name"] == "My other series" and row["series_position"] is None


@pytest.mark.parametrize("outcome", ["no_match", "transport_failed", "rate_limited", "rejected"])
def test_provider_misses_and_errors_do_not_write_or_erase(editor_client, db, monkeypatch, outcome):
    item_id = seed(db)
    before = stored(db, item_id)
    monkeypatch.setattr(audible_catalog, "lookup", AsyncMock(return_value=provider_result.ProviderResult(outcome, "audible")))
    response = editor_client.post(f"/api/audiobooks/{item_id}/metadata/preview", data={"marketplace": "us"})
    assert response.status_code == 200 and 'role="alert"' in response.text
    assert "nothing was changed" in response.text.lower() or "were kept" in response.text
    assert stored(db, item_id) == before and not db.execute("SELECT * FROM item_metadata_provenance").fetchone()


@pytest.mark.parametrize("extra", [{"asin": "B000000000"}, {"url": "https://evil.test/"}, {"source_url": "https://user:pw@audible.com"}])
def test_preview_will_not_adopt_request_identifiers_or_fetch_urls(editor_client, db, monkeypatch, extra):
    item_id = seed(db)
    lookup = AsyncMock(side_effect=AssertionError("Tampered preview requested provider"))
    monkeypatch.setattr(audible_catalog, "lookup", lookup)
    response = editor_client.post(f"/api/audiobooks/{item_id}/metadata/preview", data={"marketplace": "us", **extra})
    assert 'role="alert"' in response.text and stored(db, item_id)["asin"] == ASIN
    lookup.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["tampered", "other_user", "expired", "image_ticket", "music_ticket", "other_item", "different_asin"])
def test_ticket_user_item_purpose_expiry_and_edition_binding(editor_client, editor_user, db, monkeypatch, mutation):
    item_id = seed(db)
    value = candidate()
    signed = ticket(db, editor_user, item_id, value)
    if mutation == "tampered":
        signed = signed[:-6] + "abcdef"
    elif mutation == "other_user":
        signed = ticket(db, {"id": editor_user["id"] + 1}, item_id)
    elif mutation == "image_ticket":
        signed = ticket(db, editor_user, item_id, image=True)
    elif mutation == "music_ticket":
        signed = music_review.sign(value, user_id=editor_user["id"], item_id=item_id, fingerprint=review.fingerprint(db, item_id))
    elif mutation == "expired":
        payload = jwt.decode(signed, music_review._key(), algorithms=[JWT_ALGORITHM])
        payload["exp"] = int(time.time()) - 1
        signed = jwt.encode(payload, music_review._key(), algorithm=JWT_ALGORITHM)
    elif mutation == "different_asin":
        value = audible_catalog.normalise({"products": [{**PRODUCT, "asin": "B000000000"}]}, "B000000000", "us")
        signed = ticket(db, editor_user, item_id, value)
    download = AsyncMock(side_effect=AssertionError("Invalid ticket downloaded"))
    monkeypatch.setattr(audible_catalog, "download_artwork", download)
    response = apply(editor_client, item_id + (mutation == "other_item"), signed, fields="title", image="0")
    assert response.status_code in (200, 404) and 'role="alert"' in response.text
    assert stored(db, item_id)["title"] == "My edition" and not db.execute("SELECT * FROM item_metadata_provenance").fetchone()
    download.assert_not_awaited()
    assert auth.decode_token(signed) is None


@pytest.mark.parametrize("change", ["title", "asin", "media_type", "trash", "provenance", "cover_policy"])
def test_stale_reviews_fail_before_download(editor_client, editor_user, db, monkeypatch, change):
    item_id = seed(db)
    signed = ticket(db, editor_user, item_id)
    if change == "trash":
        item_write.trash_item(db, item_id)
    elif change == "provenance":
        db.execute("INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json) VALUES (?, 'audiobookshelf:id', 'title', '{}')", (item_id,))
    elif change == "cover_policy":
        cover_sync_policy.block(db, item_id)
    else:
        values = {"title": "Concurrent edit", "asin": "B000000000", "media_type": "book"}
        item_write.update_item_fields(db, item_id, {change: values[change]})
    db.commit()
    before = stored(db, item_id)
    download = AsyncMock(side_effect=AssertionError("Stale review downloaded"))
    monkeypatch.setattr(audible_catalog, "download_artwork", download)
    response = apply(editor_client, item_id, signed, fields="title", image="0")
    assert 'role="alert"' in response.text and stored(db, item_id) == before
    download.assert_not_awaited()


def test_concurrent_edit_during_lookup_does_not_issue_new_baseline(editor_client, db, monkeypatch):
    item_id = seed(db)

    async def lookup(*args):
        from app.database import get_db
        with get_db() as other:
            item_write.update_item_fields(other, item_id, {"narrator": "Concurrent narrator"})
        return provider_result.found("audible", candidate())
    monkeypatch.setattr(audible_catalog, "lookup", lookup)
    response = editor_client.post(f"/api/audiobooks/{item_id}/metadata/preview", data={"marketplace": "us"})
    assert "changed during lookup" in response.text and inputs(response, "ticket") == []
    assert stored(db, item_id)["narrator"] == "Concurrent narrator"


@pytest.mark.parametrize("mode", ["selected", "backfill"])
def test_missing_artwork_and_metadata_are_applied_atomically_with_staged_file(editor_client, editor_user, db, monkeypatch, mode):
    item_id = seed(db, cover_path=None, publisher=None)
    signed = ticket(db, editor_user, item_id)

    async def download(identifier, url):
        assert identifier == item_id and url == IMAGE
        path = f"{identifier}-staged.jpg"
        (config.COVERS_DIR / path).write_bytes(PNG)
        return "covers/" + path
    monkeypatch.setattr(audible_catalog, "download_artwork", download)
    response = apply(editor_client, item_id, signed, fields="publisher", image="0", mode=mode)
    assert response.status_code == 303
    row = stored(db, item_id)
    assert row["publisher"] == "Audible Studios" and row["cover_path"] == f"covers/{item_id}-staged.jpg"
    assert cover_sync_policy.blocked(db, item_id) and (config.DATA_DIR / row["cover_path"]).read_bytes() == PNG


def test_failed_artwork_download_applies_no_metadata(editor_client, editor_user, db, monkeypatch):
    item_id = seed(db)
    before = stored(db, item_id)
    monkeypatch.setattr(audible_catalog, "download_artwork", AsyncMock(return_value=None))
    response = apply(editor_client, item_id, ticket(db, editor_user, item_id), fields="title", image="0")
    assert "Nothing was changed" in response.text and stored(db, item_id) == before
    assert not db.execute("SELECT * FROM item_metadata_provenance").fetchone()


@pytest.mark.parametrize("change", ["edit", "provenance", "trash", "db_failure"])
def test_race_or_failed_write_removes_staging_and_keeps_old_artwork(editor_client, editor_user, db, monkeypatch, change):
    item_id = seed(db)
    signed = ticket(db, editor_user, item_id)
    (config.COVERS_DIR / "chosen.jpg").write_bytes(b"old artwork")

    async def download(*args):
        from app.database import get_db
        with get_db() as other:
            if change == "edit":
                item_write.update_item_fields(other, item_id, {"narrator": "Concurrent narrator"})
            elif change == "trash":
                item_write.trash_item(other, item_id)
            elif change == "provenance":
                other.execute("INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json) VALUES (?, 'audiobookshelf:id', 'title', '{}')", (item_id,))
        (config.COVERS_DIR / f"{item_id}-staged.jpg").write_bytes(PNG)
        return f"covers/{item_id}-staged.jpg"
    monkeypatch.setattr(audible_catalog, "download_artwork", download)
    if change == "db_failure":
        original = item_write.update_item_fields

        def refuse(db, target, fields):
            original(db, target, fields)
            raise sqlite3.IntegrityError("Simulated constraint failure after write")
        monkeypatch.setattr(item_write, "update_item_fields", refuse)
    response = apply(editor_client, item_id, signed, fields="title", image="0")
    assert 'role="alert"' in response.text and stored(db, item_id)["title"] == "My edition"
    assert stored(db, item_id)["cover_path"] == "covers/chosen.jpg"
    assert not (config.COVERS_DIR / f"{item_id}-staged.jpg").exists()
    assert (config.COVERS_DIR / "chosen.jpg").read_bytes() == b"old artwork"


@pytest.mark.parametrize("protection", ["existing", "block", "dismissed", "retained"])
def test_backfill_does_not_resurrect_removed_or_replace_existing_artwork(editor_client, editor_user, db, monkeypatch, protection):
    item_id = seed(db, cover_path="covers/chosen.jpg" if protection == "existing" else None)
    if protection == "block":
        cover_sync_policy.block(db, item_id)
    elif protection == "dismissed":
        item_write.update_item_fields(db, item_id, {"cover_review_dismissed": 1})
    elif protection == "retained":
        (config.COVERS_DIR / f"{item_id}.jpg").write_bytes(b"retained")
    db.commit()
    before = stored(db, item_id)
    download = AsyncMock(side_effect=AssertionError("Protected cover downloaded"))
    monkeypatch.setattr(audible_catalog, "download_artwork", download)
    response = apply(editor_client, item_id, ticket(db, editor_user, item_id), mode="backfill", image="0")
    assert "manually removed artwork" in response.text and stored(db, item_id) == before
    download.assert_not_awaited()


def test_guard_reads_hold_write_lock_and_keep_private_history_copies_and_bindings(editor_client, editor_user, db, monkeypatch):
    from app.services import personal_library
    item_id = seed(db, abs_id="server-edition", abs_library_id="library")
    db.execute("INSERT INTO item_copies (item_id, copy_number, condition, notes) VALUES (?, 1, 'Good', 'Copy notes')", (item_id,))
    personal_library.save(db, editor_user["id"], item_id, personal_library.parse_form({"notes": "Private listening notes", "rating": "5"}))
    db.commit()
    tables = ("item_copies", "user_item_state", "user_item_events", "list_items", "source_records", "item_links")
    before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}
    signed = ticket(db, editor_user, item_id)
    original = review.check_current
    locked_checks = []

    def probe(conn, *args, **kwargs):
        if conn.in_transaction:
            from app.database import get_db
            with get_db() as rival:
                rival.execute("PRAGMA busy_timeout = 1")
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    rival.execute("BEGIN IMMEDIATE")
            locked_checks.append(True)
        return original(conn, *args, **kwargs)
    monkeypatch.setattr(review, "check_current", probe)
    assert apply(editor_client, item_id, signed, fields="publisher").status_code == 303
    assert locked_checks == [True]
    assert {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in tables} == before
    assert stored(db, item_id)["abs_id"] == "server-edition"


@pytest.mark.parametrize("invalid", ["purpose", "user", "item", "stale"])
def test_image_tickets_are_distinct_user_item_and_stale_bound(editor_client, editor_user, db, monkeypatch, invalid):
    item_id = seed(db)
    signed = ticket(db, {"id": editor_user["id"] + (invalid == "user")}, item_id, image=invalid != "purpose")
    if invalid == "stale":
        item_write.update_item_fields(db, item_id, {"narrator": "Changed"})
        db.commit()
    download = AsyncMock(side_effect=AssertionError("Unauthorized image downloaded"))
    monkeypatch.setattr(audible_catalog, "download_artwork", download)
    response = editor_client.get(f"/api/audiobooks/{item_id + (invalid == 'item')}/metadata/image", params={"ticket": signed})
    assert response.status_code == 404
    download.assert_not_awaited()


def test_image_preview_is_same_origin_temporary_and_does_not_write_catalogue(editor_client, editor_user, db, monkeypatch):
    item_id = seed(db)
    before = stored(db, item_id)

    async def download(identifier, url):
        assert identifier == 0 and url == IMAGE
        (config.COVERS_DIR / "0-preview.jpg").write_bytes(PNG)
        return "covers/0-preview.jpg"
    monkeypatch.setattr(audible_catalog, "download_artwork", download)
    response = editor_client.get(f"/api/audiobooks/{item_id}/metadata/image", params={"ticket": ticket(db, editor_user, item_id, image=True)})
    assert response.content == PNG and response.headers["content-type"] == "image/png" and response.headers["cache-control"] == "no-store"
    assert not (config.COVERS_DIR / "0-preview.jpg").exists() and stored(db, item_id) == before


@pytest.mark.parametrize("fields", [["asin"], ["isbn"], ["media_type"], ["narrator", "narrator"], ["not_a_field"]])
def test_only_unique_displayed_fields_may_be_applied(editor_client, editor_user, db, fields):
    item_id = seed(db)
    response = apply(editor_client, item_id, ticket(db, editor_user, item_id), fields=fields)
    assert 'role="alert"' in response.text and stored(db, item_id)["title"] == "My edition"
    assert not db.execute("SELECT * FROM item_metadata_provenance").fetchone()


@pytest.mark.parametrize("extra", [{"asin": "B000000000"}, {"narrator": "Forged"}, {"image": "https://127.0.0.1/a.jpg"}, {"mode": "auto"}])
def test_apply_refuses_source_values_and_invalid_choices(editor_client, editor_user, db, extra):
    item_id = seed(db)
    response = apply(editor_client, item_id, ticket(db, editor_user, item_id), fields="title", **extra)
    assert 'role="alert"' in response.text and stored(db, item_id)["title"] == "My edition"


@pytest.mark.parametrize("route,method", [("", "get"), ("/preview", "post"), ("/apply", "post"), ("/image?ticket=x", "get")])
def test_all_routes_require_editor_and_connected_feature(viewer_client, route, method):
    assert getattr(viewer_client, method)(f"/api/audiobooks/1/metadata{route}").status_code == 403


@pytest.mark.parametrize("route,method", [("", "get"), ("/preview", "post"), ("/apply", "post"), ("/image?ticket=x", "get")])
def test_connected_feature_off_refuses_every_route(editor_client, route, method):
    from tests.test_features_gate import off
    off("connected_library")
    assert getattr(editor_client, method)(f"/api/audiobooks/1/metadata{route}").status_code == 403


@pytest.mark.parametrize("route", ["preview", "apply"])
def test_csrf_is_enforced(editor_client, route):
    editor_client.headers.pop("X-CSRF-Token")
    assert editor_client.post(f"/api/audiobooks/1/metadata/{route}", data={"marketplace": "us"}).status_code == 403


@pytest.mark.parametrize("route,limit", [("preview", review.PREVIEW_FORM_BYTES), ("apply", review.REVIEW_FORM_BYTES)])
def test_route_body_limits_and_pre_csrf_integration_hook(editor_client, db, route, limit):
    item_id = seed(db)
    path = f"/api/audiobooks/{item_id}/metadata/{route}"
    assert review.form_limit(path) == limit and review.form_limit(path + "/") == limit
    assert review.form_limit(f"/api/audiobooks/not-an-id/metadata/{route}") == limit
    response = editor_client.post(path, content=b"x=" + b"x" * limit, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert "too large" in response.text
    assert review.form_limit("/api/audiobooks/1/metadata/image") is None


@pytest.mark.parametrize("route", ["preview", "apply"])
@pytest.mark.parametrize("identifier", ["1", "not-an-id", "99999999999999999999999999"])
@pytest.mark.asyncio
async def test_pre_csrf_limit_hook_can_stop_stream_before_reading_its_tail(route, identifier):
    from app.http_body import BodyTooLarge, bounded_body
    limit = review.form_limit(f"/api/audiobooks/{identifier}/metadata/{route}/")
    received = []

    async def receive():
        assert not received, "Unbounded request tail was read"
        received.append(True)
        return {"type": "http.request", "body": b"x=" + b"x" * limit, "more_body": True}
    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with pytest.raises(BodyTooLarge):
        await bounded_body(request, limit)
    assert received == [True]


@pytest.mark.parametrize("kind", ["missing", "trashed", "book", "invalid_asin"])
def test_unavailable_or_invalid_identity_never_contacts_provider(editor_client, db, monkeypatch, kind):
    item_id = seed(db, media_type="book" if kind == "book" else "audiobook", asin="bad" if kind == "invalid_asin" else ASIN)
    if kind == "trashed":
        item_write.trash_item(db, item_id)
        db.commit()
    lookup = AsyncMock(side_effect=AssertionError("Invalid item requested provider"))
    monkeypatch.setattr(audible_catalog, "lookup", lookup)
    response = editor_client.post(f"/api/audiobooks/{item_id + (kind == 'missing')}/metadata/preview", data={"marketplace": "us"})
    assert 'role="alert"' in response.text
    lookup.assert_not_awaited()
