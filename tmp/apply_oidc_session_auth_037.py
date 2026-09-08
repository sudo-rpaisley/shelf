from pathlib import Path

path = Path("app/auth.py")
text = path.read_text()

# Preserve all current 0.37 signing-key hardening and item-ACL helpers. Replace
# only the four session functions whose behaviour differs for OIDC sessions.
start = text.index("def create_token(")
end = text.index("\ndef decode_token", start)
create_token = '''def create_token(
    user_id: int,
    username: str,
    role: str,
    display_name: str | None = None,
    token_version: int = 1,
    *,
    auth_method: str = "local",
    reauth_at: int | None = None,
) -> str:
    """Create Shelf's session JWT with an optional fixed reauth ceiling."""
    now = datetime.now(timezone.utc)
    normal_exp = int(now.timestamp()) + JWT_EXPIRY_SECONDS
    exp = min(normal_exp, int(reauth_at)) if reauth_at is not None else normal_exp
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "display_name": display_name or username,
        "tv": token_version,
        "authn": auth_method,
        "iat": now,
        "exp": exp,
    }
    if reauth_at is not None:
        payload["reauth"] = int(reauth_at)
    return jwt.encode(payload, get_secret_key(), algorithm=JWT_ALGORITHM)

'''
text = text[:start] + create_token + text[end + 1:]

start = text.index("def set_auth_cookie(")
end = text.index("\ndef clear_auth_cookie", start)
set_cookie = '''def set_auth_cookie(
    response: Response,
    token: str,
    csrf_token: str | None = None,
    *,
    max_age: int = JWT_EXPIRY_SECONDS,
) -> None:
    secure = not os.environ.get("SHELF_DEV_INSECURE_COOKIES")
    if os.environ.get("SHELF_DEV_INSECURE_COOKIES"):
        logger.warning(
            "SHELF_DEV_INSECURE_COOKIES is set — auth cookie is NOT secure. "
            "Never use this in production."
        )
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=max_age,
        path="/",
    )
    if csrf_token is None:
        csrf_token = secrets.token_hex(32)
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        httponly=False,
        secure=secure,
        samesite="strict",
        max_age=max_age,
        path="/",
    )

'''
text = text[:start] + set_cookie + text[end + 1:]

start = text.index("def get_current_user(")
end = text.index("\ndef should_refresh_token", start)
current_user = '''def get_current_user(request: Request) -> dict | None:
    """Read the user from the session JWT and verify local invalidation."""
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None

    token_tv = payload.get("tv", 1)
    user_id = int(payload["sub"])
    with get_db() as db:
        row = db.execute(
            "SELECT token_version FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row or row["token_version"] != token_tv:
            return None

    return {
        "id": user_id,
        "username": payload["username"],
        "role": payload["role"],
        "display_name": payload.get("display_name", payload["username"]),
        "auth_method": payload.get("authn", "local"),
        "reauth_at": payload.get("reauth"),
    }

'''
text = text[:start] + current_user + text[end + 1:]

start = text.index("def should_refresh_token(")
end = text.index("\ndef get_user_count", start)
refresh = '''def should_refresh_token(request: Request) -> str | None:
    """Refresh local sessions at half-life; OIDC sessions never slide."""
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    if payload.get("authn") == "oidc":
        return None
    exp = payload.get("exp", 0)
    iat = payload.get("iat", 0)
    now = time.time()
    half_life = (exp - iat) / 2
    if now > iat + half_life:
        return create_token(
            int(payload["sub"]),
            payload["username"],
            payload["role"],
            payload.get("display_name"),
            payload.get("tv", 1),
            auth_method=payload.get("authn", "local"),
            reauth_at=payload.get("reauth"),
        )
    return None

'''
text = text[:start] + refresh + text[end + 1:]

path.write_text(text)
print("OIDC fixed-session auth patch applied")
