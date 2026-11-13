"""Admin-only source replacement and machine binding repair review forms."""

import json
import re
import sqlite3
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app import auth, i18n
from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.http_body import BodyTooLarge, SOURCE_MIGRATION_FORM_BYTES, bounded_body
from app.services import source_migration as migration

page_router = APIRouter()
api_router = APIRouter(prefix="/api/source-migrations")
FORM_BYTES = SOURCE_MIGRATION_FORM_BYTES


def form_limit(path: str) -> int | None:
    """Pure pre-CSRF parsing hook for the coordinator's central middleware."""
    return FORM_BYTES if isinstance(path, str) and re.fullmatch(
        r"/api/source-migrations/(?:replacement/preview|repair/preview|[^/]+/(?:apply|retire|rollback))", path.rstrip("/"),
    ) else None


def _request_actor(request: Request) -> dict:
    # JWT validation may log: do it before the writer lock and before awaiting
    # a form body. Never bless this delayed request with a later re-login.
    actor = dict(request.state.user)
    claims = auth.decode_token(request.cookies.get("access_token", ""))
    if (not claims or str(claims.get("sub")) != str(actor.get("id"))
            or claims.get("tv", 1) != actor.get("token_version")
            or str(claims.get("username", "")).casefold() != str(actor.get("username", "")).casefold()):
        raise migration.MigrationError("actor_changed", "Your session changed. Sign in and review again; nothing was saved.", 401)
    actor["expires_at"] = claims.get("exp")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        migration.authorize_mutation(db, actor)
    return actor


def _page_links(request: Request, context: dict) -> None:
    params = dict(request.query_params)
    for side in ("old", "new", "repair"):
        page = context.get(side + "_page")
        if page is None:
            continue
        first = {key: value for key, value in params.items() if key not in {
            side + "_after", side + "_before", side + "_record_id", side + "_external_id",
        }}
        page["first_url"] = "/source-migrations?" + urlencode(first)
        for direction, cursor, suffix in (("previous", page["previous_before"], "before"), ("next", page["next_after"], "after")):
            values = {key: value for key, value in params.items() if key not in {side + "_after", side + "_before"}}
            if cursor is not None:
                values[side + "_" + suffix] = cursor
            page[direction + "_url"] = "/source-migrations?" + urlencode(values) if cursor is not None else None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


async def _body(request: Request, required: set[str], optional: set[str] = frozenset()) -> dict:
    try:
        data = await bounded_body(request, FORM_BYTES)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            body = json.loads(data, object_pairs_hook=_unique)
        elif content_type == "application/x-www-form-urlencoded":
            fields = parse_qs(data.decode("utf-8"), keep_blank_values=True, max_num_fields=16)
            if any(len(values) != 1 for values in fields.values()):
                raise ValueError
            body = {key: values[0] for key, values in fields.items()}
        else:
            raise ValueError
        if not isinstance(body, dict) or set(body) - required - optional - {"_csrf"} or not required <= body.keys():
            raise ValueError
        return body
    except BodyTooLarge:
        raise migration.MigrationError("too_large", "The review form is too large.", 413) from None
    except (ValueError, UnicodeError, RecursionError):
        raise migration.MigrationError("invalid_form", "Submit each review field exactly once, using only the review form.") from None


def _bool(value) -> bool:
    if type(value) is bool:
        return value
    if value == "1":
        return True
    if value in ("0", "", None):
        return False
    raise migration.MigrationError("invalid_form", "Confirm review options using the checkboxes.")


def _mappings(value) -> list:
    if not isinstance(value, str):
        return value
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not 1 <= len(lines) <= migration.MAX_MAPPINGS:
        raise migration.MigrationError("invalid_mappings", "Enter between 1 and 100 old,new record-ID pairs, one pair per line.")
    result = []
    for line in lines:
        if not re.fullmatch(r"[0-9]{1,19}\s*,\s*[0-9]{1,19}", line):
            raise migration.MigrationError("invalid_mappings", "Use old record ID,new record ID on each line. Do not enter titles or external IDs.")
        old, new = line.split(",")
        result.append({"old_record_id": old.strip(), "new_record_id": new.strip()})
    return result


