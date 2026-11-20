"""Creator/series navigation regressions, source-authored only until qualified."""

from dataclasses import FrozenInstanceError
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from markupsafe import Markup

from app.database import set_setting
from app.services import author_index, authors, caption_links, home_dashboard, personal_library
from app.services.media_cards import build_media_card
from tests.conftest import _insert_item
from tests.test_connected_layout import _Markup
from tests.test_media_cards import _environment
from tests.test_personal_library import _save


def _item(db, title="Hits Out of Hell", creator="Meat Loaf", kind="cd", **fields):
    item = _insert_item(db, title=title, isbn=None, authors=creator, media_type=kind, **fields)
    # Raw fixture inserts do not call the write funnel; explicitly seed the
    # existing derived index, never create/repair it through a navigation GET.
    author_index.reindex_item(db, item, creator)
    return item


def _card(**fields):
    return build_media_card({"id": 7, "title": "Hits Out of Hell", "authors": "Meat Loaf", "media_type": "cd",
                             "series_name": "The Albums / Collection & More", **fields})


def _env(*, series=True, newstyle=True):
    env, calls = _environment(newstyle=newstyle)
    env.globals["build_media_card"] = build_media_card
    env.globals["feature_on"] = lambda key: series if key == "series" else True
    return env, calls


@pytest.mark.parametrize("text", ["Meat Loaf", "Simon & Garfunkel", "Earth, Wind & Fire", "Stanisław Lem",
    "  Frank  Herbert, and  Ken Liu - translator, Frank Herbert  ", "Martin Luther King,Jr.",
    "<b>Raw 100%d & credit</b>", "A,  and B - illustrator", "", " , , "])
def test_credit_parts_preserve_every_raw_character_and_reuse_existing_identity_keys(text):
    parts = caption_links.creator_parts(text)
    assert "".join(part.text for part in parts) == text
    keys = {entry.name_key for entry in authors.parse(text)}
    for part in parts:
        if part.href:
            url = urlsplit(part.href)
            assert url.path == "/browse/creator" and not url.scheme and not url.netloc
            assert parse_qs(url.query)["key"][0] in keys
    if text == "Meat Loaf":
        assert len(parts) == 1 and parts[0].href == "/browse/creator?key=meat+loaf"
    if text == "Simon & Garfunkel":
        assert len([part for part in parts if part.href]) == 1


def test_credit_bounds_never_drop_text_or_accept_supplied_links():
    text = "A" * (caption_links.MAX_CREDIT_TEXT + 1)
    assert caption_links.creator_parts(text) == (caption_links.CreditPart(text),)
    card = _card(creator_href="javascript:alert(1)", series_href="//attacker.invalid", series_name="A & B / C")
    assert card.series_href == "/series?" + urlencode({"name": "A & B / C"})
    assert card.creator_parts[0].href == "/browse/creator?key=meat+loaf"
    with pytest.raises(FrozenInstanceError):
        card.creator_parts[0].href = "https://attacker.invalid"


def test_pure_credit_spans_drop_html_trust_even_without_the_media_card_adapter():
    parts = caption_links.creator_parts(Markup("<b>Raw credit</b>"))
    assert "".join(part.text for part in parts) == "<b>Raw credit</b>"
    assert all(type(part.text) is str for part in parts)


@pytest.mark.parametrize("value", ["", "   ", "x\nname", "x\x7fname", "x" * 1001])
def test_unknown_or_invalid_series_names_do_not_become_links(value):
    assert _card(series_name=value).series_href is None


@pytest.mark.parametrize("newstyle", [False, True])
def test_interactive_captions_have_sibling_native_links_with_exact_escaping_and_no_nested_anchors(newstyle):
    env, calls = _env(newstyle=newstyle)
    card = _card(title='<b>Raw 100%d & "title"</b>', authors="<i>Artist 50%s & credit</i>")
    html = env.get_template("components/media_card.html").render(card=card, media_card_presentation="shelf-preview-v1")
    markup = _Markup(html)
    stage = markup.one(**{"data-caption-navigation": "metadata-v1"})
    primaries = [node for node in markup.find("a", within=stage) if "data-cover-link" in node.attrs]
    assert len(primaries) == 1
    primary = primaries[0]
    assert primary.attrs["href"] == "/item/7" and primary.attrs["aria-label"] == card.title
    assert markup.one("h3", within=stage).text == card.title
    credit = markup.one("p", within=stage, **{"class": "media-card__creator"})
    assert credit.text == card.creator_text
    assert markup.one("a", within=stage, **{"data-caption-link": "series"}).attrs["href"] == card.series_href
    assert markup.one("a", within=stage, **{"data-caption-link": "item"}).attrs["href"] == "/item/7"
    for anchor in markup.find("a"):
        assert not any(parent.tag == "a" for parent in anchor.ancestors)
        assert not urlsplit(anchor.attrs["href"]).netloc
    assert not markup.find("b") and not markup.find("i") and not markup.find("script")
    assert card.title not in calls and card.creator_text not in calls and card.series_name not in calls


def test_static_operational_cover_mode_stays_anchor_free_and_series_feature_off_keeps_creator_links():
    env, _ = _env(series=False)
    static = env.from_string('{% from "components/media_cover.html" import media_cover with context %}{{ media_cover(card) }}').render(card=_card())
    assert not _Markup(static).find("a")
    html = env.get_template("components/media_card.html").render(card=_card(), media_card_presentation="shelf-preview-v1")
    markup = _Markup(html)
    assert markup.find("a", **{"data-caption-link": "creator"})
    assert not markup.find("a", **{"data-caption-link": "series"})


