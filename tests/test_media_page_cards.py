"""Real registered Home/Music/Library producers, not CSS or browser acceptance.

Authentication, feature gates, queries and Jinja globals are the application's.
Only the translation catalog is replaced, with an actual compiled GNU catalog.
"""

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo
import pytest

from app import config, features, i18n, nav
from app.services import item_copies, item_write, music_catalog
from tests.conftest import _insert_item, _insert_location


class _Element:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children, self.text = [], ""

    def find(self, tag=None, cls=None, **attrs):
        found = []
        for child in self.children:
            if (tag is None or child.tag == tag) and (
                cls is None or cls in child.attrs.get("class", "").split()
            ) and all(key in child.attrs and child.attrs[key] == value for key, value in attrs.items()):
                found.append(child)
            found.extend(child.find(tag, cls, **attrs))
        return found


class _HTML(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = _Element("document")
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        child = _Element(tag, attrs, self.current)
        self.current.children.append(child)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.current = child

    def handle_endtag(self, tag):
        current = self.current
        while current.parent:
            if current.tag == tag:
                self.current = current.parent
                return
            current = current.parent

    def handle_data(self, data):
        current = self.current
        while current:
            current.text += data
            current = current.parent


def _get(client, path, **kwargs):
    response = client.get(path, **kwargs)
    assert response.status_code == 200, response.text
    return response, _HTML(response.text).root


def _one(nodes):
    assert len(nodes) == 1
    return nodes[0]


def _cards(root):
    return root.find("article", cls="media-card")


def _ids(cards):
    return [int(card.attrs["data-item-id"]) for card in cards]


def _assert_artwork(card, media_type, covered):
    # Independent expected native tokens, not a registry stub or copied markup.
    shape = ("square" if media_type in {"cd", "vinyl", "digital_music", "audiobook"}
             else "cassette" if media_type == "cassette"
             else "game" if media_type == "video_game" else "portrait")
    artwork = _one(card.find(cls="media-artwork"))
    assert artwork.attrs["data-media-type"] == media_type
    assert artwork.attrs["data-cover-shape"] == shape
    assert len(artwork.find("img")) == int(covered)
    assert len(artwork.find(cls="media-artwork__placeholder")) == int(not covered)
    if covered:
        image = _one(artwork.find("img"))
        assert image.attrs["alt"] == "" and image.attrs["loading"] == "lazy"
        assert image.attrs["decoding"] == "async"
    else:
        assert _one(artwork.find(cls="media-artwork__placeholder")).attrs["aria-hidden"] == "true"
    assert not card.find(cls="cover-card")
    assert not any("object-cover" in node.attrs.get("class", "").split() for node in card.find())
    assert not card.find("button") and not card.find("input")
    assert not card.find(cls="media-card__edit"), "Adoption must not invent edit authorization"


def _mixed_seed(db):
    rows = []
    for number, media_type in enumerate((*config.MEDIA_TYPES, "legacy_unknown"), 1):
        fields = dict(title=f"Seed {30 - number:02d} {media_type}", authors=f"Creator {number}",
                      media_type=media_type, cover_path=f"covers/seed-{number}.png" if number % 2 else None,
                      owned=int(number % 3 == 1), wishlisted=number % 3 == 2,
                      created_at=f"2026-10-{number // 4 + 1:02d} 12:00:00")
        if media_type in config.MEDIA_TYPES:
            item_id = item_write.insert_item(db, **fields)
        else:
            item_id = _insert_item(db, isbn=None, **fields)  # Deliberate legacy row, not a new allowed type.
        rows.append({"id": int(item_id), **fields})
    trashed = item_write.insert_item(db, title="Trashed newest", media_type="book",
                                    created_at="2099-01-01 00:00:00")
    assert item_write.trash_item(db, trashed)
    db.commit()
    return sorted(rows, key=lambda row: (row["created_at"], row["id"]), reverse=True), int(trashed)


def test_real_app_registers_only_the_pure_card_and_shelf_adapters(viewer_client):
    from app.main import app
    from app.services.media_cards import MediaShelf, build_media_card

    assert app.state.templates.env.globals["build_media_card"] is build_media_card
    assert app.state.templates.env.globals["MediaShelf"] is MediaShelf
    # A real authenticated request must use those registrations, not a test env.
    assert viewer_client.get("/").status_code == 200


@pytest.mark.parametrize("library_state", ["visible", "feature-off", "hidden"])
def test_home_has_one_bounded_mixed_row_and_follows_its_actual_full_page_link(viewer_client, db, library_state, tmp_path):
    expected, trashed = _mixed_seed(db)
    if library_state == "feature-off":
        features.set_feature_enabled(db, "connected_library", False)
    elif library_state == "hidden":
        db.execute("INSERT INTO settings (key, value) VALUES ('nav_hidden_tabs', '[\"library\"]')")
        nav.invalidate_cache()
    db.commit()
    response, root = _get(viewer_client, "/")
    (tmp_path / "home.html").write_text(response.text)
    shelf = _one(root.find("section", cls="media-shelf"))
    assert shelf.attrs["data-shelf-key"] == "recent"
    row = _one(shelf.find("ul", cls="media-shelf__items"))
    assert row.attrs["tabindex"] == "0" and row.attrs["aria-label"] == "Recently added"
    assert len(row.children) == 9 and all(child.tag == "li" for child in row.children)
    assert all("media-shelf__slot" in child.attrs["class"].split() for child in row.children)
    assert _ids(_cards(row)) == [item["id"] for item in expected[:8]]
    assert len(response.context["recent_items"]) == 8 and response.context["total_count"] == 15
    assert not shelf.find(cls="cover-grid") and not shelf.find("button")
    for card, item in zip(_cards(row), expected[:8], strict=True):
        _assert_artwork(card, item["media_type"], bool(item["cover_path"]))
        assert _one(card.find("a")).attrs["href"] == f"/item/{item['id']}"
        assert [badge.text for badge in card.find(cls="media-card__badge")] == (["Wishlist"] if item["wishlisted"] else [])
    terminal = _one(row.children[-1].find("a", cls="media-shelf__view-more"))
    assert not any(child.find(cls="media-shelf__view-more") for child in row.children[:-1])
    assert "More items are available." in terminal.text
    expected_href = "/library?reset=1&view=items&sort=recent" if library_state == "visible" else "/browse?sort=newest"
    assert terminal.attrs["href"] == expected_href
    assert "types" not in parse_qs(urlsplit(terminal.attrs["href"]).query)
    assert "page" not in parse_qs(urlsplit(terminal.attrs["href"]).query)
    # FOLLOW the rendered anchor through the registered, authorized producer.
    destination, full = _get(viewer_client, terminal.attrs["href"])
    (tmp_path / "view-more-destination.html").write_text(destination.text)
    if library_state == "visible":
        assert destination.context["filters"]["view"] == "items"
        assert destination.context["filters"]["sort"] == "recent"
        results = _one(full.find(**{"data-testid": "library-results"}))
        actual = [int(node.attrs["data-library-member"]) for node in results.find(**{"data-matches": "true"})]
        assert _ids(_cards(results)) == actual
        assert actual == [item["id"] for item in expected]
    else:
        grid = _one(full.find(**{"data-testid": "item-grid"}))
        actual = [int(node.attrs["data-item-id"]) for node in grid.find("a") if "data-item-id" in node.attrs]
        # Core Browse's existing newest contract is timestamp-only, unlike
        # Home/Library's explicit ID tie-breaker. Do not claim or change that
        # out-of-scope query here; still prove order and the complete inventory.
        timestamps = {item["id"]: item["created_at"] for item in expected}
        assert len(actual) == len(expected) and set(actual) == set(timestamps)
        assert [timestamps[item_id] for item_id in actual] == sorted(timestamps.values(), reverse=True)
    assert trashed not in actual
    assert set(item["media_type"] for item in expected) == set(config.MEDIA_TYPES) | {"legacy_unknown"}
    summary = _one(root.find(**{"data-testid": "home-summary"}))
    assert _one(summary.find("a", href="/browse")).find("p")[-1].text == "15"
    assert _one(summary.find("a", href="/browse?owned=0")).find("p")[-1].text == "5"


@pytest.mark.parametrize("count", [1, 8, 9])
def test_home_terminal_is_not_pagination_and_only_claims_more_when_true(viewer_client, db, count):
    for number in range(count):
        item_write.insert_item(db, title=f"Entry {number}", media_type="cd")
    db.commit()
    _, root = _get(viewer_client, "/")
    shelf = _one(root.find(cls="media-shelf"))
    assert len(_cards(shelf)) == min(count, 8)
    terminal = _one(shelf.find(cls="media-shelf__view-more"))
    assert terminal.attrs["href"] == "/library?reset=1&view=items&sort=recent"
    assert ("More items are available." in terminal.text) is (count > 8)


@pytest.mark.parametrize("client_fixture", ["viewer_client", "editor_client"])
def test_home_empty_keeps_the_owned_message_and_role_checked_scan_link(request, client_fixture):
    _, root = _get(request.getfixturevalue(client_fixture), "/")
    shelf = _one(root.find(cls="media-shelf"))
    assert _one(shelf.find(cls="media-shelf__empty")).text == "No items yet."
    assert not shelf.find("ul") and not _cards(shelf) and not shelf.find("a")
    content = _one(root.find("main"))
    assert bool([link for link in content.find("a", href="/scan") if link.text == "Start scanning"]) is (client_fixture == "editor_client")


@pytest.mark.parametrize("covered", [False, True], ids=["placeholders", "images"])
@pytest.mark.parametrize("client_fixture", ["viewer_client", "editor_client"])
def test_music_local_grid_keeps_all_four_formats_metadata_and_detail_destinations(request, db, client_fixture, covered, tmp_path):
    client = request.getfixturevalue(client_fixture)
    expected = []
    for number, media_type in enumerate(("cd", "vinyl", "cassette", "digital_music"), 1):
        title = f'Release {number} <b>100%d & "title"</b>'
        artist = f'Artist {number} <em>50%s & credit</em>'
        item_id = item_write.insert_item(db, title=title, authors=artist, media_type=media_type,
                                        publish_year=1990 + number,
                                        cover_path=f"covers/music-{number}.png" if covered else None)
        catalog = f'CAT-{number} <i>100%d & pressing</i>'
        format_summary = f'Provider format {number} <u>50%s</u>'
        date = "2001-02-03" if number % 2 else None
        music_catalog.save_release(db, item_id, {"catalog_number": catalog, "release_date": date,
                                                "format_summary": format_summary})
        expected.append((int(item_id), media_type, title, artist, catalog, format_summary, date or str(1990 + number)))
    item_write.insert_item(db, title="Not a music release", media_type="book")
    trashed = item_write.insert_item(db, title="Trashed release", media_type="cd")
    item_write.trash_item(db, trashed)
    db.commit()
    response, root = _get(client, "/music")  # No search query: no remote/provider calls.
    (tmp_path / "music.html").write_text(response.text)
    local = _one(root.find(**{"data-testid": "music-local-items"}))
    cards = _cards(local)
    assert _ids(cards) == [row[0] for row in expected]
    assert "4 shown" in local.text
    grid = cards[0].parent.parent
    assert "grid" in grid.attrs.get("class", "").split()
    for card, (item_id, media_type, title, artist, catalog, format_summary, date) in zip(cards, expected, strict=True):
        _assert_artwork(card, media_type, covered)
        assert _one(card.find("h3")).text == title
        assert _one(card.find(cls="media-card__creator")).text == artist
        assert _one(card.find("a")).attrs["href"] == f"/music/item/{item_id}"
        assert catalog in card.parent.text and format_summary in card.parent.text and date in card.parent.text
        assert not any(card.parent.find(tag) for tag in ("b", "em", "i", "u"))
    assert not local.find("form") and not local.find("button")
    assert not root.find("form", action="/api/music/add")
    assert bool(root.find(**{"id": "music-matching"})) is (client_fixture == "editor_client")
    detail, _ = _get(client, _one(cards[0].find("a")).attrs["href"])
    assert detail.context["item"]["id"] == expected[0][0]


def test_music_local_cap_and_shown_count_do_not_promise_a_provider_filter(viewer_client, db):
    for number in range(251):
        item_write.insert_item(db, title=f"Album {number:03d}", media_type="vinyl" if number % 2 else "cd")
    db.commit()
    response, root = _get(viewer_client, "/music", params={"format": "cassette"})
    local = _one(root.find(**{"data-testid": "music-local-items"}))
    assert len(response.context["items"]) == len(_cards(local)) == 250
    assert "250 shown" in local.text
    assert {card.attrs["data-media-type"] for card in _cards(local)} == {"vinyl", "cd"}
    assert "Album 250" not in local.text


def test_music_off_still_uses_the_real_feature_gate(viewer_client, db):
    features.set_feature_enabled(db, "music", False)
    db.commit()
    _, root = _get(viewer_client, "/music")
    assert "Music is turned off" in root.text
    assert not root.find(**{"data-testid": "music-local-items"}) and not _cards(root)


def _library_seed(db):
    paper = item_write.insert_item(db, title="Paper <b>100%d</b>", authors="Writer <i>50%s</i>", media_type="book",
                                   subtitle="Subtitle <em>100%d</em>", series_name="星 #1/2 <u>Saga</u>", series_position=0.5)
    ebook = item_write.insert_item(db, title="Digital <b>100%d</b>", authors="Author <i>50%s</i>", media_type="ebook",
                                   owned=0, wishlisted=True, cover_path="covers/digital.png")
    audio = item_write.insert_item(db, title="Audio <b>100%d</b>", authors="Narrator <i>50%s</i>", media_type="audiobook", owned=0)
    trashed = item_write.insert_item(db, title="Trashed edition", media_type="comic")
    for member in (ebook, audio, trashed):
        db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'format')", (paper, member))
    item_write.trash_item(db, trashed)
    location = _insert_location(db, 'Shelf <b>100%d & place</b>')
    for number in (1, 2, 3):
        copy_id = item_copies.insert_copy(db, {"item_id": paper, "copy_number": number, "location_id": location,
                                              "missing_since": "2026-01-01" if number == 2 else None})
        if number == 3:
            item_copies.trash_copy(db, copy_id)
    tag = 'Tag <b>100%d & label</b>'
    tag_id = db.execute("INSERT INTO tags (name) VALUES (?)", (tag,)).lastrowid
    for member in (paper, ebook, audio):
        db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (member, tag_id))
    sources = []
    for member, failed in ((ebook, False), (audio, True)):
        source = db.execute(
            "INSERT INTO source_instances (provider, name, base_url, last_status, last_error) VALUES ('opds', ?, 'https://books.example.test', ?, ?)",
            (f'Source {member} <b>50%s</b>', "error" if failed else "ok", "private-failure-marker" if failed else None),
        ).lastrowid
        db.execute(
            "INSERT INTO source_records (source_id, item_id, external_id, title, media_type, availability, open_url, last_seen_at) "
            "VALUES (?, ?, ?, 'Cached title', ?, 'available', ?, '2026-10-01T12:00:00Z')",
            (source, member, f"external-{member}", "audiobook" if failed else "ebook", f"/books/{member}"),
        )
        sources.append(source)
    db.commit()
    return int(paper), int(ebook), int(audio), sources, tag


