"""Additive Library pages/APIs; Browse remains the collection administration UI.

Display mode is ``view=groups|items|series``. ``saved_view=<id>`` loads only the
current account's saved filters, then explicitly present GET filters override
them (including empty values). Pages are never saved. All routes are ordinary
cookie-session routes: viewer first, connected_library feature second.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app import config, i18n
from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.services import library_catalog


router = APIRouter()
_NOTICE_COOKIE = "library_view_notice"
_NOTICES = {
    "saved": i18n.N_("Personal view saved."),
    "deleted": i18n.N_("Personal view deleted."),
    "invalid_name": i18n.N_("Choose a view name between 1 and 80 characters."),
    "view_limit": i18n.N_("You can save up to 50 personal views. Delete one or reuse an existing name."),
    "invalid_form": i18n.N_("Submit the saved-view form to save a personal view."),
}


def _read_context(request: Request, user: dict) -> dict:
    query = request.query_params
    view_id = library_catalog.positive_int(query.get("saved_view"), 0, 2**63 - 1)
    with get_db() as db:
        # One read snapshot for matching flags, live graph and availability.
        db.execute("BEGIN")
        saved = library_catalog.load_saved_view(db, user["id"], view_id) if view_id else None
        filters = dict(saved["filters"]) if saved else {}
        filters.update({key: query[key] for key in library_catalog.FILTER_KEYS if key in query})
        context = library_catalog.search_catalog(
            db, filters, page=library_catalog.positive_int(query.get("page")),
        )
        context["saved_view"] = {"id": saved["id"], "name": saved["name"]} if saved else None
        context["saved_views"] = library_catalog.saved_views(db, user["id"])
    return context


def _html_context(request: Request, context: dict) -> dict:
    filters = context["filters"]
    saved_id = context["saved_view"]["id"] if context["saved_view"] else None

    def url(**changes):
        return library_catalog.library_url(filters, saved_view=saved_id, **changes)

    page, pages = context["page"], context["pages"]
    numbers = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    links = []
    previous = 0
    for number in numbers:
        if previous and number > previous + 1:
            links.append({"number": None})
        links.append({"number": number, "url": url(page=number), "current": number == page})
        previous = number
    for series in context["series_choices"]:
        series["url"] = url(series=series["key"], view="series")
    for series in context["series_results"]:
        series["url"] = url(series=series["key"], view="series")
    return {
        **context,
        "csrf_token": request.cookies.get("csrf_token", ""),
        "media_types": config.MEDIA_TYPES,
        "family_choices": [(key, value[0]) for key, value in library_catalog.FAMILIES.items()],
        "availability_choices": library_catalog.AVAILABILITY_CHOICES,
        "sort_choices": library_catalog.SORT_CHOICES,
        "presets": [{"label": label, "url": library_catalog.library_url(values)}
                    for label, values in library_catalog.PRESETS],
        "view_links": [{"value": value, "label": label, "url": url(view=value)}
                       for value, label in (("groups", i18n.N_("Grouped formats")),
                                            ("items", i18n.N_("Individual items")),
                                            ("series", i18n.N_("Local series")))],
        "page_links": links,
        "previous_url": url(page=page - 1) if page > 1 else None,
        "next_url": url(page=page + 1) if page < pages else None,
        "series_index_url": url(series="", view="series"),
        "selected_series": next((row["name"] for row in context["series_choices"]
                                 if row["key"] == filters["series"]), filters["series"]),
        "has_selected_series": any(row["key"] == filters["series"] for row in context["series_choices"]),
        "has_selected_source": any(row["value"] == filters["source"] for row in context["source_choices"]),
        "notice": _NOTICES.get(request.cookies.get(_NOTICE_COOKIE)),
        "saved_view_limit": library_catalog.MAX_SAVED_VIEWS,
    }


@router.get("/library")
async def library_page(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
):
    response = request.app.state.templates.TemplateResponse(
        request, "library.html", _html_context(request, _read_context(request, user)),
        headers={"Cache-Control": "no-store"},
    )
    if request.cookies.get(_NOTICE_COOKIE):
        response.delete_cookie(_NOTICE_COOKIE, path="/library")
    return response


@router.get("/api/library/search")
async def search_library(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
):
    """HTML results fragment; its pagination links deliberately open /library."""
    return request.app.state.templates.TemplateResponse(
        request, "fragments/library_results.html",
        _html_context(request, _read_context(request, user)),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/library/items")
async def library_items(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
):
    """The same local search/group/count/page contract as the HTML page."""
    from fastapi.responses import JSONResponse

    context = _read_context(request, user)
    # Account-scoped view names and selector inventories are not search results.
    for key in ("saved_views", "source_choices", "series_choices"):
        context.pop(key)
    return JSONResponse(context, headers={"Cache-Control": "no-store"})


def _redirect(request: Request, notice: str) -> RedirectResponse:
    # Fixed destination, never Referer, next or any form-supplied URL. A short,
    # allowlisted one-shot notice keeps invalid forms on the normal 303 flow.
    response = RedirectResponse("/library", status_code=303, headers={"Cache-Control": "no-store"})
    response.set_cookie(_NOTICE_COOKIE, notice, max_age=60, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/library")
    return response


@router.post("/api/library/views")
async def save_library_view(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/x-www-form-urlencoded", "multipart/form-data"}:
        return _redirect(request, "invalid_form")
    form = await request.form()
    # Unknown keys (including SQL/user_id) are ignored; files/objects fail the
    # normalizer's scalar checks. Repeated fields use the form's last value.
    values = {key: form[key] for key in library_catalog.FILTER_KEYS if key in form}
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            library_catalog.save_view(db, user["id"], form.get("name"), values)
    except ValueError as exc:
        return _redirect(request, str(exc) if str(exc) in _NOTICES else "invalid_form")
    return _redirect(request, "saved")


@router.post("/api/library/views/{view_id}/delete")
async def delete_library_view(
    request: Request,
    view_id: int,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if 0 < view_id <= 2**63 - 1:
            library_catalog.delete_view(db, user["id"], view_id)
    return _redirect(request, "deleted")
