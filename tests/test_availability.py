import json
import sqlite3
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app import features
from app.database import set_setting
from app.services import availability, item_copies, item_write, komga_sync, media_groups, romm_sync
from tests.conftest import _insert_borrower, _insert_item, _insert_location


_ROW_KEYS = {
    "item_id", "item_title", "media_type", "source_key", "source_name", "kind",
    "state", "location", "url", "action", "last_seen", "detail",
}
_SEEN = "2026-10-01T12:34:56+00:00"


def _item(db, title="A local edition", media_type="book", **fields):
    return _insert_item(db, title=title, isbn=None, media_type=media_type, **fields)


def _copy(db, item_id, number=1, **fields):
    return item_copies.insert_copy(db, {"item_id": item_id, "copy_number": number, **fields})


def _source(db, **fields):
    fields = {
        "provider": "opds", "name": "My OPDS library", "base_url": "http://opds:8080/catalog",
        "browser_url": "https://books.example/", "last_status": "ok", **fields,
    }
    return db.execute(
        f"INSERT INTO source_instances ({', '.join(fields)}) "
        f"VALUES ({', '.join('?' for _ in fields)})", list(fields.values()),
    ).lastrowid


def _record(db, source_id, item_id, **fields):
    fields = {
        "source_id": source_id, "item_id": item_id, "external_id": f"edition-{item_id}",
        "title": "A different remote title", "media_type": "ebook", "format": "EPUB",
        "open_url": "https://books.example/read/edition", "availability": "available",
        "last_seen_at": _SEEN, **fields,
    }
    return db.execute(
        f"INSERT INTO source_records ({', '.join(fields)}) "
        f"VALUES ({', '.join('?' for _ in fields)})", list(fields.values()),
    ).lastrowid


@pytest.fixture
def configured_existing(db):
    # These existing helpers own their connections: configure before the
    # caller opens a write transaction (G3/G18).
    komga_sync.save_configuration(
        url="http://komga:25600", public_url="https://comics.example", api_key="komga-secret",
    )
    romm_sync.save_configuration(
        url="http://romm:8080", public_url="https://games.example", token="romm-secret",
    )
    set_setting(db, "abs_url", "http://abs:80")
    set_setting(db, "abs_public_url", "https://audio.example")
    set_setting(db, "abs_token", "abs-secret")
    db.commit()


def _existing_items(db):
    audio = _item(db, "Local audio", "audiobook", abs_id="audio/1?format=audio")
    comic = _item(db, "Local comic", "comic")
    game = _item(db, "Local game", "video_game")
    db.execute(
        "INSERT INTO komga_records (komga_id, item_id, library_id, kind, updated_at) "
        "VALUES ('comic/1?format=epub', ?, 'library', 'comic', ?)", (comic, _SEEN),
    )
    db.execute(
        "INSERT INTO romm_records (romm_id, item_id, platform_id, updated_at) "
        "VALUES ('game/1?format=rom', ?, 'platform', ?)", (game, _SEEN),
    )
    return {"abs": audio, "komga": comic, "romm": game}


def test_owned_or_provider_label_without_holding_is_not_a_physical_copy(db):
    owned = _item(db, owned=1)
    unowned = _item(db, "Not owned", owned=0)
    labelled = _item(db, "Only a source label", source="komga", owned=1)
    before = db.total_changes

    assert availability.availability_for_items(db, [owned, unowned, labelled]) == {
        owned: [], unowned: [], labelled: [],
    }
    assert db.total_changes == before
    assert db.execute("SELECT COUNT(*) FROM copies_live").fetchone()[0] == 0


def test_map_contains_only_requested_live_items_including_empty_ones(db):
    wanted = _item(db, "Requested")
    other = _item(db, "Not requested")
    trashed = _item(db, "Trashed")
    _copy(db, other)
    item_write.trash_item(db, trashed)

    assert availability.availability_for_items(db, [wanted, wanted, trashed, 999999]) == {wanted: []}
    assert availability.availability_for_items(db, []) == {}


