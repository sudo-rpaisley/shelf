"""Real authenticated search -> exact preview -> signed selective Apply.

Only the outbound provider boundary is fixture-backed. No route, rendered
review, signature, insertion funnel, CSRF middleware or identity check is faked.
"""

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest


@pytest.fixture
def provider(monkeypatch):
    from app.services import outbound
    from tests.e2e.cassette_asgi import FixtureCatalogue

    fixture = FixtureCatalogue()
    monkeypatch.setattr(outbound, "fetch", fixture.fetch)
    yield fixture
    fixture.assert_clean()


def _root(response):
    from tests.test_media_page_cards import _HTML

    assert response.status_code == 200, response.text
    return _HTML(response.text).root


def _one(nodes):
    assert len(nodes) == 1, f"Expected one real form/control, found {len(nodes)}"
    return nodes[0]


def _form(response, action):
    return _one(_root(response).find("form", action=action))


def _value(form, name):
    return _one(form.find("input", name=name)).attrs.get("value", "")


def _snapshot(db):
    tables = ("items", "item_copies", "music_releases", "music_media", "music_tracks", "music_identifiers")
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}


def _search(client, *, title="Hits Out of Hell", artist="Meat Loaf"):
    return client.get("/music", params={"q": title, "artist": artist, "format": "cassette", "barcode": ""})


def _assert_provider_failure_alert(response, provider_name, *, rate_limited=False):
    import re

    alert = _one(_root(response).find(role="alert"))
    message = " ".join(alert.text.casefold().split())
    assert provider_name.casefold() in message
    assert not re.search(r"\bno\s+(?:matching\s+)?(?:releases?|results?|matches?|match)\b", message), (
        "Provider failure must not be presented as no matches"
    )
    assert any(phrase in message for phrase in (
        "could not", "failed", "error", "reject", "unavailable", "limit", "quota", "try again",
    )), "The real HTTP alert must describe a failure, not merely name the provider"
    if rate_limited:
        assert any(phrase in message for phrase in ("limit", "quota", "too many requests"))


def _assert_native_added_receipt(response, expected_title):
    """The existing native receipt contract, not prose or a toast substring."""
    assert response.status_code == 200
    root = _root(response)
    receipt = _one(root.find(cls="scan-result"))
    assert receipt.attrs.get("data-scan-status") == "added"
    assert _one(receipt.find(**{"data-scan-title": None})).text.strip() == expected_title
    assert not root.find(**{"data-scan-status": "error"})
    assert not any("data-scan-error" in node.attrs for node in root.find())


def _preview(client, release_id=18670711, *, item_id=0, format="cassette", **extra):
    return client.post("/api/music/preview", data={
        "provider": "discogs", "release_id": str(release_id), "format": format, "item_id": str(item_id), **extra,
    })


def _review(client, release_id=18670711, **kwargs):
    response = _preview(client, release_id, **kwargs)
    form = _form(response, "/api/music/apply-match")
    assert _value(form, "ticket")
    return response, form


def _apply(client, form, *, fields=("title", "authors", "publish_year"), **extra):
    return client.post("/api/music/apply-match", data={
        "ticket": _value(form, "ticket"), "item_id": _value(form, "item_id"),
        "fields": list(fields), "image": "-1", "media_type": "cassette", "owned": "1", **extra,
    }, follow_redirects=False)


def _saved_id(response):
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/music/item/")
    return int(urlsplit(location).path.rsplit("/", 1)[1])


def _rejected(response):
    assert response.status_code == 200, response.text
    assert not response.headers.get("HX-Redirect")
    assert _root(response).find(role="alert"), response.text


def _seed_canonical(db):
    from app.services import item_write, music_catalog, musicbrainz
    from tests.e2e.cassette_asgi import load_catalogue

    item_id = item_write.insert_item(db, title="My handwritten title", authors="Local artist credit",
        media_type="cassette", upc="012345678905", notes="Keep collector notes", source="manual",
        cover_path="covers/local-choice.png", owned=1)
    release = musicbrainz.normalise_release(load_catalogue()["synthetic"]["musicbrainz_release"])
    music_catalog.save_release(db, item_id, release)
    music_catalog.add_identifier(db, item_id, "matrix_runout", "SYNTHETIC LOCAL MATRIX")
    db.execute("INSERT INTO item_copies (item_id, copy_number, condition, acquisition_source) VALUES (?, 1, 'VG+', 'Local shop')", (item_id,))
    db.commit()
    return int(item_id)


def _synthetic_image(provider, release_id=18670711):
    detail = provider.details[str(release_id)]["json"]
    detail["images"] = [{"uri": provider.catalogue["synthetic"]["image_url"], "type": "primary"}]


def _assert_researched_http_tracklist(response, release_id, *, fresh=False):
    from tests.e2e.cassette_asgi import verified_track_facts
    from tests.test_cassette_provider_fallback import _assert_verified_normalized_release

    root = _root(response)
    if fresh:
        release = response.context["selected_release"]
    else:
        release = _one(response.context["candidates"])["release"]
    _assert_verified_normalized_release(release, release_id)
    expected = verified_track_facts(release_id)
    listed = [node.text.strip() for listing in root.find("ol") for node in listing.find("li")]
    assert len(listed) == len(expected)
    assert all(number in text and title in text
               for (number, title, duration, duration_ms), text in zip(expected, listed, strict=True))
    if release_id == 18670711:
        assert not any("Modern Girl" in text for text in listed)
    elif release_id == 3935124:
        assert not any("All Revved Up With No Place To Go" in text for text in listed)
    else:
        assert not any("Paradise By The Dashboard Light" in text for text in listed), "Show the three indexed parts, not a duplicate parent track"
    return listed


def _seed_selected_reference(db, release_id=18670711, *, source="musicbrainz"):
    from app.services import discogs_selection, item_write, music_catalog

    if source == "musicbrainz":
        item_id = _seed_canonical(db)
    else:
        item_id = item_write.insert_item(db, title="Synthetic locally saved reference", media_type="cassette", source=source)
        music_catalog.save_release(db, item_id, {"source": source, "media": []})
    discogs_selection.set_selected_release_id(db, item_id, release_id)
    db.commit()
    return int(item_id)


