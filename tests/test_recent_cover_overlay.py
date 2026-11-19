"""Additive revision contracts; original tests are preserved, not rewritten.

Rendering/model checks are not browser acceptance. Runtime qualification remains
separately gated; no test outcome is claimed by adding this file.
"""

import pytest

from app.services.media_cards import build_media_card
from tests.test_cover_size_control import _render, _run_script
from tests.test_media_cards import _environment, _HTML


@pytest.mark.parametrize("media_type", ("cassette", "cd", "book", "future-format"))
@pytest.mark.parametrize("cover_path", (None, "covers/overlay.jpg"))
def test_recent_caption_is_inside_the_artwork_wrapper_without_details_or_type(media_type, cover_path):
    env, _ = _environment()
    card = build_media_card({"id": 17, "title": "Bad Attitude", "authors": "Meat Loaf",
                             "media_type": media_type, "cover_path": cover_path})
    root = _HTML(env.get_template("components/media_card.html").render(
        card=card, media_card_presentation="shelf-preview-v1",
    )).root
    (link,) = root.find("a")
    assert link.attrs["href"] == "/item/17"
    (stage,) = link.find("div", cls="media-card__art-stage")
    (cover,) = stage.find("div", cls="media-card__cover-preview")
    (caption,) = cover.find("div", cls="media-card__caption")
    assert caption.find("h3")[0].text == "Bad Attitude"
    assert caption.find("p", cls="media-card__creator")[0].text == "Meat Loaf"
    assert len(root.find("h3")) == len(root.find("p", cls="media-card__creator")) == 1
    assert not root.find("details") and not root.find("summary")
    assert not root.find(cls="media-card__type")
    assert "aria-hidden" not in caption.attrs and "hidden" not in caption.attrs
    (artwork,) = cover.find("span", cls="media-artwork")
    assert artwork.attrs["data-media-type"] == media_type
    assert artwork.attrs["data-cover-shape"] == card.artwork.shape
    assert len(artwork.find("img")) == int(cover_path is not None)


@pytest.mark.parametrize("newstyle", (False, True))
def test_overlay_escapes_full_raw_title_and_creator_once_without_translating_metadata(newstyle):
    env, calls = _environment(newstyle=newstyle)
    title, creator = '<b>Title 100%d & raw</b>', '<i>Credit 50%s & raw</i>'
    card = build_media_card({"id": 7, "title": title, "authors": creator, "media_type": "cassette"})
    root = _HTML(env.get_template("components/media_card.html").render(
        card=card, media_card_presentation="shelf-preview-v1",
    )).root
    assert root.find("h3")[0].text == title
    assert root.find("p", cls="media-card__creator")[0].text == creator
    assert not root.find("b") and not root.find("i")
    assert title not in calls and creator not in calls and "Details" not in calls


@pytest.mark.parametrize("mode", (None, "", "unknown", "shelf-preview-v1 "))
def test_other_card_modes_keep_their_original_title_creator_and_media_type(mode):
    env, _ = _environment()
    card = build_media_card({"id": 3, "title": "Normal card", "authors": "Creator", "media_type": "cd"})
    root = _HTML(env.get_template("components/media_card.html").render(
        card=card, media_card_presentation=mode,
    )).root
    assert root.find("h3")[0].text == "Normal card"
    assert root.find("p", cls="media-card__creator")[0].text == "Creator"
    assert len(root.find("p", cls="media-card__type")) == 1
    assert not root.find(cls="media-card__caption")
    assert not root.find(cls="media-card__art-stage")


def test_unenhanced_range_places_the_default_in_the_middle_and_announces_percentage():
    markup, _ = _render()
    (slider,) = markup.tagged("input")
    attrs = slider["attrs"]
    assert (attrs["min"], attrs["max"], attrs["step"], attrs["value"]) == ("0", "10", "1", "5")
    assert float(attrs["value"]) == (float(attrs["min"]) + float(attrs["max"])) / 2
    assert attrs["aria-valuetext"] == "100%" and "disabled" in attrs
    assert "100%" in markup.text


def test_shipped_sizing_script_maps_all_native_positions_and_resets_to_same_physical_size():
    result = _run_script("""
        const initial = {scale: scale(), values: values(),
            larger: action(doc.roots[0], 'larger').disabled,
            reset: action(doc.roots[0], 'reset').disabled};
        const steps = [];
        for (let position = 0; position <= 10; position++) {
            const range = input(doc.roots[0]); range.valueAsNumber = position;
            emit('input', range);
            steps.push({scale: Number(scale()), values: values(),
                labels: doc.roots.map(root => input(root).getAttribute('aria-valuetext')),
                smaller: action(doc.roots[0], 'smaller').disabled,
                larger: action(doc.roots[0], 'larger').disabled});
        }
        emit('click', action(doc.roots[0], 'reset'));
        const reset = {scale: scale(), values: values()};
        emit('click', action(doc.roots[0], 'smaller'));
        const smaller = scale();
        emit('click', action(doc.roots[0], 'larger'));
        console.log(JSON.stringify({initial, steps, reset, smaller, restored: scale()}));
    """)
    assert result["initial"] == {"scale": "1", "values": ["5", "5"], "larger": False, "reset": True}
    for position, percentage in enumerate((50, 60, 70, 80, 90, 100, 120, 140, 160, 180, 200)):
        row = result["steps"][position]
        assert row["scale"] == percentage / 100
        assert row["values"] == [str(position)] * 2
        assert row["labels"] == [f"{percentage}%"] * 2
        assert row["smaller"] is (position == 0)
        assert row["larger"] is (position == 10)
    assert result["reset"] == {"scale": "1", "values": ["5", "5"]}
    assert result["smaller"] == "0.9" and result["restored"] == "1"


def test_sizing_clamps_invalid_positions_and_resynchronizes_replaced_controls():
    result = _run_script("""
        const observed = [];
        for (const value of [NaN, Infinity, -20, 200]) {
            input(doc.roots[0]).valueAsNumber = value; emit('change', input(doc.roots[0]));
            observed.push({scale: scale(), values: values()});
        }
        emit('click', action(doc.roots[0], 'reset'));
        doc.roots.push(control()); emit('htmx:afterSwap', doc.roots[2]);
        console.log(JSON.stringify({observed, values: values(),
            labels: doc.roots.map(root => input(root).getAttribute('aria-valuetext'))}));
    """)
    assert result["observed"] == [
        {"scale": "1", "values": ["5", "5"]}, {"scale": "1", "values": ["5", "5"]},
        {"scale": "0.5", "values": ["0", "0"]}, {"scale": "2", "values": ["10", "10"]},
    ]
    assert result["values"] == ["5"] * 3 and result["labels"] == ["100%"] * 3