def _error(request: Request, error: migration.MigrationError):
    message = i18n.gettext(str(error))
    if request.headers.get("content-type", "").startswith("application/json") or (
        request.method == "GET" and request.url.path.startswith("/api/source-migrations/")
    ):
        return JSONResponse({"ok": False, "code": error.code, "message": message}, status_code=error.status_code,
                            headers={"Cache-Control": "no-store"})
    if error.code in {"actor_changed", "feature_disabled"}:
        # Do not render the old request state's administrator navigation after
        # revocation, or consult cached feature state while explaining refusal.
        return Response(message, status_code=error.status_code, media_type="text/plain",
                        headers={"Cache-Control": "no-store"})
    return request.app.state.templates.TemplateResponse(request, "source_migration_error.html", {"error": message},
                                                        status_code=error.status_code, headers={"Cache-Control": "no-store"})


def _mutation_error(request: Request, error: migration.MigrationError, actor: dict | None):
    # Entry refusals already have context-free responses and no captured actor.
    if actor is None and error.code in {"actor_changed", "feature_disabled"}:
        return _error(request, error)
    if actor is not None:
        try:
            # A failed body read or mutation has unwound its transaction. Check
            # the ORIGINAL actor afresh, never a newer login/request state.
            with get_db() as db:
                db.execute("BEGIN IMMEDIATE")
                migration.authorize_mutation(db, actor)
        except migration.MigrationError as denied:
            return _error(request, denied)
        except sqlite3.Error:
            pass  # Unverifiable authority must not render stale admin context.
        else:
            return _error(request, error)
    return Response("Source review is temporarily unavailable. Nothing was saved. Try again.",
                    status_code=503, media_type="text/plain", headers={"Cache-Control": "no-store"})


def _success(request: Request, result: dict):
    if request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"ok": True, **result}, headers={"Cache-Control": "no-store"})
    return RedirectResponse(f"/source-migrations/{result['id']}", status_code=303, headers={"Cache-Control": "no-store"})


@page_router.get("/source-migrations")
async def workspace(
    request: Request,
    old_source_id: int | None = Query(None, gt=0, le=2**63 - 1),
    new_source_id: int | None = Query(None, gt=0, le=2**63 - 1),
    repair_source_id: int | None = Query(None, gt=0, le=2**63 - 1),
    repair_record_id: int | None = Query(None, gt=0, le=2**63 - 1),
    old_after: int = Query(0, ge=0, le=2**63 - 1),
    new_after: int = Query(0, ge=0, le=2**63 - 1),
    repair_after: int = Query(0, ge=0, le=2**63 - 1),
    old_before: int | None = Query(None, gt=0, le=2**63 - 1),
    new_before: int | None = Query(None, gt=0, le=2**63 - 1),
    repair_before: int | None = Query(None, gt=0, le=2**63 - 1),
    old_record_id: int | None = Query(None, gt=0, le=2**63 - 1),
    new_record_id: int | None = Query(None, gt=0, le=2**63 - 1),
    old_external_id: str | None = Query(None, min_length=1, max_length=512),
    new_external_id: str | None = Query(None, min_length=1, max_length=512),
    _=Depends(require_role("admin")),
    _feature=Depends(require_feature("connected_library")),
):
    try:
        with get_db() as db:
            # A read transaction keeps source, record and identifier evidence
            # coherent, without excluding another writer or fetching anything.
            db.execute("BEGIN")
            context = migration.workspace(db, old_source_id, new_source_id, old_after=old_after, new_after=new_after,
                                          old_before=old_before, new_before=new_before, old_record_id=old_record_id,
                                          new_record_id=new_record_id, old_external_id=old_external_id, new_external_id=new_external_id)
            if repair_source_id:
                context["repair_page"] = migration.repair_records(db, repair_source_id, after=repair_after, before=repair_before,
                                                                  record_id=repair_record_id)
            context["repair_records"] = context.get("repair_page", {}).get("records", [])
            context["recent"] = [dict(row) for row in db.execute(
                "SELECT id, state, created_at FROM source_migration_plans WHERE actor_id = ? ORDER BY created_at DESC, id DESC LIMIT 20",
                (request.state.user["id"],),
            )]
        context.update(repair_source_id=repair_source_id, repair_record_id=repair_record_id)
        _page_links(request, context)
        return request.app.state.templates.TemplateResponse(request, "source_migration_inventory.html", context,
                                                            headers={"Cache-Control": "no-store"})
    except migration.MigrationError as error:
        return _error(request, error)


@page_router.get("/source-migrations/{plan_id}")
async def receipt(request: Request, plan_id: str, _=Depends(require_role("admin")),
                  _feature=Depends(require_feature("connected_library"))):
    try:
        with get_db() as db:
            db.execute("BEGIN")
            context = migration.detail(db, plan_id, actor_id=request.state.user["id"])
        return request.app.state.templates.TemplateResponse(request, "source_migration_review.html", context,
                                                            headers={"Cache-Control": "no-store"})
    except migration.MigrationError as error:
        return _error(request, error)