def test_blank_barcode_search_shows_ambiguous_attributed_pressings_without_writes(editor_client, db, provider):
    rows = provider.discogs_search["json"]["results"]
    rows[1]["barcode"] = [provider.catalogue["synthetic"]["barcode"]]
    before = _snapshot(db)
    response = _search(editor_client)
    root = _root(response)
    forms = root.find("form", action="/api/music/preview")
    candidates = [form for form in forms if form.find("input", name="provider", value="discogs")]
    assert len(candidates) == 3, "The real Music page did not render all three explicit Discogs preview forms"
    assert {_value(form, "release_id") for form in candidates} == {"3935124", "8297774", "18670711"}
    assert all(_value(form, "format") == "cassette" for form in candidates)
    assert all(form.attrs.get("method") == "post" and _value(form, "_csrf") for form in candidates)
    assert "Discogs" in response.text and all(fact in response.text for fact in ("UK", "Europe", "Canada", "BT 57615", "EPC 40-26156"))
    direct_adds = root.find("form", action="/api/music/add")
    assert not any(_value(form, "release_id").isdigit() for form in direct_adds)
    assert _snapshot(db) == before
    assert len(provider.calls) == 2 and "barcode" not in provider.calls[-1]["params"]


def test_chosen_exact_canadian_pressing_is_reviewed_then_saved_and_reloaded_without_a_copy(editor_client, editor_user, db, provider):
    from app import auth
    from app.services import discogs_selection, music_catalog, music_review

    before = _snapshot(db)
    searched = _search(editor_client)
    candidate = _one([form for form in _root(searched).find("form", action="/api/music/preview")
                      if form.find("input", name="provider", value="discogs")
                      and form.find("input", name="release_id", value="18670711")])
    # Submit the actual candidate controls, not a fabricated preview or token.
    response = editor_client.post(candidate.attrs["action"], data={
        name: _value(candidate, name) for name in ("provider", "release_id", "format", "_csrf")
    })
    form = _form(response, "/api/music/apply-match")
    publisher_choice = _one(form.find("input", name="fields", value="publisher"))
    assert publisher_choice.attrs.get("type") == "checkbox"
    assert response.context["candidates"][0]["fields"]["publisher"] == "Epic"
    assert "Canada" in response.text and "1995" in response.text and "BT 57615" in response.text
    assert "EPC 40-26156" not in response.text and "https://www.discogs.com/release/18670711" in response.text
    _assert_researched_http_tracklist(response, 18670711)
    select = _one(form.find("select", name="media_type"))
    assert _one(select.find("option", selected=None)).attrs["value"] == "cassette"
    assert _snapshot(db) == before
    ticket = _value(form, "ticket")
    assert music_review.verify(ticket, user_id=editor_user["id"])["item"] == 0
    assert auth.decode_token(ticket) is None, "A review ticket must not become a login token"
    item_id = _saved_id(_apply(editor_client, form))
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["title"] == "Hits Out Of Hell" and row["authors"] == "Meat Loaf"
    assert row["publish_year"] == 1995 and row["media_type"] == "cassette" and row["source"] == "discogs"
    assert row["upc"] is None and row["isbn"] is None and row["location_id"] is None and row["cover_path"] is None
    assert row["publisher"] is None, "Unchecked publisher must remain NULL after creation"
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 0
    release = music_catalog.get_release(db, item_id)
    assert release["metadata_source"] == "discogs"
    assert release["musicbrainz_release_id"] is None and release["musicbrainz_release_group_id"] is None
    for field in ("artist_credit", "release_type", "release_status", "release_date", "first_release_date",
                  "country", "label", "catalog_number", "packaging", "media_count", "format_summary"):
        assert release[field] is None, f"Only the minimal container/reference is canonical, not Discogs {field}"
    assert not release["media"] and db.execute("SELECT COUNT(*) FROM music_tracks").fetchone()[0] == 0
    assert discogs_selection.get_selected_release_id(db, item_id) == 18670711
    saved = _snapshot(db)
    page = editor_client.get(f"/music/item/{item_id}")
    assert page.status_code == 200 and "Hits Out Of Hell" in page.text and "Discogs" in page.text
    assert "musicbrainz.org/release/18670711" not in page.text
    assert db.execute("SELECT publisher FROM items WHERE id = ?", (item_id,)).fetchone()[0] is None
    fresh = editor_client.get(f"/api/music/items/{item_id}/discogs", params={"public_details": "1"})
    assert fresh.status_code == 200 and "Canada" in fresh.text and "BT 57615" in fresh.text
    assert "Data provided by Discogs" in fresh.text
    _assert_researched_http_tracklist(fresh, 18670711, fresh=True)
    assert _snapshot(db) == saved
    assert provider.calls[-1]["url"] == "https://api.discogs.com/releases/18670711"
    assert not any(call["authorization_present"] for call in provider.calls)


def test_new_create_excludes_available_unselected_publisher_from_real_review(editor_client, db, provider):
    """Reach the shared new-item Apply branch even on the feature-absent baseline.

    The retained exact MusicBrainz interface supplies the real signed review;
    its label is explicitly synthetic. No review HTML, token or insert is faked.
    The Discogs creation and browser tests independently retain the same oracle.
    """
    release = provider.catalogue["synthetic"]["musicbrainz_release"]
    label = "Synthetic unselected create label"
    release["label-info"] = [{"label": {"name": label}}]
    response = editor_client.post("/api/music/preview", data={"release_id": release["id"], "item_id": "0"})
    form = _form(response, "/api/music/apply-match")
    publisher_choice = _one(form.find("input", name="fields", value="publisher"))
    assert publisher_choice.attrs.get("type") == "checkbox"
    assert response.context["candidates"][0]["fields"]["publisher"] == label
    applied = _apply(editor_client, form)
    submitted = parse_qs(applied.request.content.decode())
    assert submitted["fields"] == ["title", "authors", "publish_year"]
    assert "publisher" not in submitted["fields"]
    item_id = _saved_id(applied)
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["title"] == release["title"] and row["authors"] == "Synthetic canonical artist"
    assert row["publish_year"] == 2001 and row["media_type"] == "cassette"
    assert row["publisher"] is None, "Unchecked publisher must remain NULL after creation"
    assert row["location_id"] is None and db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 0
    page = editor_client.get(f"/music/item/{item_id}")
    assert page.status_code == 200 and release["title"] in page.text
    assert db.execute("SELECT publisher FROM items WHERE id = ?", (item_id,)).fetchone()[0] is None


