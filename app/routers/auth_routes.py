import logging
import sqlite3
import time

from fastapi import APIRouter, Form, Request, Depends
from fastapi.responses import RedirectResponse, HTMLResponse

from app.auth import (
    hash_password, verify_password, create_token,
    set_auth_cookie, clear_auth_cookie, get_user_count,
    require_role,
)
from app.config import get_client_ip
from app.database import get_db
from app.oidc import OIDCAccessDenied, OIDCError
from app.oidc_policy import get_local_login_policy, get_oidc_session_ttl_seconds
from app.services import oidc_login, oidc_accounts
from app.services.oidc_logout import get_provider_logout_url

logger = logging.getLogger(__name__)

# Unknown usernames must do the same one bcrypt verification as known usernames
# without generating a fresh salt/hash on every request. The hash is created
# once at process start; the fixed plaintext is deliberately not a valid
# account credential and only exists to equalise the password-check work factor.
_DUMMY_PASSWORD_HASH = hash_password("dummy")

router = APIRouter()


def _login_context(request: Request, error: str | None = None) -> dict:
    config = oidc_login.get_login_config()
    policy = get_local_login_policy()
    recovery_requested = request.query_params.get("local") == "1"
    return {
        "error": error,
        "oidc_enabled": config.available,
        "oidc_provider_name": config.provider_name,
        "show_local_login": not policy.recovery_only or recovery_requested,
        "recovery_login_available": policy.recovery_only,
        "recovery_login_requested": policy.recovery_only and recovery_requested,
    }


