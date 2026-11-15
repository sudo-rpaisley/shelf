"""Home-only streaming previews; the native initial eight remain real content."""

from pathlib import Path

import pytest

from app.services.media_cards import build_media_card
from tests.conftest import _insert_item
from tests.test_connected_layout import _Markup
from tests.test_media_cards import _environment


ROOT = Path(__file__).resolve().parents[1]


def _marked(markup, key, value=None, *, within=None):
    nodes = [n for n in markup.find(within=within) if key in n.attrs
             and (value is None or n.attrs[key] == value)]
    assert len(nodes) == 1, (key, len(nodes))
    return nodes[0]


def _seed(db, count):
    ids = [_insert_item(db, title='<b>Unknown title 100%d & raw</b>' if number == 21 else f"Shelf {number:02d} a long complete title", isbn=None,
                        authors='<i>Complete credit 50%s & raw</i>' if number == 21 else "A complete, unabridged creator credit",
                        media_type="future-format" if number == 21 else "cd" if number == 20 else "book",
                        wishlisted=number == 20, owned=int(number != 20),
                        created_at="2026-01-01 12:00:00") for number in range(count)]
    deleted = _insert_item(db, title="Trashed newest", isbn=None, created_at="2099-01-01",
                           deleted_at="2026-01-02")
    db.commit()
    return list(reversed(ids)), deleted


def test_home_requests_one_bounded_24_preview_and_keeps_the_real_ssr_eight(viewer_client, db, monkeypatch):
    from app.routers import pages

    expected, trashed = _seed(db, 36)
    calls = []
    statements = []
    original = pages.dashboard_summary

    def summary(connection, *, recent_limit):
        calls.append(recent_limit)
        connection.set_trace_callback(statements.append)
        try:
            return original(connection, recent_limit=recent_limit)
        finally:
            connection.set_trace_callback(None)

    monkeypatch.setattr(pages, "dashboard_summary", summary)
    response = viewer_client.get("/?recent_limit=100000&recent_card_mode=forged")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert calls == [24]
    recent_reads = [sql for sql in statements if "ORDER BY i.created_at DESC, i.id DESC" in sql]
    assert len(recent_reads) == 1 and recent_reads[0].endswith("LIMIT 24")
    assert [item["id"] for item in response.context["recent_items"]] == expected[:8]
    assert [item["id"] for item in response.context["recent_overflow"]] == expected[8:24]
    markup = _Markup(response.text)
    shell = _marked(markup, "data-recent-shelf", "streaming-v1")
    track = markup.one("ul", id="home-recent-row")
    assert shell in track.ancestors
    initial = markup.find("article", within=track)
    assert [int(n.attrs["data-item-id"]) for n in initial] == expected[:8]
    template = _marked(markup, "data-recent-overflow")
    assert template.tag == "template" and track not in template.ancestors
    assert [int(n.attrs["data-item-id"]) for n in markup.find("article", within=template)] == expected[8:24]
    unknown = markup.one("article", **{"data-item-id": str(expected[14])})
    assert unknown.attrs["data-media-type"] == "future-format"
    assert markup.one("h3", within=unknown).text.strip() == '<b>Unknown title 100%d & raw</b>'
    details = markup.one("details", within=unknown)
    assert '<i>Complete credit 50%s & raw</i>' in details.text and "future-format" in details.text
    assert not markup.find("b", within=unknown) and not markup.find("i", within=unknown)
    wishlist = markup.one("article", **{"data-item-id": str(expected[15])})
    assert markup.one("span", **{"class": "media-card__badge"}, within=wishlist).text.strip() == "Wishlist"
    assert str(trashed) not in [n.attrs["data-item-id"] for n in markup.find("article")]
    assert response.context["recent_has_more"] is True and response.context["total_count"] == 36
    assert len(markup.find(id="home-recent-row")) == 1
    assert not markup.find("button", within=track), "Native card Details are not script-only buttons"
    buttons = [n for n in markup.find("button", within=shell) if "data-recent-action" in n.attrs]
    assert len(buttons) == 2
    assert all(n.attrs["type"] == "button" and n.attrs["aria-controls"] == "home-recent-row"
               and "disabled" in n.attrs for n in buttons)


