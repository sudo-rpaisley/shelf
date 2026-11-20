"""Compact creator-caption regressions, source-authored and not executed here."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import re

from markupsafe import Markup
import pytest

from app.services import caption_links
from tests.test_caption_navigation import _card, _env
from tests.test_connected_layout import _Markup


@pytest.mark.parametrize("text", ["Lara Elena Donnelly, French Carlomagno, Pius Bak, Samantha Dodge",
                                  "山田 太郎, 李 明, 김 민수", "First Writer, Second Writer, Third Writer"])
def test_compact_preview_retains_one_exact_creator_link_and_the_complete_original_credit(text):
    card = _card(authors=text)
    original = card.creator_parts
    assert card.creator_text == text and "".join(part.text for part in original) == text
    assert len([part for part in original if part.href]) >= 2
    preview = card.creator_preview
    assert preview.truncated and len([part for part in preview.parts if part.href]) == 1
    first = next(part for part in original if part.href)
    assert next(part for part in preview.parts if part.href) is first
    assert card.creator_parts is original and card.creator_text == text
    with pytest.raises(FrozenInstanceError):
        preview.truncated = False


@pytest.mark.parametrize("text", ["Meat Loaf", "Simon & Garfunkel", "Author - illustrator", "", "x" * 5001])
def test_single_or_unparsed_credit_keeps_its_raw_text_without_an_invented_ellipsis(text):
    parts = caption_links.creator_parts(text)
    preview = caption_links.creator_preview(parts)
    assert not preview.truncated and preview.parts is parts
    assert "".join(part.text for part in preview.parts) == text


@pytest.mark.parametrize("newstyle", [False, True])
def test_rendered_multicreator_caption_has_no_clipped_focusable_extra_links_and_escapes_full_tooltip(newstyle):
    env, calls = _env(newstyle=newstyle)
    raw = Markup('First Writer, Second Writer, <credit-probe>Raw 100%d & "credit"</credit-probe>')
    card = _card(authors=raw)
    html = env.get_template("components/media_card.html").render(card=card, media_card_presentation="shelf-preview-v1")
    markup = _Markup(html)
    credit = markup.one("p", **{"class": "media-card__creator"})
    creators = markup.find("a", within=credit)
    assert len(creators) == 1 and creators[0].text == "First Writer"
    assert creators[0].attrs["href"] == "/browse/creator?key=first+writer"
    assert credit.attrs["title"] == str(raw)
    assert "Second Writer" not in credit.text and credit.text.endswith("…")
    more = markup.one("span", within=credit, **{"data-caption-more": None})
    assert "data-caption-more" in more.attrs and more.attrs["aria-hidden"] == "true"
    assert "tabindex" not in more.attrs and not markup.find("button", within=credit)
    assert markup.one("a", **{"data-caption-link": "series"}).attrs["href"] == card.series_href
    assert not markup.find("credit-probe") and raw not in calls
    static = env.from_string('{% from "components/media_cover.html" import media_cover with context %}{{ media_cover(card) }}').render(card=card)
    static_markup = _Markup(static)
    assert static_markup.one("p", **{"class": "media-card__creator"}).text == str(raw)
    assert not static_markup.find("a")


def test_only_interactive_caption_overflow_is_clipped_and_creator_names_use_single_line_ellipsis():
    css = (Path(__file__).resolve().parents[1] / "static/css/cover-size.source.css").read_text()
    caption = re.search(r'\[data-caption-navigation="metadata-v1"\] \.media-card__caption\s*\{([^}]+)\}', css)[1]
    assert "overflow: hidden" in caption and "overflow-y: auto" not in caption
    creator = re.search(r'\[data-caption-navigation="metadata-v1"\] \.media-card__caption \.media-card__creator > a\s*\{([^}]+)\}', css)[1]
    assert "white-space: nowrap" in creator and "text-overflow: ellipsis" in creator
    assert "min-inline-size: 0" in creator and "line-height: 2.75rem" in creator