def test_each_live_copy_has_its_own_location_and_the_complete_row_contract(db):
    item_id = _item(db, owned=0)
    shelf = _insert_location(db, "Study / Shelf 1")
    storage = _insert_location(db, "Storage")
    _copy(db, item_id, location_id=shelf)
    _copy(db, item_id, number=2, location_id=storage)
    _copy(db, item_id, number=3)

    rows = availability.availability_for_items(db, [item_id])[item_id]

    assert len(rows) == 3
    assert all(set(row) == _ROW_KEYS for row in rows)
    assert [row["location"] for row in rows] == ["Study / Shelf 1", "Storage", None]
    assert all(row["state"] == "available" and row["kind"] == "physical" for row in rows)
    assert all(row["source_key"] == "physical" and row["url"] is None for row in rows)
    assert all(row["last_seen"] is None for row in rows)  # edits are not observations
    assert rows[2]["detail"] == "Copy 3"


@pytest.mark.parametrize("checked_in, state", [(None, "lent"), ("2026-10-02", "available")])
def test_current_item_loan_is_not_mistaken_for_an_available_copy(db, checked_in, state):
    item_id = _item(db)
    _copy(db, item_id)
    _copy(db, item_id, number=2)
    borrower = _insert_borrower(db)
    db.execute(
        "INSERT INTO checkouts (item_id, borrower_id, checked_in) VALUES (?, ?, ?)",
        (item_id, borrower, checked_in),
    )

    rows = availability.availability_for_items(db, [item_id])[item_id]

    assert [row["state"] for row in rows] == [state, state]
    if state == "lent":
        assert all("does not identify a copy" in row["detail"] for row in rows)


def test_missing_mark_is_per_copy_and_is_not_hidden_by_a_loan(db):
    item_id = _item(db)
    _copy(db, item_id, missing_since="2026-09-15")
    _copy(db, item_id, number=2)
    borrower = _insert_borrower(db)
    db.execute("INSERT INTO checkouts (item_id, borrower_id) VALUES (?, ?)", (item_id, borrower))

    rows = availability.availability_for_items(db, [item_id])[item_id]

    assert [row["state"] for row in rows] == ["missing", "lent"]
    assert "2026-09-15" in rows[0]["detail"]


def test_trashed_copy_and_trashed_item_never_supply_availability(db, configured_existing):
    live = _item(db, "Live")
    removed = _copy(db, live)
    _copy(db, live, number=2)
    trashed = _existing_items(db)["abs"]
    _copy(db, trashed)
    source_id = _source(db)
    _record(db, source_id, trashed)
    item_copies.trash_copy(db, removed)
    item_write.trash_item(db, trashed)

    result = availability.availability_for_items(db, [live, trashed])

    assert list(result) == [live]
    assert len(result[live]) == 1
    assert result[live][0]["detail"] == "Copy 2"
    assert availability.available_here(db, trashed) is None
    assert availability.available_here(db, 999999) is None


def test_format_component_is_transitive_undirected_cycle_safe_and_format_only(db):
    a, b, c, film, film_format, related, beyond_related, trashed, beyond_trash = [
        _item(db, title) for title in (
            "Alpha", "Bravo", "Charlie", "Film", "Film format", "Related", "Beyond related",
            "Trashed bridge", "Beyond trash",
        )
    ]
    for left, right in ((a, b), (b, c), (c, a), (film, film_format),
                        (related, beyond_related), (a, trashed), (trashed, beyond_trash)):
        media_groups.link_items(db, left, right, link_type="format")
    media_groups.link_items(db, a, film, link_type="adaptation")
    media_groups.link_items(db, c, related, link_type="related")
    item_write.trash_item(db, trashed)

    assert availability.format_item_ids(db, b) == [a, b, c]
    assert availability.format_item_ids(db, trashed) == []
    assert availability.format_item_ids(db, 999999) == []


