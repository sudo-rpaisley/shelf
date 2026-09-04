import logging
import sqlite3
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request, Depends
from fastapi.responses import RedirectResponse, HTMLResponse

from app.auth import (
    hash_password, verify_password, create_token,
    set_auth_cookie, clear_auth_cookie, get_user_count,
    require_role,
)
from app.config import get_client_ip
from app.database import get_db
from app.oidc import (
    OIDCError,
    OIDCAccessDenied,
    authorization_redirect,
    clear_flow_cookie,
    complete_login,
    get_oidc_config,
    is_role_managed,
    managed_user_ids,
    save_oidc_config,
    set_flow_cookie,
)

logger = logging.getLogger(__name__)

# OIDC sessions are intentionally shorter and non-sliding. Provider group
# membership is re-evaluated at least once per day even if Shelf is used
# continuously, so a removed/demoted IdP user cannot retain a stale local role
# indefinitely. The OP's own SSO session normally makes this reauthentication
# transparent to the user.
OIDC_SESSION_TTL_SECONDS = 24 * 3600

# Unknown usernames must do the same one bcrypt verification as known usernames
# without generating a fresh salt/hash on every request. The hash is created
# once at process start; the fixed plaintext is deliberately not a valid
# account credential and only exists to equalise the password-check work factor.
_DUMMY_PASSWORD_HASH = hash_password("dummy")

router = APIRouter()


def _login_context(error: str | None = None) -> dict:
    config = get_oidc_config()
    return {
        "error": error,
        "oidc_enabled": config.enabled and config.configured,
        "oidc_provider_name": config.provider_name,
    }


def _render_login(request: Request, error: str | None = None, status_code: int = 200):
    templates = request.app.state.templates
    response = templates.TemplateResponse(
        request,
        "login.html",
        _login_context(error),
        status_code=status_code,
    )
    # Authorization responses can carry short-lived codes/state in the URL.
    # Never let those values escape through Referer headers or cached HTML.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def _oidc_user_ids() -> set[int]:
    with get_db() as db:
        return {row["user_id"] for row in db.execute("SELECT DISTINCT user_id FROM user_identities").fetchall()}


def _is_oidc_account(user_id: int) -> bool:
    with get_db() as db:
        return bool(db.execute("SELECT 1 FROM user_identities WHERE user_id = ? LIMIT 1", (user_id,)).fetchone())


# --- Public pages ---


@router.get("/login", name="login_page")
async def login_page(request: Request):
    user = getattr(request.state, "user", None)
    if user:
        return RedirectResponse(url="/browse", status_code=303)

    config = get_oidc_config()

    # Reuse the existing public/rate-limited /login endpoint for both the
    # initiation and callback. This avoids adding a broader auth-bypass path.
    if request.query_params.get("oidc") == "1":
        try:
            redirect_url, flow = await authorization_redirect(request, config)
        except OIDCError as exc:
            logger.warning("OIDC login initiation failed: %s", exc)
            return _render_login(request, str(exc), 400)
        response = RedirectResponse(url=redirect_url, status_code=302)
        response.headers["Cache-Control"] = "no-store"
        set_flow_cookie(response, flow)
        return response

    if any(key in request.query_params for key in ("code", "state", "error")):
        try:
            oidc_user = await complete_login(request, config)
        except OIDCAccessDenied as exc:
            logger.info("OIDC access denied from %s: %s", get_client_ip(request), exc)
            response = _render_login(request, str(exc), 403)
            clear_flow_cookie(response)
            return response
        except OIDCError as exc:
            logger.warning("OIDC callback failed from %s: %s", get_client_ip(request), exc)
            response = _render_login(request, str(exc), 401)
            clear_flow_cookie(response)
            return response

        reauth_at = int(time.time()) + OIDC_SESSION_TTL_SECONDS
        token = create_token(
            oidc_user["id"],
            oidc_user["username"],
            oidc_user["role"],
            oidc_user["display_name"],
            oidc_user["token_version"],
            auth_method="oidc",
            reauth_at=reauth_at,
        )
        response = RedirectResponse(url="/browse", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        clear_flow_cookie(response)
        set_auth_cookie(response, token, max_age=OIDC_SESSION_TTL_SECONDS)
        logger.info("OIDC user '%s' logged in from %s", oidc_user["username"], get_client_ip(request))
        return response

    return _render_login(request)


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with get_db() as db:
        user = db.execute(
            "SELECT id, username, password, role, display_name, token_version FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    if not user:
        # One bcrypt verification on both known and unknown username paths.
        verify_password("dummy", _DUMMY_PASSWORD_HASH)
        logger.warning("Failed login attempt for username=%s from %s", username, get_client_ip(request))
        return _render_login(request, "Invalid username or password", 401)
    if not verify_password(password, user["password"]):
        logger.warning("Failed login attempt for username=%s from %s", username, get_client_ip(request))
        return _render_login(request, "Invalid username or password", 401)

    # OIDC-created accounts have an unknowable random local password and Shelf
    # never exposes a reset path for it. A separate local admin account is the
    # break-glass mechanism; this route therefore cannot bypass OIDC policy.
    token = create_token(user["id"], user["username"], user["role"], user["display_name"], user["token_version"])
    response = RedirectResponse(url="/browse", status_code=303)
    set_auth_cookie(response, token)
    logger.info("User '%s' logged in from %s", username, get_client_ip(request))
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_auth_cookie(response)
    clear_flow_cookie(response)
    return response


# --- Setup wizard (only works when no users exist) ---


@router.get("/setup")
async def setup_page(request: Request):
    if get_user_count() > 0:
        return RedirectResponse(url="/login", status_code=303)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@router.post("/setup")
async def setup(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    if get_user_count() > 0:
        return RedirectResponse(url="/login", status_code=303)

    templates = request.app.state.templates

    username = username.strip()
    display_name = display_name.strip() or username

    if len(password) < 8:
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": "Password must be at least 8 characters"},
        )
    if password != password_confirm:
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": "Passwords do not match"},
        )
    if not username or len(username) < 2:
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": "Username must be at least 2 characters"},
        )

    with get_db() as db:
        # The initial get_user_count() is only a fast path. Serialize the
        # authoritative "still no users?" decision with the insert so two
        # concurrent first-run requests cannot both create an administrator.
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return RedirectResponse(url="/login", status_code=303)
        db.execute(
            "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, 'admin')",
            (username, hash_password(password), display_name),
        )
        user = db.execute("SELECT id, username, role, display_name, token_version FROM users WHERE username = ?", (username,)).fetchone()

    token = create_token(user["id"], user["username"], user["role"], user["display_name"], user["token_version"])
    response = RedirectResponse(url="/browse", status_code=303)
    set_auth_cookie(response, token)
    logger.info("Setup completed: admin user '%s' created", username)
    return response


