"""Saved provenance review: strict values, explicit intent and stale-write safety."""

from contextlib import contextmanager
import json
import logging
import sqlite3
from unittest.mock import Mock

import pytest
from starlette.datastructures import FormData

from app.services import item_write, metadata_policy, metadata_review as review


SOURCE = "audiobookshelf:book-1"
OBSERVED_AT = "2026-11-01 12:34:56"


@pytest.fixture(autouse=True)
def _registered_review_routes(client):
    """Keep this slice runnable before its parent registers the new router."""
    from app.routers import metadata_review as routes

    app = client.app
    before = list(app.router.routes)
    if not any(getattr(route, "path", None) == "/api/library/items/{item_id}/metadata"
               for route in before):
        app.include_router(routes.router)
    yield
    app.router.routes[:] = before


def _observation(db, item_id, field="description", observed="Remote description", *,
                 source=SOURCE, raw=None, **baseline):
    db.execute(
        "INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_id, source, field, raw if raw is not None else json.dumps({"observed": observed, **baseline}),
         OBSERVED_AT),
    )


def _seed(db, *, field="description", current="Local description", observed="Remote description",
          source=SOURCE, raw=None, **baseline):
    values = {"title": "Local title", field: current}
    item_id = item_write.insert_item(db, values)
    _observation(db, item_id, field, observed, source=source, raw=raw, **baseline)
    return item_id


def _row(db, item_id, field="description", source=SOURCE):
    return next(row for row in review.context_for(db, item_id)["metadata_rows"]
                if row["field_name"] == field and row["source_key"] == source)


def _form(row, decision="follow_source"):
    return {"source_key": row["source_key"], "field_name": row["field_name"],
            "fingerprint": row["fingerprint"], "decision": decision}


def _post(client, item_id, row, decision="follow_source", **kwargs):
    return client.post(f"/api/library/items/{item_id}/metadata/resolve", data=_form(row, decision),
                       follow_redirects=False, **kwargs)


def _saved(db, item_id, field="description", source=SOURCE):
    return json.loads(db.execute(
        "SELECT value_json FROM item_metadata_provenance WHERE item_id = ? AND source_key = ? AND field_name = ?",
        (item_id, source, field),
    ).fetchone()[0])


def _snapshot(db, item_id):
    return (dict(db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()),
            [tuple(row) for row in db.execute(
                "SELECT * FROM item_metadata_provenance WHERE item_id = ? ORDER BY source_key, field_name",
                (item_id,),
            ).fetchall()])


def _toast(response):
    toast = json.loads(response.headers["HX-Trigger"])["showToast"]
    assert toast["type"] == "error"
    return toast["message"]


def test_extracts_current_observed_ownership_override_and_timestamp_without_writes(db):
    item_id = _seed(db, local="Previous local", applied="Previous local")
    _observation(db, item_id, "title", "Local title")
    _observation(db, item_id, "authors", "Remote author", local=None, overridden=True)
    _observation(db, item_id, "media_type", "book", applied="book")
    before = _snapshot(db, item_id)
    statements = []
    db.set_trace_callback(statements.append)
    context = review.context_for(db, item_id)
    db.set_trace_callback(None)
    rows = {row["field_name"]: row for row in context["metadata_rows"]}
    assert context["item"]["id"] == item_id
    assert rows["description"]["field_label"] == "Description"
    assert rows["description"]["source_label"] == "Audiobookshelf"
    assert rows["description"]["current_display"] == "Local description"
    assert rows["description"]["observed_display"] == "Remote description"
    assert rows["description"]["observed_at"] == "2026-11-01T12:34:56+00:00"
    assert rows["description"]["different"] and rows["description"]["user_overridden"]
    assert not rows["description"]["provider_owned"]
    assert rows["authors"]["user_overridden"]
    assert not rows["title"]["different"] and not rows["title"]["provider_owned"]
    assert rows["media_type"]["provider_owned"]
    assert not rows["media_type"]["resolvable"] and not rows["media_type"]["can_follow"]
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN")) for sql in statements)
    assert _snapshot(db, item_id) == before


@pytest.mark.parametrize("source,label", [("komga:01ABC", "Komga"), ("romm:42", "RomM"),
                                         (SOURCE, "Audiobookshelf")])
