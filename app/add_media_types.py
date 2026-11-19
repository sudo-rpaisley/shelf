"""Detached values shared by the native Add host and native add operations."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapturedAddActor:
    id: int
    username: str
    role: str
    token_version: int
    expires_at: int | float


@dataclass(frozen=True, slots=True)
class CapturedAddIntent:
    mode: str
    media_type: str
    method: str
    query: str
    artist: str = ""
    title_hint: str = ""
    platform: str = ""
    country: str = ""
    music_provider: str = "auto"


@dataclass(frozen=True, slots=True)
class AddDetails:
    location_id: int | None = None
    tags: str = ""
    reading_status: str = ""


@dataclass(frozen=True, slots=True)
class DbAddResult:
    status: str
    item_id: int
    restored: bool = False
    duplicate_title: str | None = None


@dataclass(frozen=True, slots=True)
class AddReceipt:
    status: str
    actual_mode: str | None = None
    actual_media_type: str | None = None
    item_id: int | None = None
    source: str | None = None
    title: str | None = None
    authors: str | None = None
    cover_path: str | None = None
    cover_warning: bool = False
    refused_code: str | None = None


@dataclass(frozen=True, slots=True)
class CoverResult:
    cover_path: str | None = None
    attempted: bool = False
    failed: bool = False


@dataclass(frozen=True, slots=True)
class ManualValues:
    title: str
    authors: str | None = None
    media_type: str = "book"
    isbn13: str | None = None
    upc_code: str | None = None
    publisher: str | None = None
    publish_year: int | None = None
    platform: str | None = None
    series_name: str | None = None
    language: str | None = None
    extra_fields: tuple[tuple[str, str | int | float], ...] = ()


@dataclass(frozen=True, slots=True)
class MusicPreview:
    item: dict | None = None
    candidates: tuple[dict, ...] = ()
