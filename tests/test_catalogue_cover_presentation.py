"""Authored shared-cover/heading contracts, not executed browser qualification."""

from pathlib import Path

import pytest

from app.services.media_cards import MediaShelf, build_media_card
from tests.conftest import _insert_item
from tests.test_connected_layout import _Markup
from tests.test_media_cards import _environment, _HTML
from tests.test_library_artwork_sources import RECTANGLE
from tests.test_media_page_cards import _library_seed
from tests.test_media_navigation import _feature, _setting


ROOT = Path(__file__).resolve().parents[1]


def _stage(html):
    (stage,) = _HTML(html).root.find("div", cls="media-card__art-stage")
    return stage


@pytest.mark.parametrize("media_type", ("book", "cd", "cassette", "video_game", "future-format"))
@pytest.mark.parametrize("covered", (False, True))
@pytest.mark.parametrize("newstyle", (False, True))
def test_shared_cards_and_catalogue_helper_use_the_exact_recently_added_cover(media_type, covered, newstyle):
    env, calls = _environment(newstyle=newstyle)
    env.globals["build_media_card"] = build_media_card
    item = {"id": 23, "title": "<b>Raw title 100%d & full text</b>",
            "authors": "<i>Raw creator 50%s & full text</i>", "media_type": media_type,
            "cover_path": "covers/common.png" if covered else None}
    card = build_media_card(item, badges=("Wishlist",), edit_href="/item/23/edit")
    recent = env.get_template("components/media_card.html").render(
        card=card, media_card_presentation="shelf-preview-v1",
    )
    shared = env.get_template("components/media_card.html").render(card=card, media_card_size_mode="shared-v2")
    helper = env.get_template("components/catalogue_cover.html").render(cover_item=item, cover_print="card")
    assert _stage(recent) == _stage(shared) == _stage(helper)
    markup = _Markup(shared)
    stage = markup.one(**{"data-cover-presentation": "recent-v1"})
    assert stage.attrs["data-cover-size-stage"] == "catalogue-v2"
    caption = markup.one("div", within=stage, **{"class": "media-card__caption"})
    assert markup.one("h3", within=caption).text == item["title"]
    assert markup.one("p", within=caption).text == item["authors"]
    assert "hidden" not in caption.attrs and "aria-hidden" not in caption.attrs
    assert not markup.find("b") and not markup.find("i")
    assert item["title"] not in calls and item["authors"] not in calls
    artwork = markup.one("span", within=stage, **{"class": "media-artwork"})
    assert artwork.attrs["data-cover-shape"] == card.artwork.shape
    assert artwork.attrs["data-cover-size-artwork"] == "catalogue-v2"
    images = markup.find("img", within=artwork)
    assert len(images) == int(covered)
    if covered:
        assert images[0].attrs["src"] == "/covers/common.png" and images[0].attrs["alt"] == ""
    else:
        assert markup.one("span", within=artwork, **{"class": "media-artwork__placeholder"})
    assert markup.one("a", **{"class": "media-card__detail"}).attrs["href"] == "/item/23"
    assert markup.one("a", **{"class": "media-card__edit"}).attrs["href"] == "/item/23/edit"
    assert markup.one("span", **{"class": "media-card__badge"}).text == "Wishlist"
    assert markup.one("p", **{"class": "media-card__type"}).text.strip()


@pytest.mark.parametrize("href", ("/library?reset=1&view=items&sort=recent", "/browse?sort=newest"))
@pytest.mark.parametrize("count", (0, 1, 8))
def test_recent_heading_replaces_only_view_all_and_keeps_native_row_controls(href, count):
    env, _ = _environment()
    env.globals["build_media_card"] = build_media_card
    cards = tuple(build_media_card({"id": i + 1, "title": f"Recent {i}", "media_type": "book"})
                  for i in range(count))
    overflow = [{"id": 100 + i, "title": f"Overflow {i}", "media_type": "cd", "wishlisted": False}
                for i in range(16)] if count == 8 else []
    shelf = MediaShelf(key="recent", title="Recently added", items=cards, selector=(),
                       has_more=bool(overflow), view_more_href=href)
    markup = _Markup(env.get_template("fragments/home_recent_shelf.html").render(
        shelf=shelf, recent_overflow=overflow, total_count=count + len(overflow),
        item_size_mode="density-v1",
    ))
    heading = markup.one("h2", id="home-recent-heading")
    link = markup.one("a", within=heading)
    assert link.text == shelf.title and link.attrs["href"] == href
    assert link.attrs["class"] == "recent-shelf__title-link"
    assert not any(key.startswith(("on", "hx-", "@", "x-")) for key in link.attrs)
    assert not markup.find("a", **{"class": "recent-shelf__all"})
    assert markup.one(**{"data-recent-count": None}).text.strip().startswith(str(count))
    if count:
        track = markup.one("ul", id="home-recent-row")
        assert len(markup.find("article", within=track)) == count
        terminal = markup.one("a", within=track, **{"class": "media-shelf__view-more"})
        assert terminal.attrs["href"] == href
        buttons = markup.find("button")
        assert [node.attrs["data-recent-action"] for node in buttons] == ["previous", "next"]
        assert all(node.attrs["aria-controls"] == "home-recent-row" and "disabled" in node.attrs for node in buttons)
        buffer = markup.one("template", **{"data-recent-overflow": None})
        assert len(markup.find("article", within=buffer)) == len(overflow)
    else:
        assert not markup.find("button") and not markup.find("template") and not markup.find("ul")


