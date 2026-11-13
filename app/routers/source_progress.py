"""Own-account handoff/preferences and explicit, CSRF-protected progress pulls.

The coordinator registers this router and the connected_library module census.
No GET performs linking, sync, review, token decryption or remote requests.
"""

import re
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import i18n
from app.auth import decode_token, require_role
from app.database import get_db
from app.features import require_feature
from app.http_body import BodyTooLarge, bounded_body
from app.routers import items_common
from app.services import source_handoff, source_progress
from app.services.source_progress import ProgressError


router = APIRouter()
FORM_BYTES = 16 * 1024


def form_limit(path: str) -> int | None:
    """Pure pre-CSRF limit hook. Match route shapes, including invalid IDs.

    The parent must call this BEFORE request.body()/request.form() in native
    CSRF parsing. Trailing-slash redirects and malformed/oversized IDs need the
    same early bound; unrelated routes must not acquire our form restriction.
    """
    if not isinstance(path, str):
        return None
    path = path.rstrip("/")
    patterns = (
        r"/api/library/items/[^/]+/preferred-source",
        r"/api/source-progress/accounts/link",
        r"/api/source-progress/accounts/[^/]+/(?:disconnect|schedule)",
        r"/api/source-progress/accounts/[^/]+/items/[^/]+/pull",
        r"/api/source-progress/reviews/[^/]+",
    )
    return FORM_BYTES if any(re.fullmatch(pattern, path) for pattern in patterns) else None


def _request_actor(request: Request, user: dict) -> dict:
    # Decode before the write lock (auth.decode_token may log). Keep the entry
    # user's version, not a post-await re-login that would bless revoked work.
    claims = decode_token(request.cookies.get("access_token", ""))
    if not claims or "exp" not in claims or str(claims.get("sub")) != str(user["id"]):
        raise ProgressError("actor_changed")
    return {key: user[key] for key in ("id", "username", "role", "token_version")} | {"expires_at": claims["exp"]}


_MESSAGES = {
    "linked": i18n.N_("Your remote account is linked for explicit pulls only."),
    "disconnected": i18n.N_("Token and sync baselines forgotten. Your local progress and history remain. Revoke the token on the remote server separately."),
    "accepted": i18n.N_("Remote progress accepted. No changes were sent to the remote server."),
    "kept_local": i18n.N_("Local progress kept. This remote observation will not be offered again unless it changes."),
    "unchanged": i18n.N_("No new remote progress. Your local state was not changed."),
    "conflict": i18n.N_("Your local state was kept. Review the remote observation below."),
    "not_found": i18n.N_("The account, source record or live item was not found."),
    "disabled": i18n.N_("This source or feature is disabled."),
    "unsupported": i18n.N_("Progress pull is supported only for Jellyfin movies/audiobooks and Audiobookshelf whole audiobooks."),
    "invalid_form": i18n.N_("Submit each field once using the form."),
    "consent_required": i18n.N_("Explicit consent to pull your own remote progress is required."),
    "invalid_token": i18n.N_("Supply your own remote user access token, not the catalogue API key."),
    "identity_mismatch": i18n.N_("The token did not resolve to the remote user ID you confirmed. No account or progress was changed."),
    "account_in_use": i18n.N_("That remote account is already linked for this source. Each remote account may belong to only one Shelf account per configured source."),
    "credentials_unavailable": i18n.N_("The saved user token could not be decrypted. Link your account again."),
    "relink_required": i18n.N_("The configured server changed or is unavailable. Link your account again before pulling."),
    "stale_review": i18n.N_("Progress, linkage or the preview changed. Pull again before reviewing; nothing was overwritten."),
    "stale_config": i18n.N_("The source, account or item binding changed during the request. The result was discarded."),
    "auth_failed": i18n.N_("The remote server refused this user token. No progress was changed."),
    "unsafe_redirect": i18n.N_("The remote server redirected. Configure its final root; credentials were not forwarded."),
    "ambiguous_binding": i18n.N_("More than one remote record could represent this edition. Repair the binding before pulling progress."),
    "actor_changed": i18n.N_("Your session or account authority changed during the request. Sign in again; no delayed result was applied."),
    "stale_link": i18n.N_("This verification was superseded by a newer link or disconnect. Its token was not saved."),
    "stale_schedule": i18n.N_("The automatic-pull consent or job lease changed. The delayed result was discarded."),
    "unavailable": i18n.N_("The source is not currently eligible. Cached state was kept; refresh the source before pulling progress."),
    "schedule_saved": i18n.N_("Your automatic-pull choice was saved. Automatic pulls use the same local-edit and conflict rules; nothing is sent back."),
}
_REASONS = {
    "unknown_clock": i18n.N_("The source has no trustworthy update timestamp."),
    "non_advancing_clock": i18n.N_("The remote progress changed without a newer timestamp (including reopen/mark-unplayed)."),
    "local_changed": i18n.N_("Your local state changed since the last accepted observation."),
    "local_newer": i18n.N_("Your manual edit or reset is newer than this remote timestamp."),
    "existing_local_state": i18n.N_("This first pull differs from your existing local state or reset history."),
}