@pytest.mark.parametrize("release_id,country,catno,year", [
    (3935124, "UK", "EPC 40-26156", "1984"), (8297774, "Europe", "40-26156", "1984"),
    (18670711, "Canada", "BT 57615", "1995"),
])
def test_each_pressing_gets_its_own_exact_http_preview(editor_client, db, provider, release_id, country, catno, year):
    before = _snapshot(db)
    response, form = _review(editor_client, release_id)
    assert country in response.text and catno in response.text and year in response.text
    assert f"https://www.discogs.com/release/{release_id}" in response.text
    assert provider.calls[-1]["url"] == f"https://api.discogs.com/releases/{release_id}"
    _assert_researched_http_tracklist(response, release_id)
    assert _value(form, "item_id") == "0" and _snapshot(db) == before


def test_researched_uk_canada_track_orders_and_european_subtracks_through_real_http(editor_client, db, provider):
    """Exact parent-researched order, independently pinned through real routes."""
    from tests.e2e.cassette_asgi import verified_track_facts

    before = _snapshot(db)
    previews = {key: _preview(editor_client, key) for key in (3935124, 18670711, 8297774)}
    for response in previews.values():
        _form(response, "/api/music/apply-match")

    def reported_order(entries):
        # Independent projection of the fixture's reported order: never use
        # the production normalizer as the expected-value oracle.
        order = []
        for entry in entries:
            if entry.get("sub_tracks"):
                order.extend(reported_order(entry["sub_tracks"]))
            elif entry.get("type_") != "heading" and entry.get("title"):
                order.append((entry["position"], entry["title"]))
        return order

    orders = {key: reported_order(provider.details[str(key)]["json"]["tracklist"]) for key in previews}
    assert all(orders.values())
    assert orders == {key: [(number, title) for number, title, duration, duration_ms in verified_track_facts(key)] for key in previews}
    assert orders[3935124] != orders[18670711], "Do not reuse the UK track order for Canada"
    assert provider.details["8297774"]["json"]["tracklist"] and any(
        entry.get("sub_tracks") for entry in provider.details["8297774"]["json"]["tracklist"]
    )
    assert _snapshot(db) == before
    for key, response in previews.items():
        form = _form(response, "/api/music/apply-match")
        root = _root(response)
        listed = [node.text.strip() for listing in root.find("ol") for node in listing.find("li")]
        assert len(listed) == len(orders[key])
        assert all(number in text and title in text for (number, title), text in zip(orders[key], listed, strict=True))
        assert _assert_researched_http_tracklist(response, key) == listed
        item_id = _saved_id(_apply(editor_client, form))
        saved = _snapshot(db)
        fresh = editor_client.get(f"/api/music/items/{item_id}/discogs?public_details=1")
        fresh_list = [node.text.strip() for listing in _root(fresh).find("ol") for node in listing.find("li")]
        assert fresh_list == listed
        assert _assert_researched_http_tracklist(fresh, key, fresh=True) == listed
        assert provider.calls[-1]["url"] == f"https://api.discogs.com/releases/{key}"
        assert _snapshot(db) == saved
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM music_media").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM music_tracks").fetchone()[0] == 0
    assert {row[0] for row in db.execute("SELECT value FROM music_identifiers WHERE identifier_type = 'discogs_release_id'")} == {"3935124", "18670711", "8297774"}
    assert not db.execute("SELECT 1 FROM music_releases WHERE musicbrainz_release_id IS NOT NULL OR musicbrainz_release_group_id IS NOT NULL").fetchall()


def test_synthetic_indexed_tracklist_is_reviewed_live_without_becoming_canonical(editor_client, db, provider):
    synthetic = provider.catalogue["synthetic"]
    provider.details["8297774"]["json"]["tracklist"] = synthetic["tracklist"]
    response, form = _review(editor_client, 8297774)
    listed = [node.text.strip() for listing in _root(response).find("ol") for node in listing.find("li")]
    assert len(listed) == 3
    for number, title, text in zip(("A1", "B1a", "B1b"),
                                  ("Synthetic first track", "Synthetic movement one", "Synthetic movement two"), listed, strict=True):
        assert number in text and title in text
    item_id = _saved_id(_apply(editor_client, form))
    before = _snapshot(db)
    fresh = editor_client.get(f"/api/music/items/{item_id}/discogs?public_details=1")
    assert all(title in fresh.text for title in ("Synthetic first track", "Synthetic movement one", "Synthetic movement two"))
    assert _snapshot(db) == before and not before["music_media"] and not before["music_tracks"]


@pytest.mark.parametrize("source", ["musicbrainz", "manual", "discogs"])
@pytest.mark.parametrize("token", ["", "synthetic-configured-token"])
def test_explicit_public_details_is_optional_auth_for_any_selected_reference(editor_client, db, provider, source, token):
    item_id = _seed_selected_reference(db, source=source)
    if token:
        from app.routers import settings as settings_router

        settings_router._upsert_setting(db, "discogs_token", token)
        db.commit()
    before = _snapshot(db)
    response = editor_client.get(f"/api/music/items/{item_id}/discogs?public_details=1")
    _assert_researched_http_tracklist(response, 18670711, fresh=True)
    assert "Data provided by Discogs" in response.text and "BT 57615" in response.text and "Canada" in response.text
    assert provider.calls == [{"method": "GET", "url": "https://api.discogs.com/releases/18670711",
                               "params": {}, "authorization_present": bool(token)}]
    assert _snapshot(db) == before


def test_public_details_refetches_live_observation_without_storing_it(editor_client, db, provider):
    item_id = _seed_selected_reference(db)
    before = _snapshot(db)
    first = editor_client.get(f"/api/music/items/{item_id}/discogs?public_details=1")
    _assert_researched_http_tracklist(first, 18670711, fresh=True)
    # Clearly synthetic provider edit; no claim that the public catalogue changed.
    updated_title = "Synthetic newly fetched live track title"
    provider.details["18670711"]["json"]["tracklist"][0]["title"] = updated_title
    fresh = editor_client.get(f"/api/music/items/{item_id}/discogs?public_details=1")
    listed = [node.text for listing in _root(fresh).find("ol") for node in listing.find("li")]
    assert updated_title in listed[0] and "Bat Out Of Hell" not in listed[0]
    assert len(provider.calls) == 2 and _snapshot(db) == before
    assert all(call["url"] == "https://api.discogs.com/releases/18670711" for call in provider.calls)


