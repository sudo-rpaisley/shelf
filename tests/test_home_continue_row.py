"""Additive Continue-row contracts; authored only until runtime gates are cleared."""

import pytest

from app.services.media_cards import build_media_card
from tests.conftest import _create_user, _insert_item
from tests.test_connected_layout import _Markup
from tests.test_media_cards import _environment
from tests.test_personal_library import _save


def _entry(item_id, *, status="planned", progress=None, title=None, media_type="book"):
    return {"id": item_id, "title": title or f"Continue item {item_id}", "authors": "Full creator",
            "cover_path": None, "media_type": media_type, "status": status, "progress": progress}


def _render(continuing=(), planned=(), *, messages=None, newstyle=True):
    env, calls = _environment(messages, newstyle=newstyle)
    env.globals["build_media_card"] = build_media_card
    html = env.get_template("fragments/home_personal_library.html").render(
        home_activity={"continue_items": list(continuing), "up_next_items": list(planned)},
    )
    return _Markup(html), calls


def test_one_continue_row_prioritizes_in_progress_deduplicates_and_preserves_progress():
    active = _entry(7, status="in_progress", progress=0, media_type="cd")
    planned = _entry(9, progress=25)
    duplicate = {**active, "title": "Duplicate variant must not be rendered"}
    markup, _ = _render([active], [duplicate, planned])
    root = markup.testid("home-personal-library")
    assert root.attrs["data-continue-shelf"] == "streaming-v1"
    assert "data-recent-shelf" not in root.attrs
    assert [node.text.strip() for node in markup.find("h2", within=root)] == ["Continue"]
    assert not markup.find("h3", within=root, **{"class": "font-medium mb-3"})
    track = markup.one("ul", id="home-continue-row")
    assert track.attrs["tabindex"] == "0" and track.attrs["data-item-shelf"] == "density-v1"
    assert track.attrs["aria-labelledby"] == "home-continue-heading"
    cards = markup.find("article", within=track)
    assert [node.attrs["data-item-id"] for node in cards] == ["7", "9"]
    assert all(node.attrs["data-card-presentation"] == "shelf-preview-v1" for node in cards)
    assert "Duplicate variant" not in root.text
    assert [node.text.strip() for node in markup.find("span", within=cards[0], **{"class": "media-card__badge"})] == ["In progress", "0%"]
    assert [node.text.strip() for node in markup.find("span", within=cards[1], **{"class": "media-card__badge"})] == ["Up Next", "25%"]
    for item_id in (7, 9):
        assert markup.find("a", within=track, href=f"/item/{item_id}?from=activity")
    buttons = markup.find("button", within=root)
    assert [node.attrs["data-recent-action"] for node in buttons] == ["previous", "next"]
    assert all(node.attrs["aria-controls"] == "home-continue-row" and "disabled" in node.attrs for node in buttons)
    assert len(markup.find("a", within=root, href="/activity")) == 2
    assert not markup.find("template", within=root) and not markup.find("details", within=root)


@pytest.mark.parametrize("which", ("continue", "planned", "both"))
def test_both_inputs_can_supply_the_single_row_without_an_empty_second_panel(which):
    active = [_entry(1, status="in_progress")] if which != "planned" else []
    planned = [_entry(2)] if which != "continue" else []
    markup, _ = _render(active, planned)
    assert len(markup.find("ul")) == 1
    assert len(markup.find("article")) == len(active) + len(planned)
    assert not markup.find("p", **{"class": "media-shelf__empty"})


def test_empty_continue_has_one_explanation_and_native_activity_link():
    markup, _ = _render()
    assert [node.text.strip() for node in markup.find("h2")] == ["Continue"]
    assert len(markup.find("p", **{"class": "media-shelf__empty"})) == 1
    assert not markup.find("ul") and not markup.find("button")
    assert len(markup.find("a", href="/activity")) == 1


@pytest.mark.parametrize("newstyle", (False, True))
def test_combined_cards_and_owned_labels_escape_once_without_exposing_notes(newstyle):
    entry = {**_entry(4, title="<b>Raw 100%d & title</b>"),
             "authors": "<i>Credit 50%s & raw</i>", "notes": "private notes stay off the shelf"}
    markup, calls = _render([], [entry], messages={"Continue": "<em>Continue</em>"}, newstyle=newstyle)
    assert markup.one("h2").text == "<em>Continue</em>"
    assert markup.one("h3").text == entry["title"]
    assert markup.one("p", **{"class": "media-card__creator"}).text == entry["authors"]
    assert not markup.find("em") and not markup.find("b") and not markup.find("i")
    assert entry["notes"] not in markup.one("section").text
    assert entry["title"] not in calls and entry["authors"] not in calls


def test_home_combines_only_the_current_accounts_explicit_items_without_mutating_state(viewer_client, viewer_user, db):
    ids = {name: _insert_item(db, title=name, isbn=None) for name in
           ("Active personal item", "Planned personal item", "Household only", "Other account only", "Trashed personal item")}
    uid = viewer_user["id"]
    _save(db, uid, ids["Active personal item"], status="in_progress", progress="37", on_list="1")
    _save(db, uid, ids["Planned personal item"], status="planned")
    _save(db, uid, ids["Trashed personal item"], status="in_progress")
    other = _create_user("continue-other-viewer", "synthetic-test-password", "Other Continue Viewer", "viewer")
    _save(db, other["id"], ids["Other account only"], status="in_progress", on_list="1", notes="other-private-note")
    db.execute("UPDATE items SET deleted_at = '2026-01-01' WHERE id = ?", (ids["Trashed personal item"],))
    db.execute("UPDATE items SET reading_status = 'reading' WHERE id = ?", (ids["Household only"],))
    db.commit()

    def snapshot():
        return {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                for table in ("items", "user_item_state", "user_item_events")}

    before = snapshot()
    response = viewer_client.get("/")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    markup = _Markup(response.text)
    personal = markup.testid("home-personal-library")
    assert [int(node.attrs["data-item-id"]) for node in markup.find("article", within=personal)] == [
        ids["Active personal item"], ids["Planned personal item"],
    ]
    assert "Household only" not in personal.text and "Other account only" not in personal.text
    assert "Trashed personal item" not in personal.text
    assert "other-private-note" not in response.text
    assert snapshot() == before