@pytest.mark.parametrize("path", ("/library", "/api/library/search"))
def test_library_keeps_scoped_holdings_creator_labels_and_prepared_artwork_with_common_covers(viewer_client, db, path):
    paper, ebook, audio, sources, tag = _library_seed(db)
    response = viewer_client.get(path, params={"media_type": "ebook", "availability": "digital",
                                               "source": f"source:{sources[0]}"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    markup = _Markup(response.text)
    group = markup.one("article", **{"data-library-group": str(paper)})
    assert group.attrs["data-member-ids"] == f"{paper},{ebook},{audio}"
    assert group.attrs["data-matching-ids"] == str(ebook)
    items = {row["id"]: row for row in response.context["groups"][0]["members"]}
    for item_id in (paper, ebook, audio):
        member = markup.one(**{"data-library-member": str(item_id)})
        assert member.attrs["data-matches"] == ("true" if item_id == ebook else "false")
        stage = markup.one(within=member, **{"data-cover-presentation": "recent-v1"})
        assert markup.one("h3", within=stage).text == items[item_id]["title"]
        assert markup.one("p", within=stage).text == items[item_id]["authors"]
        assert markup.one("p", within=member, **{"aria-label": "Author(s)"}).text == items[item_id]["authors"]
        assert markup.one("a", within=member, **{"class": "media-card__detail"}).attrs["href"] == f"/item/{item_id}?from=library"
        assert tag in member.text and f"#{item_id}" in member.text
    paper_node = markup.one(**{"data-library-member": str(paper)})
    assert "2 physical copies" in paper_node.text and "Owned" in paper_node.text
    assert {holding.attrs["data-holding-state"] for holding in markup.find("li", within=paper_node)} == {"available", "missing"}
    digital = markup.one(**{"data-library-member": str(ebook)})
    assert markup.one("a", within=digital, target="_blank").attrs["href"] == f"https://books.example.test/books/{ebook}"
    assert "Last source sync failed; cached record retained." in markup.one(**{"data-library-member": str(audio)}).text
    assert "private-failure-marker" not in response.text
    assert not markup.find("b") and not markup.find("i") and not markup.find("em") and not markup.find("u")


@pytest.mark.parametrize("path", ("/library", "/api/library/search"))
def test_shared_cover_receives_the_actual_issued_svg_source_not_a_raw_url_or_hydrated_dto(viewer_client, db, path):
    from app import config

    raw = "covers/shared-presentation.SvG"
    (config.COVERS_DIR / "shared-presentation.SvG").write_bytes(RECTANGLE)
    item_id = _insert_item(db, title="Validated presentation", isbn=None, media_type="vinyl", cover_path=raw)
    db.commit()
    response = viewer_client.get(path, params={"view": "items"})
    assert response.status_code == 200
    prepared = response.context["prepared_library_artwork"]
    source = prepared.source_for(item_id, raw)
    assert source is not None and source.captured_bytes == RECTANGLE
    assert prepared.source_for(item_id + 1, raw) is None
    markup = _Markup(response.text)
    member = markup.one(**{"data-library-member": str(item_id)})
    stage = markup.one(within=member, **{"data-cover-presentation": "recent-v1"})
    assert markup.one("img", within=stage).attrs["src"] == source.image_src
    assert f'src="/{raw}"' not in response.text
    row = next(row for row in response.context["items"] if row["id"] == item_id)
    assert row["cover_path"] == raw and "image_src" not in row


@pytest.mark.parametrize("state", ("eligible", "disabled", "hidden"))
def test_home_heading_keeps_the_existing_library_eligibility_destination(viewer_client, db, state):
    _insert_item(db, title="Heading destination", isbn=None)
    if state == "disabled":
        _feature(db, "connected_library", False)
    elif state == "hidden":
        _setting(db, "nav_hidden_tabs", '["library"]')
    db.commit()
    response = viewer_client.get("/")
    assert response.status_code == 200
    markup = _Markup(response.text)
    href = "/library?reset=1&view=items&sort=recent" if state == "eligible" else "/browse?sort=newest"
    assert markup.one("a", within=markup.one("h2", id="home-recent-heading")).attrs["href"] == href


def test_cover_producers_share_a_renderer_without_migrating_operational_or_export_surfaces():
    for name in ("activity.html", "series.html", "item_detail.html", "music_item.html", "duplicates.html",
                 "location_order.html", "stats_wrapup.html", "valuation_report.html", "fragments/item_row.html",
                 "fragments/library_results.html", "fragments/related_media_panel.html", "fragments/trash_list.html"):
        source = (ROOT / "app/templates" / name).read_text()
        assert 'include "components/catalogue_cover.html"' in source, name
        assert 'src="/{{' not in source, name
    for name in ("share.html", "fragments/scan_result.html", "fragments/cover_search.html",
                 "fragments/item_editor_common_fields.html"):
        source = (ROOT / "app/templates" / name).read_text()
        assert 'components/catalogue_cover.html' not in source and 'components/media_cover.html' not in source
    wrapup = (ROOT / "app/templates/stats_wrapup.html").read_text()
    assert 'data-cover="{{ \'/\' ~ tile.cover_path if tile.cover_path else \'\' }}" data-title="{{ tile.title }}"' in wrapup