def test_browse_card_preserves_item_selection_loan_and_source_action_owners_without_nested_links():
    env, _ = _env()
    item = {"id": 7, "title": "Game", "authors": "Studio", "media_type": "video_game", "series_name": "Saga",
            "cover_path": None, "wishlisted": False, "lent_to": "Borrower", "lent_overdue": False,
            "reading_status": None, "created_at": None, "source": "romm"}
    html = env.get_template("fragments/item_card.html").render(item=item, seven_days_ago="2026-01-01")
    markup = _Markup(html)
    primary = markup.one("a", **{"data-item-id": "7"})
    assert primary.attrs["@click.prevent"] == "openOrToggle(7, '/item/7', $event)"
    assert markup.testid("loan-badge").attrs["@click.stop.prevent"] == "show = !show"
    toggle = markup.one("button", **{"data-cover-select-action": None})
    assert "data-cover-select-action" in toggle.attrs
    assert toggle.attrs["@click.stop.prevent"] == "toggleItem(7)"
    assert toggle.attrs["type"] == "button" and not any(parent.tag == "a" for parent in toggle.ancestors)
    assert markup.testid("romm-card-action").attrs["href"] == "/api/romm/items/7/open"
    assert all(not any(parent.tag == "a" for parent in anchor.ancestors) for anchor in markup.find("a"))


def test_creator_navigation_is_indexed_live_local_read_only_and_not_a_title_search(viewer_client, db, monkeypatch):
    first = _item(db)
    second = _item(db, "Bat Out of Hell", kind="vinyl")
    unrelated = _item(db, "A book about Meat Loaf", creator="Different Writer", kind="book")
    db.commit()
    def forbidden(*args, **kwargs):
        raise AssertionError("Creator navigation must never contact an integration")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    tables = ("items", "authors", "item_authors", "item_copies", "user_item_state", "user_item_events")
    before = {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")) for table in tables}
    result = viewer_client.get("/browse/creator?key=meat+loaf&url=https://attacker.invalid", follow_redirects=False)
    assert result.status_code == 303 and result.headers["cache-control"] == "no-store"
    author = db.execute("SELECT id FROM authors WHERE name_key = 'meat loaf'").fetchone()[0]
    assert result.headers["location"] == f"/browse?author_filter={author}"
    filtered = viewer_client.get(result.headers["location"])
    assert filtered.status_code == 200
    markup = _Markup(filtered.text)
    assert {int(node.attrs["data-item-id"]) for node in markup.find(**{"data-item-id": str(first)}) + markup.find(**{"data-item-id": str(second)})} == {first, second}
    assert not markup.find(**{"data-item-id": str(unrelated)})
    assert {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")) for table in tables} == before


def test_unknown_or_only_trashed_creator_never_repairs_the_index_or_broadens_to_all_items(viewer_client, db):
    item = _item(db)
    db.execute("UPDATE items SET deleted_at = '2026-01-01' WHERE id = ?", (item,))
    db.commit()
    assert viewer_client.get("/browse/creator?key=meat+loaf").status_code == 404
    assert viewer_client.get("/browse/creator?key=missing").status_code == 404
    assert viewer_client.get("/browse/creator").status_code == 422
    assert viewer_client.get("/browse/creator?key=" + "a" * 1001).status_code == 422


def test_series_link_selects_the_exact_casefold_group_and_preserves_global_rename_choices(viewer_client, db):
    _item(db, "One", "Author", "book", series_name="Dune", series_position=1)
    _item(db, "Two", "Author", "book", series_name="DUNE", series_position=2)
    _item(db, "Other", "Author", "book", series_name="Dune Companion")
    _item(db, "Unassigned", "Author", "book")
    db.commit()
    response = viewer_client.get("/series?name=dune")
    assert response.status_code == 200
    markup = _Markup(response.text)
    assert len(markup.find(**{"data-testid": "series-card"})) == 1
    assert markup.testid("series-card").attrs["data-item-count"] == "2"
    assert not markup.find(**{"data-testid": "unassigned-card"})
    assert markup.find("option", within=markup.one("datalist", id="series-names"), value="Dune Companion")
    assert len(_Markup(viewer_client.get("/series").text).find(**{"data-testid": "series-card"})) == 2
    assert not _Markup(viewer_client.get("/series?name=Du").text).find(**{"data-testid": "series-card"})
    set_setting(db, "feature.series", "0")
    db.commit()
    assert viewer_client.get("/series?name=dune").status_code == 403


def test_home_and_personal_preview_projections_include_the_real_series_without_mutation(db, viewer_user):
    item = _item(db, "Active", "Author", "book", series_name="A real series")
    db.commit()
    _save(db, viewer_user["id"], item, status="in_progress")
    before = db.total_changes
    summary = home_dashboard.dashboard_summary(db)
    personal = personal_library.dashboard(db, viewer_user["id"])
    assert next(row for row in summary["recent_items"] if row["id"] == item)["series_name"] == "A real series"
    assert personal["continue_items"][0]["series_name"] == "A real series"
    assert db.total_changes == before