def _message(code: str) -> str:
    return i18n.gettext(_MESSAGES.get(code, i18n.N_("The remote progress request failed. Your local progress was preserved.")))


async def _form(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise ProgressError("invalid_form")
    try:
        data = await bounded_body(request, form_limit(request.url.path) or FORM_BYTES)
    except BodyTooLarge:
        raise HTTPException(413, i18n.gettext("Source progress form too large")) from None
    try:
        fields = parse_qs(data.decode("utf-8"), keep_blank_values=True, max_num_fields=24)
        if any(len(values) != 1 for values in fields.values()):
            raise ValueError
        return {key: values[0] for key, values in fields.items()}
    except (ValueError, UnicodeError):
        raise ProgressError("invalid_form") from None


def _response(request: Request, code: str, *, ok=True, item_id=None, **data):
    if "application/json" in request.headers.get("accept", ""):
        status = (200 if ok else 401 if code == "actor_changed" else 404 if code == "not_found"
                  else 409 if code in {"stale_review", "stale_config", "account_in_use", "stale_link", "stale_schedule"} else 422)
        return JSONResponse({"ok": ok, "code": code, "message": _message(code), **data}, status_code=status,
                            headers={"Cache-Control": "no-store"})
    location = "/account/source-progress?"
    if item_id is not None:
        location += f"item_id={item_id}&"
    return RedirectResponse(f"{location}notice={code}", status_code=303, headers={"Cache-Control": "no-store"})


@router.get("/account/source-progress")
async def accounts_page(request: Request, item_id: int | None = Query(default=None, gt=0, le=2**63 - 1),
                        review_before: int | None = Query(default=None, gt=0, le=2**63 - 1),
                        history_before: int | None = Query(default=None, gt=0, le=2**63 - 1),
                        user=Depends(require_role("viewer")), _=Depends(require_feature("connected_library"))):
    with get_db() as db:
        item = db.execute("SELECT id, title FROM items_live WHERE id = ?", (item_id,)).fetchone() if item_id else None
        if item_id is not None and item is None:
            raise HTTPException(404, i18n.gettext("Item not found"))
        progress = source_progress.context(db, user["id"], item_id=item_id,
                                           review_before=review_before, history_before=history_before,
                                           scheduler_active=bool(getattr(request.app.state, "source_progress_scheduler_active", False)))
    code = request.query_params.get("notice", "")
    return request.app.state.templates.TemplateResponse(request, "source_progress.html", {
        "progress": progress, "progress_item": dict(item) if item else None,
        "progress_reasons": _REASONS, "notice": _message(code) if code in _MESSAGES or code in source_progress._ERRORS else None,
    }, headers={"Cache-Control": "no-store"})


@router.get("/api/library/items/{item_id}/handoff")
async def handoff_panel(request: Request, item_id: int = Path(gt=0, le=2**63 - 1),
                        user=Depends(require_role("viewer")), _=Depends(require_feature("connected_library"))):
    with get_db() as db:
        context = source_handoff.context(db, user["id"], item_id)
    if context is None:
        raise HTTPException(404, i18n.gettext("Item not found"))
    return request.app.state.templates.TemplateResponse(request, "fragments/available_here.html", context,
                                                         headers={"Cache-Control": "no-store"})


@router.get("/api/library/items/{item_id}/open/{action}")
async def open_source(item_id: int = Path(gt=0, le=2**63 - 1), action: str = Path(max_length=10),
                      source_key: str | None = Query(default=None, max_length=30),
                      binding: str | None = Query(default=None, max_length=64),
                      user=Depends(require_role("viewer")), _=Depends(require_feature("connected_library"))):
    with get_db() as db:
        selected = source_handoff.resolve(db, user["id"], item_id, action, source_key=source_key, binding=binding)
    if selected is None:
        raise HTTPException(404, i18n.gettext("No eligible source is available. Refresh the item to choose another source."))
    return RedirectResponse(selected["url"], status_code=303,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/api/library/items/{item_id}/preferred-source")
async def prefer_source(request: Request, item_id: int = Path(gt=0, le=2**63 - 1),
                        user=Depends(require_role("viewer")), _=Depends(require_feature("connected_library"))):
    try:
        actor = _request_actor(request, user)
        fields = await _form(request)
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            source_progress._actor(db, user["id"], actor)
            source_handoff.set_preference(db, user["id"], item_id, fields.get("action", ""), fields.get("source_key", ""))
            context = source_handoff.context(db, user["id"], item_id)
    except (ValueError, ProgressError) as exc:
        message = _message(exc.code) if isinstance(exc, ProgressError) else i18n.gettext(str(exc))
        raise HTTPException(422, message, headers={"HX-Trigger": items_common._toast_header(message, "error")}) from None
    if request.headers.get("HX-Request", "").lower() == "true":
        return request.app.state.templates.TemplateResponse(request, "fragments/available_here.html", context,
                                                             headers={"Cache-Control": "no-store"})
    return RedirectResponse(f"/item/{item_id}#available-here", status_code=303)


@router.post("/api/source-progress/accounts/link")
async def link(request: Request, user=Depends(require_role("viewer")), _=Depends(require_feature("connected_library"))):
    try:
        actor = _request_actor(request, user)
        fields = await _form(request)
        account_id = await source_progress.link_account(
            user["id"], fields.get("source_key", ""), token=fields.get("token", ""),
            remote_user_id=fields.get("remote_user_id", ""), consent=fields.get("consent", ""), actor=actor,
        )
    except ProgressError as exc:
        return _response(request, exc.code, ok=False)
    return _response(request, "linked", account_id=account_id)


@router.post("/api/source-progress/accounts/{account_id}/disconnect")
async def disconnect(request: Request, account_id: int = Path(gt=0, le=2**63 - 1),
                     user=Depends(require_role("viewer")), _=Depends(require_feature("connected_library"))):
    try:
        actor = _request_actor(request, user)
        await _form(request)
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            source_progress.disconnect(db, user["id"], account_id, actor=actor)
    except ProgressError as exc:
        return _response(request, exc.code, ok=False)
    return _response(request, "disconnected")


@router.post("/api/source-progress/accounts/{account_id}/items/{item_id}/pull")
async def pull(request: Request, account_id: int = Path(gt=0, le=2**63 - 1), item_id: int = Path(gt=0, le=2**63 - 1),
               user=Depends(require_role("viewer")), _=Depends(require_feature("connected_library"))):
    try:
        actor = _request_actor(request, user)
        await _form(request)
        result = await source_progress.pull_item(user["id"], account_id, item_id, actor=actor)
    except ProgressError as exc:
        return _response(request, exc.code, ok=False, item_id=item_id)
    return _response(request, result["outcome"], item_id=item_id, observation_id=result["observation_id"])


@router.post("/api/source-progress/accounts/{account_id}/schedule")
async def schedule(request: Request, account_id: int = Path(gt=0, le=2**63 - 1),
                   user=Depends(require_role("viewer")), _=Depends(require_feature("connected_library"))):
    try:
        actor = _request_actor(request, user)
        fields = await _form(request)
        value = fields.get("interval_minutes", "")
        if re.fullmatch(r"[0-9]{1,4}", value, re.ASCII) is None:
            raise ProgressError("invalid_form")
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            source_progress.set_schedule(db, user["id"], account_id, interval_minutes=int(value),
                                         consent=fields.get("consent", ""), actor=actor)
    except ProgressError as exc:
        return _response(request, exc.code, ok=False)
    return _response(request, "schedule_saved")


@router.post("/api/source-progress/reviews/{observation_id}")
async def review(request: Request, observation_id: int = Path(gt=0, le=2**63 - 1),
                 user=Depends(require_role("viewer")), _=Depends(require_feature("connected_library"))):
    try:
        actor = _request_actor(request, user)
        fields = await _form(request)
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            item_id = source_progress.review(db, user["id"], observation_id,
                                              token=fields.get("review_token", ""), decision=fields.get("decision", ""), actor=actor)
    except ProgressError as exc:
        return _response(request, exc.code, ok=False)
    return _response(request, fields["decision"], item_id=item_id)