def test_available_here_uses_local_metadata_and_only_format_component_holdings(db):
    printed = _item(db, "Local printed title")
    audio = _item(db, "Local audio title", "audiobook")
    ebook = _item(db, "Local ebook title", "ebook")
    film = _item(db, "Excluded adaptation", "digital_video")
    media_groups.link_items(db, printed, audio, link_type="format")
    media_groups.link_items(db, audio, ebook, link_type="format")
    media_groups.link_items(db, audio, film, link_type="adaptation")
    _copy(db, printed)
    source_id = _source(db)
    for item_id in (audio, ebook, film):
        _record(db, source_id, item_id)

    context = availability.available_here(db, printed)

    assert set(context) == {"item", "rows", "media_types"}
    assert context["item"]["title"] == "Local printed title"
    assert {row["item_id"] for row in context["rows"]} == {printed, audio, ebook}
    assert context["rows"][0]["item_id"] == printed
    assert "A different remote title" not in json.dumps(context)
    # The batch primitive does not itself pull in neighbouring editions.
    assert set(availability.availability_for_items(db, [printed])) == {printed}


def test_enabled_source_record_proves_digital_access_not_physical_ownership(db):
    item_id = _item(db, owned=0)
    source_id = _source(db, name="A named source")
    _record(db, source_id, item_id)

    rows = availability.availability_for_items(db, [item_id])[item_id]

    assert len(rows) == 1
    assert set(rows[0]) == _ROW_KEYS
    assert rows[0] == {
        "item_id": item_id, "item_title": "A local edition", "media_type": "book",
        "source_key": f"source:{source_id}", "source_name": "A named source", "kind": "digital",
        "state": "available", "location": None, "url": "https://books.example/read/edition",
        "action": "Read", "last_seen": _SEEN,
        "detail": "Cached source record; no live connection check.",
    }
    assert db.execute("SELECT owned FROM items_live WHERE id = ?", (item_id,)).fetchone()[0] == 0


@pytest.mark.parametrize("enabled, status, cached_state, expected", [
    (0, "ok", "available", "unknown"),
    (0, "ok", "unavailable", "unavailable"),
    (1, "error", "available", "unknown"),
    (1, "error", "unavailable", "unavailable"),
    (1, "partial", "available", "unknown"),
    (1, "ok", "unknown", "unknown"),
])
def test_disabled_or_failed_sources_retain_cached_records_without_claiming_availability(
    db, enabled, status, cached_state, expected,
):
    item_id = _item(db)
    error = "Authorization failed: token=never-render-this" if status == "error" else None
    source_id = _source(db, enabled=enabled, last_status=status, last_error=error)
    record_id = _record(db, source_id, item_id, availability=cached_state,
                        metadata_json='{"password":"never-render-metadata"}')
    before = db.total_changes

    row = availability.availability_for_items(db, [item_id])[item_id][0]

    assert row["state"] == expected
    assert row["last_seen"] == _SEEN
    assert bool(row["url"]) == bool(enabled)
    assert "never-render" not in json.dumps(row)
    assert db.total_changes == before
    stored = db.execute("SELECT availability, last_seen_at FROM source_records WHERE id = ?",
                        (record_id,)).fetchone()
    assert tuple(stored) == (cached_state, _SEEN)


@pytest.mark.parametrize("media_type, action", [
    ("ebook", "Read"), ("audiobook", "Listen"), ("digital_video", "Watch"),
    ("tv_series", "Watch"), ("video_game", "Play"), ("digital_music", "Listen"),
])
def test_source_actions_follow_the_record_medium(db, media_type, action):
    item_id = _item(db)
    _record(db, _source(db), item_id, media_type=media_type)

    assert availability.availability_for_items(db, [item_id])[item_id][0]["action"] == action


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,test", "file:///etc/passwd", "//outside.example/read",
    "https://user:password@example.test/read", "https://user@example.test/read",
    "https://@example.test/read", "https://example.test:invalid/read",
    "https://example.test:70000/read", "https://example.test/\\outside",
    "https://example.test/rea\td", "https://example.test/read?token=link-secret",
    "https://example.test/read?api_key=link-secret",
    "https://example.test/read?X-Plex-Token=link-secret",
    "https://example.test/read?id=42;token=link-secret",
    "https://example.test/read?authentication=link-secret",
    "https://example.test/#/read?access_token=link-secret",
    "https://example.test/#access_token=link-secret",
])
def test_unsafe_or_credential_bearing_record_urls_are_not_exposed(db, url):
    item_id = _item(db)
    _record(db, _source(db), item_id, open_url=url)

    row = availability.availability_for_items(db, [item_id])[item_id][0]

    assert row["url"] is None
    assert row["action"] is None
    assert "link-secret" not in json.dumps(row)
    assert "password" not in json.dumps(row)