@pytest.mark.parametrize("path", ["/library", "/api/library/search"])
def test_library_common_cards_preserve_full_group_members_holdings_and_same_item_matches(viewer_client, db, path, tmp_path):
    paper, ebook, audio, sources, tag = _library_seed(db)
    response, root = _get(viewer_client, path, params={"media_type": "ebook", "availability": "digital", "source": f"source:{sources[0]}"})
    (tmp_path / "library.html").write_text(response.text)
    results = _one(root.find(**{"data-testid": "library-results"}))
    counts = _one(results.find(**{"data-testid": "library-counts"}))
    assert (counts.attrs["data-matching-count"], counts.attrs["data-group-count"]) == ("1", "1")
    group = _one(results.find("article", **{"data-library-group": str(paper)}))
    assert group.attrs["data-member-ids"] == f"{paper},{ebook},{audio}"
    assert group.attrs["data-matching-ids"] == str(ebook)
    assert _ids(_cards(group)) == [ebook, audio, paper]
    assert "3 live editions · 1 matching" in _one(group.find("summary")).text
    items = {item["id"]: item for item in response.context["groups"][0]["members"]}
    for member_id in (paper, ebook, audio):
        member = _one(group.find(**{"data-library-member": str(member_id)}))
        assert member.attrs["data-matches"] == ("true" if member_id == ebook else "false")
        card = _one(_cards(member))
        _assert_artwork(card, items[member_id]["media_type"], bool(items[member_id]["cover_path"]))
        assert _one(card.find("a")).attrs["href"] == f"/item/{member_id}?from=library"
        assert not card.find(cls="media-card__creator"), "Keep the domain creator role, not a duplicate generic row"
        creator = _one(member.find("p", **{"aria-label": "Author(s)"}))
        assert creator.text == items[member_id]["authors"]
        assert tag in member.text and f"#{member_id}" in member.text
        assert not any(member.find(tag_name) for tag_name in ("b", "i", "em", "u"))
    paper_node = _one(group.find(**{"data-library-member": str(paper)}))
    assert "2 physical copies" in paper_node.text and "Owned" in paper_node.text
    assert "Subtitle <em>100%d</em>" in paper_node.text and "星 #1/2 <u>Saga</u> · 0.5" in paper_node.text
    assert "Other format — does not match filters" in paper_node.text
    assert {holding.attrs["data-holding-state"] for holding in paper_node.find("li")} == {"available", "missing"}
    digital = _one(group.find(**{"data-library-member": str(ebook)}))
    assert "Wishlist" in digital.text and "Matches filters" in digital.text
    assert _one(digital.find("a", target="_blank")).attrs["href"] == f"https://books.example.test/books/{ebook}"
    audio_node = _one(group.find(**{"data-library-member": str(audio)}))
    assert "Wishlist" not in audio_node.text and "Owned" not in audio_node.text
    assert _one(audio_node.find("li")).attrs["data-holding-state"] == "unknown"
    assert "Last source sync failed; cached record retained." in audio_node.text
    assert "Last seen: 2026-10-01T12:00:00Z" in audio_node.text
    assert "Listen" in _one(audio_node.find("a", target="_blank")).text
    assert "private-failure-marker" not in response.text
    assert not results.find("input") and not results.find("script")