def test_public_details_refuses_another_returned_release_identity(editor_client, db, provider):
    item_id = _seed_selected_reference(db)
    provider.details["18670711"]["json"] = provider.catalogue["discogs_releases"]["3935124"]
    before = _snapshot(db)
    response = editor_client.get(f"/api/music/items/{item_id}/discogs?public_details=1")
    root = _root(response)
    assert root.find(role="status") or root.find(role="alert")
    assert not root.find("ol") and "Modern Girl" not in response.text
    assert _snapshot(db) == before and len(provider.calls) == 1


def test_public_details_does_not_anonymously_retry_a_rejected_configured_token(editor_client, db, provider):
    from app.routers import settings as settings_router

    item_id = _seed_selected_reference(db)
    settings_router._upsert_setting(db, "discogs_token", "synthetic-rejected-token")
    db.commit()
    provider.details["18670711"] = provider.catalogue["synthetic"]["rejected"]
    before = _snapshot(db)
    response = editor_client.get(f"/api/music/items/{item_id}/discogs?public_details=1")
    root = _root(response)
    assert root.find(role="status") or root.find(role="alert")
    assert "Discogs" in response.text and "reject" in response.text.lower()
    assert _snapshot(db) == before
    assert provider.calls == [{"method": "GET", "url": "https://api.discogs.com/releases/18670711",
                               "params": {}, "authorization_present": True}]


def test_public_details_remains_editor_gated_without_spending_anonymous_requests(viewer_client, db, provider):
    item_id = _seed_selected_reference(db)
    before = _snapshot(db)
    response = viewer_client.get(f"/api/music/items/{item_id}/discogs?public_details=1")
    assert response.status_code == 403 and not provider.calls and _snapshot(db) == before


def test_public_details_remains_music_feature_gated(editor_client, db, provider):
    from app.features import set_feature_enabled

    item_id = _seed_selected_reference(db)
    set_feature_enabled(db, "music", False)
    db.commit()
    before = _snapshot(db)
    response = editor_client.get(f"/api/music/items/{item_id}/discogs?public_details=1")
    assert response.status_code == 403 and not provider.calls and _snapshot(db) == before


@pytest.mark.parametrize("query", [{}, {"details": "1"}, {"search": "1"}])
def test_legacy_discogs_panel_actions_do_not_implicitly_become_public(editor_client, db, provider, query):
    item_id = _seed_selected_reference(db)
    before = _snapshot(db)
    response = editor_client.get(f"/api/music/items/{item_id}/discogs", params=query)
    assert response.status_code == 200 and "Discogs" in response.text
    if query:
        assert "not configured" in response.text
    assert not provider.calls and _snapshot(db) == before


def test_selective_discogs_enrichment_keeps_canonical_mb_tree_and_local_decisions(editor_client, db, provider):
    from app.services import discogs_selection

    item_id = _seed_canonical(db)
    provider.details["3935124"]["json"]["labels"][0]["name"] = provider.catalogue["synthetic"]["label"]
    before = _snapshot(db)
    response, form = _review(editor_client, 3935124, item_id=item_id)
    assert "checked" not in _one(form.find("input", name="fields", value="title")).attrs
    assert "MusicBrainz" in response.text
    item_id = _saved_id(_apply(editor_client, form, fields=("publisher", "publish_year")))
    after = _snapshot(db)
    for table in ("music_releases", "music_media", "music_tracks", "item_copies"):
        assert after[table] == before[table], table
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["title"] == "My handwritten title" and row["authors"] == "Local artist credit"
    assert row["notes"] == "Keep collector notes" and row["cover_path"] == "covers/local-choice.png"
    assert row["upc"] == "012345678905" and row["source"] == "manual" and row["owned"] == 1
    assert row["publisher"] == provider.catalogue["synthetic"]["label"] and row["publish_year"] == 1984
    assert discogs_selection.get_selected_release_id(db, item_id) == 3935124
    assert db.execute("SELECT value FROM music_identifiers WHERE identifier_type = 'matrix_runout'").fetchone()[0] == "SYNTHETIC LOCAL MATRIX"
    page = editor_client.get(f"/music/item/{item_id}")
    assert "Synthetic canonical track" in page.text and "My handwritten title" in page.text


def test_unknown_pressing_offers_native_manual_add_without_invented_ids(editor_client, db, provider):
    query = provider.catalogue["synthetic"]["manual_query"]
    before = _snapshot(db)
    response = _search(editor_client, title=query["title"], artist=query["artist"])
    root = _root(response)
    links = [link for link in root.find("a") if urlsplit(link.attrs.get("href", "")).path == "/scan"
             and parse_qs(urlsplit(link.attrs.get("href", "")).query).get("add") == ["manual"]]
    link = _one(links)
    assert "manual" in link.text.lower() or "hand" in link.text.lower()
    destination = editor_client.get(link.attrs["href"])
    assert _root(destination).find("form", **{"hx-post": "/api/items/manual"})
    assert _snapshot(db) == before
    result = editor_client.post("/api/items/manual", data={
        "title": query["title"], "authors": query["artist"], "media_type": "cassette", "isbn": "", "location_id": "",
    })
    _assert_native_added_receipt(result, query["title"])
    row = db.execute("SELECT * FROM items").fetchone()
    assert row["source"] == "manual" and row["media_type"] == "cassette" and row["isbn"] is None and row["upc"] is None
    assert not db.execute("SELECT * FROM music_identifiers").fetchall()
    assert not db.execute("SELECT * FROM music_media").fetchall() and not db.execute("SELECT * FROM music_tracks").fetchall()
    assert not db.execute("SELECT * FROM item_copies").fetchall()
    assert not db.execute("SELECT * FROM music_releases WHERE musicbrainz_release_id IS NOT NULL OR musicbrainz_release_group_id IS NOT NULL").fetchall()
    assert len(provider.calls) == 2