@pytest.mark.parametrize("url", [
    "http://localhost:8096/read?id=42", "http://192.168.1.2/read/42",
    "http://[::1]:8096/read/42", "https://media.example/web/#/details?id=42",
])
def test_lan_urls_and_nonsecret_query_or_spa_routes_remain_usable(db, url):
    item_id = _item(db)
    _record(db, _source(db), item_id, open_url=url)

    assert availability.availability_for_items(db, [item_id])[item_id][0]["url"] == url


def test_relative_record_links_use_only_a_safe_browser_root(db):
    item_id = _item(db)
    source_id = _source(db, browser_url="https://books.example/library")
    _record(db, source_id, item_id, open_url="read/42")
    row = availability.availability_for_items(db, [item_id])[item_id][0]
    assert row["url"] == "https://books.example/library/read/42"

    db.execute("UPDATE source_instances SET browser_url = 'https://user:password@books.example' "
               "WHERE id = ?", (source_id,))
    row = availability.availability_for_items(db, [item_id])[item_id][0]
    assert row["url"] is None
    assert "password" not in json.dumps(row)


def test_existing_integrations_reuse_browser_roots_quote_ids_and_do_not_leak_secrets(db, configured_existing):
    ids = _existing_items(db)

    result = availability.availability_for_items(db, ids.values())

    expected = {
        "abs": ("https://audio.example/item/audio%2F1%3Fformat%3Daudio", "Listen"),
        "komga": ("https://comics.example/book/comic%2F1%3Fformat%3Depub", "Read"),
        "romm": ("https://games.example/rom/game%2F1%3Fformat%3Drom", "Play"),
    }
    for key, item_id in ids.items():
        assert len(result[item_id]) == 1
        row = result[item_id][0]
        assert row["source_key"] == key
        assert row["state"] == "available" and row["kind"] == "digital"
        assert (row["url"], row["action"]) == expected[key]
        assert row["last_seen"] == (None if key == "abs" else _SEEN)
    assert "secret" not in json.dumps(result)
    assert db.execute("SELECT COUNT(*) FROM copies_live").fetchone()[0] == 0


@pytest.mark.parametrize("provider, feature", [("abs", "abs_sync"), ("komga", "komga"), ("romm", "romm")])
def test_existing_source_feature_flags_suppress_both_old_and_generic_holdings(
    db, configured_existing, provider, feature,
):
    ids = _existing_items(db)
    source_id = _source(db, provider="audiobookshelf" if provider == "abs" else provider)
    _record(db, source_id, ids[provider])
    features.set_feature_enabled(db, feature, False)
    db.commit()

    result = availability.availability_for_items(db, ids.values())

    assert result[ids[provider]] == []
    assert all(result[item_id] for key, item_id in ids.items() if key != provider)


@pytest.mark.parametrize("provider", ["abs", "komga", "romm"])
@pytest.mark.parametrize("setting_suffix", ["url", "public_url"])
def test_credentials_or_unsafe_configured_urls_never_become_existing_service_links(
    db, configured_existing, provider, setting_suffix,
):
    ids = _existing_items(db)
    set_setting(db, f"{provider}_public_url", "")
    set_setting(db, f"{provider}_{setting_suffix}",
                "https://user:password@example.test" if setting_suffix == "url" else "javascript:alert(1)")
    db.commit()

    row = availability.availability_for_items(db, [ids[provider]])[ids[provider]][0]

    assert row["state"] == "unknown" and row["url"] is None
    assert "password" not in json.dumps(row)
    assert "javascript" not in json.dumps(row)


