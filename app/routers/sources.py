"""Connected-library forms and admin actions. Every action is POST + CSRF.

The listing is viewer-readable and redacted. Configuration/detail and all
mutations are admin-only. Role dependencies deliberately precede the feature.
"""

import json
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import i18n
from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.services import legacy_sources, source_links, sources
from app.services.source_connectors import PROVIDERS
from app.services.source_connectors.base import ConnectorError, safe_code

page_router = APIRouter()
api_router = APIRouter(prefix="/api/sources")

_MESSAGES = {
    "saved": i18n.N_("Source settings saved."),
    "created": i18n.N_("Source added."),
    "attached": i18n.N_("External availability link saved."),
    "detached": i18n.N_("External availability link removed. The catalogue item is unchanged."),
    "disabled": i18n.N_("Source disabled. Existing catalogue entries are unchanged."),
    "tested": i18n.N_("Connection successful. No catalogue entries were changed."),
    "ok": i18n.N_("Sync complete."),
    "partial": i18n.N_("Partial catalogue received. Missing records were not marked unavailable."),
    "invalid_url": i18n.N_("Use a credential-free HTTP or HTTPS URL. Put credentials in the credential fields."),
    "invalid_config": i18n.N_("Check the source name and field lengths."),
    "invalid_options": i18n.N_("Check the options JSON and supported limits."),
    "invalid_credentials": i18n.N_("Check the credential fields."),
    "credentials_unavailable": i18n.N_("Saved credentials could not be decrypted. Enter them again."),
    "unsupported_provider": i18n.N_("Unknown source provider."),
    "auth_failed": i18n.N_("The server refused authentication."),
    "unauthorized": i18n.N_("The server refused authentication."),
    "forbidden": i18n.N_("The server refused access."),
    "rate_limited": i18n.N_("The server rate limit was reached. Try again later."),
    "timeout": i18n.N_("The server timed out. Existing availability was preserved."),
    "busy": i18n.N_("A sync is already running. An interrupted sync can be retried after three minutes."),
    "stale_config": i18n.N_("Settings changed during sync. The stale result was discarded."),
    "manual_only": i18n.N_("This action is not supported by this provider."),
    "record_conflict": i18n.N_("That external identity is already attached to another catalogue entry."),
    "not_found": i18n.N_("The source or live catalogue entry was not found."),
    "limit_reached": i18n.N_("A safety limit was reached."),
}


def _message(code: str) -> str:
    return i18n.gettext(_MESSAGES.get(code, i18n.N_("The source request failed. Existing catalogue entries are unchanged.")))


def _notice(request: Request) -> str | None:
    code = request.query_params.get("notice", "")
    return _message(code) if code and (code in _MESSAGES or safe_code(code) == code) else None


async def _body(request: Request) -> dict:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 16384:
            raise ConnectorError("invalid_config")
        data.extend(chunk)
    try:
        if request.headers.get("content-type", "").split(";", 1)[0] == "application/json":
            body = json.loads(data)
            if not isinstance(body, dict):
                raise ValueError
            return body
        if request.headers.get("content-type", "").split(";", 1)[0] not in {"application/x-www-form-urlencoded", ""}:
            raise ValueError
        return {k: v[-1] for k, v in parse_qs(data.decode("utf-8"), keep_blank_values=True, max_num_fields=40).items()}
    except (ValueError, UnicodeError, RecursionError):
        raise ConnectorError("invalid_config") from None


def _bool(value) -> bool:
    if type(value) is bool:
        return value
    if value in ("1", "true", "on"):
        return True
    if value in ("0", "false", "", None):
        return False
    raise ConnectorError("invalid_config")


def _configuration(body: dict, *, provider: str, creating=False, existing: dict | None = None) -> dict:
    existing = existing or {}
    credentials = body.get("credentials")
    if credentials is None:
        credentials = {key: body.get(key, "") for key in PROVIDERS[provider].credential_fields}
        if not any(credentials.values()):
            credentials = {} if creating else None
    result = {
        "name": body.get("name", existing.get("name", "")),
        "base_url": body.get("base_url", existing.get("base_url", "")),
        "browser_url": body.get("browser_url", existing.get("browser_url", "")), "credentials": credentials,
        "options": body.get("options", body.get("options_json", existing.get("options", {}))),
        "enabled": _bool(body.get("enabled", existing.get("enabled", creating))),
    }
    if not creating:
        result["clear_credentials"] = _bool(body.get("clear_credentials", False))
    return result


def _response(request: Request, result: dict, *, source_id: int | None = None, success="saved"):
    code = success if result.get("ok") else safe_code(result.get("code"))
    if request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({**result, "message": _message(code)}, status_code=200)
    location = f"/sources/{source_id}" if source_id is not None else "/sources"
    return RedirectResponse(f"{location}?notice={code}", status_code=303)