def test_native_added_receipt_accepts_actual_core_manual_post_without_music_link(editor_client, db, provider):
    """Positive control: real auth/CSRF/core POST/receipt/insert, no synthetic HTML."""
    query = provider.catalogue["synthetic"]["manual_query"]
    result = editor_client.post("/api/items/manual", data={
        "title": query["title"], "authors": query["artist"], "media_type": "cassette", "isbn": "", "location_id": "",
    })
    _assert_native_added_receipt(result, query["title"])
    row = db.execute("SELECT * FROM items").fetchone()
    assert row["title"] == query["title"] and row["authors"] == query["artist"]
    assert row["source"] == "manual" and row["media_type"] == "cassette" and row["isbn"] is None and row["upc"] is None
    assert not db.execute("SELECT * FROM music_identifiers").fetchall()
    assert not db.execute("SELECT * FROM music_media").fetchall() and not db.execute("SELECT * FROM music_tracks").fetchall()
    assert not db.execute("SELECT * FROM item_copies").fetchall()
    assert not db.execute("SELECT * FROM music_releases WHERE musicbrainz_release_id IS NOT NULL OR musicbrainz_release_group_id IS NOT NULL").fetchall()
    assert not provider.calls


@pytest.mark.parametrize("status_code,synthetic_html", [
    pytest.param(200, '<article class="scan-result" data-scan-status="error"><p data-scan-title>Synthetic expected receipt title</p>Added</article>', id="error-status-with-Added"),
    pytest.param(200, '<article class="scan-result" data-scan-status="duplicate"><p data-scan-title>Synthetic expected receipt title</p>Added</article>', id="duplicate-status-with-Added"),
    pytest.param(200, '<article class="scan-result" data-scan-status="wishlisted"><p data-scan-title>Synthetic expected receipt title</p>Added</article>', id="wishlisted-status-with-Added"),
    pytest.param(200, '<article class="scan-result" data-scan-status="anything"><p data-scan-title>Synthetic expected receipt title</p>Added</article>', id="unknown-status-with-Added"),
    pytest.param(200, '<article class="scan-result" data-scan-status="Added"><p data-scan-title>Synthetic expected receipt title</p>Added</article>', id="wrong-case-status"),
    pytest.param(200, '<article class="scan-result"><p data-scan-title>Synthetic expected receipt title</p>Added</article>', id="missing-status"),
    pytest.param(200, '<article class="scan-result" data-scan-status=""><p data-scan-title>Synthetic expected receipt title</p>Added</article>', id="empty-status"),
    pytest.param(200, '', id="empty-body"),
    pytest.param(200, '<article data-scan-status="added"><p data-scan-title>Synthetic expected receipt title</p>Added</article>', id="missing-receipt-class"),
    pytest.param(200, '<article class="scan-result" data-scan-status="added"><p>Synthetic expected receipt title</p>Added</article>', id="missing-title-marker"),
    pytest.param(200, '<article class="scan-result" data-scan-status="added"><p data-scan-title></p>Added</article>', id="empty-title"),
    pytest.param(200, '<article class="scan-result" data-scan-status="added"><p data-scan-title>Incorrect title</p>Added</article>', id="incorrect-title"),
    pytest.param(200, '<article class="scan-result" data-scan-status="added"><p data-scan-title>Synthetic expected receipt title</p><p data-scan-title>Synthetic expected receipt title</p>Added</article>', id="multiple-title-markers"),
    pytest.param(200, '<article class="scan-result" data-scan-status="added"><p data-scan-title="">Synthetic expected receipt title</p>Added</article>', id="nonbare-title-marker"),
    pytest.param(200, '<article class="scan-result" data-scan-status="added"><p data-scan-title>Synthetic expected receipt title</p>Added</article><article class="scan-result" data-scan-status="added"><p data-scan-title>Synthetic expected receipt title</p>Added</article>', id="multiple-added-receipts"),
    pytest.param(200, '<article class="scan-result" data-scan-status="added"><p data-scan-title>Synthetic expected receipt title</p>Added</article><article class="scan-result" data-scan-status="duplicate">Added</article>', id="conflicting-receipts"),
    pytest.param(200, '<article class="scan-result" data-scan-status="added"><p data-scan-title>Synthetic expected receipt title</p>Added</article><aside data-scan-status="error">Added</aside>', id="root-error-status"),
    pytest.param(200, '<article class="scan-result" data-scan-status="added"><p data-scan-title>Synthetic expected receipt title</p><span data-scan-error>Added</span></article>', id="bare-error-marker-inside"),
    pytest.param(200, '<article class="scan-result" data-scan-status="added"><p data-scan-title>Synthetic expected receipt title</p>Added</article><aside data-scan-error="Added">Error</aside>', id="valued-error-marker-outside"),
    pytest.param(409, '<article class="scan-result" data-scan-status="added"><p data-scan-title>Synthetic expected receipt title</p>Added</article>', id="non-200-receipt"),
])
def test_native_added_receipt_rejects_labelled_local_oracle_counterexamples(status_code, synthetic_html):
    """Synthetic HTML is ONLY a local negative oracle input, never an endpoint/browser response."""
    from types import SimpleNamespace

    response = SimpleNamespace(status_code=status_code, text=synthetic_html)
    with pytest.raises(AssertionError):
        _assert_native_added_receipt(response, "Synthetic expected receipt title")


@pytest.mark.parametrize("format", ["CD", "Vinyl"])
def test_known_non_cassette_detail_cannot_be_relabelled_by_preview_or_apply(editor_client, db, provider, monkeypatch, format):
    from app.services import manual_cover

    detail = provider.details["18670711"]["json"]
    detail["formats"] = [{"name": format}]
    download = AsyncMock(side_effect=AssertionError("Rejected format must not download"))
    monkeypatch.setattr(manual_cover, "download", download)
    before = _snapshot(db)
    refused = _preview(editor_client)
    _rejected(refused)
    assert not _root(refused).find("form", action="/api/music/apply-match")
    _, form = _review(editor_client, format=format.lower())
    _rejected(_apply(editor_client, form, media_type="cassette"))
    assert _snapshot(db) == before
    download.assert_not_awaited()


def test_unknown_format_has_no_default_and_needs_an_explicit_choice(editor_client, db, provider):
    provider.details["18670711"]["json"]["formats"] = [{"name": "Synthetic unrecognized format"}]
    _, form = _review(editor_client, format="")
    select = _one(form.find("select", name="media_type"))
    selected = select.find("option", selected=None)
    assert not selected or all(option.attrs.get("value", "") == "" for option in selected)
    assert "required" in select.attrs
    before = _snapshot(db)
    body = {"ticket": _value(form, "ticket"), "item_id": "0", "fields": ["title", "authors"], "image": "-1"}
    refused = editor_client.post("/api/music/apply-match", data=body)
    _rejected(refused)
    assert _snapshot(db) == before
    saved = _apply(editor_client, form)
    item_id = _saved_id(saved)
    assert db.execute("SELECT media_type FROM items WHERE id = ?", (item_id,)).fetchone()[0] == "cassette"