def test_static_provider_labels_not_raw_source_keys(db, source, label):
    item_id = _seed(db, source=source)
    assert _row(db, item_id, source=source)["source_label"] == label


def test_connected_source_selects_provider_only_and_never_exposes_credentials(viewer_client, db):
    source_id = db.execute(
        "INSERT INTO source_instances (provider, name, base_url, credentials_enc, options_json) "
        "VALUES ('opds', 'private-instance-name', 'https://private-root.invalid/', 'secret-ciphertext', "
        "'{\"password\":\"secret-option\"}')"
    ).lastrowid
    item_id = _seed(db, source=f"source:{source_id}")
    db.commit()
    context = review.context_for(db, item_id)
    assert context["metadata_rows"][0]["source_label"] == "OPDS catalogue"
    response = viewer_client.get(f"/api/library/items/{item_id}/metadata")
    assert response.status_code == 200
    serialized = json.dumps(context) + response.text
    for private in ("private-instance-name", "private-root.invalid", "secret-ciphertext", "secret-option"):
        assert private not in serialized


def test_viewer_fragment_is_collapsed_read_only_escaped_and_get_never_mutates(viewer_client, db):
    hostile = '<img src=x onerror="alert(1)"> <script>bad()</script>'
    item_id = _seed(db, current=hostile, observed=hostile + " & remote")
    db.commit()
    before = _snapshot(db, item_id)
    response = viewer_client.get(f"/api/library/items/{item_id}/metadata")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert 'id="metadata-provenance"' in response.text
    assert 'data-testid="metadata-provenance"' in response.text
    assert "Metadata sources" in response.text
    assert "<details" in response.text and " open" not in response.text.split("<summary", 1)[0]
    assert "&lt;img" in response.text and "&lt;script&gt;" in response.text
    assert "<img" not in response.text and "<script" not in response.text
    assert "<form" not in response.text and 'name="fingerprint"' not in response.text
    assert SOURCE not in response.text
    assert _snapshot(db, item_id) == before


def test_editors_get_csrf_fingerprinted_field_forms_but_identity_is_read_only(editor_client, db):
    item_id = _seed(db)
    _observation(db, item_id, "isbn", "9780441172719")
    db.commit()
    response = editor_client.get(f"/api/library/items/{item_id}/metadata")
    assert response.status_code == 200
    assert response.text.count("<form") == 1
    assert 'name="_csrf" value="test-csrf-token-fixed"' in response.text
    assert 'hx-target="#metadata-provenance"' in response.text
    assert 'hx-swap="outerHTML"' in response.text
    assert 'value="keep_local"' in response.text and 'value="follow_source"' in response.text
    assert 'name="fingerprint"' in response.text and 'name="observed"' not in response.text
    assert "Read-only identity field" in response.text
    assert "including a blank" in response.text


def test_follow_writes_only_that_field_and_source_through_funnel(editor_client, editor_user, db, monkeypatch):
    item_id = _seed(db, local="Local description", overridden=True)
    location_id = db.execute("INSERT INTO locations (name) VALUES ('My shelf')").lastrowid
    item_write.update_item_fields(db, item_id, {"notes": "My note", "owned": 0, "reading_status": "read",
                                               "cover_path": "covers/mine.jpg", "location_id": location_id,
                                               "wishlisted": True})
    tag_id = db.execute("INSERT INTO tags (name) VALUES ('My tag')").lastrowid
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item_id, tag_id))
    db.execute("INSERT INTO user_item_state (user_id, item_id, notes, favourite) VALUES (?, ?, 'Personal', 1)",
               (editor_user["id"], item_id))
    db.execute("INSERT INTO user_item_events (user_id, item_id, event_type, snapshot_json) VALUES (?, ?, 'saved', '{}')",
               (editor_user["id"], item_id))
    tables = ("copies_live", "item_tags", "list_items", "user_item_state", "user_item_events")
    protected = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} WHERE item_id = ?", (item_id,))]
                 for table in tables}
    _observation(db, item_id, observed="Another provider", source="komga:other")
    other_before = _saved(db, item_id, source="komga:other")
    before, _ = _snapshot(db, item_id)
    row = _row(db, item_id)
    db.commit()
    write = Mock(wraps=item_write.update_item_fields)
    monkeypatch.setattr(item_write, "update_item_fields", write)
    response = _post(editor_client, item_id, row)
    assert response.status_code == 303
    assert response.headers["location"] == f"/item/{item_id}#metadata-provenance"
    write.assert_called_once()
    assert write.call_args.args[1:] == (item_id, {"description": "Remote description"})
    after, _ = _snapshot(db, item_id)
    assert {key: value for key, value in after.items() if key not in {"description", "updated_at"}} == {
        key: value for key, value in before.items() if key not in {"description", "updated_at"}}
    assert _saved(db, item_id) == {"observed": "Remote description", "local": "Remote description",
                                   "applied": "Remote description", "overridden": False, "follow_source": True}
    assert _saved(db, item_id, source="komga:other") == other_before
    assert db.execute("SELECT observed_at FROM item_metadata_provenance WHERE source_key = ?", (SOURCE,)).fetchone()[0] == OBSERVED_AT
    assert {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} WHERE item_id = ?", (item_id,))]
            for table in tables} == protected


