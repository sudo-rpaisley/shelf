"""Bound gettext/escaping contracts, not completion of the shipped locale catalogs."""

from gettext import GNUTranslations
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from babel.messages.catalog import Catalog
from babel.messages.extract import extract
from babel.messages.mofile import write_mo
import pytest

from app import i18n
from app.database import set_setting
from app.features import set_feature_enabled
from app.services import item_copies
from tests.conftest import _insert_item


ROOT = Path(__file__).resolve().parents[1]
DISCOVER_LABELS = (
    "Discover",
    "Discover Books",
    "Connect your Hardcover account to search for books and add them to your wishlist.",
    "Go to Settings",
    "Hardcover is not connected. Ask an administrator to configure the integration.",
    "Search by title or author...",
    "Search Hardcover's catalog to find books and add them to your wishlist.",
    "Search Hardcover's catalog to explore books.",
)
COPY_TRASH = "Move copy #%(n)s to Trash? Restore it from Trash to get it back."
MESSAGES = {
    source: f'Tradotto {n} <l10n-probe data-note="virgolette"> & "doppie" \'singole\' 100%%'
    for n, source in enumerate(DISCOVER_LABELS)
}
# Reorder and repeat the placeholder; the copy value is data, not another format string.
MESSAGES[COPY_TRASH] = (
    'Dal Cestino puoi ripristinare %(n)s. Spostare la copia %(n)s? '
    '<l10n-probe data-note="virgolette"> & "doppie" \'singole\' 100%%'
)
HOSTILE_VALUE = '27" autofocus onfocus="bad()" <copy-probe> & \'100%d\' %(n)s'


class _Markup(HTMLParser):
    """Read decoded text/attributes once, so double escaping cannot pass."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.nodes, self.stack = [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "text": "", "parents": tuple(self.stack)}
        self.nodes.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(len(self.nodes) - 1)

    def handle_endtag(self, tag):
        for offset in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[offset]]["tag"] == tag:
                del self.stack[offset:]
                break

    def handle_data(self, data):
        for index in self.stack:
            self.nodes[index]["text"] += data

    def find(self, tag, *, within=None, **attrs):
        parent = self.nodes.index(within) if within is not None else None
        return [node for node in self.nodes if node["tag"] == tag
                and (parent is None or parent in node["parents"])
                and all(node["attrs"].get(key) == value for key, value in attrs.items())]

    def one(self, tag, *, within=None, **attrs):
        nodes = self.find(tag, within=within, **attrs)
        assert len(nodes) == 1, (tag, attrs, nodes)
        return nodes[0]


def _localized(message, **values):
    return MESSAGES[message] % values


@pytest.fixture
def translator(monkeypatch):
    """In-memory GNU catalog through the app's already-bound gettext callable."""
    monkeypatch.setenv("SHELF_I18N", "on")
    catalog = Catalog(locale="it")
    for source, translated in MESSAGES.items():
        catalog.add(source, translated)
    data = BytesIO()
    write_mo(data, catalog)
    data.seek(0)
    translated = GNUTranslations(data)
    gettext = translated.gettext
    calls = []

    def recording_gettext(message):
        calls.append((i18n.current_locale.get(), message))
        return gettext(message)

    monkeypatch.setattr(translated, "gettext", recording_gettext)
    original = i18n._catalog
    monkeypatch.setattr(i18n, "_catalog", lambda domain, lang: (
        translated if domain == "messages" and lang == "it" else original(domain, lang)
    ))
    return calls


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
@pytest.mark.parametrize("connected", [False, True], ids=["disconnected", "connected"])
def test_discover_translates_each_role_branch_without_changing_controls(
        request, db, translator, role, connected):
    client = request.getfixturevalue(f"{role}_client")
    set_setting(db, "hardcover_token", "translation-fixture-not-a-credential" if connected else "")
    db.commit()

    response = client.get("/discover", headers={"Accept-Language": "it"})

    assert response.status_code == 200
    markup = _Markup(response.text)
    assert markup.one("html")["attrs"]["lang"] == "it"
    assert markup.one("title")["text"] == _localized("Discover") + " — Shelf"
    assert markup.one("h1")["text"] == _localized("Discover Books")
    # Restrict control assertions to this page's content, not the shared navigation.
    main = markup.one("main")
    expected = {"Discover", "Discover Books"}
    if connected:
        instruction = DISCOVER_LABELS[6 if role in {"admin", "editor"} else 7]
        expected.update({DISCOVER_LABELS[5], instruction})
        search = markup.one("input", within=main, name="q")["attrs"]
        assert search["placeholder"] == _localized(DISCOVER_LABELS[5])
        assert search["type"] == "search" and "autofocus" in search
        assert search["x-model"] == "query"
        assert {key: search[key] for key in ("hx-get", "hx-trigger", "hx-target", "hx-include")} == {
            "hx-get": "/api/hardcover/search", "hx-trigger": "keyup changed delay:400ms",
            "hx-target": "#hc-results", "hx-include": "this",
        }
        assert markup.one("div", within=main, id="hc-results")
        assert not markup.find("a", within=main)
    elif role == "admin":
        instruction = DISCOVER_LABELS[2]
        expected.update({instruction, "Go to Settings"})
        settings = markup.one("a", within=main, href="/settings")
        assert settings["text"] == _localized("Go to Settings")
        assert not any(key.startswith(("hx-", "on")) for key in settings["attrs"])
    else:
        instruction = DISCOVER_LABELS[4]
        expected.add(instruction)
        assert not markup.find("a", within=main)
    assert markup.one("p", within=main)["text"] == _localized(instruction)
    assert not markup.find("form", within=main) and not markup.find("script", within=main)
    assert bool(markup.find("input", within=main)) == connected
    assert not markup.find("l10n-probe")
    main_index = markup.nodes.index(main)
    assert all(not key.startswith("on") for node in markup.nodes if main_index in node["parents"]
               for key in node["attrs"])
    assert _localized(instruction) in main["text"]
    assert expected <= {message for locale, message in translator if locale == "it"}
    for message in set(DISCOVER_LABELS[2:]) - expected:
        assert _localized(message) not in main["text"]


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
def test_translated_discover_does_not_bypass_a_disabled_hardcover_feature(request, db, translator, role):
    client = request.getfixturevalue(f"{role}_client")
    set_setting(db, "hardcover_token", "translation-fixture-not-a-credential")
    set_feature_enabled(db, "hardcover", False)
    db.commit()

    response = client.get("/discover", headers={"Accept-Language": "it"})

    # Native disabled-feature pages deliberately answer 200, not the API's 403.
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert markup.one("h1", **{"data-testid": "feature-disabled"})
    assert not markup.find("input", name="q")
    assert not markup.find("div", id="hc-results")
    assert not markup.find("a", **{"data-nav-tab": "discover"})
    assert ("it", "Discover Books") not in translator


