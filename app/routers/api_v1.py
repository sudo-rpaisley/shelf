"""The versioned, read-only JSON API: `/api/v1`.

- `GET /api/v1/stats` (and `HEAD`) — one document for dashboards: item
  counts, loans, collection value and upcoming releases. A section whose
  feature is off is `null`, never absent, so a template reading
  `value_json.loans.overdue` gets None rather than an error.

Readable with a session cookie or a read-only API token (the bearer branch in
`AuthMiddleware`, `app/main.py`). Every route here declares a `response_model`:
the model, not a service's dict, is the contract the recipes depend on, and
`tests/test_api_v1_stats.py` walks the routes to keep it so. Role first, then
the `api` feature — the order the registry lint checks.
New read contracts and paging rules are documented in ``docs/api-v1.md``;
``GET /api/v1/schema`` publishes their OpenAPI document under the same auth.
"""

from contextlib import contextmanager
from copy import copy
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel

from app.auth import require_role
from app.api_v1_schemas import (
    MAX_ID, ItemDetailResponse, ItemPage, ItemQuery, ListPage, ListQuery,
    ListSummary, Loan, LoanPage, LoanQuery, NoQuery, OpenAPIDocument, PagingQuery,
    SeriesDetailResponse, SeriesItemsQuery, SeriesPage, SeriesQuery,
    UpcomingPage, UpcomingQuery,
)
from app.currency import get_currency
from app.database import get_db
from app.features import feature_enabled, require_feature
from app.routers.checkouts import count_overdue
from app.services import api_v1 as reads
from app.services import collection_value, release_radar
from app.services.home_dashboard import dashboard_summary

router = APIRouter(prefix="/api/v1", tags=["v1"])


class MediaTypeCount(BaseModel):
    media_type: str
    total: int
    owned: int
    wishlist: int


class ItemsSection(BaseModel):
    total: int
    owned: int
    wishlist: int
    by_media_type: list[MediaTypeCount]


class LoansSection(BaseModel):
    open: int
    overdue: int


class ValueSection(BaseModel):
    total: float
    currency: str


class UpcomingSection(BaseModel):
    count: int
    next_release_date: str | None


class StatsResponse(BaseModel):
    items: ItemsSection
    loans: LoansSection | None
    value: ValueSection | None
    upcoming: UpcomingSection | None
    generated_at: str


