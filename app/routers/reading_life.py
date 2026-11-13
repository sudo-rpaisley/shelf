"""Native server-rendered private reading life; viewer first, feature second.

All identities come from authentication. Forms never choose another account or
redirect target. Each write rechecks ownership/live visibility under its lock.
Every route belongs to the existing connected_library feature.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse

from app import i18n
from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.http_body import BodyTooLarge, READING_LIFE_FORM_BYTES, bounded_body
from app.routers import items_common
from app.services import reading_life as life


router = APIRouter()
_SESSION_INPUTS = ("activity", "status", "started_on", "ended_on", "version", "submission_key")
_JOURNAL_INPUTS = ("entry_on", "body", "session_id", "version", "submission_key")
_GOAL_INPUTS = ("year", "activity", "count_rule", "target", "version")


def _not_found():
    message = i18n.gettext("Reading life record not found")
    raise HTTPException(404, message, headers={
        "HX-Trigger": items_common._toast_header(message, "error"), "Cache-Control": "no-store",
    })


def _render(request: Request, template: str, context: dict, *, error=None, status=200):
    headers = {"Cache-Control": "no-store"}
    if error:
        # Native forms render errors/unsaved input. HTMX callers receive a visible
        # toast even though HTMX does not swap 4xx responses (G145).
        headers["HX-Trigger"] = items_common._toast_header(error, "error")
    return request.app.state.templates.TemplateResponse(
        request, template, {**life.labels(), **context, "reading_error": error},
        status_code=status, headers=headers,
    )


def _redirect(url: str):
    # Every caller supplies a fixed local route, never next/Referer/user input.
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store", "HX-Redirect": url})


async def _form(request: Request, names, parser, **options):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in ("application/x-www-form-urlencoded", "multipart/form-data"):
        message = i18n.gettext("Submit reading life changes using the form")
        raise HTTPException(415, message, headers={
            "HX-Trigger": items_common._toast_header(message, "error"), "Cache-Control": "no-store",
        })
    try:
        await bounded_body(request, READING_LIFE_FORM_BYTES)
    except BodyTooLarge:
        raise HTTPException(413, i18n.gettext("Reading life form too large")) from None
    async with request.form() as form:
        supplied = life.form_values(form, names)
        try:
            return parser(form, **options), supplied, None
        except ValueError as exc:
            return None, supplied, str(exc)


def _write(operation, *args):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        return operation(db, *args)


def _dashboard(db, user_id, year, session_page=1, journal_page=1):
    return {
        "reading_year": year, "reading_goals": life.goals_for(db, user_id, year),
        "reading_sessions": life.sessions_page(db, user_id, page=session_page),
        "reading_journal": life.journal_page(db, user_id, page=journal_page),
        "goal_form": {"year": year, "activity": "all", "count_rule": "sessions", "target": ""},
    }


def _item_context(db, user_id, item_id, session_page=1, journal_page=1):
    item = life.item_for(db, item_id)
    return {
        "item": item, "session_form": life.new_session_form(item["media_type"]),
        "journal_form": life.new_journal_form(),
        "reading_sessions": life.sessions_page(db, user_id, item_id=item_id, page=session_page),
        "reading_journal": life.journal_page(db, user_id, item_id=item_id, page=journal_page),
    }


def _session_context(db, user_id, session_id, journal_page=1, history_page=1):
    session = life.session_for(db, user_id, session_id)
    return {
        "session": session, "session_form": {**session, "ended_on": session["ended_on"] or ""},
        "item": {"id": session["item_id"], "title": session["title"], "media_type": session["media_type"]},
        "journal_form": life.new_journal_form(session_id),
        "reading_journal": life.journal_page(db, user_id, session_id=session_id, page=journal_page),
        "reading_history": life.history_page(session, history_page),
    }


def _journal_context(db, user_id, entry_id, history_page=1):
    entry = life.journal_for(db, user_id, entry_id)
    return {"entry": entry, "journal_form": entry,
            "item": {"id": entry["item_id"], "title": entry["title"], "media_type": entry["media_type"]},
            "reading_history": life.history_page(entry, history_page)}


@router.get("/reading-life")
async def reading_life_page(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
    year: int | None = Query(None, ge=1, le=9999),
    session_page: int = Query(1, ge=1, le=100000),
    journal_page: int = Query(1, ge=1, le=100000),
):
    with get_db() as db:
        context = _dashboard(db, user["id"], year or date.today().year, session_page, journal_page)
    return _render(request, "reading_life.html", context)


@router.get("/reading-life/items/{item_id}")
async def reading_item_page(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
    item_id: int = Path(gt=0, le=life.MAX_ID),
    session_page: int = Query(1, ge=1, le=100000),
    journal_page: int = Query(1, ge=1, le=100000),
):
    try:
        with get_db() as db:
            context = _item_context(db, user["id"], item_id, session_page, journal_page)
    except life.NotFound:
        _not_found()
    return _render(request, "reading_life_item.html", context)


@router.get("/reading-life/sessions/{session_id}")
async def reading_session_page(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
    session_id: int = Path(gt=0, le=life.MAX_ID),
    journal_page: int = Query(1, ge=1, le=100000),
    history_page: int = Query(1, ge=1, le=100000),
):
    try:
        with get_db() as db:
            context = _session_context(db, user["id"], session_id, journal_page, history_page)
    except life.NotFound:
        _not_found()
    return _render(request, "reading_life_session.html", context)


@router.get("/reading-life/journal/{entry_id}")
async def reading_journal_page(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
    entry_id: int = Path(gt=0, le=life.MAX_ID),
    history_page: int = Query(1, ge=1, le=100000),
):
    try:
        with get_db() as db:
            context = _journal_context(db, user["id"], entry_id, history_page)
    except life.NotFound:
        _not_found()
    return _render(request, "reading_life_journal.html", context)


@router.post("/api/reading-life/items/{item_id}/sessions")
async def create_session(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
    item_id: int = Path(gt=0, le=life.MAX_ID),
):
    values, supplied, error = await _form(request, _SESSION_INPUTS, life.parse_session)
    status = 422
    try:
        if not error:
            try:
                session_id = _write(life.create_session, user["id"], item_id, values)
                return _redirect(f"/reading-life/sessions/{session_id}")
            except life.Conflict as exc:
                error, status = str(exc), 409
        with get_db() as db:
            context = _item_context(db, user["id"], item_id)
    except life.NotFound:
        _not_found()
    context["session_form"].update(supplied)
    return _render(request, "reading_life_item.html", context, error=error, status=status)


@router.post("/api/reading-life/sessions/{session_id}")
async def update_session(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
    session_id: int = Path(gt=0, le=life.MAX_ID),
):
    values, supplied, error = await _form(request, _SESSION_INPUTS, life.parse_session, update=True)
    status = 422
    try:
        if not error:
            try:
                _write(life.update_session, user["id"], session_id, values)
                return _redirect(f"/reading-life/sessions/{session_id}")
            except life.Conflict as exc:
                error, status = str(exc), 409
        with get_db() as db:
            context = _session_context(db, user["id"], session_id)
    except life.NotFound:
        _not_found()
    context["session_form"].update(supplied)
    return _render(request, "reading_life_session.html", context, error=error, status=status)


@router.post("/api/reading-life/items/{item_id}/journal")
async def create_journal(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
    item_id: int = Path(gt=0, le=life.MAX_ID),
):
    values, supplied, error = await _form(request, _JOURNAL_INPUTS, life.parse_journal)
    status = 422
    try:
        if not error:
            try:
                entry_id = _write(life.create_journal, user["id"], item_id, values)
                return _redirect(f"/reading-life/journal/{entry_id}")
            except life.Conflict as exc:
                error, status = str(exc), 409
        with get_db() as db:
            context = _item_context(db, user["id"], item_id)
            # A session-specific form returns to that same session on errors.
            session_id = (values or {}).get("session_id")
            if session_id is None:
                raw_id = supplied.get("session_id", "")
                if (len(raw_id) <= 19 and raw_id.isascii() and raw_id.isdigit()
                        and 0 < int(raw_id) <= life.MAX_ID):
                    session_id = int(raw_id)
            if session_id is not None:
                context = _session_context(db, user["id"], session_id)
                if context["item"]["id"] != item_id:
                    raise life.NotFound
    except life.NotFound:
        _not_found()
    context["journal_form"].update(supplied)
    template = "reading_life_session.html" if "session" in context else "reading_life_item.html"
    return _render(request, template, context, error=error, status=status)


@router.post("/api/reading-life/journal/{entry_id}")
async def update_journal(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
    entry_id: int = Path(gt=0, le=life.MAX_ID),
):
    values, supplied, error = await _form(request, _JOURNAL_INPUTS, life.parse_journal, update=True)
    status = 422
    try:
        if not error:
            try:
                _write(life.update_journal, user["id"], entry_id, values)
                return _redirect(f"/reading-life/journal/{entry_id}")
            except life.Conflict as exc:
                error, status = str(exc), 409
        with get_db() as db:
            context = _journal_context(db, user["id"], entry_id)
    except life.NotFound:
        _not_found()
    context["journal_form"] = {**context["journal_form"], **supplied}
    return _render(request, "reading_life_journal.html", context, error=error, status=status)


@router.post("/api/reading-life/goals")
async def create_goal(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
):
    values, supplied, error = await _form(request, _GOAL_INPUTS, life.parse_goal)
    status = 422
    if not error:
        try:
            _write(life.create_goal, user["id"], values)
            return _redirect(f"/reading-life?year={values['year']}#reading-goals")
        except life.Conflict as exc:
            error, status = str(exc), 409
    year = (values or {}).get("year", date.today().year)
    with get_db() as db:
        context = _dashboard(db, user["id"], year)
    context["goal_form"].update(supplied)
    return _render(request, "reading_life.html", context, error=error, status=status)


@router.post("/api/reading-life/goals/{goal_id}")
async def update_goal(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
    goal_id: int = Path(gt=0, le=life.MAX_ID),
):
    values, supplied, error = await _form(request, _GOAL_INPUTS, life.parse_goal, update=True)
    status = 422
    try:
        if not error:
            try:
                year = _write(life.update_goal, user["id"], goal_id, values)
                return _redirect(f"/reading-life?year={year}#reading-goals")
            except life.Conflict as exc:
                error, status = str(exc), 409
        with get_db() as db:
            goal = life.goal_for(db, user["id"], goal_id)
            context = _dashboard(db, user["id"], goal["year"])
    except life.NotFound:
        _not_found()
    context["goal_attempt"] = {"id": goal_id, **supplied}
    return _render(request, "reading_life.html", context, error=error, status=status)


@router.post("/api/reading-life/goals/{goal_id}/remove")
async def remove_goal(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
    goal_id: int = Path(gt=0, le=life.MAX_ID),
):
    version, supplied, error = await _form(request, ("version",), life.parse_version)
    status = 422
    try:
        if not error:
            try:
                year = _write(life.remove_goal, user["id"], goal_id, version)
                return _redirect(f"/reading-life?year={year}#reading-goals")
            except life.Conflict as exc:
                error, status = str(exc), 409
        with get_db() as db:
            goal = life.goal_for(db, user["id"], goal_id)
            context = _dashboard(db, user["id"], goal["year"])
    except life.NotFound:
        _not_found()
    context["goal_attempt"] = {"id": goal_id, "target": goal["target"], **supplied}
    return _render(request, "reading_life.html", context, error=error, status=status)
