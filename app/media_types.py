"""Shared media-storage semantics.

Media families answer how something is catalogued; these helpers answer how a
holding exists. Physical media can have copies, shelf locations and position.
Digital media belongs to providers/servers instead and must never be forced
into the physical location tree.
"""

from app.config import MEDIA_TYPES

# These types currently represent digital/provider-backed holdings in Shelf.
# Keep this list deliberately explicit: a newly-added media type should default
# to physical until its storage semantics are considered rather than silently
# escaping copy/location handling.
DIGITAL_MEDIA_TYPES = frozenset({
    "audiobook",
    "ebook",
    "digital_music",
    "digital_comic",
    "digital_manga",
    "digital_game",
})

PHYSICAL_MEDIA_TYPES = frozenset(MEDIA_TYPES) - DIGITAL_MEDIA_TYPES


def is_digital_media(media_type: str | None) -> bool:
    return bool(media_type and media_type in DIGITAL_MEDIA_TYPES)


def is_physical_media(media_type: str | None) -> bool:
    return bool(media_type and media_type in PHYSICAL_MEDIA_TYPES)


def requires_physical_location(media_type: str | None) -> bool:
    """Whether owned holdings of this type may be placed in the location tree."""
    return is_physical_media(media_type)
