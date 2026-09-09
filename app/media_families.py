"""User-facing media families shared by Home and Browse.

Shelf stores concrete media types (book, manga, vinyl, and so on), but the
front door should let people enter the catalogue through broader, stable
families.  Keep that mapping in one small presentation-neutral module so Home
cards, Browse filtering and future statistics cannot silently disagree.
"""

from __future__ import annotations

from typing import Iterable, Mapping


MEDIA_FAMILIES: dict[str, dict[str, object]] = {
    "books": {
        "label": "Books",
        "description": "Books and eBooks",
        "types": ("book", "kids_book", "ebook"),
    },
    "comics": {
        "label": "Comics & Manga",
        "description": "Comics, graphic novels and manga",
        "types": ("comic", "manga"),
    },
    "periodicals": {
        "label": "Magazines & Periodicals",
        "description": "Magazines and serial publications",
        "types": ("magazine",),
    },
    "music": {
        "label": "Music",
        "description": "Vinyl, CD, cassette and digital music",
        "types": ("vinyl", "cassette", "cd", "digital_music"),
    },
    "film": {
        "label": "Film & TV",
        "description": "DVD and Blu-ray",
        "types": ("dvd",),
    },
    "games": {
        "label": "Games",
        "description": "Video games",
        "types": ("video_game",),
    },
    "audiobooks": {
        "label": "Audiobooks",
        "description": "Spoken-word editions",
        "types": ("audiobook",),
    },
}


def types_for_family(key: str) -> tuple[str, ...]:
    """Return the concrete media types for ``key`` or an empty tuple."""
    family = MEDIA_FAMILIES.get(key)
    if not family:
        return ()
    return tuple(family["types"])


def sql_condition(key: str) -> tuple[str, list]:
    """Browse SQL condition for one family.

    Unknown keys deliberately match nothing instead of dropping the filter.
    A hand-edited ``?media_family_filter=...`` URL must never broaden back to
    the whole catalogue while still displaying an active Family chip.
    """
    media_types = types_for_family(key)
    if not media_types:
        return "1 = 0", []
    placeholders = ", ".join("?" for _ in media_types)
    return f"i.media_type IN ({placeholders})", list(media_types)


def cards(media_type_rows: Iterable[Mapping[str, object]]) -> list[dict]:
    """Project per-type dashboard rows into stable user-facing family cards."""
    counts = {
        str(row["media_type"]): int(row["item_count"] or 0)
        for row in media_type_rows
    }
    result: list[dict] = []
    for key, family in MEDIA_FAMILIES.items():
        family_types = tuple(family["types"])
        result.append(
            {
                "key": key,
                "label": str(family["label"]),
                "description": str(family["description"]),
                "count": sum(counts.get(media_type, 0) for media_type in family_types),
                "href": f"/browse?media_family_filter={key}",
                "types": family_types,
            }
        )
    return result