def test_fetched_identity_must_equal_the_explicit_selected_release_before_signing(editor_client, db, provider, monkeypatch):
    from app.services import manual_cover

    provider.details["18670711"]["json"]["id"] = 3935124
    download = AsyncMock()
    monkeypatch.setattr(manual_cover, "download", download)
    before = _snapshot(db)
    response = _preview(editor_client)
    _rejected(response)
    assert not _root(response).find("form", action="/api/music/apply-match")
    assert _snapshot(db) == before
    download.assert_not_awaited()


@pytest.mark.parametrize("controls", [
    [("provider", "other"), ("release_id", "3935124"), ("format", "cassette")],
    [("provider", "discogs"), ("provider", "musicbrainz"), ("release_id", "3935124")],
    [("provider", "discogs"), ("release_id", "3935124"), ("release_id", "18670711")],
    [("provider", "discogs"), ("release_id", "3935124"), ("format", "cassette"), ("format", "cd")],
    [("provider", "discogs"), ("release_id", "0"), ("format", "cassette")],
    [("provider", "discogs"), ("release_id", "https://www.discogs.com/master/24278"), ("format", "cassette")],
    [("provider", "musicbrainz"), ("release_id", "3935124"), ("format", "cassette")],
    [("provider", "discogs"), ("release_id", "3935124"), ("url", "https://www.discogs.com/release/18670711"), ("format", "cassette")],
    [("provider", "discogs"), ("release_id", "3935124"), ("unexpected", "control")],
])
def test_ambiguous_invalid_or_unknown_preview_controls_fail_before_provider_io(editor_client, db, provider, controls):
    before = _snapshot(db)
    response = editor_client.post("/api/music/preview", content=urlencode([("item_id", "0"), *controls]),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    _rejected(response)
    assert not _root(response).find("form", action="/api/music/apply-match")
    assert not provider.calls and _snapshot(db) == before


def test_missing_provider_retains_existing_exact_musicbrainz_preview(editor_client, db, provider):
    release = provider.catalogue["synthetic"]["musicbrainz_release"]
    response = editor_client.post("/api/music/preview", data={"release_id": release["id"], "item_id": "0"})
    form = _form(response, "/api/music/apply-match")
    assert "Synthetic canonical track" in response.text and _value(form, "ticket")
    assert all("discogs" not in call["url"] for call in provider.calls)
    assert not _snapshot(db)["items"]


def test_replay_does_not_create_another_selected_pressing_or_copy(editor_client, db, provider):
    _, form = _review(editor_client)
    item_id = _saved_id(_apply(editor_client, form))
    before = _snapshot(db)
    second = _apply(editor_client, form)
    assert second.status_code in (200, 303)
    assert _snapshot(db) == before
    assert db.execute("SELECT item_id FROM music_identifiers WHERE identifier_type = 'discogs_release_id'").fetchone()[0] == item_id


def test_apply_reads_selected_release_duplicates_inside_an_immediate_write_transaction(editor_client, db, provider, monkeypatch):
    from contextlib import contextmanager

    from app.database import get_db
    from app.routers import music_matching

    _, form = _review(editor_client)
    checks = []

    @contextmanager
    def traced_db():
        with get_db() as connection:
            immediate = False

            def trace(statement):
                nonlocal immediate
                sql = statement.strip().upper()
                if sql.startswith("BEGIN IMMEDIATE"):
                    immediate = True
                elif sql.startswith(("COMMIT", "ROLLBACK")):
                    immediate = False
                if sql.startswith("SELECT") and "MUSIC_IDENTIFIERS" in sql and "DISCOGS_RELEASE_ID" in sql:
                    checks.append((immediate, connection.in_transaction))

            connection.set_trace_callback(trace)
            yield connection

    monkeypatch.setattr(music_matching, "get_db", traced_db)
    _saved_id(_apply(editor_client, form))
    assert any(immediate and in_transaction for immediate, in_transaction in checks), (
        "Checking before the lock alone leaves a duplicate race; read the selected ID again under BEGIN IMMEDIATE"
    )
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


@pytest.mark.parametrize("trashed", [False, True])
def test_selected_release_duplicate_is_checked_before_any_image_download(editor_client, db, provider, monkeypatch, trashed):
    from app.services import discogs_selection, item_write, manual_cover, music_catalog

    _synthetic_image(provider)
    _, form = _review(editor_client)
    other = item_write.insert_item(db, title="Already selected", media_type="cassette", source="manual")
    music_catalog.save_release(db, other, {"source": "discogs", "media": []})
    discogs_selection.set_selected_release_id(db, other, 18670711)
    if trashed:
        item_write.trash_item(db, other)
    db.commit()
    before = _snapshot(db)
    download = AsyncMock(side_effect=AssertionError("Duplicate/Trash must be checked before download"))
    monkeypatch.setattr(manual_cover, "download", download)
    response = _apply(editor_client, form, image="0")
    assert response.status_code in (200, 303)
    if trashed:
        _rejected(response)
        assert "restor" in response.text.lower()
    assert _snapshot(db) == before
    download.assert_not_awaited()


@pytest.mark.parametrize("trashed", [False, True])
def test_duplicate_racing_the_image_download_is_rechecked_under_write_lock_and_stage_cleaned(editor_client, db, provider, monkeypatch, trashed):
    from app import config
    from app.database import get_db
    from app.services import discogs_selection, item_write, manual_cover, music_catalog
    from tests.e2e.cassette_asgi import local_png

    _synthetic_image(provider)
    _, form = _review(editor_client)
    rivals = []

    async def download(*args, **kwargs):
        with get_db() as other_db:
            rival = item_write.insert_item(other_db, title="Concurrent selection", media_type="cassette", source="manual")
            music_catalog.save_release(other_db, rival, {"source": "discogs", "media": []})
            discogs_selection.set_selected_release_id(other_db, rival, 18670711)
            if trashed:
                item_write.trash_item(other_db, rival)
            rivals.append(int(rival))
        (config.COVERS_DIR / "synthetic-stage.png").write_bytes(local_png())
        return "covers/synthetic-stage.png"

    spy = AsyncMock(side_effect=download)
    monkeypatch.setattr(manual_cover, "download", spy)
    response = _apply(editor_client, form, image="0")
    assert response.status_code in (200, 303)
    spy.assert_awaited_once()
    assert len(rivals) == 1 and db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    row = db.execute("SELECT * FROM items WHERE id = ?", (rivals[0],)).fetchone()
    assert row["title"] == "Concurrent selection" and bool(row["deleted_at"]) == trashed
    assert row["cover_path"] is None and not (config.COVERS_DIR / "synthetic-stage.png").exists()
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 0
    if trashed:
        _rejected(response)
        assert "restor" in response.text.lower()


@pytest.mark.parametrize("change", ["edit", "trash"])
def test_existing_item_fingerprint_is_rechecked_before_downloading(editor_client, db, provider, monkeypatch, change):
    from app.services import item_write, manual_cover

    item_id = _seed_canonical(db)
    _synthetic_image(provider)
    _, form = _review(editor_client, item_id=item_id)
    if change == "edit":
        item_write.update_item_fields(db, item_id, {"title": "Concurrent local edit"})
    else:
        item_write.trash_item(db, item_id)
    db.commit()
    before = _snapshot(db)
    download = AsyncMock()
    monkeypatch.setattr(manual_cover, "download", download)
    _rejected(_apply(editor_client, form, fields=("title",), image="0"))
    assert _snapshot(db) == before
    download.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["tamper", "actor", "item", "expired", "oversized", "login", "image-purpose"])