@pytest.mark.parametrize("invalid", [0, -1, 2**63, True, "1", "1) OR 1=1"])
def test_batch_ids_must_be_valid_positive_sqlite_integers(db, invalid):
    with pytest.raises(ValueError, match="positive integers"):
        availability.availability_for_items(db, [invalid])


def test_large_batches_chunk_every_holding_query_to_the_connection_limit(db, configured_existing):
    source_id = _source(db)
    ids = [_item(db, f"Edition {number}") for number in range(425)]
    for item_id in ids:
        _copy(db, item_id)
        _record(db, source_id, item_id)
    db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)

    result = availability.availability_for_items(db, (item_id for item_id in ids))

    assert list(result) == ids
    assert all(len(rows) == 2 for rows in result.values())


def test_resolvers_do_not_write_or_make_network_requests(db, configured_existing, monkeypatch):
    ids = _existing_items(db)
    printed = _item(db, "Printed")
    _copy(db, printed)
    media_groups.link_items(db, printed, ids["abs"], link_type="format")
    _record(db, _source(db), printed)
    db.commit()

    def no_network(*args, **kwargs):
        pytest.fail("Availability reads must not make network requests")

    monkeypatch.setattr(httpx.AsyncClient, "request", no_network)
    monkeypatch.setattr(httpx.Client, "request", no_network)
    before = db.total_changes
    db.execute("PRAGMA query_only = ON")
    try:
        assert availability.available_here(db, printed)["rows"]
        assert availability.availability_for_items(db, ids.values())
        assert availability.format_item_ids(db, printed) == sorted([printed, ids["abs"]])
    finally:
        db.execute("PRAGMA query_only = OFF")
    assert db.total_changes == before
    assert not db.in_transaction


class _Fragment(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.roots = []
        self.external_links = []
        self.forms = []
        self.inputs = []
        self.selects = []
        self.buttons = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id") == "available-here":
            self.roots.append(attrs)
        if tag == "a" and attrs.get("target") == "_blank":
            self.external_links.append(attrs)
        if tag in {"form", "input", "select", "button"}:
            getattr(self, {"form": "forms", "input": "inputs", "select": "selects", "button": "buttons"}[tag]).append(attrs)


def test_viewer_fragment_has_rechecked_links_and_only_own_preference_controls(viewer_client, viewer_user, db):
    item_id = _item(db, "Reader's local edition", "ebook")
    source_id = _source(db, name="My source")
    _record(db, source_id, item_id)
    db.commit()
    catalogue_tables = ("items", "source_instances", "source_records", "item_copies")

    def catalogue():
        return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]
                for table in catalogue_tables}

    before = catalogue()

    response = viewer_client.get(f"/api/library/items/{item_id}/availability")

    assert response.status_code == 200
    fragment = _Fragment(response.text)
    assert len(fragment.roots) == 1
    assert fragment.roots[0]["data-testid"] == "available-here"
    assert "hx-swap-oob" not in fragment.roots[0]
    # The action picker and the exact holding each re-resolve on navigation.
    # Neither exposes a caller-provided destination or skips link protections.
    assert len(fragment.external_links) == 2
    targets = [urlsplit(link["href"]) for link in fragment.external_links]
    assert all(not target.scheme and not target.netloc and target.path == f"/api/library/items/{item_id}/open/Read"
               for target in targets)
    assert targets[0].query == ""
    binding = parse_qs(targets[1].query, strict_parsing=True)
    assert set(binding) == {"binding"} and len(binding["binding"]) == 1
    assert len(binding["binding"][0]) == 64 and set(binding["binding"][0]) <= set("0123456789abcdef")
    for link in fragment.external_links:
        assert set(link["rel"].split()) == {"noopener", "noreferrer"}
        assert link["referrerpolicy"] == "no-referrer"
        opened = viewer_client.get(link["href"], follow_redirects=False)
        assert opened.status_code == 303 and opened.headers["location"] == "https://books.example/read/edition"
        assert opened.headers["referrer-policy"] == "no-referrer"
    assert "Available here" in response.text and "My source" in response.text
    assert "Read</a>" in response.text and _SEEN in response.text
    # Viewers may change their own F4 preference, never catalogue metadata.
    preference_path = f"/api/library/items/{item_id}/preferred-source"
    assert len(fragment.forms) == len(fragment.buttons) == len(fragment.selects) == 1
    assert fragment.forms[0]["method"] == "post"
    assert fragment.forms[0]["action"] == fragment.forms[0]["hx-post"] == preference_path
    assert fragment.buttons[0]["type"] == "submit"
    assert fragment.selects[0]["name"] == "source_key"
    assert {entry["name"] for entry in fragment.inputs} == {"_csrf", "action"}
    assert all(entry["type"] == "hidden" for entry in fragment.inputs)
    assert next(entry["value"] for entry in fragment.inputs if entry["name"] == "action") == "Read"
    assert next(entry["value"] for entry in fragment.inputs if entry["name"] == "_csrf") == viewer_client.cookies.get("csrf_token")
    assert db.execute("SELECT COUNT(*) FROM user_source_preferences").fetchone()[0] == 0
    assert catalogue() == before

    saved = viewer_client.post(preference_path, data={"action": "Read", "source_key": f"source:{source_id}", "user_id": "99999"})
    assert saved.status_code == 200
    assert [tuple(row) for row in db.execute("SELECT user_id, item_id, action, source_key FROM user_source_preferences")] == [
        (viewer_user["id"], item_id, "Read", f"source:{source_id}"),
    ]
    denied = viewer_client.post(f"/api/items/{item_id}", data={"title": "Forbidden catalogue edit"})
    assert denied.status_code == 403
    assert catalogue() == before
    assert "A different remote title" not in response.text