@pytest.mark.parametrize("current", [None, "", "My description"])
def test_keep_local_clears_applied_and_survives_repeated_source_refreshes(editor_client, db, current):
    item_id = _seed(db, current=current, applied="Old provider description", local="Old provider description")
    row = _row(db, item_id)
    db.commit()
    assert _post(editor_client, item_id, row, "keep_local").status_code == 303
    assert _saved(db, item_id) == {"observed": "Remote description", "local": current, "overridden": True}
    for remote in ("Remote version 2", "Remote version 3", current):
        assert metadata_policy.refresh_metadata(db, item_id, SOURCE, {"description": remote}) == {}
        assert db.execute("SELECT description FROM items_live WHERE id = ?", (item_id,)).fetchone()[0] == current
        assert _saved(db, item_id)["overridden"] is True
        assert "applied" not in _saved(db, item_id)


def test_follow_allows_refresh_until_next_local_edit_including_clear(editor_client, db):
    item_id = _seed(db)
    row = _row(db, item_id)
    db.commit()
    assert _post(editor_client, item_id, row).status_code == 303
    for remote in ("Remote 2", "Remote 3"):
        assert metadata_policy.refresh_metadata(db, item_id, SOURCE, {"description": remote}) == {"description": remote}
        assert _saved(db, item_id)["applied"] == remote
    item_write.update_item_fields(db, item_id, {"description": None})
    for remote in ("Remote 4", "Remote 5"):
        assert metadata_policy.refresh_metadata(db, item_id, SOURCE, {"description": remote}) == {}
        assert db.execute("SELECT description FROM items_live WHERE id = ?", (item_id,)).fetchone()[0] is None
        assert _saved(db, item_id)["overridden"] is True


def test_explicit_follow_works_through_adopted_komga_refresh(editor_client, db):
    from app.services import komga_records
    from tests.conftest import _insert_item

    item_id = _insert_item(db, title="My title", description="My description", media_type="comic", isbn="9780000009982")
    candidate = {"komga_id": "follow-entry", "komga_library_id": "comics", "library_kind": "comic",
                 "title": "Source title", "description": "Source description", "isbn": "9780000009982"}
    assert komga_records.persist_candidate(db, candidate)["action"] == "adopted"
    source = "komga:follow-entry"
    db.commit()
    for field in ("title", "description"):
        row = _row(db, item_id, field, source)
        assert _post(editor_client, item_id, row).status_code == 303
    for number in (2, 3):
        komga_records.persist_candidate(db, {**candidate, "title": f"Source title {number}",
                                            "description": f"Source description {number}"})
        state = db.execute("SELECT title, description FROM items_live WHERE id = ?", (item_id,)).fetchone()
        assert state[:] == (f"Source title {number}", f"Source description {number}")
        assert _saved(db, item_id, "description", source)["follow_source"] is True
    item_write.update_item_fields(db, item_id, {"description": None})
    komga_records.persist_candidate(db, {**candidate, "description": "Must not refill"})
    assert db.execute("SELECT description FROM items_live WHERE id = ?", (item_id,)).fetchone()[0] is None
    assert _saved(db, item_id, "description", source)["overridden"] is True


