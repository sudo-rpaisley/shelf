"""Independent P2 review regressions on real authenticated cassette routes.

Provider transport is fixture-backed; forms, CSRF, ownership, review verification,
the write funnel and the physical catalogue tables remain production code.
"""

from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from tests.test_cassette_entry_workflow import (
    _form, _one, _rejected, _root, _saved_id, _seed_canonical, _snapshot, _value,
    provider,
)


def _native_controls(form):
    """Serialize successful rendered controls, without supplying a missing mode."""
    controls = []
    for node in form.find():
        name = node.attrs.get("name")
        if not name or "disabled" in node.attrs:
            continue
        if node.tag == "input":
            kind = node.attrs.get("type", "text")
            if kind in {"button", "submit", "reset", "file", "image"}:
                continue
            if kind in {"checkbox", "radio"} and "checked" not in node.attrs:
                continue
            controls.append((name, node.attrs.get("value", "")))
        elif node.tag == "select":
            options = [option for option in node.find("option") if "disabled" not in option.attrs]
            selected = [option for option in options if "selected" in option.attrs]
            for option in selected or options[:1]:
                controls.append((name, option.attrs.get("value", option.text)))
        elif node.tag == "textarea":
            controls.append((name, node.text))
    return controls


def _post_native(client, form, controls=None):
    assert form.attrs.get("method", "").lower() == "post"
    assert _value(form, "_csrf"), "The native form must carry real rendered CSRF"
    client.headers.pop("X-CSRF-Token", None)
    return client.post(form.attrs["action"], content=urlencode(
        _native_controls(form) if controls is None else controls, doseq=True),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)