def test_real_preview_tickets_bind_actor_item_expiry_size_and_purpose(editor_client, editor_user, db, provider, monkeypatch, mutation):
    from app import auth
    from app.services import manual_cover, music_review

    _synthetic_image(provider)
    response, form = _review(editor_client)
    ticket = _value(form, "ticket")
    body = {"ticket": ticket, "item_id": "0", "fields": "title", "media_type": "cassette", "image": "-1"}
    if mutation == "tamper":
        body["ticket"] = ticket[:-8] + "tampered"
    elif mutation == "actor":
        from tests.conftest import _create_user

        actor = _create_user("other-editor", "password123", "Other editor", "editor")
        editor_client.cookies.set("access_token", auth.create_token(actor["id"], actor["username"], actor["role"], actor["display_name"]))
    elif mutation == "item":
        item_id = _seed_canonical(db)
        body["item_id"] = str(item_id)
    elif mutation == "expired":
        from datetime import datetime, timedelta, timezone
        import jwt.api_jwt

        class AfterReviewExpiry(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz or timezone.utc) + timedelta(seconds=901)

        monkeypatch.setattr(jwt.api_jwt, "datetime", AfterReviewExpiry)
    elif mutation == "oversized":
        body["ticket"] = "x" * (music_review.MAX_TICKET + 1)
    elif mutation == "login":
        body["ticket"] = auth.create_token(editor_user["id"], editor_user["username"], "editor", editor_user["display_name"])
    elif mutation == "image-purpose":
        image = _one(_root(response).find("img"))
        body["ticket"] = parse_qs(urlsplit(image.attrs["src"]).query)["ticket"][0]
    before = _snapshot(db)
    download = AsyncMock()
    monkeypatch.setattr(manual_cover, "download", download)
    rejected = editor_client.post("/api/music/apply-match", data=body)
    _rejected(rejected)
    assert _snapshot(db) == before
    download.assert_not_awaited()


def test_review_token_cannot_fetch_images_but_real_image_ticket_can(editor_client, db, provider, monkeypatch):
    from app import config
    from app.services import manual_cover
    from tests.e2e.cassette_asgi import local_png

    _synthetic_image(provider)
    response, form = _review(editor_client)
    image = _one(_root(response).find("img"))
    image_url = image.attrs["src"]
    assert image_url.startswith("/api/music/preview-image?") and "https://i.discogs.com" not in image_url
    before = _snapshot(db)

    async def download(*args, **kwargs):
        (config.COVERS_DIR / "synthetic-preview.png").write_bytes(local_png())
        return "covers/synthetic-preview.png"

    spy = AsyncMock(side_effect=download)
    monkeypatch.setattr(manual_cover, "download", spy)
    refused = editor_client.get("/api/music/preview-image", params={"ticket": _value(form, "ticket"), "index": "0"})
    assert refused.status_code == 404
    spy.assert_not_awaited()
    accepted = editor_client.get(image_url)
    assert accepted.status_code == 200 and accepted.content == local_png()
    assert accepted.headers["content-type"] == "image/png" and accepted.headers["cache-control"].startswith("private")
    assert not (config.COVERS_DIR / "synthetic-preview.png").exists() and _snapshot(db) == before
    spy.assert_awaited_once()


@pytest.mark.parametrize("endpoint", ["/api/music/preview", "/api/music/apply-match"])
def test_cassette_mutations_keep_editor_role_gate(viewer_client, db, provider, endpoint):
    before = _snapshot(db)
    response = viewer_client.post(endpoint, data={"provider": "discogs", "release_id": "3935124", "format": "cassette"})
    assert response.status_code == 403 and not provider.calls and _snapshot(db) == before


@pytest.mark.parametrize("endpoint", ["/api/music/preview", "/api/music/apply-match"])
def test_cassette_mutations_keep_feature_gate(editor_client, db, provider, endpoint):
    from app.features import set_feature_enabled

    set_feature_enabled(db, "music", False)
    db.commit()
    before = _snapshot(db)
    response = editor_client.post(endpoint, data={"provider": "discogs", "release_id": "3935124", "format": "cassette"})
    assert response.status_code == 403 and not provider.calls and _snapshot(db) == before


@pytest.mark.parametrize("endpoint", ["/api/music/preview", "/api/music/apply-match"])
def test_cassette_mutations_refuse_missing_csrf(editor_client, db, provider, endpoint):
    before = _snapshot(db)
    editor_client.headers.pop("X-CSRF-Token")
    response = editor_client.post(endpoint, data={"provider": "discogs", "release_id": "3935124", "format": "cassette"})
    assert response.status_code == 403 and not provider.calls and _snapshot(db) == before