@pytest.mark.parametrize("connected", [False, True])
def test_discover_marking_does_not_enable_the_dormant_locale_flag(
        admin_client, db, monkeypatch, translator, connected):
    monkeypatch.delenv("SHELF_I18N")
    set_setting(db, "hardcover_token", "translation-fixture-not-a-credential" if connected else "")
    db.commit()

    response = admin_client.get("/discover", headers={"Accept-Language": "it"})

    assert response.status_code == 200
    markup = _Markup(response.text)
    assert markup.one("html")["attrs"]["lang"] == "en"
    assert markup.one("title")["text"] == "Discover — Shelf"
    assert markup.one("h1")["text"] == "Discover Books"
    assert not markup.find("select", **{"data-testid": "account-language"})
    assert not translator


def test_discover_results_keep_provider_values_verbatim(admin_client, db, monkeypatch, translator):
    from app.services import hardcover

    calls = []

    async def search_books(query, client, *, token):
        calls.append((query, token))
        return [{"hardcover_book_id": 123, "title": HOSTILE_VALUE, "authors": HOSTILE_VALUE,
                 "description": HOSTILE_VALUE, "series_name": HOSTILE_VALUE,
                 "series_position": 2, "cover_url": None, "year": 2026, "pages": 123,
                 "rating": 4.5}]

    monkeypatch.setattr(hardcover, "search_books", search_books)
    set_setting(db, "hardcover_token", "translation-fixture-not-a-credential")
    db.commit()

    response = admin_client.get("/api/hardcover/search", params={"q": HOSTILE_VALUE},
                                headers={"Accept-Language": "it"})

    assert response.status_code == 200
    markup = _Markup(response.text)
    title = markup.one("p", title=HOSTILE_VALUE)
    assert title["text"] == HOSTILE_VALUE
    assert sum(node["text"] == HOSTILE_VALUE for node in markup.find("p")) == 3
    assert not markup.find("copy-probe")
    assert HOSTILE_VALUE not in {message for _, message in translator}
    assert calls == [(HOSTILE_VALUE, "translation-fixture-not-a-credential")]