@pytest.mark.parametrize("view", ["items", "groups"])
def test_library_common_cards_keep_filter_counts_and_real_pagination_links(viewer_client, db, view):
    ids = [int(item_write.insert_item(db, title=f"Filtered album {number}", media_type="cd", created_at="2026-10-01")) for number in range(5)]
    item_write.insert_item(db, title="Unrelated", media_type="dvd")
    db.commit()
    _, root = _get(viewer_client, "/library", params={"q": "Filtered", "media_type": "cd", "view": view, "sort": "recent", "page_size": "2"})
    results = _one(root.find(**{"data-testid": "library-results"}))
    assert _ids(_cards(results)) == ids[::-1][:2]
    next_link = _one(results.find("a", rel="next"))
    assert parse_qs(urlsplit(next_link.attrs["href"]).query) == {
        "q": ["Filtered"], "media_type": ["cd"], "sort": ["recent"], "page_size": ["2"], "page": ["2"],
        **({"view": ["items"]} if view == "items" else {}),
    }
    _, following = _get(viewer_client, next_link.attrs["href"])
    results = _one(following.find(**{"data-testid": "library-results"}))
    assert _ids(_cards(results)) == ids[::-1][2:4]
    assert "5 matching items in 5 groups" in results.text


def test_library_series_representative_uses_common_artwork_without_changing_drilldown(viewer_client, db):
    item_id = item_write.insert_item(db, title="Cassette", media_type="cassette", series_name="星 #1/2", series_position=0.5)
    db.commit()
    _, root = _get(viewer_client, "/library", params={"view": "series"})
    series = _one(root.find(**{"data-library-series": "星 #1/2"}))
    _assert_artwork(series, "cassette", False)
    link = _one(series.find("a"))
    assert parse_qs(urlsplit(link.attrs["href"]).query)["series"] == ["星 #1/2"]
    _, detail = _get(viewer_client, link.attrs["href"])
    assert _ids(_cards(_one(detail.find(**{"data-testid": "library-results"})))) == [int(item_id)]


