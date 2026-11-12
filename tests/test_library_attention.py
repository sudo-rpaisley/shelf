"""Bounded, non-mutating quality queues over live catalogue data only."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.services import connection_suggestions, item_copies, item_write, library_attention


_LEGACY_SETTINGS = {
    "abs": ("abs_url", "abs_token", "abs_sync"),
    "komga": ("komga_url", "komga_api_key", "komga"),
    "romm": ("romm_url", "romm_token", "romm"),
}
_PRIVATE_URL = "https://private-user:private-password@legacy-private.invalid/api?token=PRIVATE-TOKEN"


def _legacy(db, provider, *, status="never", success=None, attempt=None):
    from app.database import set_setting

    url_key, credential_key, feature = _LEGACY_SETTINGS[provider]
    for key, value in (
        (url_key, _PRIVATE_URL), (credential_key, "PRIVATE-TOKEN"), (f"feature.{feature}", "1"),
        (f"connection.{provider}.last_status", status),
        (f"connection.{provider}.last_sync_at", success or ""),
        (f"connection.{provider}.last_attempt_at", attempt or ""),
        (f"connection.{provider}.last_error", _PRIVATE_URL),
    ):
        set_setting(db, key, value)


def _item(db, title="A book", media_type="book", authors="A Writer", **fields):
    return item_write.insert_item(db, title=title, media_type=media_type, authors=authors, **fields)


def _source(db, name="Source", **fields):
    values = {"provider": "opds", "name": name, "base_url": "https://library.invalid/catalog", **fields}
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    return db.execute(f"INSERT INTO source_instances ({columns}) VALUES ({placeholders})", tuple(values.values())).lastrowid


def _record(db, source, item, availability="available", external_id="record"):
    return db.execute(
        "INSERT INTO source_records (source_id, item_id, external_id, title, media_type, availability, last_seen_at) "
        "VALUES (?, ?, ?, 'Remote title', 'ebook', ?, '2026-01-01T00:00:00Z')",
        (source, item, external_id, availability),
    ).lastrowid


def test_cover_counts_exclude_dismissals_covers_and_trash(db):
    missing = _item(db, title="Missing")
    blank = _item(db, title="Blank")
    db.execute("UPDATE items SET cover_path = '' WHERE id = ?", (blank,))
    _item(db, title="Dismissed", cover_review_dismissed=1)
    _item(db, title="Covered", cover_path="covers/a.jpg")
    trashed = _item(db, title="Trash")
    item_write.trash_item(db, trashed)
    context = library_attention.summary(db)
    assert context["counts"]["covers"] == 2
    assert {row["item_id"] for row in context["queues"]["covers"]} == {missing, blank}


def test_location_queue_counts_real_live_copies_not_remote_ownership(db):
    physical = _item(db, title="Physical")
    first = item_copies.add_copy(db, physical)
    second = item_copies.add_copy(db, physical)
    trashed_copy = item_copies.add_copy(db, physical)
    item_copies.trash_copy(db, trashed_copy)
    located = _item(db, title="Located")
    location = db.execute("INSERT INTO locations (name) VALUES ('Study')").lastrowid
    item_copies.add_copy(db, located, {"location_id": location})
    _item(db, title="Remote owned", media_type="ebook", owned=1, abs_id="remote-item")
    _item(db, title="Owned without copy", owned=1)
    _item(db, title="Wishlist only", owned=0, wishlisted=True)
    trashed = _item(db, title="Trashed physical")
    item_copies.add_copy(db, trashed)
    item_write.trash_item(db, trashed)
    context = library_attention.summary(db)
    assert context["counts"]["locations"] == 2
    assert {row["copy_id"] for row in context["queues"]["locations"]} == {first, second}
    assert all(row["item_id"] == physical for row in context["queues"]["locations"])


def test_missing_creators_are_meaningful_and_periodicals_do_not_need_byline(db):
    book = _item(db, title="Book without author", authors=None)
    game = _item(db, title="Game without developer", authors=" ", media_type="video_game")
    _item(db, title="Magazine without byline", authors=None, media_type="magazine")
    music = _item(db, title="Release credit", authors=None, media_type="vinyl")
    db.execute("INSERT INTO music_releases (item_id, artist_credit) VALUES (?, '坂本龍一')", (music,))
    _item(db, title="CJK creator", authors="刘慈欣")
    _item(db, title="Accented creator", authors="Björk")
    _item(db, title="Edited anthology", authors="Named Editor - editor")
    trashed = _item(db, title="Deleted authorless book", authors=None)
    item_write.trash_item(db, trashed)
    context = library_attention.summary(db)
    assert context["counts"]["creators"] == 2
    assert {row["item_id"] for row in context["queues"]["creators"]} == {book, game}


def test_periodical_queue_requires_publication_and_some_issue_designation(db):
    publication = db.execute("INSERT INTO periodical_publications (title) VALUES ('Science')").lastrowid
    missing_issue = _item(db, title="Unidentified issue", media_type="magazine", authors=None)
    blank_issue = _item(db, title="Blank issue", media_type="magazine", authors=None)
    db.execute("INSERT INTO periodical_issues (item_id, publication_id, volume) VALUES (?, ?, '8')", (blank_issue, publication))
    for field, value in (("issue_number", "10"), ("issue_date", "2026-02-01"), ("cover_date_label", "Winter 2026")):
        item = _item(db, title=f"Complete {field}", media_type="magazine", authors=None)
        db.execute(f"INSERT INTO periodical_issues (item_id, publication_id, {field}) VALUES (?, ?, ?)", (item, publication, value))
    trashed = _item(db, title="Trashed incomplete issue", media_type="magazine", authors=None)
    item_write.trash_item(db, trashed)
    context = library_attention.summary(db)
    assert context["counts"]["periodicals"] == 2
    assert {row["item_id"] for row in context["queues"]["periodicals"]} == {missing_issue, blank_issue}
    assert context["counts"]["creators"] == 0


def test_source_issues_keep_exact_success_and_attempt_times_separate(db):
    recent = db.execute("SELECT datetime('now')").fetchone()[0]
    old = db.execute("SELECT datetime('now', '-10 days')").fetchone()[0]
    never = _source(db, name="Never checked")
    partial = _source(db, name="Partial", last_status="partial", last_sync_at=old, last_attempt_at=recent)
    failed = _source(db, name="Failed", last_status="error", last_sync_at=recent, last_attempt_at=recent,
                     last_error="secret credential must not render")
    stale = _source(db, name="Stale success", last_status="ok", last_sync_at=old, last_attempt_at=recent)
    _source(db, name="Fresh", last_status="ok", last_sync_at=recent, last_attempt_at=recent)
    _source(db, name="Disabled", enabled=0)
    _source(db, name="Currently checking", last_status="syncing", last_attempt_at=recent)
    context = library_attention.summary(db)
    rows = {row["source_id"]: row for row in context["queues"]["sources"]}
    assert context["counts"]["sources"] == 4
    assert set(rows) == {never, partial, failed, stale}
    assert rows[stale]["last_sync_at"] == old
    assert rows[stale]["last_attempt_at"] == recent
    assert "seven days" in " ".join(rows[stale]["reasons"])
    assert rows[partial]["last_sync_at"] == old
    assert "partial" in " ".join(rows[partial]["reasons"])
    assert "last_error" not in rows[failed]
    assert "credentials_enc" not in rows[failed]


@pytest.mark.parametrize("provider", list(_LEGACY_SETTINGS))
@pytest.mark.parametrize("status", ["never", "error", "partial", "ok"])
def test_legacy_source_health_uses_complete_success_not_scheduler_or_attempt(db, provider, status):
    from app.database import set_setting

    recent = datetime.now(timezone.utc).replace(microsecond=0)
    old = (recent - timedelta(days=10)).isoformat()
    _legacy(db, provider, status=status, success=None if status == "never" else old,
            attempt=None if status == "never" else recent.isoformat())
    set_setting(db, "abs_last_sync", recent.isoformat())  # Not a historical success timestamp.
    context = library_attention.summary(db, category="sources")
    assert context["counts"]["sources"] == 1
    row = context["queues"]["sources"][0]
    assert row["key"] == f"legacy:{provider}"
    assert row["legacy"] is True
    assert row["provider"] == provider
    assert "source_id" not in row
    assert "last_error" not in row
    assert "settings_path" not in row
    assert not any("url" in key or "credential" in key for key in row)
    assert _PRIVATE_URL not in json.dumps(context)
    assert "PRIVATE-TOKEN" not in json.dumps(context)
    reasons = " ".join(row["reasons"])
    if status == "never":
        assert row["last_sync_at"] is None
        assert row["last_attempt_at"] is None
        assert reasons == "Never checked"
    else:
        assert row["last_sync_at"] == old
        assert row["last_attempt_at"] == recent.isoformat()
        assert "seven days" in reasons
        if status == "error":
            assert "failed" in reasons
        elif status == "partial":
            assert "partial" in reasons
        _legacy(db, provider, status="ok", success=recent.isoformat(), attempt=recent.isoformat())
        assert library_attention.summary(db)["counts"]["sources"] == 0


@pytest.mark.parametrize("provider", list(_LEGACY_SETTINGS))
def test_legacy_health_requires_enabled_and_configured_integration(db, provider):
    from app.database import set_setting

    assert library_attention.summary(db)["counts"]["sources"] == 0
    _legacy(db, provider, status="error")
    url_key, credential_key, feature = _LEGACY_SETTINGS[provider]
    set_setting(db, credential_key, "")
    assert library_attention.summary(db)["counts"]["sources"] == 0
    set_setting(db, credential_key, "PRIVATE-TOKEN")
    assert library_attention.summary(db)["counts"]["sources"] == 1
    set_setting(db, f"feature.{feature}", "0")
    assert library_attention.summary(db)["counts"]["sources"] == 0
    set_setting(db, f"feature.{feature}", "1")
    set_setting(db, url_key, "")
    assert library_attention.summary(db)["counts"]["sources"] == 0


def test_abs_environment_only_configuration_is_included_without_leaking_urls(db, monkeypatch):
    monkeypatch.setenv("ABS_URL", _PRIVATE_URL)
    monkeypatch.setenv("ABS_TOKEN", "PRIVATE-TOKEN")
    context = library_attention.summary(db)
    assert [row["key"] for row in context["queues"]["sources"]] == ["legacy:abs"]
    assert _PRIVATE_URL not in json.dumps(context)
    assert "PRIVATE-TOKEN" not in json.dumps(context)


def test_legacy_health_live_counts_exclude_trashed_holdings(db):
    for provider in _LEGACY_SETTINGS:
        _legacy(db, provider)
    for index, trashed in enumerate((False, True)):
        abs_item = _item(db, title=f"ABS {index}", media_type="audiobook", abs_id=f"abs-{index}")
        komga_item = _item(db, title=f"Komga {index}", media_type="comic")
        romm_item = _item(db, title=f"RomM {index}", media_type="video_game")
        db.execute("INSERT INTO komga_records (komga_id, item_id, library_id, kind) VALUES (?, ?, 'library', 'comic')",
                   (f"komga-{index}", komga_item))
        db.execute("INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES (?, ?, 'platform')",
                   (f"romm-{index}", romm_item))
        if trashed:
            for item in (abs_item, komga_item, romm_item):
                item_write.trash_item(db, item)
    context = library_attention.summary(db)
    assert context["counts"]["sources"] == 3
    assert {row["key"]: row["records_count"] for row in context["queues"]["sources"]} == {
        "legacy:abs": 1, "legacy:komga": 1, "legacy:romm": 1,
    }
    assert context["counts"]["unavailable"] == 0  # Sync health is not record absence.


def test_generic_and_legacy_health_keys_counts_and_display_limit_are_distinct(db):
    for provider in _LEGACY_SETTINGS:
        _legacy(db, provider)
    source = _source(db, name="Audiobookshelf")
    context = library_attention.summary(db, category="sources", limit=2)
    assert context["counts"]["sources"] == 4
    assert context["has_more"]["sources"] is True
    assert {row["key"] for row in context["queues"]["sources"]} == {"legacy:abs", f"source:{source}"}


@pytest.mark.parametrize("status", ["never", "ok", "error", "partial"])
def test_manual_link_source_has_no_sync_health_warning_but_preserves_unavailable_queue(db, status):
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    source = _source(db, provider="links", last_status=status, last_sync_at=None if status == "never" else old)
    item = _item(db, title="Explicitly unavailable manual link", media_type="ebook")
    record = _record(db, source, item, "unavailable")
    trashed = _item(db, title="Deleted manual link", media_type="ebook")
    _record(db, source, trashed, "unavailable", "deleted")
    item_write.trash_item(db, trashed)
    context = library_attention.summary(db)
    assert context["counts"]["sources"] == 0
    assert context["queues"]["sources"] == []
    assert context["counts"]["unavailable"] == 1
    assert context["queues"]["unavailable"][0]["record_id"] == record


def test_only_confirmed_source_unavailability_counts_and_trash_is_excluded(db):
    source = _source(db, last_status="error", last_attempt_at="2026-01-02T00:00:00Z")
    available = _item(db, title="Still cached", media_type="ebook")
    _record(db, source, available)
    unknown = _item(db, title="Unknown", media_type="ebook")
    _record(db, source, unknown, "unknown", "unknown")
    missing = _item(db, title="Confirmed unavailable", media_type="ebook")
    record = _record(db, source, missing, "unavailable", "missing")
    trashed = _item(db, title="Deleted unavailable", media_type="ebook")
    _record(db, source, trashed, "unavailable", "trashed")
    item_write.trash_item(db, trashed)
    context = library_attention.summary(db)
    assert context["counts"]["unavailable"] == 1
    assert context["queues"]["unavailable"][0]["record_id"] == record
    assert context["queues"]["unavailable"][0]["item_id"] == missing


def test_summary_limits_are_bounded_and_counts_do_not_change_with_category(db):
    for index in range(120):
        _item(db, title=f"Book {index:03}", authors=None)
    context = library_attention.summary(db, limit=3, category="covers")
    assert context["counts"]["covers"] == 120
    assert context["counts"]["creators"] == 120
    assert len(context["queues"]["covers"]) == 3
    assert context["queues"]["creators"] == []
    assert context["has_more"]["covers"]
    assert len(library_attention.summary(db, limit=999)["queues"]["covers"]) == 100
    assert library_attention.summary(db, limit=0)["queues"]["covers"] == []
    with pytest.raises(ValueError, match="category"):
        library_attention.summary(db, category="arbitrary SQL")


def test_summary_has_no_network_or_catalogue_writes(db, monkeypatch):
    import httpx

    a = _item(db)
    _item(db, media_type="ebook")
    source = _source(db)
    _record(db, source, a)
    for provider in _LEGACY_SETTINGS:
        _legacy(db, provider, status="error")
    monkeypatch.setattr(library_attention.legacy_sources, "get_db", Mock(side_effect=AssertionError("second connection")))
    monkeypatch.setattr(httpx.Client, "request", Mock(side_effect=AssertionError("network on summary")))
    monkeypatch.setattr(httpx.AsyncClient, "request", Mock(side_effect=AssertionError("network on summary")))
    statements = []
    db.set_trace_callback(statements.append)
    before = db.total_changes
    context = library_attention.summary(db)
    db.set_trace_callback(None)
    assert context["counts"]["connections"] == 1
    assert context["counts"]["sources"] == 4
    assert db.total_changes == before
    assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
                   for statement in statements)


def test_connection_counts_respect_current_evidence_dismissals(db):
    a = _item(db, hardcover_book_id=42)
    b = _item(db, media_type="ebook", hardcover_book_id=42)
    candidate = connection_suggestions.candidate_for_pair(db, a, b)
    db.execute("INSERT INTO connection_dismissals (item_a_id, item_b_id, evidence_key) VALUES (?, ?, ?)",
               (a, b, candidate["evidence_key"]))
    context = library_attention.summary(db)
    assert context["counts"]["connections"] == 0
    assert context["dismissed_connection_count"] == 1
    item_write.update_item_fields(db, a, {"title": "New local title"})
    assert library_attention.summary(db)["counts"]["connections"] == 1


def test_page_is_editor_only_and_role_precedes_feature(viewer_client, db):
    from app.features import set_feature_enabled

    set_feature_enabled(db, "connected_library", False)
    db.commit()
    response = viewer_client.get("/needs-attention", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/browse"
    assert "disabled" not in response.text.lower()


def test_page_is_feature_gated(editor_client, db):
    from app.features import set_feature_enabled

    set_feature_enabled(db, "connected_library", False)
    db.commit()
    response = editor_client.get("/needs-attention")
    assert response.status_code == 200  # Existing disabled-feature page contract.
    assert 'data-testid="feature-disabled"' in response.text
    assert 'data-attention-queue=' not in response.text


def test_page_renders_escaped_data_actions_and_exact_source_times(editor_client, db):
    title = '<script>alert("title")</script>'
    item = _item(db, title=title, authors=None)
    source = _source(db, name='<img src=x onerror="steal()">', last_status="partial",
                     last_sync_at="2026-01-01T00:00:00Z", last_attempt_at="2026-02-01T00:00:00Z",
                     last_error="TOKEN-DO-NOT-RENDER")
    _record(db, source, item, "unavailable")
    db.commit()
    response = editor_client.get("/needs-attention")
    assert response.status_code == 200
    assert "&lt;script&gt;" in response.text
    assert "&lt;img" in response.text
    assert title not in response.text
    assert "TOKEN-DO-NOT-RENDER" not in response.text
    assert "Last successful complete check:" in response.text
    assert "2026-01-01T00:00:00Z" in response.text
    assert "Last attempt:" in response.text
    assert "2026-02-01T00:00:00Z" in response.text
    assert 'href="/sources"' in response.text
    assert f'href="/sources/{source}"' not in response.text
    assert f'href="/item/{item}/edit"' in response.text
    assert 'href="/cover-review"' in response.text
    assert 'href="/duplicates"' not in response.text


@pytest.mark.parametrize("client_fixture,is_admin", [("editor_client", False), ("admin_client", True)])
def test_attention_source_links_follow_role_and_never_render_configuration(request, db, client_fixture, is_admin):
    client = request.getfixturevalue(client_fixture)
    source = _source(db, name="Generic sync source", base_url=_PRIVATE_URL, browser_url=_PRIVATE_URL,
                     credentials_enc="PRIVATE-TOKEN", options_json=json.dumps({"private": _PRIVATE_URL}),
                     last_error=_PRIVATE_URL)
    manual = _source(db, name="Manual source", provider="links", base_url=_PRIVATE_URL)
    item = _item(db, title="Live unavailable item")
    _record(db, source, item, "unavailable")
    _record(db, manual, item, "unavailable")
    for provider in _LEGACY_SETTINGS:
        _legacy(db, provider)
    db.commit()
    response = client.get("/needs-attention")
    assert response.status_code == 200
    generic_href = f"/sources/{source}" if is_admin else "/sources"
    manual_href = f"/sources/{manual}" if is_admin else "/sources"
    legacy_href = "/settings" if is_admin else "/sources"
    assert f'href="{generic_href}" data-attention-source="source:{source}"' in response.text
    assert f'href="{generic_href}" data-attention-unavailable-source="{source}"' in response.text
    assert f'href="{manual_href}" data-attention-unavailable-source="{manual}"' in response.text
    for provider in _LEGACY_SETTINGS:
        assert f'href="{legacy_href}" data-attention-source="legacy:{provider}"' in response.text
    assert f'data-attention-source="source:{manual}"' not in response.text
    if not is_admin:
        assert f'href="/sources/{source}"' not in response.text
        assert f'href="/sources/{manual}"' not in response.text
    for private in (_PRIVATE_URL, "PRIVATE-TOKEN", "legacy-private.invalid", "private-password"):
        assert private not in response.text


def test_admin_duplicate_link_is_role_and_collector_feature_gated(admin_client, db):
    from app.features import set_feature_enabled

    assert 'href="/duplicates"' in admin_client.get("/needs-attention").text
    set_feature_enabled(db, "collector", False)
    db.commit()
    assert 'href="/duplicates"' not in admin_client.get("/needs-attention").text


def test_category_and_limit_filter_page_without_changing_counts(editor_client, db):
    for index in range(5):
        _item(db, title=f"Book {index}", authors=None)
    db.commit()
    response = editor_client.get("/needs-attention?category=covers&limit=2")
    assert response.status_code == 200
    assert response.text.count('data-attention-queue="covers"') == 1
    assert 'data-attention-queue="creators"' not in response.text
    assert "Showing the first 2 of 5" in response.text
    assert editor_client.get("/needs-attention?limit=101").status_code == 422
    assert editor_client.get("/needs-attention?category=unknown").status_code == 422


def test_connection_review_forms_escape_provider_metadata_and_include_csrf(editor_client, db):
    title = '<b onclick="attack()">Provider title</b>'
    a = _item(db, title=title, hardcover_book_id=42)
    b = _item(db, title=title, media_type="ebook", hardcover_book_id=42)
    db.commit()
    response = editor_client.get("/needs-attention?category=connections")
    assert response.status_code == 200
    assert title not in response.text
    assert "&lt;b" in response.text
    assert f'data-connection-pair="{a},{b}"' in response.text
    assert 'action="/api/library/connections/accept"' in response.text
    assert 'action="/api/library/connections/dismiss"' in response.text
    assert 'name="evidence_key"' in response.text
    assert 'name="_csrf" value="test-csrf-token-fixed"' in response.text
    assert 'hx-target="#connection-suggestions"' in response.text