def _catalogue_state(db):
    state = _snapshot(db)
    for table in ("list_items", "item_links", "cover_sync_blocks"):
        state[table] = [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
    return state


def _assert_manual_item(db, *, title, artist, mode):
    items = db.execute("SELECT * FROM items ORDER BY id").fetchall()
    item = _one(items)
    assert item["title"] == title and item["authors"] == artist
    assert item["media_type"] == "cassette" and item["source"] == "manual"
    assert item["owned"] == int(mode == "add"), "Scan Wishlist must not silently become owned Add"
    membership = db.execute(
        "SELECT l.slug FROM list_items li JOIN lists l ON l.id = li.list_id WHERE li.item_id = ?",
        (item["id"],),
    ).fetchall()
    assert [row[0] for row in membership] == (["wishlist"] if mode == "wishlist" else [])
    assert item["isbn"] is None and item["upc"] is None and item["cover_path"] is None
    assert item["location_id"] is None
    for table in ("item_copies", "music_identifiers", "music_releases", "music_media", "music_tracks"):
        assert not db.execute(f"SELECT * FROM {table}").fetchall(), table
    logged = _one(db.execute("SELECT item_id, mode, result FROM scan_log").fetchall())
    assert tuple(logged) == (item["id"], mode, "wishlisted" if mode == "wishlist" else "added")


@pytest.mark.parametrize("music_enabled", [False, True], ids=["music-off", "music-on"])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_scan_title_search_native_form_preserves_add_or_wishlist_intent(
    editor_client, db, provider, music_enabled, mode,
):
    from app.features import set_feature_enabled

    set_feature_enabled(db, "music", music_enabled)
    db.commit()
    title = f'Synthetic Scan {mode} tape / "A & B"'
    artist = "Synthetic direct-form artist"
    before = _catalogue_state(db)
    response = editor_client.get("/api/title-search", params={
        "q": title, "media_type": "cassette", "mode": mode,
    })
    form = _form(response, "/api/items/manual")
    assert _catalogue_state(db) == before and not provider.calls
    controls = dict(_native_controls(form))
    assert controls["title"] == title and controls["authors"] == ""
    assert controls["media_type"] == "cassette"
    controls["authors"] = artist  # Type into an actual rendered artist control.
    links = [node for node in _root(response).find("a")
             if urlsplit(node.attrs.get("href", "")).path == "/music"]
    assert bool(links) == music_enabled
    if music_enabled:
        query = parse_qs(urlsplit(_one(links).attrs["href"]).query)
        assert query["q"] == [title] and query["format"] == ["cassette"]
    result = _post_native(editor_client, form, list(controls.items()))
    assert result.status_code == 200, result.text
    assert title in _root(result).text
    _assert_manual_item(db, title=title, artist=artist, mode=mode)
    assert controls.get("mode") == mode, "The direct native form needs a genuine intent control"
    assert not provider.calls, "Title handoff and identifier-free manual save need no outbound lookup"


@pytest.mark.parametrize("music_enabled", [False, True], ids=["music-off", "music-on"])
@pytest.mark.parametrize("mode", ["lookup", "unsupported-intent"])
def test_unsupported_scan_title_intent_cannot_create_an_owned_item(
    editor_client, db, provider, music_enabled, mode,
):
    from app.features import set_feature_enabled

    set_feature_enabled(db, "music", music_enabled)
    db.commit()
    before = _catalogue_state(db)
    response = editor_client.get("/api/title-search", params={
        "q": "Synthetic unsupported-mode tape", "media_type": "cassette", "mode": mode,
    })
    if response.status_code == 200:
        for form in _root(response).find("form", action="/api/items/manual"):
            submits = [node for node in form.find("button", type="submit") if "disabled" not in node.attrs]
            if submits:
                _rejected(_post_native(editor_client, form))
    else:
        assert response.status_code in (400, 422), response.text
    assert _catalogue_state(db) == before and not provider.calls


def test_ordinary_music_manual_form_defaults_to_owned_add(editor_client, db, provider):
    query = provider.catalogue["synthetic"]["manual_query"]
    response = editor_client.get("/music", params={
        "q": query["title"], "artist": query["artist"], "format": "cassette", "barcode": "",
    })
    form = _form(response, "/api/items/manual")
    assert not db.execute("SELECT * FROM items").fetchall()
    assert dict(_native_controls(form))["authors"] == query["artist"]
    assert dict(_native_controls(form)).get("mode", "add") == "add"
    result = _post_native(editor_client, form)
    assert result.status_code == 200, result.text
    _assert_manual_item(db, title=query["title"], artist=query["artist"], mode="add")
    assert len(provider.calls) == 2, "Only the actual Music catalogue search should contact providers"


@pytest.fixture
def format_provider(provider):
    """Distinct synthetic exact releases, not relabelled researched cassettes.

    These IDs and facts are offline test data, not claims about public Discogs.
    The existing exact-release dispatcher serves each through its real parser.
    """
    for release_id, format_name, catno in (
        (910000001, "CD", "SYN-CD-REVIEW-01"),
        (910000002, "Vinyl", "SYN-LP-REVIEW-02"),
        (910000003, "Synthetic unclassified carrier", "SYN-UNKNOWN-REVIEW-03"),
    ):
        detail = deepcopy(provider.details["18670711"]["json"])
        detail.update({
            "id": release_id, "title": f"Synthetic {format_name} exact release",
            "uri": f"https://www.discogs.com/release/{release_id}",
            "formats": [{"name": format_name, "qty": "1"}],
            "artists": [{"name": "Synthetic pressing artist"}],
            "labels": [{"name": "Synthetic pressing label", "catno": catno}],
            "country": "Germany", "released": "2004", "year": 2004,
            "images": [{"uri": provider.catalogue["synthetic"]["image_url"], "type": "primary"}],
        })
        provider.details[str(release_id)] = {"status": 200, "json": detail}
    return provider


def _existing_cassette(db, canonical):
    from app.services import cover_sync_policy, discogs_selection, item_write, music_catalog

    if canonical:
        item_id = _seed_canonical(db)
    else:
        item_id = item_write.insert_item(db, title="My handwritten title", authors="Local artist credit",
            media_type="cassette", upc="012345678905", notes="Keep collector notes", source="manual",
            cover_path="covers/local-choice.png", owned=1)
        music_catalog.save_release(db, item_id, {"source": "manual", "media": []})
        music_catalog.add_identifier(db, item_id, "matrix_runout", "SYNTHETIC LOCAL MATRIX")
        db.execute("INSERT INTO item_copies (item_id, copy_number, condition, acquisition_source) "
                   "VALUES (?, 1, 'VG+', 'Local shop')", (item_id,))
    discogs_selection.set_selected_release_id(db, item_id, 3935124)
    cover_sync_policy.block(db, item_id)
    db.commit()
    assert db.execute("SELECT media_type FROM items WHERE id = ?", (item_id,)).fetchone()[0] == "cassette"
    assert bool(music_catalog.get_release(db, item_id)["musicbrainz_release_id"]) == canonical
    return int(item_id)


def _exact_preview(client, *, item_id, release_id, client_format):
    controls = {"provider": "discogs", "release_id": str(release_id), "item_id": str(item_id)}
    if client_format is not None:
        controls["format"] = client_format
    return client.post("/api/music/preview", data=controls)


def _reference_only_apply(client, form):
    # Uncheck every catalogue-field checkbox; keep the genuine reference-only
    # attachment path, not the separate selective-field repair owned elsewhere.
    controls = [(key, value) for key, value in _native_controls(form) if key != "fields"]
    return _post_native(client, form, controls)


def _legacy_server_review(db, user, item_id, detail):
    """A narrowly scoped, *server-issued* pre-fix review fixture for Apply.

    Preview must reject this detail after the fix. Reissuing its old candidate
    with music_review.sign and the real current fingerprint independently tests
    still-valid reviews issued by the earlier server. No key/JWT-file access,
    decoder patch, forged browser token or fabricated HTTP response is involved.
    """
    from app.services import discogs, music_review

    release = discogs.normalise_release(detail)
    candidate = {
        "kind": "discogs", "source_url": release["discogs_url"], "release": release,
        "fields": music_review.fields_for(release),
        "images": [image["url"] for image in release["images"]],
        "image_labels": [image["label"] for image in release["images"]],
    }
    fingerprint = music_review.fingerprint(db, item_id)
    ticket = music_review.sign(candidate, user_id=user["id"], item_id=item_id, fingerprint=fingerprint)
    verified = music_review.verify(ticket, user_id=user["id"])
    assert verified["fingerprint"] == fingerprint and verified["item"] == item_id
    assert verified["candidate"]["release"]["discogs_release_id"] == detail["id"]
    return ticket


@pytest.mark.parametrize("canonical", [False, True], ids=["manual", "mb-backed"])
@pytest.mark.parametrize("release_id,lying_format", [(910000001, "cd"), (910000002, "vinyl")], ids=["cd", "vinyl"])
@pytest.mark.parametrize("omit_format", [False, True], ids=["client-lies", "client-omits"])
def test_existing_cassette_refuses_exact_non_cassette_preview_before_artwork(
    editor_client, db, format_provider, monkeypatch, canonical, release_id, lying_format, omit_format,
):
    from app.services import manual_cover

    item_id = _existing_cassette(db, canonical)
    before = _catalogue_state(db)
    download = AsyncMock(return_value=None)
    monkeypatch.setattr(manual_cover, "download", download)
    response = _exact_preview(editor_client, item_id=item_id, release_id=release_id,
                              client_format=None if omit_format else lying_format)
    assert download.await_count == 0
    assert _catalogue_state(db) == before
    assert format_provider.calls == [{"method": "GET", "url": f"https://api.discogs.com/releases/{release_id}",
                                      "params": {}, "authorization_present": False}]
    _rejected(response)
    assert not _root(response).find("form", action="/api/music/apply-match")
    assert not _root(response).find("img"), "Contradictory detail must not offer artwork previews"


@pytest.mark.parametrize("canonical", [False, True], ids=["manual", "mb-backed"])
@pytest.mark.parametrize("release_id", [910000001, 910000002], ids=["cd", "vinyl"])
@pytest.mark.parametrize("image", ["-1", "0"], ids=["no-artwork", "selected-artwork"])
def test_still_valid_old_server_review_cannot_attach_cd_or_vinyl_to_existing_cassette(
    editor_client, editor_user, db, format_provider, monkeypatch, canonical, release_id, image,
):
    from app.services import manual_cover

    item_id = _existing_cassette(db, canonical)
    ticket = _legacy_server_review(db, editor_user, item_id, format_provider.details[str(release_id)]["json"])
    before = _catalogue_state(db)
    # Returning None deliberately resembles an ordinary download failure. A
    # rejection alone is insufficient: the observable counter MUST stay zero.
    download = AsyncMock(return_value=None)
    monkeypatch.setattr(manual_cover, "download", download)
    response = editor_client.post("/api/music/apply-match", data={
        "ticket": ticket, "item_id": str(item_id), "fields": ["title", "authors", "publisher", "publish_year"],
        "media_type": "cd" if release_id == 910000001 else "vinyl", "image": image,
    }, follow_redirects=False)
    assert download.await_count == 0, "Reject the contradictory legacy review before even attempting artwork"
    assert _catalogue_state(db) == before, "No local row, pressing reference, MB tree or copy may change"
    _rejected(response)
    assert not format_provider.calls, "Apply consumes its signed snapshot; it must not refetch a provider"


@pytest.mark.parametrize("canonical", [False, True], ids=["manual", "mb-backed"])
@pytest.mark.parametrize("client_format", ["cassette", None], ids=["explicit-format", "omitted-format"])
def test_existing_cassette_accepts_known_cassette_reference_without_rewriting_local_state(
    editor_client, db, provider, canonical, client_format,
):
    from app.services import discogs_selection

    item_id = _existing_cassette(db, canonical)
    before = _catalogue_state(db)
    response = _exact_preview(editor_client, item_id=item_id, release_id=18670711, client_format=client_format)
    form = _form(response, "/api/music/apply-match")
    assert "Cassette" in response.text and "Canada" in response.text and "BT 57615" in response.text
    assert _catalogue_state(db) == before
    assert _saved_id(_reference_only_apply(editor_client, form)) == item_id
    after = _catalogue_state(db)
    for table in before.keys() - {"music_identifiers"}:
        assert after[table] == before[table], table
    assert discogs_selection.get_selected_release_id(db, item_id) == 18670711
    assert [tuple(row) for row in db.execute(
        "SELECT identifier_type, value FROM music_identifiers WHERE item_id = ? AND identifier_type != 'discogs_release_id'",
        (item_id,),
    )] == [("matrix_runout", "SYNTHETIC LOCAL MATRIX")]


@pytest.mark.parametrize("client_format", [None, "cd"], ids=["omitted-format", "untrusted-client-format"])
def test_unknown_fetched_format_keeps_existing_explicit_reference_review_behavior(
    editor_client, db, format_provider, client_format,
):
    from app.services import discogs_selection

    item_id = _existing_cassette(db, True)
    before = _catalogue_state(db)
    response = _exact_preview(editor_client, item_id=item_id, release_id=910000003, client_format=client_format)
    form = _form(response, "/api/music/apply-match")
    assert "Synthetic unclassified carrier" in response.text
    assert _saved_id(_reference_only_apply(editor_client, form)) == item_id
    after = _catalogue_state(db)
    for table in before.keys() - {"music_identifiers"}:
        assert after[table] == before[table], table
    assert discogs_selection.get_selected_release_id(db, item_id) == 910000003


def test_same_format_musicbrainz_review_still_attaches_to_a_manual_cassette(editor_client, db, provider):
    from app.services import music_catalog

    item_id = _existing_cassette(db, False)
    before = _catalogue_state(db)
    release = provider.catalogue["synthetic"]["musicbrainz_release"]
    response = editor_client.post("/api/music/preview", data={
        "item_id": str(item_id), "provider": "musicbrainz", "release_id": release["id"], "format": "cassette",
    })
    form = _form(response, "/api/music/apply-match")
    assert "Synthetic canonical track" in response.text
    assert _catalogue_state(db) == before
    assert _saved_id(_reference_only_apply(editor_client, form)) == item_id
    after = _catalogue_state(db)
    for table in ("items", "item_copies", "music_identifiers", "list_items", "cover_sync_blocks"):
        assert after[table] == before[table], table
    saved = music_catalog.get_release(db, item_id)
    assert saved["musicbrainz_release_id"] == release["id"]
    assert saved["media"][0]["format"] == "Cassette"
    assert saved["media"][0]["tracks"][0]["title"] == "Synthetic canonical track"


def test_same_format_token_only_discogs_link_review_remains_useful(editor_client, db, provider):
    from app.routers import settings as settings_router
    from app.services import discogs_selection

    item_id = _existing_cassette(db, True)
    settings_router._upsert_setting(db, "discogs_token", "synthetic-review-token")
    db.commit()
    before = _catalogue_state(db)
    response = editor_client.post("/api/music/preview", data={
        "item_id": str(item_id), "url": "https://www.discogs.com/release/18670711",
    })
    form = _form(response, "/api/music/apply-match")
    assert _saved_id(_reference_only_apply(editor_client, form)) == item_id
    after = _catalogue_state(db)
    for table in before.keys() - {"music_identifiers"}:
        assert after[table] == before[table], table
    assert discogs_selection.get_selected_release_id(db, item_id) == 18670711
    assert provider.calls == [{"method": "GET", "url": "https://api.discogs.com/releases/18670711",
                               "params": {}, "authorization_present": True}]


@pytest.mark.parametrize("new_format", ["cd", "vinyl"])
def test_format_changed_after_real_preview_is_refused_before_artwork(
    editor_client, db, provider, monkeypatch, new_format,
):
    from app.services import item_write, manual_cover

    item_id = _existing_cassette(db, True)
    provider.details["18670711"]["json"]["images"] = [
        {"uri": provider.catalogue["synthetic"]["image_url"], "type": "primary"},
    ]
    response = _exact_preview(editor_client, item_id=item_id, release_id=18670711, client_format="cassette")
    form = _form(response, "/api/music/apply-match")
    item_write.update_item_fields(db, item_id, {"media_type": new_format})
    db.commit()
    before = _catalogue_state(db)
    download = AsyncMock(return_value=None)
    monkeypatch.setattr(manual_cover, "download", download)
    controls = [(key, "0" if key == "image" else value) for key, value in _native_controls(form)]
    refused = _post_native(editor_client, form, controls)
    assert download.await_count == 0
    assert _catalogue_state(db) == before
    _rejected(refused)


def test_apply_revalidates_fresh_item_after_acquiring_the_write_lock(editor_client, db, provider, monkeypatch):
    from app.database import get_db
    from app.routers import music_matching
    from app.services import item_write, manual_cover

    item_id = _existing_cassette(db, True)
    response = _exact_preview(editor_client, item_id=item_id, release_id=18670711, client_format="cassette")
    form = _form(response, "/api/music/apply-match")
    before = _catalogue_state(db)
    locked_edits = []

    class LockedEditConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, statement, *args, **kwargs):
            result = self.connection.execute(statement, *args, **kwargs)
            if statement.strip().upper() == "BEGIN IMMEDIATE":
                # Deterministic injection into this real locked transaction,
                # not a parallel timing race or a patched fingerprint oracle.
                assert self.connection.in_transaction
                item_write.update_item_fields(self.connection, item_id, {"media_type": "cd"})
                locked_edits.append(self.connection.execute(
                    "SELECT media_type FROM items_live WHERE id = ?", (item_id,),
                ).fetchone()[0])
            return result

    @contextmanager
    def fresh_locked_db():
        with get_db() as connection:
            yield LockedEditConnection(connection)

    monkeypatch.setattr(music_matching, "get_db", fresh_locked_db)
    download = AsyncMock(return_value=None)
    monkeypatch.setattr(manual_cover, "download", download)
    refused = _reference_only_apply(editor_client, form)
    assert locked_edits == ["cd"], "The test must reach the actual BEGIN IMMEDIATE boundary"
    assert download.await_count == 0
    _rejected(refused)
    # The injected edit and any attempted attachment roll back together.
    assert _catalogue_state(db) == before
