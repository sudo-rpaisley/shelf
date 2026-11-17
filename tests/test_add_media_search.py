"""Independent native Add-host contract tests, authored against 0e8139bc.

Only provider boundaries are substituted. Forms, session/CSRF verification,
purpose-separated producers and the catalogue remain the real application.
These helpers are local to the three additive Add test modules, not conftest.
"""

import asyncio
import copy
import inspect
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

import pytest
import httpx
import jwt

from app import config
from app.database import get_db, set_setting
from app.services import (
    bnf, covers, discogs, googlebooks, hardcover, igdb, manual_cover, music_artwork,
    musicbrainz, openlibrary, outbound, provider_result, shop_metadata, tmdb,
    upcitemdb,
)


ISBN = "9780441172719"
MBID = "11111111-1111-4111-8111-111111111111"
MUSIC_FORMATS = ("cd", "vinyl", "cassette", "digital_music")
FORMAT_LABELS = {"cd": "CD", "vinyl": "Vinyl", "cassette": "Cassette", "digital_music": "Digital Media"}
BOOK = {
    "title": "Dune <edition>", "subtitle": "A native confirmation", "authors": "Frank Herbert",
    "publisher": "Stored Publisher", "publish_year": 1965, "page_count": 412,
    "description": "Resolved book description", "series_name": "Dune", "series_position": 1,
    "language": "en", "cover_url": "https://covers.openlibrary.org/b/id/7-L.jpg", "cover_id": 7,
}
GAME = {
    "igdb_id": 123, "title": "Selected Game", "developer": "Preview-only Developer",
    "publisher": "Game Publisher", "publish_year": 2024, "series_name": "Game Series",
    "description": "Selected game details", "platform_names": ["Nintendo Switch"],
    "cover_url": "https://images.igdb.com/igdb/image/upload/t_cover_big/fixture.jpg",
}
MOVIE = {
    "tmdb_id": 456, "title": "Selected Movie", "publish_year": 2023,
    "description": "Exact selected movie snapshot",
    "cover_url": "https://image.tmdb.org/t/p/w500/fixture.jpg",
}


def release(provider="musicbrainz", media_type="cassette", **changes):
    values = {
        "title": "Hits Out of Hell", "artist_credit": "Meat Loaf", "release_date": "1984-10-01",
        "country": "GB", "label": "Epic", "catalog_number": "EPC 40-26156",
        "format_summary": FORMAT_LABELS[media_type], "source": provider, "barcode": "078073003501",
        "media": [{"position": 1, "format": FORMAT_LABELS[media_type], "tracks": [
            {"position": 1, "number": "A1", "title": "Provider track fact", "duration_ms": 123000},
        ]}],
        "images": [{"url": "https://i.discogs.com/fixture.jpg", "label": "Provider artwork"}],
    }
    if provider == "musicbrainz":
        values["musicbrainz_release_id"] = MBID
    else:
        values.update(discogs_release_id=77, provider="discogs", provider_release_id=77,
                      source_url="https://www.discogs.com/release/77")
    values.update(changes)
    return values


class Forms(HTMLParser):
    """Read real successful form controls, independently of attribute ordering."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.forms, self.current, self.select, self.option, self.textarea = [], None, None, None, None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"attrs": attrs, "controls": []}
            self.forms.append(self.current)
        if self.current is None:
            return
        if tag == "input":
            self.current["controls"].append(attrs)
        elif tag == "select":
            self.select = {**attrs, "options": []}
            self.current["controls"].append(self.select)
        elif tag == "option" and self.select is not None:
            self.option = {**attrs, "text": ""}
            self.select["options"].append(self.option)
        elif tag == "textarea":
            self.textarea = {**attrs, "value": ""}
            self.current["controls"].append(self.textarea)

    def handle_data(self, data):
        if self.option is not None:
            self.option["text"] += data
        if self.textarea is not None:
            self.textarea["value"] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None
        elif tag == "select":
            self.select = None
        elif tag == "option":
            self.option = None
        elif tag == "textarea":
            self.textarea = None

    def named(self, testid):
        return [form for form in self.forms if form["attrs"].get("data-testid") == testid]


def form(response, testid, index=0):
    assert response.status_code == 200, (response.status_code, response.text[:500])
    choices = Forms(response.text).named(testid)
    assert len(choices) > index, f"Missing real {testid} form: {response.text[:1000]}"
    selected = choices[index]
    assert selected["attrs"].get("method", "get").casefold() == "post"
    assert urlsplit(selected["attrs"]["action"]).netloc in ("", "testserver")
    return selected


def pairs(selected):
    values = []
    for control in selected["controls"]:
        name = control.get("name")
        if not name or "disabled" in control:
            continue
        kind = control.get("type", "text").casefold()
        if kind in {"submit", "button", "file", "reset"}:
            continue
        if kind in {"checkbox", "radio"} and "checked" not in control:
            continue
        if "options" in control:
            options = control["options"]
            chosen = [option for option in options if "selected" in option] or options[:1]
            values.extend((name, option.get("value", option["text"])) for option in chosen)
        else:
            values.append((name, control.get("value", "on" if kind == "checkbox" else "")))
    return values


def value(selected, name):
    values = [entry for key, entry in pairs(selected) if key == name]
    assert len(values) == 1, (name, values)
    return values[0]


def replace(values, **changes):
    kept = [(key, val) for key, val in values if key not in changes]
    for key, val in changes.items():
        if val is None:
            continue
        kept.extend((key, str(part)) for part in (val if isinstance(val, (tuple, list)) else [val]))
    return kept


def post(client, action, values, **kwargs):
    """Native browser encoding, not httpx's version-dependent list-data handling."""
    return client.post(action, content=urlencode(values).encode("ascii"),
                       headers={"Content-Type": "application/x-www-form-urlencoded"},
                       follow_redirects=False, **kwargs)


def page(client, **query):
    response = client.get("/add", params=query, follow_redirects=False)
    assert response.status_code == 200, f"Add host unavailable: {response.status_code} {response.text[:300]}"
    assert 'data-testid="add-media"' in response.text
    return response


def search(client, **changes):
    selected = form(page(client), "add-media-search")
    values = replace(pairs(selected), mode="add", media_type="book", method="title", provider="auto",
                     q="Dune", artist="", title_hint="", platform="", country="")
    return post(client, selected["attrs"]["action"], replace(values, **changes))


def preview(client, searched, index=0):
    selected = form(searched, "add-media-preview", index)
    assert urlsplit(selected["attrs"]["action"]).path == "/add/preview"
    return post(client, selected["attrs"]["action"], pairs(selected))