@page_router.get("/sources")
async def overview(request: Request, _=Depends(require_role("viewer")),
                   __=Depends(require_feature("connected_library"))):
    with get_db() as db:
        instances = sources.list_instances(db)
        counts = {row["source_id"]: row["count"] for row in db.execute(
            "SELECT r.source_id, COUNT(*) AS count FROM source_records r "
            "JOIN items_live i ON i.id = r.item_id GROUP BY r.source_id"
        )}
        for instance in instances:
            instance["records_count"] = counts.get(instance["id"], 0)
        legacy = [source for source in legacy_sources.statuses(db)
                  if request.state.user["role"] == "admin" or source["configured"] or source["records_count"]]
    return request.app.state.templates.TemplateResponse(request, "sources.html", {
        "sources": instances, "providers": PROVIDERS,
        "legacy_sources": legacy,
        "notice": _notice(request),
    })


@page_router.get("/sources/{source_id}")
async def detail(request: Request, source_id: int = Path(..., gt=0, le=2**63 - 1), _=Depends(require_role("admin")),
                 __=Depends(require_feature("connected_library"))):
    with get_db() as db:
        source = sources.get_instance(db, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail=i18n.gettext("Source not found"))
        records = sources.list_records(db, source_id)
    return request.app.state.templates.TemplateResponse(request, "source_detail.html", {
        "source": source, "records": records, "providers": PROVIDERS,
        "notice": _notice(request),
    })


@api_router.post("")
async def create(request: Request, _=Depends(require_role("admin")),
                 __=Depends(require_feature("connected_library"))):
    source_id = None
    try:
        body = await _body(request)
        provider = body.get("provider")
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise ConnectorError("unsupported_provider")
        configuration = _configuration(body, provider=provider, creating=True)
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            source_id = sources.create_instance(db, provider=provider, **configuration)
        result = {"ok": True, "id": source_id}
    except ConnectorError as exc:
        result = {"ok": False, "code": exc.code}
    return _response(request, result, source_id=source_id, success="created")


@api_router.post("/{source_id}/settings")
async def settings(request: Request, source_id: int = Path(..., gt=0, le=2**63 - 1), _=Depends(require_role("admin")),
                   __=Depends(require_feature("connected_library"))):
    try:
        body = await _body(request)
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            source = sources.get_instance(db, source_id)
            if source is None:
                raise ConnectorError("not_found")
            sources.save_settings(db, source_id, **_configuration(body, provider=source["provider"], existing=source))
        result = {"ok": True}
    except ConnectorError as exc:
        result = {"ok": False, "code": exc.code}
    return _response(request, result, source_id=source_id)


@api_router.post("/{source_id}/test")
async def test(request: Request, source_id: int = Path(..., gt=0, le=2**63 - 1), _=Depends(require_role("admin")),
               __=Depends(require_feature("connected_library"))):
    result = await sources.test_instance(source_id)
    return _response(request, result, source_id=source_id,
                     success="partial" if result.get("status") == "partial" else "tested")


@api_router.post("/{source_id}/sync")
async def sync(request: Request, source_id: int = Path(..., gt=0, le=2**63 - 1), _=Depends(require_role("admin")),
               __=Depends(require_feature("connected_library"))):
    result = await sources.sync_instance(source_id)
    return _response(request, result, source_id=source_id, success=result.get("status", "ok"))


@api_router.post("/{source_id}/disable")
async def disable(request: Request, source_id: int = Path(..., gt=0, le=2**63 - 1), _=Depends(require_role("admin")),
                  __=Depends(require_feature("connected_library"))):
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            sources.disable_instance(db, source_id)
        result = {"ok": True}
    except ConnectorError as exc:
        result = {"ok": False, "code": exc.code}
    return _response(request, result, source_id=source_id, success="disabled")


@api_router.post("/{source_id}/records")
async def attach(request: Request, source_id: int = Path(..., gt=0, le=2**63 - 1), _=Depends(require_role("admin")),
                 __=Depends(require_feature("connected_library"))):
    try:
        body = await _body(request)
        raw_id = body.get("item_id")
        if type(raw_id) is int:
            item_id = raw_id
        elif isinstance(raw_id, str) and raw_id.isascii() and raw_id.isdigit() and len(raw_id) < 16:
            item_id = int(raw_id)
        else:
            raise ConnectorError("invalid_config")
        if not 0 < item_id <= 2**63 - 1:
            raise ConnectorError("invalid_config")
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            record_id = sources.attach_record(
                db, source_id, item_id=item_id, external_id=body.get("external_id", ""),
                open_url=body.get("open_url", ""), library_id=body.get("library_id") or None,
                format=body.get("format") or None,
            )
        result = {"ok": True, "id": record_id}
    except ConnectorError as exc:
        result = {"ok": False, "code": exc.code}
    return _response(request, result, source_id=source_id, success="attached")


@api_router.post("/{source_id}/records/{record_id}/detach")
async def detach(
    request: Request,
    source_id: int = Path(..., gt=0, le=2**63 - 1),
    record_id: int = Path(..., gt=0, le=2**63 - 1),
    _=Depends(require_role("admin")),
    __=Depends(require_feature("connected_library")),
):
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            source_links.detach(db, source_id, record_id)
        result = {"ok": True}
    except ConnectorError as exc:
        result = {"ok": False, "code": exc.code}
    return _response(request, result, source_id=source_id, success="detached")