@pytest.mark.parametrize("field,current,observed", [
    ("title", "Local title", "Source title"), ("authors", "Me", "A. Writer, B. Writer"),
    ("narrator", None, "A Reader"), ("publisher", None, "A Press"),
    ("publish_year", 2000, 2026), ("page_count", 10, 25), ("duration_mins", None, 123),
    ("language", "en", "fr"), ("series_name", None, "A series"), ("series_position", None, 1.5),
    ("description", "Local", None),
])
def test_allowlisted_shapes_and_stored_sqlite_baseline(editor_client, db, field, current, observed):
    item_id = _seed(db, field=field, current=current, observed=observed)
    row = _row(db, item_id, field)
    db.commit()
    assert _post(editor_client, item_id, row).status_code == 303
    actual = db.execute(f"SELECT {field} FROM items_live WHERE id = ?", (item_id,)).fetchone()[0]
    saved = _saved(db, item_id, field)
    assert actual == observed
    assert saved == {"observed": observed, "local": actual, "applied": actual, "overridden": False, "follow_source": True}
    assert type(saved["applied"]) is type(actual)


def test_successful_htmx_returns_open_self_contained_panel(editor_client, db):
    item_id = _seed(db)
    row = _row(db, item_id)
    db.commit()
    response = _post(editor_client, item_id, row, "keep_local", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'id="metadata-provenance"' in response.text and "<html" not in response.text
    assert 'role="status"' in response.text and "Local value kept" in response.text
    assert "Local override" in response.text


@pytest.mark.parametrize("decision", ["keep_local", "follow_source"])
@pytest.mark.parametrize("change", ["local", "remote", "timestamp", "raw_json", "deleted"])
def test_stale_snapshots_local_edits_and_deleted_rows_refused_with_visible_toast(editor_client, db, decision, change):
    item_id = _seed(db)
    row = _row(db, item_id)
    if change == "local":
        item_write.update_item_fields(db, item_id, {"description": "Edited since review"})
    elif change == "remote":
        metadata_policy.record_observation(db, item_id, SOURCE, {"description": "Refreshed remote"})
    elif change == "timestamp":
        db.execute("UPDATE item_metadata_provenance SET observed_at = '2026-11-02 00:00:00'")
    elif change == "raw_json":
        db.execute("UPDATE item_metadata_provenance SET value_json = ?", ('{ "observed" : "Remote description" }',))
    else:
        db.execute("DELETE FROM item_metadata_provenance")
    db.commit()
    before = _snapshot(db, item_id)
    response = _post(editor_client, item_id, row, decision, headers={"HX-Request": "true"})
    assert response.status_code == 409
    assert "reload" in _toast(response).lower()
    assert 'role="alert"' in response.text
    assert _snapshot(db, item_id) == before


def test_fingerprint_uses_full_values_not_truncated_display(editor_client, db):
    prefix = "x" * (review.MAX_DISPLAY_CHARS + 1)
    item_id = _seed(db, observed=prefix + "first")
    row = _row(db, item_id)
    metadata_policy.record_observation(db, item_id, SOURCE, {"description": prefix + "second"})
    assert _row(db, item_id)["observed_display"] == row["observed_display"]
    db.commit()
    response = _post(editor_client, item_id, row)
    assert response.status_code == 409
    assert "changed" in _toast(response)


@pytest.mark.parametrize("raw", [
    "not json", "null", "[]", '"old scalar"', '{"value":"legacy"}',
    '{"observed": {"html":"bad"}}', '{"observed":["bad"]}', '{"observed":true}',
    '{"observed":"good","local":[]}', '{"observed":"good","applied":false}',
    '{"observed":"good","overridden":"false"}', '{"observed":"good","credentials":"private"}',
    '{"observed":"first","observed":"second"}', '{"observed":"\\ud800"}',
    '{"observed":"\\u0000"}', '{"observed":NaN}', '{"observed":Infinity}',
    '{"observed":' + "[" * 1100 + "0" + "]" * 1100 + "}",
    " " * (review.MAX_SNAPSHOT_CHARS + 1),
])
def test_unknown_corrupt_hostile_json_is_ignored_and_cannot_resolve(editor_client, db, raw):
    item_id = _seed(db, raw=raw)
    assert review.context_for(db, item_id)["metadata_rows"] == []
    db.commit()
    before = _snapshot(db, item_id)
    response = _post(editor_client, item_id, {"source_key": SOURCE, "field_name": "description", "fingerprint": "0" * 64})
    assert response.status_code == 422
    assert "not safe" in _toast(response)
    assert _snapshot(db, item_id) == before


@pytest.mark.parametrize("field,current,observed", [
    ("title", "Local", "x" * 2001), ("authors", "Local", ["Remote"]),
    ("description", "Local", "x" * 20001), ("language", "en", "x" * 101),
    ("publish_year", 2000, "2026"), ("publish_year", 2000, 10000), ("publish_year", 2000, -1),
    ("page_count", 20, True), ("page_count", 20, 1.5), ("page_count", 20, 1000001),
    ("duration_mins", 10, -1), ("duration_mins", 10, float("inf")),
    ("series_position", 1, float("nan")), ("series_position", 1, 10**1000),
    ("series_position", 1, {"position": 2}),
])
def test_bad_types_oversized_and_nonfinite_values_refused(editor_client, db, field, current, observed):
    item_id = _seed(db, field=field, current=current, observed=observed)
    assert review.context_for(db, item_id)["metadata_rows"] == []
    db.commit()
    before = _snapshot(db, item_id)
    response = _post(editor_client, item_id, {"source_key": SOURCE, "field_name": field, "fingerprint": "0" * 64})
    assert response.status_code == 422
    assert _snapshot(db, item_id) == before


@pytest.mark.parametrize("observed", [None, "", " \t\n"])
def test_empty_title_cannot_be_followed_even_with_current_fingerprint(editor_client, db, observed):
    item_id = _seed(db, field="title", current="Local title", observed=observed)
    row = _row(db, item_id, "title")
    assert row["resolvable"] and not row["can_follow"]
    db.commit()
    before = _snapshot(db, item_id)
    response = _post(editor_client, item_id, row)
    assert response.status_code == 422
    assert "non-empty title" in _toast(response)
    assert _snapshot(db, item_id) == before


@pytest.mark.parametrize("field", ["media_type", "isbn", "isbn10", "upc", "asin", "platform",
                                    "abs_id", "owned", "notes", "location_id", "cover_path", "tags"])
@pytest.mark.parametrize("decision", ["keep_local", "follow_source"])
def test_identity_and_personal_fields_cannot_be_resolved(editor_client, db, field, decision):
    item_id = _seed(db)
    row = {**_row(db, item_id), "field_name": field}
    db.commit()
    before = _snapshot(db, item_id)
    response = _post(editor_client, item_id, row, decision)
    assert response.status_code == 422
    assert "read-only" in _toast(response)
    assert _snapshot(db, item_id) == before


@pytest.mark.parametrize("source", ["", " komga:book", "komga:book ", "Komga:book", "abs:book",
    "komga:../book", "komga:book?token=secret", "komga:<script>", "komga:book\n", "komga:book\x00",
    "audiobookshelf:https://user:secret@host/", "source:01", "source:-1", "source:9223372036854775808",
    "source:1:book/part", "komga:" + "x" * 257,
])
def test_source_keys_bounded_canonical_opaque_and_not_rendered_or_adopted(editor_client, db, source):
    item_id = _seed(db, source=source)
    assert review.context_for(db, item_id)["metadata_rows"] == []
    db.commit()
    before = _snapshot(db, item_id)
    response = _post(editor_client, item_id, {"source_key": source, "field_name": "description", "fingerprint": "0" * 64})
    assert response.status_code == 422
    assert "valid saved metadata source" in _toast(response)
    assert _snapshot(db, item_id) == before


def test_cannot_adopt_another_source_or_reuse_another_item_fingerprint(editor_client, db):
    first = _seed(db)
    second = _seed(db)
    row = _row(db, first)
    db.commit()
    assert _post(editor_client, second, row).status_code == 409
    assert _post(editor_client, first, {**row, "source_key": "komga:new-source"}).status_code == 409
    assert db.execute("SELECT COUNT(*) FROM item_metadata_provenance").fetchone()[0] == 2
    assert _saved(db, first) == {"observed": "Remote description"}
    assert _saved(db, second) == {"observed": "Remote description"}


def test_display_row_and_text_limits_are_bounded(db):
    item_id = item_write.insert_item(db, title="Local")
    for number in range(review.MAX_ROWS + 5):
        _observation(db, item_id, observed="x" * 2000, source=f"komga:record-{number:03}")
    context = review.context_for(db, item_id)
    assert len(context["metadata_rows"]) == review.MAX_ROWS
    assert context["metadata_limited"]
    assert all(len(row["observed_display"]) == review.MAX_DISPLAY_CHARS + 1 for row in context["metadata_rows"])


def test_unknown_fields_do_not_use_the_display_budget(db):
    item_id = _seed(db)
    for number in range(review.MAX_ROWS + 5):
        _observation(db, item_id, field=f"unknown-{number}", source="audiobookshelf:aaa")
    assert len(review.context_for(db, item_id)["metadata_rows"]) == 1


def test_funnel_failure_rolls_back_item_and_provenance_before_render(editor_client, db, monkeypatch):
    item_id = _seed(db)
    row = _row(db, item_id)
    db.commit()
    before = _snapshot(db, item_id)
    original_write = item_write.update_item_fields

    def failing_write(db, item_id, fields):
        original_write(db, item_id, fields)
        raise item_write.ItemValueError("Refuse after a partial write")

    monkeypatch.setattr(item_write, "update_item_fields", failing_write)
    response = _post(editor_client, item_id, row, headers={"HX-Request": "true"})
    assert response.status_code == 422
    assert "cannot be stored" in _toast(response)
    assert _snapshot(db, item_id) == before


@pytest.mark.parametrize("decision", ["keep_local", "follow_source"])
def test_viewers_cannot_resolve_even_with_valid_form(viewer_client, db, decision):
    item_id = _seed(db)
    row = _row(db, item_id)
    db.commit()
    before = _snapshot(db, item_id)
    assert _post(viewer_client, item_id, row, decision).status_code == 403
    assert _snapshot(db, item_id) == before


def test_role_precedes_feature_refusal(viewer_client, db):
    from app.database import set_setting

    item_id = _seed(db)
    row = _row(db, item_id)
    set_setting(db, "feature.connected_library", "0")
    db.commit()
    response = _post(viewer_client, item_id, row)
    assert response.status_code == 403 and "disabled" not in response.text.lower()


def test_feature_off_refuses_get_and_post_without_writes(editor_client, db):
    from app.database import set_setting

    item_id = _seed(db)
    row = _row(db, item_id)
    set_setting(db, "feature.connected_library", "0")
    db.commit()
    before = _snapshot(db, item_id)
    assert editor_client.get(f"/api/library/items/{item_id}/metadata").status_code == 403
    response = _post(editor_client, item_id, row, headers={"HX-Request": "true"})
    assert response.status_code == 403
    assert "turned off" in _toast(response).lower()
    assert _snapshot(db, item_id) == before


def test_csrf_required_and_plain_form_redirect_is_fixed_local(editor_client, db):
    item_id = _seed(db)
    row = _row(db, item_id)
    db.commit()
    token = editor_client.headers.pop("X-CSRF-Token")
    before = _snapshot(db, item_id)
    assert _post(editor_client, item_id, row).status_code == 403
    assert _snapshot(db, item_id) == before
    response = editor_client.post(f"/api/library/items/{item_id}/metadata/resolve", data={
        **_form(row), "_csrf": token,
    }, headers={"Referer": "https://attacker.invalid/", "HX-Current-URL": "https://attacker.invalid/"},
        follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/item/{item_id}#metadata-provenance"


@pytest.mark.parametrize("field,value", [("observed", "attacker value"), ("value_json", "{}"),
                                          ("local", "attacker value"), ("applied", "attacker value")])
def test_no_user_supplied_observed_or_baseline_values_accepted(editor_client, db, field, value):
    item_id = _seed(db)
    row = _row(db, item_id)
    db.commit()
    before = _snapshot(db, item_id)
    response = editor_client.post(f"/api/library/items/{item_id}/metadata/resolve",
                                  data={**_form(row), field: value}, headers={"HX-Request": "true"})
    assert response.status_code == 422
    assert "cannot be supplied" in _toast(response)
    assert _snapshot(db, item_id) == before


@pytest.mark.parametrize("changes", [{"decision": "adopt"}, {"fingerprint": ""},
    {"fingerprint": "F" * 64}, {"fingerprint": "0" * 65}, {"source_key": "x" * 5000}])
def test_bad_forms_have_useful_htmx_feedback_and_no_writes(editor_client, db, changes):
    item_id = _seed(db)
    row = _row(db, item_id)
    db.commit()
    before = _snapshot(db, item_id)
    response = editor_client.post(f"/api/library/items/{item_id}/metadata/resolve",
                                  data={**_form(row), **changes}, headers={"HX-Request": "true"})
    assert response.status_code == 422
    assert _toast(response)
    assert _snapshot(db, item_id) == before


def test_duplicate_missing_file_and_json_inputs_refused(editor_client, db):
    item_id = _seed(db)
    row = _row(db, item_id)
    values = _form(row)
    with pytest.raises(review.ReviewError, match="exactly once"):
        review.parse_form(FormData([*values.items(), ("decision", "keep_local")]))
    with pytest.raises(review.ReviewError, match="exactly once"):
        review.parse_form(FormData([(key, value) for key, value in values.items() if key != "fingerprint"]))
    db.commit()
    before = _snapshot(db, item_id)
    url = f"/api/library/items/{item_id}/metadata/resolve"
    assert editor_client.post(url, json=values, headers={"HX-Request": "true"}).status_code == 422
    response = editor_client.post(url, data={key: value for key, value in values.items() if key != "source_key"},
                                  files={"source_key": ("source.txt", SOURCE)}, headers={"HX-Request": "true"})
    assert response.status_code == 422 and _toast(response)
    response = editor_client.post(url, content="malformed", headers={
        "Content-Type": "multipart/form-data", "HX-Request": "true",
    })
    assert response.status_code == 422 and "valid metadata review form" in _toast(response)
    assert _snapshot(db, item_id) == before


@pytest.mark.parametrize("trashed", [False, True])
def test_missing_and_trashed_items_not_read_or_resolved(editor_client, db, trashed):
    item_id = _seed(db)
    row = _row(db, item_id)
    if trashed:
        item_write.trash_item(db, item_id)
    else:
        db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    assert review.context_for(db, item_id) is None
    assert editor_client.get(f"/api/library/items/{item_id}/metadata").status_code == 404
    response = _post(editor_client, item_id, row, headers={"HX-Request": "true"})
    assert response.status_code == 404
    assert _toast(response) == "Item not found"
    assert "Local description" not in response.text


@pytest.mark.parametrize("decision", ["keep_local", "follow_source"])
@pytest.mark.parametrize("stale", [False, True])
def test_lock_before_guard_render_and_logging_after_release(editor_client, db, monkeypatch, decision, stale):
    from app.database import get_db
    from app.routers import metadata_review as routes

    item_id = _seed(db)
    row = _row(db, item_id)
    if stale:
        item_write.update_item_fields(db, item_id, {"description": "New local"})
    db.commit()
    statements, locked = [], {"value": False}

    class RecordingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            statements.append(sql)
            result = self.connection.execute(sql, *args)
            if sql == "BEGIN IMMEDIATE":
                locked["value"] = True
            elif sql.startswith("SELECT") and locked["value"]:
                with get_db() as other:
                    other.execute("PRAGMA busy_timeout=0")
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        other.execute("BEGIN IMMEDIATE")
            return result

        def __getattr__(self, key):
            return getattr(self.connection, key)

    @contextmanager
    def recording_db():
        try:
            with get_db() as connection:
                yield RecordingConnection(connection)
        finally:
            locked["value"] = False

    original_fragment, original_log = routes._fragment, logging.Logger._log

    def fragment(*args, **kwargs):
        assert not locked["value"], "Rendering under a SQLite write lock"
        return original_fragment(*args, **kwargs)

    def log(self, *args, **kwargs):
        assert not locked["value"], "Logging under a SQLite write lock"
        return original_log(self, *args, **kwargs)

    monkeypatch.setattr(routes, "get_db", recording_db)
    monkeypatch.setattr(routes, "_fragment", fragment)
    monkeypatch.setattr(logging.Logger, "_log", log)
    response = _post(editor_client, item_id, row, decision, headers={"HX-Request": "true"})
    assert response.status_code == (409 if stale else 200)
    assert statements[0] == "BEGIN IMMEDIATE"