@pytest.fixture
def gnu_catalog(tmp_path, monkeypatch):
    def install(messages):
        catalog = Catalog(locale="it")
        for msgid, translated in messages.items():
            catalog.add(msgid, translated)
        locale_dir = tmp_path / "locales"
        target = locale_dir / "it" / "LC_MESSAGES" / "messages.mo"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as output:
            write_mo(output, catalog)
        monkeypatch.setattr(i18n, "LOCALE_DIR", locale_dir)
        monkeypatch.setenv("SHELF_I18N", "on")
        i18n._catalog.cache_clear()
        i18n._js_catalog.cache_clear()
    yield install
    i18n._catalog.cache_clear()
    i18n._js_catalog.cache_clear()


def test_home_registered_newstyle_gnu_labels_are_escaped_once_with_raw_metadata(viewer_client, db, gnu_catalog):
    title, creator = '<b>Title 100%d & text</b>', '<i>Artist 50%s & credit</i>'
    item_id = item_write.insert_item(db, title=title, authors=creator, media_type="cd", owned=0, wishlisted=True)
    for number in range(8):
        item_write.insert_item(db, title=f"Older {number}", media_type="book", created_at="2000-01-01")
    db.commit()
    gnu_catalog({label: f'<b data-hostile="label">{label} 100%% & text</b>' for label in
                 ("Recently added", "View more", "More items are available.", "Wishlist", "CD")}
                | {title: "WRONGLY TRANSLATED", creator: "WRONGLY TRANSLATED"})
    _, root = _get(viewer_client, "/", headers={"Accept-Language": "it"})
    shelf = _one(root.find(cls="media-shelf"))
    card = _one([card for card in _cards(shelf) if card.attrs["data-item-id"] == str(item_id)])
    assert _one(card.find("h3")).text == title
    assert _one(card.find(cls="media-card__creator")).text == creator
    for label in ("Recently added", "View more", "More items are available.", "Wishlist", "CD"):
        assert f'<b data-hostile="label">{label} 100% & text</b>' in shelf.text
    assert not shelf.find("b") and not shelf.find("i") and "WRONGLY TRANSLATED" not in shelf.text
    assert not any(name.startswith("on") for node in shelf.find() for name in node.attrs)