# --- OIDC settings (admin only) ---


@router.post("/api/oidc/settings")
async def update_oidc_settings(
    request: Request,
    oidc_enabled: bool = Form(False),
    oidc_provider_name: str = Form("OpenID Connect"),
    oidc_issuer: str = Form(""),
    oidc_client_id: str = Form(""),
    oidc_client_secret: str = Form(""),
    oidc_clear_client_secret: bool = Form(False),
    oidc_scopes: str = Form("openid profile email"),
    oidc_group_claim: str = Form("groups"),
    oidc_required_group: str = Form(""),
    oidc_admin_groups: str = Form(""),
    oidc_editor_groups: str = Form(""),
    oidc_viewer_groups: str = Form(""),
    oidc_default_role: str = Form("deny"),
    oidc_auto_provision: bool = Form(False),
    oidc_sync_roles: bool = Form(False),
    _=Depends(require_role("admin")),
):
    try:
        save_oidc_config(
            {
                "enabled": oidc_enabled,
                "provider_name": oidc_provider_name,
                "issuer": oidc_issuer,
                "client_id": oidc_client_id,
                "client_secret": oidc_client_secret,
                "clear_client_secret": oidc_clear_client_secret,
                "scopes": oidc_scopes,
                "group_claim": oidc_group_claim,
                "required_group": oidc_required_group,
                "admin_groups": oidc_admin_groups,
                "editor_groups": oidc_editor_groups,
                "viewer_groups": oidc_viewer_groups,
                "default_role": oidc_default_role,
                "auto_provision": oidc_auto_provision,
                "sync_roles": oidc_sync_roles,
            }
        )
    except OIDCError as exc:
        logger.warning("OIDC settings rejected for admin '%s': %s", request.state.user["username"], exc)
        return RedirectResponse(
            url="/settings?" + urlencode({"oidc_error": str(exc)}),
            status_code=303,
        )

    logger.info("OIDC settings updated by admin '%s'", request.state.user["username"])
    return RedirectResponse(url="/settings?oidc_saved=1", status_code=303)


# --- User management (admin only) ---


@router.get("/api/users")
async def list_users(request: Request, _=Depends(require_role("admin"))):
    managed = managed_user_ids()
    oidc_users = _oidc_user_ids()
    with get_db() as db:
        users = db.execute(
            "SELECT id, username, display_name, role, created_at FROM users ORDER BY created_at"
        ).fetchall()
    result = []
    for user in users:
        item = dict(user)
        item["role_managed"] = user["id"] in managed
        item["auth_provider"] = "OIDC" if user["id"] in oidc_users else "Local"
        result.append(item)
    return result


@router.post("/api/users")
async def create_user(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    role: str = Form("viewer"),
    _=Depends(require_role("admin")),
):
    username = username.strip()
    display_name = display_name.strip() or username
    if role not in ("admin", "editor", "viewer"):
        return {"ok": False, "message": "Invalid role"}
    if len(password) < 8:
        return {"ok": False, "message": "Password must be at least 8 characters"}
    if not username or len(username) < 2:
        return {"ok": False, "message": "Username must be at least 2 characters"}

    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, ?)",
                (username, hash_password(password), display_name, role),
            )
    except sqlite3.IntegrityError:
        logger.warning("Failed to create user '%s': username already exists", username)
        return {"ok": False, "message": "Username already exists"}

    logger.info("User '%s' created with role '%s'", username, role)
    return {"ok": True, "message": f"User '{username}' created"}