@pytest.mark.parametrize("number", [27, HOSTILE_VALUE], ids=["number", "hostile-copy-value"])
def test_copy_confirmation_escapes_localized_text_and_bound_value_exactly_once(db, translator, number):
    from app.main import templates

    set_feature_enabled(db, "collector", False)
    db.commit()
    copy = dict(id=83, copy_number=number, is_primary=0, location_id=3,
                condition=HOSTILE_VALUE, acquired_date="2026-10-20", acquisition_source=HOSTILE_VALUE,
                acquisition_price=18.5, copy_barcode=HOSTILE_VALUE, provenance=HOSTILE_VALUE)
    token = i18n.current_locale.set("it")
    try:
        html = templates.env.get_template("fragments/item_copy_edit.html").render(
            item_id=91, copy=copy, locations=[{"id": 3, "name": HOSTILE_VALUE}],
        )
    finally:
        i18n.current_locale.reset(token)

    markup = _Markup(html)
    remove = markup.one("button", **{"data-testid": "remove-copy"})["attrs"]
    assert remove["hx-confirm"] == _localized(COPY_TRASH, n=number)
    assert remove["hx-delete"] == "/api/items/91/copies/83"
    assert remove["type"] == "button"
    assert remove["hx-target"] == "#item-copies" and remove["hx-swap"] == "outerHTML"
    assert markup.one("form")["attrs"]["hx-post"] == "/api/items/91/copies/83"
    for field in ("condition", "acquisition_source", "copy_barcode", "provenance"):
        assert markup.one("input", name=field)["attrs"]["value"] == HOSTILE_VALUE
    assert markup.one("option", value="3")["text"] == HOSTILE_VALUE
    assert not markup.find("l10n-probe") and not markup.find("copy-probe")
    assert not any(key.startswith("on") for node in markup.nodes for key in node["attrs"])
    assert not markup.find("button", **{"data-testid": "assign-copy-code"})
    assert ("it", COPY_TRASH) in translator
    assert HOSTILE_VALUE not in {message for _, message in translator}


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
@pytest.mark.parametrize("collector", [False, True], ids=["collector-off", "collector-on"])
def test_copy_panel_retains_role_feature_identity_and_read_only_get_contracts(
        request, db, translator, role, collector):
    client = request.getfixturevalue(f"{role}_client")
    set_feature_enabled(db, "collector", collector)
    item_id = _insert_item(db, title="Provider title 100%d <copy-probe>")
    copy_id = item_copies.insert_copy(db, {"item_id": item_id, "copy_number": 27, "is_primary": 1})
    db.commit()
    before = dict(db.execute("SELECT * FROM copies_live WHERE id = ?", (copy_id,)).fetchone())

    response = client.get(f"/api/items/{item_id}/copies/{copy_id}/edit", headers={"Accept-Language": "it"})

    assert dict(db.execute("SELECT * FROM copies_live WHERE id = ?", (copy_id,)).fetchone()) == before
    markup = _Markup(response.text)
    if role == "viewer":
        assert response.status_code == 403
        assert not markup.find("div", id="item-copies")
        assert not markup.find("button", **{"data-testid": "remove-copy"})
        assert ("it", COPY_TRASH) not in translator
        return
    assert response.status_code == 200
    remove = markup.one("button", **{"data-testid": "remove-copy"})["attrs"]
    assert remove["hx-confirm"] == _localized(COPY_TRASH, n=27)
    assert remove["hx-delete"] == f"/api/items/{item_id}/copies/{copy_id}"
    assert bool(markup.find("button", **{"data-testid": "assign-copy-code"})) == collector
    names = {node["attrs"]["name"] for node in markup.nodes if "name" in node["attrs"]}
    assert names == {"location_id", "condition", "acquired_date", "acquisition_source",
                     "acquisition_price", "copy_barcode", "provenance"}


@pytest.mark.parametrize("template, expected", [
    ("discover.html", set(DISCOVER_LABELS)),
    ("fragments/item_copy_edit.html", {COPY_TRASH}),
])
def test_owned_gettext_messages_and_named_copy_placeholder_are_extractable(template, expected):
    with (ROOT / "app/templates" / template).open("rb") as source:
        messages = {message for _, message, _, _ in extract(
            "jinja2", source, keywords={"_": None}, options={"extensions": "jinja2.ext.i18n"},
        )}
    assert expected <= messages