def test_fragment_can_render_the_resolver_context_directly_and_optionally_oob(db):
    from starlette.templating import Jinja2Templates

    item_id = _item(db)
    context = availability.available_here(db, item_id)
    templates = Jinja2Templates(directory="app/templates")
    templates.env.globals["_"] = lambda text: text
    template = templates.env.get_template("fragments/available_here.html")

    ordinary = template.render(**context)
    refreshed = template.render(**context, available_here_oob=True)

    assert _Fragment(ordinary).roots[0].get("hx-swap-oob") is None
    assert _Fragment(refreshed).roots[0]["hx-swap-oob"] == "true"
    assert "No physical copies or connected source records" in ordinary


@pytest.mark.parametrize("trashed", [False, True])
def test_availability_endpoint_is_404_for_missing_or_trashed_items(viewer_client, db, trashed):
    item_id = _item(db) if trashed else 999999
    if trashed:
        item_write.trash_item(db, item_id)
        db.commit()

    assert viewer_client.get(f"/api/library/items/{item_id}/availability").status_code == 404


def test_connected_library_gate_refuses_availability_before_running_the_resolver(viewer_client, db, monkeypatch):
    features.set_feature_enabled(db, "connected_library", False)
    db.commit()

    def unexpected_read(*args, **kwargs):
        pytest.fail("Disabled availability endpoint must not resolve holdings")

    monkeypatch.setattr(availability, "available_here", unexpected_read)
    response = viewer_client.get("/api/library/items/999999/availability")

    assert response.status_code == 403
    assert response.json()["feature"] == "connected_library"


def test_availability_route_declares_viewer_before_its_feature_gate():
    from app.auth import ROLE_LEVELS
    from app.routers.availability import router

    route = next(route for route in router.routes if route.path == "/api/library/items/{item_id}/availability")
    dependencies = route.dependant.dependencies
    assert len(dependencies) == 2
    assert dependencies[0].call.__closure__[0].cell_contents == ROLE_LEVELS["viewer"]
    assert getattr(dependencies[1].call, "feature_key", None) == "connected_library"