@pytest.mark.parametrize("count", (0, 1, 8, 9, 24, 25))
def test_empty_small_and_overflow_shelves_have_truthful_counts_and_bounded_buffers(viewer_client, db, count):
    expected, _ = _seed(db, count)
    response = viewer_client.get("/")
    assert response.status_code == 200
    assert [item["id"] for item in response.context["recent_items"]] == expected[:8]
    assert [item["id"] for item in response.context["recent_overflow"]] == expected[8:24]
    assert response.context["recent_has_more"] is (count > 24)
    markup = _Markup(response.text)
    if count == 0:
        assert not markup.find("ul", id="home-recent-row")
        assert not [n for n in markup.find("button") if "data-recent-action" in n.attrs]


@pytest.mark.parametrize("mode", (None, "", "future", "shelf-preview-v1 ", "shelf-preview-v1"))
def test_only_exact_preview_mode_adds_a_stage_and_native_full_details(mode):
    env, _ = _environment()
    card = build_media_card({"id": 17, "title": "A complete title", "authors": "Full creator", "media_type": "book"})
    markup = _Markup(env.get_template("components/media_card.html").render(card=card, media_card_presentation=mode))
    article = markup.one("article")
    assert article.attrs.get("data-card-presentation") == ("shelf-preview-v1" if mode == "shelf-preview-v1" else None)
    assert len(markup.find("details")) == int(mode == "shelf-preview-v1")
    assert markup.one("h3").text.strip() == "A complete title"
    assert len(markup.find("a")) == 1, "The native item link is preserved, not replaced by an editor/player"


@pytest.mark.parametrize("newstyle", (False, True), ids=("raw-gnu", "newstyle-gnu"))
def test_full_preview_metadata_and_owned_details_label_escape_once(newstyle):
    env, calls = _environment({"Details": '<b onclick="bad()">Details</b>'}, newstyle=newstyle)
    title, creator = '<b>Raw title 100%d & text</b>', '<i>Raw credit 50%s & text</i>'
    card = build_media_card({"id": 7, "title": title, "authors": creator, "media_type": "book"})
    markup = _Markup(env.get_template("components/media_card.html").render(
        card=card, media_card_presentation="shelf-preview-v1",
    ))
    assert markup.one("h3").text.strip() == title
    details = markup.one("details")
    assert title in details.text and creator in details.text
    assert '<b onclick="bad()">Details</b>' in markup.one("summary").text
    assert not markup.find("b") and not markup.find("i")
    assert title not in calls and creator not in calls


def test_new_assets_are_home_only_revalidated_and_not_new_private_precache(viewer_client):
    for path in ("/", "/library", "/music", "/browse"):
        markup = _Markup(viewer_client.get(path).text)
        for tag, key, asset in (("link", "href", "/static/css/recent-shelf.source.css"),
                                ("script", "src", "/static/js/recent-shelf.js")):
            nodes = markup.find(tag, **{key: asset})
            assert len(nodes) == int(path == "/")
            if nodes:
                response = viewer_client.get(asset)
                assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"
    source = (ROOT / "static/css/input.css").read_text()
    assert source.count('@import "./recent-shelf.source.css";') == 1
    worker = (ROOT / "static/sw.js").read_text()
    assert "/static/css/recent-shelf.source.css" not in worker and "/static/js/recent-shelf.js" not in worker


def test_shelf_gets_do_not_mutate_catalogue_or_account_state(viewer_client, db):
    _seed(db, 30)
    tables = ("items", "item_copies", "locations", "settings", "user_item_state", "user_item_events")
    def snapshot():
        return {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                for table in tables}
    before = snapshot()
    assert viewer_client.get("/").status_code == 200
    assert snapshot() == before


@pytest.mark.parametrize("locale,label,caption", (("it", "Mostra tutto", "8 elementi recenti"),
                                                   ("zh_Hans", "查看全部", "8 个最近添加的项目")))
def test_actual_account_locale_translates_header_and_buffer_labels(viewer_client, viewer_user, db, monkeypatch, locale, label, caption):
    _seed(db, 30)
    monkeypatch.setenv("SHELF_I18N", "on")
    db.execute("UPDATE users SET language = ? WHERE id = ?", (locale, viewer_user["id"]))
    db.commit()
    markup = _Markup(viewer_client.get("/").text)
    assert markup.one("a", **{"class": "recent-shelf__all"}).text.strip().startswith(label)
    assert _marked(markup, "data-recent-count").text == caption
    assert "24" in _marked(markup, "data-recent-overflow").attrs["data-recent-total-label"]
