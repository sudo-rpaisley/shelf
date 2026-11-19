"""Authored cover-only Library oracles; rendering/browser execution stays gated."""

from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.media_cards import build_media_card
from tests.test_connected_layout import _Markup
from tests.test_media_cards import _environment, _HTML


def _item(item_id=17, *, matches=True, media_type="cassette", covered=True):
    return {"id": item_id, "title": "<b>Bad Attitude & full title</b>", "authors": "<i>Meat Loaf & full credit</i>",
            "cover_path": "covers/bad-attitude.png" if covered else None, "media_type": media_type,
            "matches": matches, "subtitle": "No subtitle row", "series_name": "No series row",
            "series_key": "no-series-row", "series_position": 2, "owned": True, "wishlisted": True,
            "copy_count": 2, "tags": ["No tag row"], "holdings": [{"source_name": "No source box",
                "state": "available", "url": "https://unfollowed.example.test/books/17", "detail": "No holding detail"}]}


def _render(kind, *, item=None, mode=None):
    env, calls = _environment()
    env.globals["build_media_card"] = build_media_card
    item = item or _item()
    context = {"library_presentation_mode": mode, "result_kind": kind,
               "matching_count": 1, "group_count": 1, "filters": {"series": "", "view": kind},
               "view_links": [], "pages": 1, "items": [item], "groups": [], "series_results": []}
    if kind == "groups":
        sibling = {**item, "id": item["id"] + 1, "title": "Other format", "matches": False}
        context["groups"] = [{"id": item["id"], "representative": item, "members": [item, sibling],
                              "member_ids": [item["id"], sibling["id"]], "matching_ids": [item["id"]],
                              "matching_count": 1}]
    elif kind == "series":
        context["series_results"] = [{"key": "series-key", "name": "<u>Local series & complete name</u>",
            "representative": item, "url": "/library?view=series&series=series-key", "item_count": 7, "group_count": 3}]
    before = deepcopy(context)
    html = env.get_template("fragments/library_results.html").render(context)
    assert context == before, "Presentation must not mutate catalogue/group/holding projections"
    return html, calls, context


@pytest.mark.parametrize("media_type", ("book", "cd", "cassette", "video_game", "future-format"))
@pytest.mark.parametrize("covered", (False, True))
@pytest.mark.parametrize("mode", (None, "phone-wrap-v1"))
def test_individual_tile_is_exact_recent_artwork_caption_with_no_outer_panel_or_metadata(media_type, covered, mode):
    item = _item(media_type=media_type, covered=covered)
    html, calls, _ = _render("items", item=item, mode=mode)
    markup = _Markup(html)
    results = markup.testid("library-results")
    assert results.attrs["data-library-result-layout"] == "cover-only-v1"
    member = markup.one(**{"data-library-member": "17"})
    assert member.attrs["class"] == "min-w-0" and member.attrs["data-matches"] == "true"
    assert member.attrs["data-library-member-presentation"] == "cover-only-v1"
    outer = member.ancestors[-1]
    assert outer.tag == "article" and outer.attrs["class"] == "min-w-0"
    card = markup.one("article", within=member)
    assert card.attrs["data-card-presentation"] == "shelf-preview-v1"
    caption = markup.one("div", within=member, **{"class": "media-card__caption"})
    assert markup.one("h3", within=caption).text == item["title"]
    assert markup.one("p", within=caption).text == item["authors"]
    assert member.text.split() == caption.text.split(), "No text may remain outside the hover caption"
    assert not markup.find("p", within=member, **{"class": "media-card__type"})
    assert not markup.find("li", within=member) and not markup.find("a", within=member, target="_blank")
    assert len(markup.find("a", within=member)) == 1
    assert markup.one("a", within=member).attrs["href"] == "/item/17?from=library"
    assert not any(markup.find(tag, within=member) for tag in ("b", "i", "u", "details", "button"))
    assert item["title"] not in calls and item["authors"] not in calls
    env, _ = _environment()
    recent = env.get_template("components/media_card.html").render(
        card=build_media_card(item), media_card_presentation="shelf-preview-v1")
    stage = _HTML(html).root.find("div", cls="media-card__art-stage")
    assert stage == _HTML(recent).root.find("div", cls="media-card__art-stage")


def test_group_hosts_are_unpadded_and_keep_native_editions_and_exact_matching_membership():
    html, _, context = _render("groups")
    markup = _Markup(html)
    group = markup.one("article", **{"data-library-group": "17"})
    assert group.attrs["class"] == "min-w-0"
    assert group.attrs["data-member-ids"] == "17,18" and group.attrs["data-matching-ids"] == "17"
    disclosure = markup.one("details", within=group)
    assert "open" not in disclosure.attrs
    assert "2 live editions · 1 matching" in markup.one("summary", within=disclosure).text
    members = [markup.one(**{"data-library-member": str(item_id)}) for item_id in (17, 18)]
    assert disclosure not in members[0].ancestors and disclosure in members[1].ancestors
    assert [node.attrs["data-matches"] for node in members] == ["true", "false"]
    assert all(node.attrs["class"] == "min-w-0" for node in members)
    assert len(markup.find("a", within=group)) == 2
    assert context["groups"][0]["members"][1]["holdings"] == context["groups"][0]["members"][0]["holdings"]


def test_series_tile_moves_its_own_name_into_the_hover_caption_without_changing_drilldown_or_artwork():
    html, calls, context = _render("series")
    markup = _Markup(html)
    series = markup.one("article", **{"data-library-series": "series-key"})
    assert series.attrs["class"] == "min-w-0"
    link = markup.one("a", within=series)
    assert parse_qs(urlsplit(link.attrs["href"]).query) == {"view": ["series"], "series": ["series-key"]}
    caption = markup.one("div", within=series, **{"class": "media-card__caption"})
    assert markup.one("h3", within=caption).text == context["series_results"][0]["name"]
    assert series.text.split() == caption.text.split()
    assert markup.one("img", within=series).attrs["src"] == "/covers/bad-attitude.png"
    assert "Explore series" not in series.text and not markup.find("u", within=series)
    assert context["series_results"][0]["representative"]["title"] == "<b>Bad Attitude & full title</b>"
    assert context["series_results"][0]["name"] not in calls