@router.post("/api/users/{user_id}/role")
async def update_user_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    _=Depends(require_role("admin")),
):
    if role not in ("admin", "editor", "viewer"):
        return {"ok": False, "message": "Invalid role"}
    if is_role_managed(user_id):
        return {"ok": False, "message": "This user's role is managed by OIDC group mapping"}

    current_user = request.state.user
    with get_db() as db:
        target = db.execute("SELECT id, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            return {"ok": False, "message": "User not found"}

        # Prevent demoting the last admin
        if target["role"] == "admin" and role != "admin":
            admin_count = db.execute("SELECT COUNT(*) as cnt FROM users WHERE role = 'admin'").fetchone()["cnt"]
            if admin_count <= 1:
                return {"ok": False, "message": "Cannot demote the last admin"}

        db.execute(
            "UPDATE users SET role = ?, token_version = token_version + 1, updated_at = datetime('now') WHERE id = ?",
            (role, user_id),
        )

    logger.info("User id=%d role changed to '%s' by user '%s'", user_id, role, current_user["username"])
    return {"ok": True, "message": "Role updated — user's sessions have been invalidated"}


@router.post("/api/users/{user_id}/password")
async def reset_user_password(
    request: Request,
    user_id: int,
    password: str = Form(...),
    _=Depends(require_role("admin")),
):
    if _is_oidc_account(user_id):
        return {
            "ok": False,
            "message": "OIDC accounts do not use Shelf passwords; keep a separate local break-glass administrator",
        }
    if len(password) < 8:
        return {"ok": False, "message": "Password must be at least 8 characters"}

    with get_db() as db:
        target = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            return {"ok": False, "message": "User not found"}
        db.execute(
            "UPDATE users SET password = ?, token_version = token_version + 1, updated_at = datetime('now') WHERE id = ?",
            (hash_password(password), user_id),
        )

    logger.info("Password reset for user id=%d by admin", user_id)
    return {"ok": True, "message": "Password updated — user's sessions have been invalidated"}


@router.post("/api/account/password")
async def change_own_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    _=Depends(require_role("viewer")),
):
    """Any locally authenticated account can change its own password."""
    user = request.state.user
    if _is_oidc_account(user["id"]):
        return {"ok": False, "message": "Your account is managed by OIDC and does not use a Shelf password"}
    if len(new_password) < 8:
        return {"ok": False, "message": "New password must be at least 8 characters"}

    with get_db() as db:
        row = db.execute("SELECT password FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not row or not verify_password(current_password, row["password"]):
            logger.warning("Failed password change attempt for user '%s'", user["username"])
            return {"ok": False, "message": "Current password is incorrect"}
        db.execute(
            "UPDATE users SET password = ?, token_version = token_version + 1, updated_at = datetime('now') WHERE id = ?",
            (hash_password(new_password), user["id"]),
        )
        row = db.execute("SELECT token_version FROM users WHERE id = ?", (user["id"],)).fetchone()
        new_tv = row["token_version"]

    logger.info("User '%s' changed their password", user["username"])
    # Issue a new token with updated version so the user stays logged in
    new_token = create_token(user["id"], user["username"], user["role"], user.get("display_name"), new_tv)
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True, "message": "Password changed"})
    set_auth_cookie(resp, new_token)
    return resp


@router.post("/api/account/display-name")
async def change_display_name(
    request: Request,
    display_name: str = Form(...),
    _=Depends(require_role("viewer")),
):
    """Locally managed users can update their own display name."""
    user = request.state.user
    if _is_oidc_account(user["id"]):
        return {"ok": False, "message": "Your display name is managed by your OIDC identity provider"}
    display_name = display_name.strip()
    if not display_name:
        return {"ok": False, "message": "Display name cannot be empty"}

    with get_db() as db:
        db.execute(
            "UPDATE users SET display_name = ?, updated_at = datetime('now') WHERE id = ?",
            (display_name, user["id"]),
        )
        # Re-read token_version so the refreshed JWT matches the DB value
        row = db.execute(
            "SELECT token_version FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        token_version = row["token_version"] if row else 1

    # Refresh the JWT so the nav bar updates immediately
    token = create_token(user["id"], user["username"], user["role"], display_name, token_version)
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True, "message": "Display name updated", "display_name": display_name})
    set_auth_cookie(resp, token)
    return resp


@router.delete("/api/users/{user_id}")
async def delete_user(request: Request, user_id: int, _=Depends(require_role("admin"))):
    current_user = request.state.user
    if current_user["id"] == user_id:
        return {"ok": False, "message": "Cannot delete your own account"}

    with get_db() as db:
        target = db.execute("SELECT id, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            return {"ok": False, "message": "User not found"}

        if target["role"] == "admin":
            admin_count = db.execute("SELECT COUNT(*) as cnt FROM users WHERE role = 'admin'").fetchone()["cnt"]
            if admin_count <= 1:
                return {"ok": False, "message": "Cannot delete the last admin"}

        db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    logger.info("User id=%d deleted by admin '%s'", user_id, current_user["username"])
    return {"ok": True, "message": "User deleted"}