def test_cassette_preview_accepts_real_rendered_form_csrf_without_header(editor_client, provider):
    searched = _search(editor_client)
    form = _one([form for form in _root(searched).find("form", action="/api/music/preview")
                 if form.find("input", name="provider", value="discogs")
                 and form.find("input", name="release_id", value="3935124")])
    editor_client.headers.pop("X-CSRF-Token")
    response = editor_client.post("/api/music/preview", data={name: _value(form, name) for name in ("provider", "release_id", "format", "_csrf")})
    assert _value(_form(response, "/api/music/apply-match"), "ticket")


def test_anonymous_cassette_requests_require_real_authentication(client, editor_user, db, provider):
    before = _snapshot(db)
    for method, endpoint in (("get", "/music"), ("post", "/api/music/preview"), ("post", "/api/music/apply-match")):
        response = getattr(client, method)(endpoint, follow_redirects=False)
        assert response.status_code in (302, 303, 307)
        assert urlsplit(response.headers["location"]).path == "/login"
    assert not provider.calls and _snapshot(db) == before


@pytest.mark.parametrize("extra", [
    [("fields", "musicbrainz_release_id")], [("provider", "discogs")],
    [("item_id", "0")], [("media_type", "cd")], [("image", "0")], [("location_id", "1"), ("location_id", "2")],
])
def test_apply_allowlist_and_single_valued_controls_remain_strict(editor_client, db, provider, extra):
    # Use a real old-interface MB preview so these negative controls reach
    # Apply even on the independent feature-absent baseline.
    release = provider.catalogue["synthetic"]["musicbrainz_release"]
    response = editor_client.post("/api/music/preview", data={"release_id": release["id"]})
    form = _form(response, "/api/music/apply-match")
    controls = [("ticket", _value(form, "ticket")), ("item_id", "0"), ("fields", "title"),
                ("media_type", "cassette"), ("image", "-1"), *extra]
    before = _snapshot(db)
    refused = editor_client.post("/api/music/apply-match", content=urlencode(controls),
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
    _rejected(refused)
    assert _snapshot(db) == before


@pytest.mark.parametrize("endpoint", ["/api/music/preview", "/api/music/apply-match"])
def test_cassette_review_form_limits_still_apply(editor_client, db, provider, endpoint):
    from app.http_body import MUSIC_PREVIEW_FORM_BYTES, MUSIC_REVIEW_FORM_BYTES

    limit = MUSIC_PREVIEW_FORM_BYTES if endpoint.endswith("preview") else MUSIC_REVIEW_FORM_BYTES
    before = _snapshot(db)
    response = editor_client.post(endpoint, content=b"provider=discogs&url=" + b"x" * (limit + 1),
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code in (200, 413) and "too large" in response.text.lower()
    assert not provider.calls and _snapshot(db) == before


@pytest.mark.parametrize("enabled", [False, True])
def test_scan_music_title_search_never_calls_openlibrary_and_handoff_preserves_query(editor_client, db, provider, monkeypatch, enabled):
    from app.features import set_feature_enabled
    from app.services import openlibrary, provider_result

    set_feature_enabled(db, "music", enabled)
    db.commit()
    books = AsyncMock(return_value=provider_result.found("openlibrary", []))
    monkeypatch.setattr(openlibrary, "search_books", books)
    title = 'Meat Loaf / Hits & "Hell"'
    response = editor_client.get("/api/title-search", params={"q": title, "media_type": "cassette"})
    root = _root(response)
    books.assert_not_awaited()
    assert not provider.calls
    if enabled:
        links = [node for node in root.find("a") if urlsplit(node.attrs.get("href", "")).path == "/music"]
        query = parse_qs(urlsplit(_one(links).attrs["href"]).query)
        assert query["q"] == [title] and query["format"] == ["cassette"]
    else:
        assert "music" in response.text.lower() and ("off" in response.text.lower() or "disabled" in response.text.lower())
        assert "manual" in response.text.lower() or "hand" in response.text.lower()
        assert not any(urlsplit(node.attrs.get("href", "")).path == "/music" for node in root.find("a"))


@pytest.mark.parametrize("case,provider_name", [("mb", "MusicBrainz"), ("discogs", "Discogs")])
def test_search_errors_attribute_the_failed_provider_and_do_not_write(editor_client, db, provider, case, provider_name):
    before = _snapshot(db)
    if case == "mb":
        provider.mb_search = provider.catalogue["synthetic"]["rate_limited"]
    else:
        provider.discogs_search = provider.catalogue["synthetic"]["rate_limited"]
    response = _search(editor_client)
    alert = _one(_root(response).find(role="alert"))
    assert provider_name in alert.text and ("rate" in alert.text.lower() or "limit" in alert.text.lower())
    _assert_provider_failure_alert(response, provider_name, rate_limited=True)
    assert len(provider.calls) == (1 if case == "mb" else 2) and _snapshot(db) == before


@pytest.mark.parametrize("response", [
    {"status": 429, "json": {"message": "Synthetic MusicBrainz quota"}},
    {"status": 403, "json": {"message": "Synthetic MusicBrainz rejection"}},
    {"status": 404, "json": {"message": "Synthetic endpoint failure, not an empty search"}},
    {"status": 200, "text": "{synthetic broken JSON"},
    {"status": 200, "json": {"releases": "synthetic malformed list"}},
    {"status": 200, "json": {"synthetic_missing_releases": True}},
    {"status": 503, "json": {"message": "Synthetic unavailable service"}},
])
def test_musicbrainz_failed_search_is_actionable_http_error_not_no_matches(editor_client, db, provider, response):
    before = _snapshot(db)
    provider.mb_search = response
    result = _search(editor_client)
    _assert_provider_failure_alert(result, "MusicBrainz", rate_limited=response["status"] == 429)
    assert len(provider.calls) == 1 and provider.calls[0]["url"] == "https://musicbrainz.org/ws/2/release/"
    assert not _root(result).find("form", action="/api/music/apply-match")
    assert _snapshot(db) == before


@pytest.mark.parametrize("case", ["rate_limited", "rejected", "malformed_json", "malformed_discogs"])
def test_discogs_failed_search_is_actionable_http_error_not_no_matches(editor_client, db, provider, case):
    before = _snapshot(db)
    provider.discogs_search = provider.catalogue["synthetic"][case]
    response = _search(editor_client)
    _assert_provider_failure_alert(response, "Discogs", rate_limited=case == "rate_limited")
    assert len(provider.calls) == 2 and provider.calls[-1]["url"] == "https://api.discogs.com/database/search"
    assert not _root(response).find("form", action="/api/music/apply-match")
    assert _snapshot(db) == before
