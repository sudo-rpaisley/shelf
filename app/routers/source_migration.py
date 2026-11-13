"""Admin-only source replacement and machine binding repair review forms."""

import json
import re
import sqlite3
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import i18n
from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.http_body import BodyTooLarge, SOURCE_MIGRATION_FORM_BYTES, bounded_body
from app.services import source_migration as migration

page_router = APIRouter()
api_router = APIRouter(prefix="/api/source-migrations")
FORM_BYTES = SOURCE_MIGRATION_FORM_BYTES


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
    if request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"ok": False, "code": error.code, "message": message}, status_code=error.status_code,
                            headers={"Cache-Control": "no-store"})
    return request.app.state.templates.TemplateResponse(request, "source_migration_error.html", {"error": message},
                                                        status_code=error.status_code, headers={"Cache-Control": "no-store"})


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
    _=Depends(require_role("admin")),
    _feature=Depends(require_feature("connected_library")),
):
    try:
        with get_db() as db:
            # A read transaction keeps source, record and identifier evidence
            # coherent, without excluding another writer or fetching anything.
            db.execute("BEGIN")
            context = migration.workspace(db, old_source_id, new_source_id)
            context["repair_records"] = migration.repair_records(db, repair_source_id) if repair_source_id else []
            context["recent"] = [dict(row) for row in db.execute(
                "SELECT id, state, created_at FROM source_migration_plans WHERE actor_id = ? ORDER BY created_at DESC, id DESC LIMIT 20",
                (request.state.user["id"],),
            )]
        context.update(repair_source_id=repair_source_id, repair_record_id=repair_record_id)
        return request.app.state.templates.TemplateResponse(request, "source_migration.html", context,
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


@api_router.post("/replacement/preview")
async def preview_replacement(request: Request, _=Depends(require_role("admin")),
                              _feature=Depends(require_feature("connected_library"))):
    try:
        body = await _body(request, {"old_source_id", "new_source_id", "mappings"}, {"manual_reviewed", "retire_old"})
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = migration.preview_replacement(
                db, actor_id=request.state.user["id"], old_source_id=body["old_source_id"], new_source_id=body["new_source_id"],
                mappings=_mappings(body["mappings"]), manual_reviewed=_bool(body.get("manual_reviewed")),
                retire_old=_bool(body.get("retire_old")),
            )
        return _success(request, result)
    except migration.MigrationError as error:
        return _error(request, error)


@api_router.post("/repair/preview")
async def preview_repair(request: Request, _=Depends(require_role("admin")),
                         _feature=Depends(require_feature("connected_library"))):
    try:
        body = await _body(request, {"source_id", "record_id", "item_id", "manual_reviewed"})
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = migration.preview_repair(
                db, actor_id=request.state.user["id"], source_id=body["source_id"], record_id=body["record_id"],
                item_id=body["item_id"], manual_reviewed=_bool(body["manual_reviewed"]),
            )
        return _success(request, result)
    except migration.MigrationError as error:
        return _error(request, error)


async def _decision(request: Request, plan_id: str, action):
    try:
        body = await _body(request, {"fingerprint", "reviewed"})
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = action(db, plan_id, actor_id=request.state.user["id"],
                            fingerprint=body["fingerprint"], reviewed=_bool(body["reviewed"]))
        return _success(request, result)
    except migration.MigrationError as error:
        return _error(request, error)
    except (sqlite3.IntegrityError, sqlite3.DataError):
        return _error(request, migration.MigrationError("write_failed", "The binding decision could not be saved. No partial changes were saved.", 409))


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