def test_library_registered_gnu_creator_labels_and_unknown_metadata_are_not_html(viewer_client, db, gnu_catalog):
    unknown = '<b>Legacy 100%d & format</b>'
    title = '<i>Metadata 50%s & title</i>'
    _insert_item(db, isbn=None, title=title, authors="Raw creator", media_type=unknown, owned=0)
    db.commit()
    gnu_catalog({"Author(s)": '<b data-hostile="label">Creator 100%% & role</b>',
                 "Matches filters": '<b data-hostile="label">Match 100%%</b>',
                 unknown: "WRONGLY TRANSLATED", title: "WRONGLY TRANSLATED"})
    _, root = _get(viewer_client, "/library", params={"view": "items"}, headers={"Accept-Language": "it"})
    results = _one(root.find(**{"data-testid": "library-results"}))
    card = _one(_cards(results))
    assert _one(card.find("h3")).text == title
    assert _one(card.find(cls="media-card__type")).text == unknown
    creator = _one(results.find("p", **{"aria-label": '<b data-hostile="label">Creator 100% & role</b>'}))
    assert creator.text == "Raw creator"
    assert '<b data-hostile="label">Match 100%</b>' in results.text
    assert not results.find("b") and not results.find("i") and "WRONGLY TRANSLATED" not in results.text


