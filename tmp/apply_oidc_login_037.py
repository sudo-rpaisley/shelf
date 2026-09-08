from pathlib import Path

# --- auth route ---------------------------------------------------------
path = Path("app/routers/auth_routes.py")
text = path.read_text()

text = text.replace("import sqlite3\n", "import sqlite3\nimport time\n", 1)
needle = "from app.database import get_db\n"
insert = '''from app.database import get_db
from app.oidc import OIDCAccessDenied, OIDCError
from app.oidc_policy import get_local_login_policy, get_oidc_session_ttl_seconds
from app.services import oidc_login
'''
if needle not in text:
    raise SystemExit("auth_routes database import anchor missing")
text = text.replace(needle, insert, 1)

router_anchor = "router = APIRouter()\n\n\n# --- Public pages ---"
helpers = '''router = APIRouter()


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


# --- Public pages ---'''
if router_anchor not in text:
    raise SystemExit("auth_routes router anchor missing")
text = text.replace(router_anchor, helpers, 1)

start = text.index('@router.get("/login")')
end = text.index("\n\n# --- Setup wizard", start)
login_block = '''@router.get("/login", name="login_page")
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
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_auth_cookie(response)
    oidc_login.clear_flow_cookie(response)
    return response
'''
text = text[:start] + login_block + text[end:]
path.write_text(text)

# --- security headers ---------------------------------------------------
path = Path("app/main.py")
text = path.read_text()
old = 'response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"'
new = 'response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")'
if old not in text:
    raise SystemExit("main Referrer-Policy anchor missing")
path.write_text(text.replace(old, new, 1))

# --- sensitive setting + optional secret env override ------------------
path = Path("app/crypto.py")
text = path.read_text()
needle = '        "igdb_client_secret",\n'
if needle not in text:
    raise SystemExit("crypto sensitive-key anchor missing")
text = text.replace(needle, needle + '        "oidc_client_secret",\n', 1)
path.write_text(text)

path = Path("app/config.py")
text = path.read_text()
needle = '    "igdb_client_secret": "IGDB_CLIENT_SECRET",\n'
if needle not in text:
    raise SystemExit("config secret-env anchor missing")
text = text.replace(needle, needle + '    "oidc_client_secret": "OIDC_CLIENT_SECRET",\n', 1)
path.write_text(text)

print("OIDC login/callback patch applied")