def snapshot(db):
    """Actual catalogue, copies, relationships, provenance and cover bytes; no secrets."""
    names = (
        "items", "item_copies", "checkouts", "lists", "list_items", "tags", "item_tags",
        "music_releases", "music_media", "music_tracks", "music_identifiers", "scan_log",
        "authors", "item_authors", "item_links", "reading_log", "series_meta", "legacy_book_mappings",
        "user_item_state", "user_item_events", "cover_sync_blocks", "item_metadata_provenance",
        "source_records", "source_binding_versions", "reading_sessions", "reading_journal",
    )
    existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = {name: sorted((tuple(row) for row in db.execute(f'SELECT * FROM "{name}"')), key=repr)
            for name in names if name in existing}
    files = {str(path.relative_to(config.COVERS_DIR)): path.read_bytes()
             for path in config.COVERS_DIR.rglob("*") if path.is_file()}
    return rows, files


def no_cookie(response):
    assert not response.headers.get_list("set-cookie"), response.headers.get_list("set-cookie")
    assert response.headers.get("cache-control") == "no-store"


class ErrorEnvelope(HTMLParser):
    """Inspect only error-envelope structure and accessible text, not app actions."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.nodes, self.stack, self.outside, self.comments, self.declarations = [], [], [], [], []
        self.processing_instructions = []
        self.feed(text)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        style = "".join(attrs.get("style", "").casefold().split())
        hidden = (bool(self.stack and self.nodes[self.stack[-1]]["hidden"]) or tag in {"head", "script", "style", "template"}
                  or "hidden" in attrs or "inert" in attrs
                  or attrs.get("aria-hidden", "").casefold() == "true" or "display:none" in style or "visibility:hidden" in style)
        self.nodes.append({"tag": tag, "attrs": attrs, "duplicate_attrs": len(attrs) != len(attributes), "hidden": hidden, "text": ""})
        if tag not in self.VOID:
            self.stack.append(len(self.nodes) - 1)

    def handle_endtag(self, tag):
        for position in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[position]]["tag"] == tag:
                del self.stack[position:]
                break

    def handle_data(self, text):
        if not self.stack:
            self.outside.append(text)
        elif not self.nodes[self.stack[-1]]["hidden"]:
            for index in self.stack:
                self.nodes[index]["text"] += text

    def handle_comment(self, text):
        self.comments.append(text)

    def handle_decl(self, text):
        self.declarations.append(text)

    def handle_pi(self, text):
        self.processing_instructions.append(text)


PROVIDER_OUTCOMES = {"no_credential", "rejected", "rate_limited", "transport_failed", "search_timeout", "unresolved", "no_match"}

# Recorded, immutable pre-repair protocol copy, not imports of writer WIP:
# add-media-parent-transport-v1-reviewed and add-media-backend-author-v3.
EARLY_ERROR_TEXT = {
    "invalid_form": "Submit each control once using the Add media form.",
    "unsupported_media_type": "Submit the form without attachments or compression.",
    "body_too_large": "The Add media form is too large.",
    "query_too_large": "The Add media address is too long.",
    "actor_changed": "Your session or account changed. Open Add media again before continuing.",
    "not_found": "This Add media address was not found.",
    "method_not_allowed": "Use the method shown on the Add media form.",
    "outcome_unknown": "The request could not be completed. If you were saving, check your library before trying again.",
}
ROUTER_ERROR_TEXT = {
    "invalid_form": "The submitted form could not be read. Reload Add Media and try again.",
    "invalid_input": "Check the selected format, method and entered values.",
    "actor_changed": "Your session changed or expired. Reload Add Media before continuing.",
    "stale_review": "That preview expired or changed. Search and preview the selection again.",
    "intent_changed": "The add intent changed. Search and preview the selection again.",
    "format_conflict": "The selected format contradicts the provider's release details. Choose the correct pressing.",
    "music_disabled": "Music matching is turned off. You can still add music manually.",
    "outcome_unknown": "The save outcome is unknown. Check your collection before submitting again.",
}
TIMEOUT_HUMAN_TEXT = "The search timed out. No automatic retry is made. You can enter details manually or deliberately search again."


def accessible_error_text(response, code, *, outcome=False):
    parsed = ErrorEnvelope(response.text)
    roles = {"alert", "status"} if outcome else {"alert"}
    texts = [" ".join(node["text"].split()) for node in parsed.nodes
             if node["attrs"].get("role") in roles and not node["hidden"] and not node["duplicate_attrs"]]
    human = [text for text in texts if any(character.isalpha() for character in text.replace(code, ""))]
    assert human, "A real accessible human error/outcome is required, not just model metadata or an identifier"
    return human


def minimal_error_paragraph(response, code):
    parsed = ErrorEnvelope(response.text)
    assert len(parsed.nodes) == 1 and parsed.nodes[0]["tag"] == "p"
    node = parsed.nodes[0]
    assert node["attrs"] == {"role": "alert", "data-testid": "add-media-error", "data-error-code": code}
    assert not node["duplicate_attrs"] and not node["hidden"]
    assert not "".join(parsed.outside).strip() and not parsed.comments and not parsed.declarations
    assert not parsed.processing_instructions
    assert code in ROUTER_ERROR_TEXT
    assert " ".join(node["text"].split()) == ROUTER_ERROR_TEXT[code]
    return accessible_error_text(response, code)[0]


def error(response, status, code):
    assert response.status_code == status, (response.status_code, response.text[:1000])
    no_cookie(response)
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    context = getattr(response, "context", None)
    if isinstance(context, dict) and "add" in context:
        assert media_type == "text/html"
        field = "provider_outcome" if code in PROVIDER_OUTCOMES else "error_code"
        assert context["add"][field] == code, (field, context["add"][field], code)
        accessible_error_text(response, code, outcome=field == "provider_outcome")
    elif media_type == "application/json":
        assert code in EARLY_ERROR_TEXT
        assert response.json() == {"code": code, "message": EARLY_ERROR_TEXT[code]}
    else:
        assert media_type == "text/html"
        minimal_error_paragraph(response, code)


class ProviderBoundary:
    def __init__(self, monkeypatch):
        self.monkeypatch, self.calls, self.violations, self.originals = monkeypatch, [], [], {}

    def install(self, module, name, answer=None, handler=None, *, forbidden=False):
        label = module.__name__ + "." + name
        self.originals.setdefault((module, name), getattr(module, name))

        async def boundary(*args, **kwargs):
            self.calls.append((label, args, kwargs))
            if forbidden:
                self.violations.append(label)
                raise AssertionError("Unexpected provider/artwork call: " + label)
            try:
                result = handler(*args, **kwargs) if handler else copy.deepcopy(answer)
                return await result if inspect.isawaitable(result) else result
            except AssertionError as exc:
                self.violations.append((label, str(exc)))
                raise

        self.monkeypatch.setattr(module, name, boundary)
        return boundary

    def real(self, module, name):
        self.monkeypatch.setattr(module, name, self.originals[(module, name)])

    def seen(self, module, name):
        return [(args, kwargs) for label, args, kwargs in self.calls if label == module.__name__ + "." + name]

    def clear(self):
        self.calls.clear()


@pytest.fixture
def io(monkeypatch):
    guard = ProviderBoundary(monkeypatch)
    targets = {
        bnf: ("lookup",),
        openlibrary: ("search_books", "lookup"), googlebooks: ("lookup",), hardcover: ("lookup_by_isbn",),
        igdb: ("search_games", "lookup_game"), tmdb: ("search_movies", "lookup_by_title"),
        upcitemdb: ("lookup",), musicbrainz: ("search_releases", "lookup_release", "cover_art"),
        discogs: ("search_public_releases", "lookup_public_release", "search_releases", "lookup_release"),
        covers: ("download_cover", "_download_to_item"), manual_cover: ("download",),
        music_artwork: ("release_candidates", "download"), shop_metadata: ("preview",), outbound: ("fetch",),
    }
    for module, names in targets.items():
        for name in names:
            guard.install(module, name, forbidden=True)
    yield guard
    # Providers intentionally swallow Exception. Raising alone is not an oracle.
    assert guard.violations == [], f"Swallowed unexpected calls/assertions: {guard.violations!r}"


@pytest.fixture
def credentials():
    with get_db() as db:
        for key in ("igdb_client_id", "igdb_client_secret", "tmdb_api_key"):
            set_setting(db, key, "add-contract-fixture-not-a-real-credential")


@pytest.fixture
def catalogue(io, credentials):
    io.install(openlibrary, "search_books", provider_result.found("openlibrary", [
        {"isbn": ISBN, "title": BOOK["title"], "authors": BOOK["authors"], "publish_year": 1965},
    ]))
    io.install(openlibrary, "lookup", provider_result.found("openlibrary", BOOK))
    io.install(igdb, "search_games", provider_result.found("igdb", [GAME]))
    io.install(igdb, "lookup_game", GAME)
    io.install(tmdb, "search_movies", provider_result.found("tmdb", [MOVIE]))
    io.install(musicbrainz, "search_releases", provider_result.found("musicbrainz", [release()]))
    io.install(musicbrainz, "lookup_release", provider_result.found("musicbrainz", release()))
    io.install(discogs, "search_public_releases", provider_result.found("discogs", [release("discogs")]))
    io.install(discogs, "lookup_public_release", provider_result.found("discogs", release("discogs")))
    return io


@pytest.fixture
def clock(monkeypatch):
    """Advance standard producer/verification clocks; never read/re-sign a key."""
    from app import auth

    current = [time.time()]

    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(current[0], tz or timezone.utc)

    monkeypatch.setattr(time, "time", lambda: current[0])
    monkeypatch.setattr(auth, "datetime", ControlledDatetime)
    monkeypatch.setattr(jwt.api_jwt, "datetime", ControlledDatetime)
    return current


def test_add_page_is_native_read_only_host(editor_client, db, io):
    before = snapshot(db)
    response = page(editor_client)
    selected = form(response, "add-media-search")
    assert urlsplit(selected["attrs"]["action"]).path == "/add/search"
    assert value(selected, "mode") == "add"
    assert value(selected, "media_type") == ""
    assert value(selected, "_csrf") == editor_client.cookies.get("csrf_token")
    marker = value(selected, "context")
    assert marker.isascii() and 0 < len(marker) <= 4096
    no_cookie(response)
    assert io.calls == []
    assert snapshot(db) == before


def test_host_has_the_ratified_complete_context_schema(editor_client, io):
    response = page(editor_client)
    add = response.context["add"]
    assert set(add) == {
        "input", "context", "intent", "intent_context", "results", "preview", "manual", "receipt",
        "error_code", "provider_outcome", "host_warning", "media_types", "platforms", "music_enabled", "status_types",
    }
    assert isinstance(add["input"], dict) and isinstance(add["context"], str)
    assert add["intent"] is None and add["intent_context"] == "" and add["results"] == ()
    assert add["preview"] is None and add["manual"] is None and add["receipt"] is None
    assert add["error_code"] is None and add["provider_outcome"] is None and add["host_warning"] is None
    assert add["media_types"] == config.MEDIA_TYPES
    assert set(add["status_types"]) == config.STATUS_MEDIA_TYPES
    assert io.calls == []


def test_search_captures_every_signed_intent_field(editor_client, catalogue):
    response = search(editor_client, mode="wishlist", media_type="cassette", method="catalog_number", provider="discogs",
                      q="4026156", artist="Meat Loaf", title_hint="Hits Out of Hell", country="Europe")
    assert response.status_code == 200 and is_dataclass(response.context["add"]["intent"])
    assert asdict(response.context["add"]["intent"]) == {
        "mode": "wishlist", "media_type": "cassette", "method": "catalog_number", "query": "4026156",
        "artist": "Meat Loaf", "title_hint": "Hits Out of Hell", "platform": "", "country": "Europe",
        "music_provider": "discogs",
    }


@pytest.mark.parametrize("changes", [
    {"mode": ""}, {"mode": "Add"}, {"mode": "delete"}, {"mode": "all"},
    {"media_type": ""}, {"media_type": "auto"}, {"media_type": "kids_book"}, {"media_type": "BOOK"},
    {"method": ""}, {"method": "automatic"}, {"method": "release_id"}, {"provider": ""},
    {"provider": "all"}, {"provider": "Discogs"}, {"provider": "discogs"}, {"provider": "musicbrainz"},
    {"media_type": "dvd", "method": "isbn", "q": ISBN},
    {"media_type": "video_game", "method": "isbn", "q": ISBN},
    {"media_type": "cassette", "method": "isbn", "q": ISBN},
    {"method": "catalog_number", "q": "4026156"},
    {"artist": "Inapplicable artist"}, {"title_hint": "Inapplicable hint"},
    {"platform": "switch"}, {"country": "GB"}, {"media_type": "dvd", "platform": "switch"},
    {"media_type": "video_game", "platform": "not-configured"},
    {"media_type": "video_game", "platform": "other"},  # configured, but NOT IGDB-filterable
    {"media_type": "cassette", "platform": "switch"},
    {"media_type": "cassette", "country": "Europe"}, {"media_type": "cassette", "country": "ＧＢ"},
    {"media_type": "cassette", "country": "G1"}, {"q": ""},
])
def test_input_classification_refuses_before_any_provider(editor_client, db, io, changes):
    before = snapshot(db)
    response = search(editor_client, **changes)
    error(response, 422, "invalid_input")
    assert io.calls == []
    assert snapshot(db) == before


@pytest.mark.parametrize("raw", [
    "0441172710",  # invalid ORIGINAL ISBN10; permissive conversion can invent a valid 13
    "0-441-17271-0", "9780441172710", "4006381333931", "078073003501", "4026156",
    "ISBN 9780441172719", "9780441172719!", "978/0441172719", "９７８０４４１１７２７１９",
    "978044117271９", "978\u00a00441172719", "978_0441172719", "978044117271X",
    "-9780441172719", "9780441172719-", "04411727X7",
])
def test_strict_original_isbn_grammar_and_checksum(editor_client, db, io, raw):
    before = snapshot(db)
    error(search(editor_client, method="isbn", q=raw), 422, "invalid_input")
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.parametrize("raw,canonical", [
    (ISBN, ISBN), ("978-0-441-17271-9", ISBN), ("0 441 17271 7", ISBN),
    ("0441172717", ISBN), ("080442957X", "9780804429573"), ("0-8044-2957-x", "9780804429573"),
    ("9791090636071", "9791090636071"),
])
def test_valid_isbn_uses_existing_lookup_cascade_without_writing(editor_client, db, io, raw, canonical):
    # Ratified V3/V1-prefix erratum: 97910 routes through the native BnF
    # registry before Open Library; BnF otherwise uses direct client.get().
    if canonical == "9791090636071":
        io.install(bnf, "lookup", provider_result.no_match("bnf", status=200))
    io.install(openlibrary, "lookup", provider_result.found("openlibrary", BOOK))
    before = snapshot(db)
    response = search(editor_client, method="isbn", q=raw)
    assert response.status_code == 200
    calls = io.seen(openlibrary, "lookup")
    assert len(calls) == 1 and calls[0][0][0] == canonical
    assert_native_isbn_cascade_calls(io.calls, canonical)
    assert snapshot(db) == before
    assert Forms(response.text).named("add-media-preview"), "Valid ISBN must produce a real selectable candidate"


@pytest.mark.parametrize("raw", [
    "4026156", "078073003500", "4006381333932", ISBN, "9791090636071",
    "UPC078073003501", "078073003501!", "07807300.3501", "０７８０７３００３５０１",
    "07807300350١", "078073\u00a0003501", "-078073003501", "078073003501-",
])
def test_retail_barcode_is_ascii_checksum_valid_and_not_bookland(editor_client, db, io, raw):
    before = snapshot(db)
    error(search(editor_client, method="barcode", media_type="dvd", q=raw), 422, "invalid_input")
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.parametrize("media_type", ["book", "dvd", "video_game"])
@pytest.mark.parametrize("raw,wire", [("078073003501", "078073003501"), ("0-78073-00350-1", "078073003501"),
                                      ("4006381333931", "4006381333931")])
def test_nonmusic_barcode_is_one_facts_lookup_not_a_mutating_scan_or_title_ladder(
    editor_client, db, io, media_type, raw, wire,
):
    io.install(upcitemdb, "lookup", provider_result.found("upcitemdb", {
        "title": "Retail Product [DVD]", "category": "Media", "brand": "Retail Brand",
        "images": ["https://retailer.example.test/cover.jpg"],
    }))
    before = snapshot(db)
    response = search(editor_client, media_type=media_type, method="barcode", q=raw)
    assert response.status_code == 200
    assert len(io.calls) == 1 and io.seen(upcitemdb, "lookup")[0][0][0] == wire
    assert Forms(response.text).named("add-media-manual")
    assert not Forms(response.text).named("add-media-save")
    assert snapshot(db) == before


@pytest.mark.parametrize("media_type", ["tv_series", "digital_video", "magazine"])
def test_unsupported_title_lookup_is_useful_manual_with_zero_io(editor_client, db, io, media_type):
    before = snapshot(db)
    response = search(editor_client, media_type=media_type, q="A deliberately entered title")
    assert response.status_code == 200
    assert "manual" in response.text.casefold()
    selected = form(response, "add-media-manual")
    assert value(selected, "context") and value(selected, "title") == "A deliberately entered title"
    assert not Forms(response.text).named("add-media-preview")
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.parametrize("media_type", tuple(config.MEDIA_TYPES))
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_every_concrete_format_has_a_zero_provider_manual_path(editor_client, db, io, media_type, mode):
    before = snapshot(db)
    response = search(editor_client, media_type=media_type, mode=mode, method="manual", q="A manual title")
    selected = form(response, "add-media-manual")
    assert value(selected, "context")
    assert urlsplit(selected["attrs"]["action"]).path == "/add/manual"
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.parametrize("field,limit,changes", [
    ("q", 200, {}), ("artist", 200, {"media_type": "cassette"}),
    ("title_hint", 200, {"media_type": "cassette", "method": "catalog_number", "q": "4026156"}),
    ("q", 100, {"media_type": "cassette", "method": "catalog_number"}),
    ("q", 32, {"media_type": "cassette", "method": "barcode"}),
    ("platform", 100, {"media_type": "video_game"}),
    ("country", 80, {"media_type": "cassette", "provider": "discogs"}),
])
def test_literal_search_field_limits_are_not_silent_truncation(editor_client, db, io, field, limit, changes):
    before = snapshot(db)
    submitted = {**changes, field: "x" * (limit + 1)}
    error(search(editor_client, **submitted), 422, "invalid_input")
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.parametrize("media_type", MUSIC_FORMATS)
@pytest.mark.parametrize("method,q,title", [("title", "Hits Out of Hell", "Hits Out of Hell"),
    ("catalog_number", "4026156", ""), ("barcode", "078073003501", "")])
def test_direct_discogs_all_formats_and_catalogue_only_never_call_musicbrainz(
    editor_client, db, io, media_type, method, q, title,
):
    io.install(discogs, "search_public_releases", provider_result.found("discogs", [release("discogs", media_type)]))
    before = snapshot(db)
    response = search(editor_client, media_type=media_type, provider="discogs", method=method, q=q)
    selected = form(response, "add-media-preview")
    assert value(selected, "selection")
    assert "Discogs" in response.text and "Hits Out of Hell" in response.text
    calls = io.seen(discogs, "search_public_releases")
    assert len(calls) == 1 and len(io.calls) == 1
    args, kwargs = calls[0]
    assert args[0] == title
    assert kwargs["media_type"] == media_type and kwargs["limit"] == 20
    assert (kwargs.get("catalog_number") or "") == (q if method == "catalog_number" else "")
    assert (kwargs.get("barcode") or "") == (q if method == "barcode" else "")
    assert snapshot(db) == before
    if method == "catalog_number":
        assert "40-26156" not in args[0] and "Bat Out of Hell" not in response.text
        manual = form(response, "add-media-manual")
        assert value(manual, "title") == "" and value(manual, "identifier") == ""


def test_direct_discogs_applicable_refinements_preserve_original_catalogue(editor_client, io):
    io.install(discogs, "search_public_releases", provider_result.found("discogs", [release("discogs")]))
    response = search(editor_client, media_type="cassette", provider="discogs", method="catalog_number", q="4026156",
                      artist="Meat Loaf", title_hint="Hits Out of Hell", country="Europe")
    assert response.status_code == 200
    args, kwargs = io.seen(discogs, "search_public_releases")[0]
    assert args[0] == "Hits Out of Hell"
    assert kwargs["catalog_number"] == "4026156" and kwargs["artist"] == "Meat Loaf"
    assert kwargs["country"] == "Europe" and kwargs["media_type"] == "cassette" and kwargs["limit"] == 20
    assert len(io.calls) == 1


@pytest.mark.parametrize("provider", ["auto", "musicbrainz"])
def test_automatic_catalogue_only_remains_musicbrainz_only(editor_client, io, provider):
    io.install(musicbrainz, "search_releases", provider_result.found("musicbrainz", []))
    response = search(editor_client, media_type="cassette", provider=provider, method="catalog_number", q="4026156")
    assert response.status_code == 200
    assert len(io.calls) == 1
    args, kwargs = io.seen(musicbrainz, "search_releases")[0]
    assert args[0] == "" and kwargs["catalog_number"] == "4026156"
    assert kwargs["media_type"] == "cassette" and kwargs["limit"] == 30
    manual = form(response, "add-media-manual")
    assert value(manual, "title") == "" and value(manual, "identifier") == ""


@pytest.mark.parametrize("provider,kind,empty,artist,fallback", [
    ("auto", "cassette", True, "Meat Loaf", True),
    ("auto", "cassette", False, "Meat Loaf", False),
    ("auto", "cassette", True, "", False),
    ("auto", "cd", True, "Meat Loaf", False),
    ("auto", "vinyl", True, "Meat Loaf", False),
    ("auto", "digital_music", True, "Meat Loaf", False),
    ("musicbrainz", "cassette", True, "Meat Loaf", False),
])
def test_default_genuine_empty_cassette_title_artist_policy_is_unchanged(
    editor_client, io, provider, kind, empty, artist, fallback,
):
    io.install(musicbrainz, "search_releases", provider_result.found("musicbrainz", [] if empty else [release(media_type=kind)]))
    if fallback:
        io.install(discogs, "search_public_releases", provider_result.found("discogs", [release("discogs")]))
    response = search(editor_client, media_type=kind, provider=provider, q="Hits Out of Hell", artist=artist)
    assert response.status_code == 200
    assert len(io.seen(musicbrainz, "search_releases")) == 1
    assert len(io.seen(discogs, "search_public_releases")) == int(fallback)
    assert len(io.calls) == 1 + int(fallback)
    if fallback:
        assert [label for label, _, _ in io.calls] == [
            "app.services.musicbrainz.search_releases", "app.services.discogs.search_public_releases",
        ]


@pytest.mark.parametrize("answer,code", [
    (provider_result.rejected("musicbrainz", status=403), "rejected"),
    (provider_result.rate_limited("musicbrainz"), "rate_limited"),
    (provider_result.transport_failed("musicbrainz"), "transport_failed"),
    (provider_result.no_match("musicbrainz", status=404), "transport_failed"),
    pytest.param("malformed_wire", "transport_failed", id="answer4-transport_failed"),
    (provider_result.found("musicbrainz", [{"title": "No exact release identity"}]), "transport_failed"),
])
def test_music_failure_is_not_empty_and_does_not_enable_fallback(editor_client, io, answer, code):
    if answer == "malformed_wire":
        install_malformed_mb_wire(io)
    else:
        io.install(musicbrainz, "search_releases", answer)
    response = search(editor_client, media_type="cassette", q="Hits Out of Hell", artist="Meat Loaf")
    assert response.status_code == 200
    assert response.context["add"]["provider_outcome"] == code
    assert Forms(response.text).named("add-media-manual")
    if answer == "malformed_wire":
        assert_malformed_mb_wire_calls(io.calls)
    else:
        assert len(io.calls) == 1 and len(io.seen(musicbrainz, "search_releases")) == 1


@pytest.mark.parametrize("method,q,title_hint,expected", [
    ("title", "Hits Out of Hell", "", 1), ("catalog_number", "4026156", "Hits Out of Hell", 2),
    ("barcode", "078073003501", "Hits Out of Hell", 3),
])
def test_explicit_musicbrainz_logical_search_budget_not_http_count(editor_client, io, method, q, title_hint, expected):
    io.install(musicbrainz, "search_releases", provider_result.found("musicbrainz", []))
    response = search(editor_client, provider="musicbrainz", media_type="cassette", method=method, q=q,
                      title_hint=title_hint, artist="Meat Loaf")
    assert response.status_code == 200
    assert len(io.calls) == expected and len(io.seen(musicbrainz, "search_releases")) == expected
    if method == "barcode":
        assert [kwargs.get("barcode") for _, kwargs in io.seen(musicbrainz, "search_releases")[:2]] == [
            "078073003501", "0078073003501",
        ]


def isbn_for(index):
    stem = "978000" + f"{index:06d}"
    check = (-sum(int(digit) * (1 if pos % 2 == 0 else 3) for pos, digit in enumerate(stem))) % 10
    return stem + str(check)


@pytest.mark.parametrize("family,cap", [("book", 10), ("dvd", 10), ("video_game", 10),
                                      ("musicbrainz", 30), ("discogs", 20)])
def test_host_enforces_local_caps_even_if_adapter_ignores_requested_limit(editor_client, io, credentials, family, cap):
    if family == "book":
        module, name = openlibrary, "search_books"
        rows = [{"title": f"Book sentinel {i}", "isbn": isbn_for(i), "authors": "Author"} for i in range(cap + 1)]
    elif family == "dvd":
        module, name = tmdb, "search_movies"
        rows = [{**MOVIE, "title": f"Movie sentinel {i}", "tmdb_id": 100 + i} for i in range(cap + 1)]
    elif family == "video_game":
        module, name = igdb, "search_games"
        rows = [{**GAME, "title": f"Game sentinel {i}", "igdb_id": 100 + i} for i in range(cap + 1)]
    else:
        module, name = (musicbrainz, "search_releases") if family == "musicbrainz" else (discogs, "search_public_releases")
        rows = [release(family, title=f"Album sentinel {i}", musicbrainz_release_id=f"{i + 1:08x}-1111-4111-8111-111111111111",
                        discogs_release_id=100 + i, provider_release_id=str(100 + i)) for i in range(cap + 1)]
    io.install(module, name, provider_result.found(module.__name__.rsplit(".", 1)[-1], rows))
    response = search(editor_client, media_type=family if family in {"book", "dvd", "video_game"} else "cassette",
                      provider=family if family in {"musicbrainz", "discogs"} else "auto")
    assert response.status_code == 200
    assert len(Forms(response.text).named("add-media-preview")) == cap
    assert rows[cap]["title"] not in response.text and rows[cap - 1]["title"] in response.text
    assert len(io.calls) == 1 and io.seen(module, name)[0][1]["limit"] == cap


@pytest.mark.parametrize("family,module,name", [("book", openlibrary, "search_books"),
                                                ("dvd", tmdb, "search_movies"), ("video_game", igdb, "search_games")])
def test_lossy_empty_adapter_uses_honest_unresolved_wording(editor_client, io, credentials, family, module, name):
    io.install(module, name, provider_result.no_match(module.__name__.rsplit(".", 1)[-1]))
    response = search(editor_client, media_type=family)
    assert response.status_code == 200
    assert "No usable results were returned" in response.text
    assert response.context["add"]["provider_outcome"] == "unresolved"
    assert "malformed" not in response.text.casefold() and "credential was rejected" not in response.text.casefold()
    assert Forms(response.text).named("add-media-manual") and len(io.calls) == 1


@pytest.mark.parametrize("media_type,wire_format", [("cd", "CD"), ("vinyl", "Vinyl"),
                                                   ("cassette", "Cassette"), ("digital_music", "File")])
@pytest.mark.parametrize("configured", [False, True])
def test_direct_discogs_real_adapter_fixed_release_only_wire_and_no_rejected_token_retry(
    editor_client, io, media_type, wire_format, configured,
):
    io.real(discogs, "search_public_releases")
    if configured:
        with get_db() as db:
            set_setting(db, "discogs_token", "synthetic-discogs-contract-token")

    def fetch(client, method, url, **kwargs):
        assert method == "GET" and url == "https://api.discogs.com/database/search"
        assert kwargs["follow_redirects"] is False
        assert kwargs["params"] == {"type": "release", "per_page": "20", "page": "1",
                                    "catno": "4026156", "format": wire_format}
        headers = kwargs["headers"]
        assert ("Authorization" in headers) is configured
        assert "synthetic-discogs-contract-token" not in url and "token" not in kwargs["params"]
        return httpx.Response(401 if configured else 200, json={"results": []})

    io.install(outbound, "fetch", handler=fetch)
    response = search(editor_client, provider="discogs", media_type=media_type, method="catalog_number", q="4026156")
    assert response.status_code == 200
    assert len(io.calls) == 1
    assert "synthetic-discogs-contract-token" not in response.text
    if configured:
        assert response.context["add"]["provider_outcome"] == "rejected"


@pytest.mark.asyncio
async def test_actual_thirty_second_search_deadline_cancels_and_awaits_provider_cleanup(editor_client, io, db):
    # Deliberately uses the REAL 30-second deadline, not a patched timeout or a
    # preconstructed TimeoutError. Events, not sleeps, control cancellation cleanup.
    selected = form(page(editor_client), "add-media-search")
    body = replace(pairs(selected), mode="add", media_type="cassette", method="title", provider="musicbrainz",
                   q="Hits Out of Hell", artist="Meat Loaf", title_hint="", platform="", country="")
    entered, cancelled, release_cleanup, cleaned = (asyncio.Event() for _ in range(4))

    async def pending(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release_cleanup.wait()
            cleaned.set()
            raise

    io.install(musicbrainz, "search_releases", handler=pending)
    before = snapshot(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_client.app), base_url="https://testserver",
                                cookies=editor_client.cookies, headers=dict(editor_client.headers), timeout=None) as client:
        task = asyncio.create_task(client.post("/add/search", content=urlencode(body).encode(),
                                              headers={"Content-Type": "application/x-www-form-urlencoded"}))
        try:
            try:
                await asyncio.wait_for(entered.wait(), 5)
                await asyncio.wait_for(cancelled.wait(), 33)
                assert not task.done(), "Host returned while its provider cancellation cleanup was detached"
            finally:
                release_cleanup.set()
            response = await asyncio.wait_for(task, 5)
        finally:
            release_cleanup.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert cleaned.is_set() and response.status_code == 200
    assert_search_timeout_human_outcome(response)
    assert Forms(response.text).named("add-media-manual") and snapshot(db) == before


@pytest.mark.asyncio
async def test_request_cancellation_is_not_reported_as_provider_failure_or_detached(editor_client, io, db):
    selected = form(page(editor_client), "add-media-search")
    body = replace(pairs(selected), mode="add", media_type="cassette", method="title", provider="musicbrainz",
                   q="Hits Out of Hell", artist="Meat Loaf", title_hint="", platform="", country="")
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    io.install(musicbrainz, "search_releases", handler=pending)
    before = snapshot(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_client.app), base_url="https://testserver",
                                cookies=editor_client.cookies, headers=dict(editor_client.headers)) as client:
        task = asyncio.create_task(client.post("/add/search", content=urlencode(body).encode(),
                                              headers={"Content-Type": "application/x-www-form-urlencoded"}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert cleaned.is_set() and snapshot(db) == before and len(io.calls) == 1


@pytest.mark.parametrize("provider", ["musicbrainz", "discogs"])
@pytest.mark.parametrize("mutation,code", [("identity", "stale_review"), ("format", "format_conflict")])
def test_real_selected_music_preview_checks_exact_detail_identity_and_format_before_signing(
    editor_client, db, io, provider, mutation, code,
):
    module, search_name, detail_name = ((musicbrainz, "search_releases", "lookup_release") if provider == "musicbrainz"
                                       else (discogs, "search_public_releases", "lookup_public_release"))
    io.install(module, search_name, provider_result.found(provider, [release(provider)]))
    searched = search(editor_client, media_type="cassette", provider=provider, q="Hits Out of Hell")
    details = release(provider, "cd" if mutation == "format" else "cassette")
    if mutation == "identity":
        details["musicbrainz_release_id" if provider == "musicbrainz" else "discogs_release_id"] = (
            "22222222-2222-4222-8222-222222222222" if provider == "musicbrainz" else 78)
    io.install(module, detail_name, provider_result.found(provider, details))
    before = snapshot(db)
    io.clear()
    response = preview(editor_client, searched)
    error(response, 409, code)
    assert not Forms(response.text).named("add-media-save")
    assert snapshot(db) == before and len(io.calls) == 1


def test_book_result_without_valid_isbn_stays_manual_without_synthesized_selection(editor_client, db, io):
    io.install(openlibrary, "search_books", provider_result.found("openlibrary", [
        {"title": "Identifier-free edition", "authors": "Known author", "isbn": None},
        {"title": "Invalid original ten", "isbn": "0441172710"},
    ]))
    before = snapshot(db)
    response = search(editor_client)
    assert response.status_code == 200 and Forms(response.text).named("add-media-manual")
    assert not Forms(response.text).named("add-media-preview")
    assert snapshot(db) == before and len(io.calls) == 1


@pytest.mark.parametrize("family", ["dvd", "video_game"])
def test_second_candidate_preview_is_exact_selected_snapshot_or_id_not_first_title_relookup(
    editor_client, db, io, credentials, family,
):
    if family == "dvd":
        io.install(tmdb, "search_movies", provider_result.found("tmdb", [
            {**MOVIE, "tmdb_id": 455, "title": "First misleading title"},
            {**MOVIE, "title": "Exact second title", "description": "Selected second snapshot", "publish_year": 1999},
        ]))
    else:
        io.install(igdb, "search_games", provider_result.found("igdb", [
            {**GAME, "igdb_id": 122, "title": "First misleading title"}, {**GAME, "title": "Exact second title"},
        ]))

        def exact(identifier, *args, **kwargs):
            assert identifier == 123
            return {**GAME, "title": "Exact second title", "description": "Selected second snapshot"}

        io.install(igdb, "lookup_game", handler=exact)
    searched = search(editor_client, media_type=family)
    io.clear()
    before = snapshot(db)
    reviewed = preview(editor_client, searched, index=1)
    assert reviewed.status_code == 200 and Forms(reviewed.text).named("add-media-save")
    assert "Exact second title" in reviewed.text and "Selected second snapshot" in reviewed.text
    assert "First misleading title" not in reviewed.text
    assert len(io.calls) == (1 if family == "video_game" else 0) and snapshot(db) == before


@pytest.mark.parametrize("method,q,expected_mb", [("catalog_number", "4026156", 2), ("barcode", "078073003501", 3)])
def test_automatic_hint_fallback_keeps_bounded_logical_search_budget(editor_client, io, method, q, expected_mb):
    io.install(musicbrainz, "search_releases", provider_result.found("musicbrainz", []))
    io.install(discogs, "search_public_releases", provider_result.found("discogs", [release("discogs")]))
    response = search(editor_client, media_type="cassette", provider="auto", method=method, q=q,
                      title_hint="Hits Out of Hell", artist="Meat Loaf")
    assert response.status_code == 200
    assert len(io.seen(musicbrainz, "search_releases")) == expected_mb
    assert len(io.seen(discogs, "search_public_releases")) == 1 and len(io.calls) == expected_mb + 1


@pytest.mark.parametrize("stage", ["preview", "save"])
@pytest.mark.parametrize("direction", ["switch_to_ps5", "ps5_to_switch"])
def test_genuine_game_platform_contexts_cannot_be_cross_mixed_after_valid_native_controls(
    editor_client, editor_user, db, catalogue, stage, direction,
):
    from tests.test_add_media_save import assert_row_receipt, receipt, stored, submit

    platform_names = {"switch": "Nintendo Switch", "ps5": "PlayStation 5"}

    def filtered(query, client_id, client_secret, client, platform=None, limit=10):
        assert query == "Same multi-platform game" and platform in platform_names and limit == 10
        return provider_result.found("igdb", [{**GAME, "platform_names": [platform_names[platform]]}])

    catalogue.install(igdb, "search_games", handler=filtered)
    catalogue.install(igdb, "lookup_game", {**GAME, "platform_names": list(platform_names.values())})
    selections, reviews, saved_ids = {}, {}, {}
    for platform in ("switch", "ps5"):
        searched = search(editor_client, media_type="video_game", q="Same multi-platform game", platform=platform)
        selections[platform] = form(searched, "add-media-preview")
        reviewed = preview(editor_client, searched)
        reviews[platform] = form(reviewed, "add-media-save")
        _, actual = receipt(editor_client, submit(editor_client, reviews[platform]), editor_user["id"])
        row = stored(db, actual.item_id)
        assert actual.status == "added" and actual.actual_mode == "add"
        assert row["platform"] == platform and row["title"] == GAME["title"] and row["source"] == "igdb"
        assert_row_receipt(actual, row)
        saved_ids[platform] = actual.item_id
    assert saved_ids["switch"] != saved_ids["ps5"]
    source, target = ("switch", "ps5") if direction == "switch_to_ps5" else ("ps5", "switch")
    controls = selections if stage == "preview" else reviews
    before = snapshot(db)
    catalogue.clear()
    mixed = submit(editor_client, controls[source], context=value(controls[target], "context"))
    error(mixed, 409, "intent_changed")
    assert snapshot(db) == before and catalogue.calls == []


def assert_native_isbn_cascade_calls(calls, canonical):
    expected_labels = (["app.services.bnf.lookup", "app.services.openlibrary.lookup"]
                       if canonical == "9791090636071" else ["app.services.openlibrary.lookup"])
    assert len(calls) == len(expected_labels)
    client = calls[0][1][1]
    assert isinstance(client, httpx.AsyncClient)
    assert calls == [(label, (canonical, client), {}) for label in expected_labels]


@pytest.mark.asyncio
async def test_french_fixture_native_cascade_control_requires_exact_bnf_then_openlibrary(db, io):
    from app.routers import items_common

    canonical = "9791090636071"
    io.install(bnf, "lookup", provider_result.no_match("bnf", status=200))
    io.install(openlibrary, "lookup", provider_result.found("openlibrary", BOOK))
    before = snapshot(db)
    async with httpx.AsyncClient() as client:
        metadata, source, identifiers, outcome = await items_common._lookup_metadata(canonical, None, client)
    assert metadata == BOOK and source == "openlibrary" and identifiers == {}
    assert outcome.found and outcome.provider == "openlibrary"
    assert_native_isbn_cascade_calls(io.calls, canonical)
    assert snapshot(db) == before
    # Detector controls use observed real native-operation calls, not fake
    # application HTML or a replacement auth/DB decision.
    with pytest.raises(AssertionError):
        assert_native_isbn_cascade_calls(io.calls[1:], canonical)
    with pytest.raises(AssertionError):
        assert_native_isbn_cascade_calls(list(reversed(io.calls)), canonical)
    wrong_isbn = list(io.calls)
    label, args, kwargs = wrong_isbn[0]
    wrong_isbn[0] = (label, (ISBN, args[1]), kwargs)
    with pytest.raises(AssertionError):
        assert_native_isbn_cascade_calls(wrong_isbn, canonical)


def install_malformed_mb_wire(io):
    # io captured this public adapter BEFORE installing its own bomb. Only
    # this malformed-wire case delegates; every other boundary stays guarded.
    original_search = io.originals[(musicbrainz, "search_releases")]
    io.install(musicbrainz, "search_releases", handler=original_search)

    def malformed(client, method, url, **kwargs):
        assert method == "GET" and url == "https://musicbrainz.org/ws/2/release/"
        assert kwargs == {
            "params": {"query": 'release:"Hits Out of Hell" AND artist:"Meat Loaf" AND format:Cassette',
                       "fmt": "json", "limit": "30"},
            "headers": {"User-Agent": "Shelf/1.0 (https://github.com/dgahagan/shelf)", "Accept": "application/json"},
            "timeout": 15, "follow_redirects": False,
        }
        # Syntactically valid JSON, but a malformed release-list shape. The
        # actual adapter, not a fabricated found-dict service result, rejects it.
        return httpx.Response(200, json={"releases": {"not": "a list"}})

    io.install(outbound, "fetch", handler=malformed)


def assert_malformed_mb_wire_calls(calls):
    assert [label for label, _, _ in calls] == ["app.services.musicbrainz.search_releases", "app.services.outbound.fetch"]
    service_args, service_kwargs = calls[0][1:]
    assert len(service_args) == 2 and service_args[0] == "Hits Out of Hell"
    client = service_args[1]
    assert isinstance(client, httpx.AsyncClient)
    assert service_kwargs == {"artist": "Meat Loaf", "media_type": "cassette", "country": "", "limit": 30}
    wire_args, wire_kwargs = calls[1][1:]
    assert wire_args == (client, "GET", "https://musicbrainz.org/ws/2/release/")
    assert wire_kwargs == {
        "params": {"query": 'release:"Hits Out of Hell" AND artist:"Meat Loaf" AND format:Cassette', "fmt": "json", "limit": "30"},
        "headers": {"User-Agent": "Shelf/1.0 (https://github.com/dgahagan/shelf)", "Accept": "application/json"},
        "timeout": 15, "follow_redirects": False,
    }


@pytest.mark.asyncio
async def test_malformed_mb_wire_native_adapter_control_is_filtered_and_never_falls_back(db, io):
    from app.services import music_matching

    install_malformed_mb_wire(io)
    before = snapshot(db)
    async with httpx.AsyncClient() as client:
        answer = await music_matching.search_catalogue(client, title="Hits Out of Hell", artist="Meat Loaf", format="cassette")
    assert answer.outcome == "transport_failed" and answer.provider == "musicbrainz" and not answer.found
    assert_malformed_mb_wire_calls(io.calls)
    assert snapshot(db) == before and io.seen(discogs, "search_public_releases") == []
    with pytest.raises(AssertionError):
        assert_malformed_mb_wire_calls(io.calls + io.calls[-1:])
    wrong_host = list(io.calls)
    label, args, kwargs = wrong_host[-1]
    wrong_host[-1] = (label, (args[0], args[1], "https://unexpected.example.test/releases"), kwargs)
    with pytest.raises(AssertionError):
        assert_malformed_mb_wire_calls(wrong_host)


def assert_search_timeout_human_outcome(response):
    assert response.status_code == 200
    assert response.headers.get("content-type", "").split(";", 1)[0] == "text/html"
    no_cookie(response)
    texts = accessible_error_text(response, "search_timeout", outcome=True)
    assert any(TIMEOUT_HUMAN_TEXT in text for text in texts), texts


def oracle_envelope_response(status, body, *, media_type="text/html; charset=utf-8", context=None, headers=None):
    """Pure metadata/fragment envelope for helper controls ONLY.

    Never returned by an application handler, used as an Add page, passed to
    auth, or substituted for a native search/preview/save result.
    """
    response = httpx.Response(status, content=body.encode(), headers={"Content-Type": media_type, "Cache-Control": "no-store", **(headers or {})},
                              request=httpx.Request("POST", "https://oracle-metadata.invalid/envelope"))
    response.context = context
    return response


@pytest.mark.parametrize("representation,status,code", [("template_error", 409, "stale_review"),
    ("template_outcome", 200, "transport_failed"), ("json", 400, "invalid_form"), ("paragraph", 503, "outcome_unknown")])
def test_error_helper_metadata_controls_pin_correct_code_and_reject_wrong_same_status(representation, status, code):
    import json

    if representation.startswith("template"):
        field = "provider_outcome" if representation == "template_outcome" else "error_code"
        response = oracle_envelope_response(status, '<p role="alert">The operation could not complete. Please review the form.</p>',
                                            context={"add": {"error_code": None, "provider_outcome": None, field: code}})
        assert code not in response.text, "A model code is deliberately not a DOM-text requirement"
        error(response, status, code)
        response.context["add"][field] = "intent_changed"
    elif representation == "json":
        response = oracle_envelope_response(status, json.dumps({"code": code, "message": EARLY_ERROR_TEXT[code]}), media_type="application/json")
        error(response, status, code)
        response = oracle_envelope_response(status, json.dumps({"code": "intent_changed", "message": EARLY_ERROR_TEXT[code]}), media_type="application/json")
    else:
        response = oracle_envelope_response(status, '<p role="alert" data-testid="add-media-error" data-error-code="outcome_unknown">The save outcome is unknown. Check your collection before submitting again.</p>')
        error(response, status, code)
        response = oracle_envelope_response(status, '<p role="alert" data-testid="add-media-error" data-error-code="intent_changed">The save outcome is unknown. Check your collection before submitting again.</p>')
    with pytest.raises(AssertionError):
        error(response, status, code)


@pytest.mark.parametrize("body", [
    '<p>Review this form again.</p>', '<p role="alert" hidden>Review this form again.</p>',
    '<p role="alert" aria-hidden="true">Review this form again.</p>',
    '<div hidden><p role="alert">Review this form again.</p></div>',
    '<p role="alert" style="display: none">Review this form again.</p>', '<p role="alert">stale_review</p>',
    '<div role="alert"><script>Review this form again.</script></div>',
])
def test_error_helper_metadata_rejects_missing_hidden_or_identifier_only_human_error(body):
    response = oracle_envelope_response(409, body, context={"add": {"error_code": "stale_review", "provider_outcome": None}})
    with pytest.raises(AssertionError):
        error(response, 409, "stale_review")


def test_error_helper_metadata_does_not_mask_wrong_model_code_with_other_field_or_dom_literal():
    response = oracle_envelope_response(409, '<p role="alert" data-error-code="stale_review">stale_review: please preview again.</p>',
                                        context={"add": {"error_code": "intent_changed", "provider_outcome": "stale_review"}})
    with pytest.raises(AssertionError):
        error(response, 409, "stale_review")


def test_timeout_human_oracle_metadata_requires_specific_timeout_not_generic_failure_or_identifier():
    response = oracle_envelope_response(200, f'<p role="status">{TIMEOUT_HUMAN_TEXT}</p>')
    assert_search_timeout_human_outcome(response)
    for body in ('<p role="status">The provider could not complete this search.</p>', '<p role="status">search_timeout</p>'):
        with pytest.raises(AssertionError):
            assert_search_timeout_human_outcome(oracle_envelope_response(200, body))