def _render_login(request: Request, error: str | None = None, status_code: int = 200):
    templates = request.app.state.templates
    response = templates.TemplateResponse(
        request,
        "login.html",
        _login_context(request, error),
        status_code=status_code,
    )
    # Callback URLs contain short-lived code/state values. Never cache the
    # rendered response or allow them to escape in a Referer header.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def _is_oidc_account(user_id: int) -> bool:
    with get_db() as db:
        return bool(
            db.execute(
                "SELECT 1 FROM user_identities WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
        )


# --- Public pages ---


@router.get("/login", name="login_page")
async def login_page(request: Request):
    user = getattr(request.state, "user", None)
    if user:
        return RedirectResponse(url="/", status_code=303)

    config = oidc_login.get_login_config()

    # Initiation and callback deliberately reuse Shelf's existing public,
    # rate-limited /login surface instead of creating another auth bypass.
    if request.query_params.get("oidc") == "1":
        try:
            redirect_url, flow = await oidc_login.begin_login(request, config)
        except OIDCError as exc:
            logger.warning("OIDC login initiation failed: %s", exc)
            return _render_login(request, str(exc), 400)
        response = RedirectResponse(url=redirect_url, status_code=302)
        response.headers["Cache-Control"] = "no-store"
        oidc_login.set_flow_cookie(response, flow)
        return response

    if any(key in request.query_params for key in ("code", "state", "error")):
        try:
            oidc_user = await oidc_login.complete_login(request, config)
        except OIDCAccessDenied as exc:
            logger.info("OIDC access denied from %s: %s", get_client_ip(request), exc)
            response = _render_login(request, str(exc), 403)
            oidc_login.clear_flow_cookie(response)
            return response
        except OIDCError as exc:
            logger.warning("OIDC callback failed from %s: %s", get_client_ip(request), exc)
            response = _render_login(request, str(exc), 401)
            oidc_login.clear_flow_cookie(response)
            return response

        session_ttl = get_oidc_session_ttl_seconds()
        reauth_at = int(time.time()) + session_ttl
        token = create_token(
            oidc_user["id"],
            oidc_user["username"],
            oidc_user["role"],
            oidc_user["display_name"],
            oidc_user["token_version"],
            auth_method="oidc",
            reauth_at=reauth_at,
        )
        response = RedirectResponse(url="/", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        oidc_login.clear_flow_cookie(response)
        set_auth_cookie(response, token, max_age=session_ttl)
        logger.info(
            "OIDC user '%s' logged in from %s",
            oidc_user["username"],
            get_client_ip(request),
        )
        return response

    return _render_login(request)


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    policy = get_local_login_policy()
    with get_db() as db:
        user = db.execute(
            "SELECT id, username, password, role, display_name, token_version "
            "FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    if not user:
        # One bcrypt verification on both known and unknown username paths.
        verify_password("dummy", _DUMMY_PASSWORD_HASH)
        logger.warning(
            "Failed login attempt for username=%s from %s",
            username,
            get_client_ip(request),
        )
        return _render_login(request, "Invalid username or password", 401)

    # Verify the password before account-policy decisions. This keeps
    # recovery-only/OIDC linkage from becoming a useful username oracle.
    password_valid = verify_password(password, user["password"])
    if not password_valid:
        logger.warning(
            "Failed login attempt for username=%s from %s",
            username,
            get_client_ip(request),
        )
        return _render_login(request, "Invalid username or password", 401)

    if policy.recovery_only and user["id"] != policy.break_glass_user_id:
        logger.warning(
            "Blocked local login outside recovery account username=%s from %s",
            username,
            get_client_ip(request),
        )
        return _render_login(request, "Invalid username or password", 401)

    if _is_oidc_account(user["id"]):
        logger.warning(
            "Blocked local login for OIDC account username=%s from %s",
            username,
            get_client_ip(request),
        )
        return _render_login(
            request,
            "This account signs in through the configured identity provider",
            401,
        )

    token = create_token(
        user["id"],
        user["username"],
        user["role"],
        user["display_name"],
        user["token_version"],
    )
    response = RedirectResponse(url="/", status_code=303)
    set_auth_cookie(response, token)
    logger.info("User '%s' logged in from %s", username, get_client_ip(request))
    return response


@router.post("/logout")
async def logout(request: Request):
    user = getattr(request.state, "user", None)
    target = "/login"
    if user and user.get("auth_method") == "oidc":
        target = get_provider_logout_url() or "/login"
    response = RedirectResponse(url=target, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    clear_auth_cookie(response)
    oidc_login.clear_flow_cookie(response)
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
    response = RedirectResponse(url="/", status_code=303)
    set_auth_cookie(response, token)
    logger.info("Setup completed: admin user '%s' created", username)
    return response


# --- User management (admin only) ---


@router.get("/api/users")
async def list_users(request: Request, _=Depends(require_role("admin"))):
    config = oidc_login.get_login_config().core
    managed = oidc_accounts.managed_user_ids() if config.sync_roles else set()
    policy = get_local_login_policy()
    with get_db() as db:
        users = db.execute(
            "SELECT id, username, display_name, role, created_at FROM users ORDER BY created_at"
        ).fetchall()
        oidc_user_ids = {
            int(row["user_id"])
            for row in db.execute(
                "SELECT DISTINCT user_id FROM user_identities"
            ).fetchall()
        }
    result = []
    for row in users:
        item = dict(row)
        user_id = int(row["id"])
        item["role_managed"] = user_id in managed
        item["auth_provider"] = "OIDC" if user_id in oidc_user_ids else "Local"
        item["break_glass"] = policy.recovery_only and user_id == policy.break_glass_user_id
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
    if oidc_accounts.is_role_managed(
        user_id, oidc_login.get_login_config().core
    ):
        return {"ok": False, "message": "This user's role is managed by OIDC group mapping"}

    policy = get_local_login_policy()
    if policy.recovery_only and user_id == policy.break_glass_user_id and role != "admin":
        return {"ok": False, "message": "The break-glass recovery account must remain an administrator"}

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

    policy = get_local_login_policy()
    if policy.recovery_only and user_id == policy.break_glass_user_id:
        return {"ok": False, "message": "Cannot delete the configured break-glass recovery account"}

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