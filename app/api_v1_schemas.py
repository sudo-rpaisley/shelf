"""Versioned read contracts, independent of UI/service dictionaries.

Query models reject unknown keys as well as invalid values. A response can grow
additively in v1; fields are not renamed/removed and disabled sections stay null.
No model includes personal activity, source configuration or opaque provider JSON.
"""

from datetime import date
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import MEDIA_TYPES


MAX_ID = 2**63 - 1
MAX_PAGE = 10_000
T = TypeVar("T")


class NoQuery(BaseModel):
    """Unpaged reads accept no query keys, rather than silently ignoring them."""

    model_config = ConfigDict(extra="forbid")


class PagingQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(1, ge=1, le=MAX_PAGE, description="One-based page; out-of-range pages are empty.")
    page_size: int = Field(50, ge=1, le=100)


class OrderedQuery(PagingQuery):
    order: Literal["asc", "desc"] = "asc"


class ItemQuery(OrderedQuery):
    q: str = Field("", max_length=200, description="Literal substring of title, creators or ISBN/UPC/ASIN.")
    media_type: str | None = Field(None, json_schema_extra={"enum": [*MEDIA_TYPES, None]})
    owned: bool | None = None
    wishlisted: bool | None = None
    sort: Literal["title", "authors", "created_at", "id"] = "title"

    @field_validator("media_type")
    @classmethod
    def known_media_type(cls, value):
        if value is not None and value not in MEDIA_TYPES:
            raise ValueError("Unknown media type")
        return value


class LoanQuery(OrderedQuery):
    status: Literal["open", "returned", "overdue", "all"] = "open"
    item_id: int | None = Field(None, ge=1, le=MAX_ID)
    borrower_id: int | None = Field(None, ge=1, le=MAX_ID)
    sort: Literal["checked_out", "due_date", "title", "id"] = "checked_out"


class ListQuery(OrderedQuery):
    q: str = Field("", max_length=200)
    sort: Literal["name", "created_at", "item_count"] = "name"


class SeriesQuery(OrderedQuery):
    q: str = Field("", max_length=200)
    sort: Literal["name", "item_count", "owned_count", "wishlist_count"] = "name"


class SeriesItemsQuery(OrderedQuery):
    sort: Literal["position", "title", "id"] = "position"


class UpcomingQuery(OrderedQuery):
    series_name: str | None = Field(None, min_length=1, max_length=1000)
    dated: bool | None = None
    release_from: date | None = None
    release_to: date | None = None
    sort: Literal["release_date", "title", "series_name"] = "release_date"

    @model_validator(mode="after")
    def date_range(self):
        if self.release_from and self.release_to and self.release_from > self.release_to:
            raise ValueError("release_from must not be after release_to")
        return self


class Page(BaseModel, Generic[T]):
    """Totals are calculated after visibility/filtering, before SQL LIMIT/OFFSET.

    Ordering is deterministic for unchanged data, not a cursor/snapshot across
    requests. An empty result has zero pages. next_page is null at the bound.
    """

    data: list[T]
    total: int = Field(ge=0)
    page: int = Field(ge=1, le=MAX_PAGE)
    page_size: int = Field(ge=1, le=100)
    pages: int = Field(ge=0)
    next_page: int | None


class ItemSeries(BaseModel):
    name: str
    position: float | None


class ItemSummary(BaseModel):
    """Catalogue metadata only; shared legacy status and personal activity omitted."""

    id: int
    title: str
    subtitle: str | None
    authors: str | None
    media_type: str
    isbn: str | None
    isbn10: str | None
    upc: str | None
    asin: str | None
    cover_url: str | None = Field(description="Same-origin /covers/ URL, never a filesystem/provider path.")
    owned: bool
    wishlisted: bool
    series: ItemSeries | None = Field(description="Null when Series is off or no series is assigned.")
    created_at: str
    updated_at: str


class ItemPage(Page[ItemSummary]):
    pass


class ItemDetail(ItemSummary):
    publisher: str | None
    publish_year: int | None
    language: str | None
    description: str | None
    page_count: int | None
    narrator: str | None
    duration_mins: int | None
    platform: str | None


class ItemValue(BaseModel):
    amount: float | None = Field(description="Manual value preferred over estimate; null when unpriced.")
    currency: str


class ItemSource(BaseModel):
    """One cached digital holding, not a live reachability/access check."""

    key: str
    name: str
    state: Literal["available", "unavailable", "unknown"]
    url: str | None = Field(description="Validated credential-free HTTP(S) browser URL, otherwise null.")
    last_seen_at: str | None


class SourcePage(Page[ItemSource]):
    pass


class ItemDetailResponse(BaseModel):
    item: ItemDetail
    value: ItemValue | None = Field(description="Null when Valuation is off.")
    sources: SourcePage | None = Field(description="Null when Connected library is off; page/page_size page this section.")


class BorrowerSummary(BaseModel):
    id: int
    name: str


class Loan(BaseModel):
    id: int
    item_id: int
    item_title: str
    borrower: BorrowerSummary
    checked_out: str
    due_date: str | None
    checked_in: str | None
    overdue: bool


class LoanPage(Page[Loan]):
    pass


class ListSummary(BaseModel):
    """Shared catalogue list, not an account's personal My List or gift claims."""

    slug: str
    name: str
    item_count: int
    created_at: str


class ListPage(Page[ListSummary]):
    pass


class SeriesSummary(BaseModel):
    name: str
    item_count: int
    owned_count: int
    wishlist_count: int
    complete: bool | None = Field(description="Manual completeness override, null means automatic/unspecified.")


class SeriesPage(Page[SeriesSummary]):
    pass


class SeriesDetailResponse(BaseModel):
    series: SeriesSummary
    description: str | None
    items: ItemPage


class UpcomingRelease(BaseModel):
    id: int
    hardcover_book_id: int
    title: str
    authors: str | None
    series_name: str
    series_position: float | None
    release_date: str | None
    cover_url: str | None


class UpcomingPage(Page[UpcomingRelease]):
    pass


class OpenAPIDocument(BaseModel):
    """OpenAPI 3.1 document for this read surface only (no collection data).

    OpenAPI owns the extensible objects inside paths/components; catalogue
    responses themselves always use explicit field-level models above.
    """

    openapi: str
    info: dict[str, Any]
    paths: dict[str, Any]
    components: dict[str, Any]
