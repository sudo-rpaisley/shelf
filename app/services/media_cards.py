"""Pure inputs for the shared card/shelf partials; no reads, routes or policy.

The separately owned presentation registry supplies every immutable artwork
spec, including the unknown-type fallback. No shape, aspect or selector policy
is repeated here, and callers cannot substitute arbitrary CSS/artwork specs.

Metadata remains raw text. Callers translate owned shelf titles when appropriate
and supply an owned English msgid as empty_label, never arbitrary metadata. The
adapter is not a replacement for Library's holdings/matches/tags/group DTOs.
"""

from dataclasses import InitVar, dataclass, field
import re
from urllib.parse import unquote, urlsplit

from app import config, media_presentation
from app.i18n import N_


def _text(value, name):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    # Drop Markup/other str-subclass HTML trust, not the metadata itself.
    return str(value)


def _item_id(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    raise ValueError("id must be a positive integer")


def _local_cover_path(value):
    # Existing writers return covers/<flat filename>, including versioned UUID
    # names; the API also accepts legacy bare filenames. No probing or fetching.
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(?:covers/)?([A-Za-z0-9_-]+\.(?i:jpg|jpeg|png|webp|gif))", value)
    return "covers/" + match[1] if match else None


def _origin_relative_href(value, name):
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        raise ValueError(f"{name} must be an origin-relative link")
    if "\\" in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must not contain whitespace or backslashes")
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise ValueError(f"{name} is invalid") from error
    path = unquote(parts.path)
    if (parts.scheme or parts.netloc or not path.startswith("/") or path.startswith("//")
            or "\\" in path or any(ord(c) <= 32 or ord(c) == 127 for c in path)
            or any(segment in {".", ".."} for segment in path.split("/"))):
        raise ValueError(f"{name} must be a local link without traversal")
    return str(value)


def _item_href(value, item_id, name):
    value = _origin_relative_href(value, name)
    paths = {f"/item/{item_id}", f"/music/item/{item_id}"} if name == "detail_href" else {f"/item/{item_id}/edit"}
    if urlsplit(value).path not in paths:
        raise ValueError(f"{name} must target this item's approved page")
    return value


@dataclass(frozen=True, slots=True)
class MediaCard:
    id: int
    title: str
    creator_text: str
    media_type: str
    cover_path: str | None
    detail_href: str
    edit_href: str | None = None
    badges: tuple[str, ...] = ()
    known_subtype: InitVar[str | None] = None
    artwork: media_presentation.ArtworkSpec = field(init=False)

    def __post_init__(self, known_subtype):
        item_id = _item_id(self.id)
        object.__setattr__(self, "id", item_id)
        for name in ("title", "creator_text", "media_type"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "cover_path", _local_cover_path(self.cover_path))
        object.__setattr__(self, "detail_href", _item_href(self.detail_href, item_id, "detail_href"))
        if self.edit_href is not None:
            object.__setattr__(self, "edit_href", _item_href(self.edit_href, item_id, "edit_href"))
        if isinstance(self.badges, (str, bytes)):
            raise ValueError("badges must be a sequence of text labels")
        object.__setattr__(self, "badges", tuple(_text(badge, "badge") for badge in self.badges))
        object.__setattr__(self, "artwork", media_presentation.artwork_spec(self.media_type, known_subtype))

    @property
    def media_type_label(self) -> str | None:
        """Only canonical owned labels are gettext inputs; unknown types stay raw."""
        return config.MEDIA_TYPES.get(self.media_type)


def build_media_card(item, *, detail_href=None, edit_href=None, creator_text=None,
                     badges=(), known_subtype=None) -> MediaCard:
    """Snapshot a mapping/SQLite row without trusting any of its link fields.

    The default is /item/<numeric id>. A server caller may explicitly choose the
    same item's /music/item/<id> page (with its query/fragment) and may provide an
    edit link only after its own permission check. Nothing here grants access.
    Only the registry chooses artwork, including unknown-type fallback.
    """
    item = dict(item)
    item_id = _item_id(item.get("id"))
    return MediaCard(
        id=item_id, title=item.get("title"),
        creator_text=item.get("authors") if creator_text is None else creator_text,
        media_type=item.get("media_type"), cover_path=item.get("cover_path"),
        detail_href=f"/item/{item_id}" if detail_href is None else detail_href,
        edit_href=edit_href, badges=badges, known_subtype=known_subtype,
    )


@dataclass(frozen=True, slots=True)
class MediaShelf:
    """A bounded preview and an approved full filtered-page link, not pagination.

    has_more labels only; it never controls whether View more is offered. An
    empty shelf may omit the link. Selectors remain opaque registry inputs, and
    keys are data attributes only, not interpolated DOM IDs or selectors.
    """

    key: str
    title: str
    items: tuple[MediaCard, ...]
    selector: tuple[str, ...]
    has_more: bool
    view_more_href: str | None
    empty_label: str = N_("No items yet.")

    def __post_init__(self):
        for name in ("key", "title", "empty_label"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if not self.key or not self.empty_label:
            raise ValueError("key and owned empty_label must be nonempty")
        if type(self.has_more) is not bool:
            raise ValueError("has_more must be a boolean supplied by the caller")
        items = tuple(self.items)
        if any(not isinstance(card, MediaCard) for card in items):
            raise ValueError("items must be validated MediaCard inputs")
        object.__setattr__(self, "items", items)
        if isinstance(self.selector, (str, bytes)):
            raise ValueError("selector must be a sequence of opaque registry selectors")
        object.__setattr__(self, "selector", tuple(_text(value, "selector") for value in self.selector))
        if self.view_more_href is not None or items:
            object.__setattr__(self, "view_more_href", _origin_relative_href(self.view_more_href, "view_more_href"))