# HEAD is listed so a monitor's HEAD probe gets 200; FastAPI's `get` serves GET only.
@router.api_route("/stats", methods=["GET", "HEAD"], response_model=StatsResponse)
async def stats(
    response: Response,
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
):
    with get_db() as db:
        summary = dashboard_summary(db, recent_limit=0)
        items = ItemsSection(
            total=summary["total_count"],
            owned=summary["owned_count"],
            wishlist=summary["wishlist_count"],
            by_media_type=[
                MediaTypeCount(
                    media_type=r["media_type"],
                    total=r["item_count"],
                    owned=r["owned_count"] or 0,
                    wishlist=r["wishlist_count"] or 0,
                )
                for r in summary["media_types"]
            ],
        )
        loans = None
        if feature_enabled("lending"):
            loans = LoansSection(open=summary["lent_out_count"], overdue=count_overdue(db))
        value = None
        if feature_enabled("valuation"):
            # Rounded here, not in the shared sum: a float sum of prices
            # reads 65.47999999999999 in any consumer that shows it raw.
            value = ValueSection(total=round(collection_value.owned_total(db), 2),
                                 currency=get_currency().code)
        upcoming = None
        if feature_enabled("release_radar") and feature_enabled("hardcover"):
            # limit=None: VISIBLE_LIMIT is the Upcoming page's render cap, and
            # `count` is a total, not a page size.
            rows = release_radar.visible_rows(db, limit=None)
            upcoming = UpcomingSection(
                count=len(rows),
                next_release_date=release_radar.next_release_date(rows, date.today()),
            )
    # Live counts with no validators: nothing between Shelf and a dashboard
    # may store and replay them.
    response.headers["Cache-Control"] = "no-store"
    return StatsResponse(
        items=items,
        loans=loans,
        value=value,
        upcoming=upcoming,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


@contextmanager
def _snapshot(response):
    response.headers["Cache-Control"] = "no-store"
    with get_db() as db:
        # sqlite3's deferred default does not begin a transaction for SELECT.
        # Counts, pages and optional sections must describe the same snapshot.
        db.execute("BEGIN")
        yield db


def _found(value, kind):
    # Hidden/trashed and unknown resources share exactly the same answer.
    if value is None:
        raise HTTPException(status_code=404, detail=f"{kind} not found")
    return value


@router.api_route("/items", methods=["GET", "HEAD"], response_model=ItemPage)
async def items(
    response: Response,
    query: Annotated[ItemQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
):
    """Page visible editions, not grouped works or physical copies."""
    with _snapshot(response) as db:
        return reads.item_page(db, query)


@router.api_route("/items/{item_id}", methods=["GET", "HEAD"], response_model=ItemDetailResponse)
async def item_detail(
    response: Response,
    item_id: Annotated[int, Path(ge=1, le=MAX_ID)],
    query: Annotated[PagingQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
):
    """One edition; page/page_size apply to cached digital sources only.

    Valuation and Connected library sections are null when disabled. Neither
    this route nor any other v1 read fetches a source or updates activity.
    """
    with _snapshot(response) as db:
        return _found(reads.item_detail(db, item_id, query), "Item")


@router.api_route("/loans", methods=["GET", "HEAD"], response_model=LoanPage)
async def loans(
    response: Response,
    query: Annotated[LoanQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
    ___=Depends(require_feature("lending")),
):
    """Open loans by default; returned/all/overdue are explicit status filters."""
    with _snapshot(response) as db:
        return reads.loan_page(db, query)


@router.api_route("/loans/{loan_id}", methods=["GET", "HEAD"], response_model=Loan)
async def loan_detail(
    response: Response,
    loan_id: Annotated[int, Path(ge=1, le=MAX_ID)],
    _query: Annotated[NoQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
    ___=Depends(require_feature("lending")),
):
    """A visible item's loan, without free-text loan notes."""
    with _snapshot(response) as db:
        return _found(reads.loan_detail(db, loan_id), "Loan")


@router.api_route("/wishlist", methods=["GET", "HEAD"], response_model=ItemPage)
async def wishlist(
    response: Response,
    query: Annotated[ItemQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
):
    """Alias of /lists/wishlist/items; membership, not owned=0, means wanted."""
    with _snapshot(response) as db:
        return reads.item_page(db, query, list_slug="wishlist")


@router.api_route("/lists", methods=["GET", "HEAD"], response_model=ListPage)
async def lists(
    response: Response,
    query: Annotated[ListQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
):
    """Shared lists containing visible items, plus the always-present wishlist.

    Personal My List, saved views, private activity and gift claims are not
    shared catalogue lists and are never part of this API.
    """
    with _snapshot(response) as db:
        return reads.list_page(db, query)


@router.api_route("/lists/{slug}", methods=["GET", "HEAD"], response_model=ListSummary)
async def list_detail(
    response: Response,
    slug: Annotated[str, Path(min_length=1, max_length=200)],
    _query: Annotated[NoQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
):
    with _snapshot(response) as db:
        return _found(reads.list_detail(db, slug), "List")


@router.api_route("/lists/{slug}/items", methods=["GET", "HEAD"], response_model=ItemPage)
async def list_items(
    response: Response,
    slug: Annotated[str, Path(min_length=1, max_length=200)],
    query: Annotated[ItemQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
):
    with _snapshot(response) as db:
        _found(reads.list_detail(db, slug), "List")
        return reads.item_page(db, query, list_slug=slug)


@router.api_route("/series", methods=["GET", "HEAD"], response_model=SeriesPage)
async def series(
    response: Response,
    query: Annotated[SeriesQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
    ___=Depends(require_feature("series")),
):
    """Local series only; counts use visible editions and explicit wishlists."""
    with _snapshot(response) as db:
        return reads.series_page(db, query)


@router.api_route("/series/{name:path}", methods=["GET", "HEAD"], response_model=SeriesDetailResponse)
async def series_detail(
    response: Response,
    name: Annotated[str, Path(min_length=1, max_length=1000)],
    query: Annotated[SeriesItemsQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
    ___=Depends(require_feature("series")),
):
    """Local metadata plus paginated editions; names containing / round-trip."""
    with _snapshot(response) as db:
        return _found(reads.series_detail(db, name, query), "Series")


@router.api_route("/upcoming", methods=["GET", "HEAD"], response_model=UpcomingPage)
async def upcoming(
    response: Response,
    query: Annotated[UpcomingQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
    ___=Depends(require_feature("release_radar")),
    ____=Depends(require_feature("hardcover")),
):
    """Cached, undismissed, unheld releases in series with a visible edition.

    Past dates remain visible until normal radar pruning. Date bounds are
    inclusive and exclude undated records; no provider requests are made.
    """
    with _snapshot(response) as db:
        return reads.upcoming_page(db, query)


@router.api_route("/schema", methods=["GET", "HEAD"], response_model=OpenAPIDocument,
                  responses={422: {"description": "Unknown query parameter"}})
async def schema(
    response: Response,
    _query: Annotated[NoQuery, Query()],
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("api")),
):
    """Publish this version's OpenAPI schema using the normal token/session.

    Only v1 routes are included. HEAD has the GET contract without a body;
    documenting GET once avoids duplicate operation IDs for the same route.
    """
    response.headers["Cache-Control"] = "no-store"
    routes = []
    for route in router.routes:
        if "GET" in (getattr(route, "methods", None) or ()):
            route = copy(route)
            route.methods = {"GET"}
            routes.append(route)
    document = get_openapi(title="Shelf read API", version="1", routes=routes,
                           description="Read-only v1. GET and HEAD accept bearer tokens or a session cookie. "
                           "All routes require viewer and the API feature. See docs/api-v1.md for paging/privacy rules.")
    document.setdefault("components", {})["securitySchemes"] = {
        "BearerToken": {"type": "http", "scheme": "bearer"},
        "SessionCookie": {"type": "apiKey", "in": "cookie", "name": "access_token"},
    }
    for path, methods in document["paths"].items():
        for operation in methods.values():
            operation["security"] = [{"BearerToken": []}, {"SessionCookie": []}]
            operation["responses"].update({
                "401": {"description": "Missing/invalid/revoked credential. WWW-Authenticate: Bearer; no redirect."},
                "403": {"description": "Insufficient role or a required feature is disabled."},
            })
            if "{" in path:
                operation["responses"]["404"] = {"description": "Resource is unknown, hidden or trashed."}
    return document