def test_music_registered_gnu_labels_escape_without_translating_release_metadata(viewer_client, db, gnu_catalog):
    metadata = {"title": '<b>Album 100%d & title</b>', "authors": '<i>Artist 50%s</i>',
                "catalog_number": '<em>CAT 100%d</em>', "format_summary": '<u>Provider 50%s</u>',
                "release_date": '<strong>2001-01-01 & date</strong>'}
    item_id = item_write.insert_item(db, title=metadata["title"], authors=metadata["authors"], media_type="cassette")
    music_catalog.save_release(db, item_id, metadata)
    db.commit()
    gnu_catalog({"Your music": '<b data-hostile="label">Music 100%%</b>',
                 "%(count)s shown": '<b data-hostile="label">%(count)s shown 100%%</b>'}
                | {value: "WRONGLY TRANSLATED" for value in metadata.values()})
    _, root = _get(viewer_client, "/music", headers={"Accept-Language": "it"})
    local = _one(root.find(**{"data-testid": "music-local-items"}))
    assert '<b data-hostile="label">Music 100%</b>' in local.text
    assert '<b data-hostile="label">1 shown 100%</b>' in local.text
    for value in metadata.values():
        assert value in local.text
    assert not any(local.find(tag) for tag in ("b", "i", "em", "u", "strong"))
    assert "WRONGLY TRANSLATED" not in local.text


def test_library_gnu_holding_labels_escape_before_interpolating_raw_last_seen(viewer_client, db, gnu_catalog):
    paper, ebook, audio, sources, tag = _library_seed(db)
    last_seen = '<i>Seen 50%s & observation</i>'
    db.execute("UPDATE source_records SET last_seen_at = ?", (last_seen,))
    db.commit()
    gnu_catalog({"Last seen: %(when)s": '<b data-hostile="label">Last seen: %(when)s 100%%</b>',
                 **{label: f'<b data-hostile="label">{label} 100%%</b>' for label in
                    ("Owned", "Wishlist", "Physical copy", "Copies and sources", "Available", "Read", "Listen")}})
    _, root = _get(viewer_client, "/api/library/search", headers={"Accept-Language": "it"})
    results = _one(root.find(**{"data-testid": "library-results"}))
    assert f'<b data-hostile="label">Last seen: {last_seen} 100%</b>' in results.text
    assert '<b data-hostile="label">Physical copy 100%</b>' in results.text
    assert '<b data-hostile="label">Owned 100%</b>' in results.text
    assert '<b data-hostile="label">Wishlist 100%</b>' in results.text
    assert not results.find("b") and not results.find("i")
