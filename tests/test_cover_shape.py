"""Native media artwork policy and the existing compatibility hooks.

The presentation registry owns shapes; config delegates to it. These tests pin
the selected native defaults and the legacy Jinja/square-CSS hooks. They do not
establish adoption or visual correctness of the new cassette/game CSS consumers.
"""

import re
from pathlib import Path

from app.config import (
    MEDIA_TYPES,
    MUSIC_MEDIA_TYPES,
    SQUARE_COVER_MEDIA_TYPES,
    cover_shape,
)
from app.media_presentation import artwork_spec

APP_CSS = Path(__file__).resolve().parent.parent / "static" / "css" / "app.css"
EXPECTED_SHAPES = {
    "book": "portrait", "audiobook": "square", "ebook": "portrait",
    "comic": "portrait", "manga": "portrait", "vinyl": "square",
    "cassette": "cassette", "cd": "square", "digital_music": "square",
    "magazine": "portrait", "dvd": "portrait", "digital_video": "portrait",
    "tv_series": "portrait", "video_game": "game",
}


def test_every_music_type_uses_its_native_artwork_shape():
    for media_type in MUSIC_MEDIA_TYPES:
        assert cover_shape(media_type) == EXPECTED_SHAPES[media_type], media_type


def test_every_other_type_uses_its_native_artwork_shape():
    assert set(EXPECTED_SHAPES) == set(MEDIA_TYPES)
    for media_type in set(MEDIA_TYPES) - MUSIC_MEDIA_TYPES:
        assert cover_shape(media_type) == EXPECTED_SHAPES[media_type], media_type


def test_square_compatibility_set_is_derived_from_the_registry():
    assert SQUARE_COVER_MEDIA_TYPES == frozenset(
        media_type for media_type, shape in EXPECTED_SHAPES.items() if shape == "square"
    )
    assert SQUARE_COVER_MEDIA_TYPES == frozenset(
        media_type for media_type in MEDIA_TYPES if artwork_spec(media_type).shape == "square"
    )


def test_missing_or_unknown_type_is_portrait_not_an_error():
    assert cover_shape(None) == "portrait"
    assert cover_shape("not_a_type") == "portrait"


def test_registered_as_a_jinja_global():
    from app.main import templates  # G14: import inside the test

    assert templates.env.globals["cover_shape"] is cover_shape


def test_built_stylesheet_carries_the_square_rule():
    # Tailwind's minifier may drop the attribute value's quotes; accept both.
    css = APP_CSS.read_text()
    assert re.search(r'\[data-cover-shape=("square"|square)\]', css)