@api_router.get("/inventory")
async def inventory(request: Request, source_id: int = Query(gt=0, le=2**63 - 1),
                    after: int = Query(0, ge=0, le=2**63 - 1), before: int | None = Query(None, gt=0, le=2**63 - 1),
                    record_id: int | None = Query(None, gt=0, le=2**63 - 1),
                    external_id: str | None = Query(None, min_length=1, max_length=512),
                    _=Depends(require_role("admin")), _feature=Depends(require_feature("connected_library"))):
    try:
        with get_db() as db:
            db.execute("BEGIN")
            result = migration.record_page(db, source_id, after=after, before=before, record_id=record_id, external_id=external_id)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except migration.MigrationError as error:
        return _error(request, error)


@api_router.get("/candidates")
async def candidates(request: Request, old_source_id: int = Query(gt=0, le=2**63 - 1),
                     new_source_id: int = Query(gt=0, le=2**63 - 1), new_record_id: int = Query(gt=0, le=2**63 - 1),
                     _=Depends(require_role("admin")), _feature=Depends(require_feature("connected_library"))):
    try:
        with get_db() as db:
            db.execute("BEGIN")
            context = migration.workspace(db, old_source_id, new_source_id, new_record_id=new_record_id)
        return JSONResponse({"suggestions": context["suggestions"], "record": context["new_records"]},
                            headers={"Cache-Control": "no-store"})
    except migration.MigrationError as error:
        return _error(request, error)


@api_router.post("/replacement/preview")
async def preview_replacement(request: Request, _=Depends(require_role("admin")),
                              _feature=Depends(require_feature("connected_library"))):
    actor = None
    try:
        actor = _request_actor(request)
        body = await _body(request, {"old_source_id", "new_source_id", "mappings"}, {"manual_reviewed", "retire_old"})
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            migration.authorize_mutation(db, actor)
            result = migration.preview_replacement(
                db, actor_id=actor["id"], old_source_id=body["old_source_id"], new_source_id=body["new_source_id"],
                mappings=_mappings(body["mappings"]), manual_reviewed=_bool(body.get("manual_reviewed")),
                retire_old=_bool(body.get("retire_old")),
            )
            migration.authorize_mutation(db, actor)
        return _success(request, result)
    except migration.MigrationError as error:
        return _mutation_error(request, error, actor)


@api_router.post("/repair/preview")
async def preview_repair(request: Request, _=Depends(require_role("admin")),
                         _feature=Depends(require_feature("connected_library"))):
    actor = None
    try:
        actor = _request_actor(request)
        body = await _body(request, {"source_id", "record_id", "item_id", "manual_reviewed"})
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            migration.authorize_mutation(db, actor)
            result = migration.preview_repair(
                db, actor_id=actor["id"], source_id=body["source_id"], record_id=body["record_id"],
                item_id=body["item_id"], manual_reviewed=_bool(body["manual_reviewed"]),
            )
            migration.authorize_mutation(db, actor)
        return _success(request, result)
    except migration.MigrationError as error:
        return _mutation_error(request, error, actor)


async def _decision(request: Request, plan_id: str, action):
    actor = None
    try:
        actor = _request_actor(request)
        body = await _body(request, {"fingerprint", "reviewed"})
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            migration.authorize_mutation(db, actor)
            result = action(db, plan_id, actor_id=actor["id"],
                            fingerprint=body["fingerprint"], reviewed=_bool(body["reviewed"]))
            migration.authorize_mutation(db, actor)
        return _success(request, result)
    except migration.MigrationError as error:
        return _mutation_error(request, error, actor)
    except (sqlite3.IntegrityError, sqlite3.DataError):
        return _mutation_error(request, migration.MigrationError("write_failed", "The binding decision could not be saved. No partial changes were saved.", 409), actor)


@api_router.post("/{plan_id}/apply")
async def apply(request: Request, plan_id: str, _=Depends(require_role("admin")),
                _feature=Depends(require_feature("connected_library"))):
    return await _decision(request, plan_id, migration.apply)


@api_router.post("/{plan_id}/retire")
async def retire(request: Request, plan_id: str, _=Depends(require_role("admin")),
                 _feature=Depends(require_feature("connected_library"))):
    return await _decision(request, plan_id, migration.retire)


@api_router.post("/{plan_id}/rollback")
async def rollback(request: Request, plan_id: str, _=Depends(require_role("admin")),
                   _feature=Depends(require_feature("connected_library"))):
    return await _decision(request, plan_id, migration.rollback)
