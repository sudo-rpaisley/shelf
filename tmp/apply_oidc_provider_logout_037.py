from pathlib import Path

# Extend the OIDC Settings slice with one explicit provider logout URL.
path = Path("app/routers/oidc_settings.py")
text = path.read_text()
needle = "from app.services import oidc_login\n"
replacement = "from app.services import oidc_login\nfrom app.services.oidc_logout import validate_provider_logout_url, OIDCLogoutError\n"
if needle not in text:
    raise SystemExit("OIDC settings service import anchor missing")
text = text.replace(needle, replacement, 1)

needle = '        client_secret = _text(form, "oidc_client_secret", limit=4096)\n'
replacement = '''        client_secret = _text(form, "oidc_client_secret", limit=4096)
        provider_logout_url = _text(form, "oidc_provider_logout_url", limit=2048)
        if provider_logout_url:
            validate_provider_logout_url(provider_logout_url)
'''
if needle not in text:
    raise SystemExit("OIDC settings client-secret anchor missing")
text = text.replace(needle, replacement, 1)
text = text.replace(
    "    except (OIDCSettingsError, OIDCError):\n",
    "    except (OIDCSettingsError, OIDCError, OIDCLogoutError):\n",
    1,
)
needle = '            "oidc_client_id": client_id,\n'
replacement = '            "oidc_client_id": client_id,\n            "oidc_provider_logout_url": provider_logout_url,\n'
if needle not in text:
    raise SystemExit("OIDC settings values anchor missing")
text = text.replace(needle, replacement, 1)
path.write_text(text)

# Add the optional URL to the OIDC configuration card. We store exactly what
# the administrator entered and append no guessed discovery parameters.
path = Path("app/templates/fragments/settings/oidc.html")
text = path.read_text()
anchor = '''            <div>
                <label for="oidc_scopes" class="block text-sm font-medium text-shelf-muted mb-1">Scopes</label>'''
field = '''            <div>
                <label for="oidc_provider_logout_url" class="block text-sm font-medium text-shelf-muted mb-1">Provider logout URL <span class="font-normal">(optional)</span></label>
                <input id="oidc_provider_logout_url" name="oidc_provider_logout_url" type="url" inputmode="url" maxlength="2048"
                       value="{{ settings.get('oidc_provider_logout_url', '') }}" placeholder="https://auth.example.com/application/o/shelf/end-session/"
                       class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text focus:outline-none focus:border-shelf-accent">
                <p class="text-xs text-shelf-muted mt-1">Used only for users signed in through OIDC. Shelf never guesses this endpoint from provider discovery.</p>
            </div>

'''
if anchor not in text:
    raise SystemExit("OIDC settings scopes anchor missing")
text = text.replace(anchor, field + anchor, 1)
path.write_text(text)

# Route OIDC sessions through the explicit URL after clearing all Shelf auth
# cookies. Local sessions always return to Shelf's own login page.
path = Path("app/routers/auth_routes.py")
text = path.read_text()
needle = "from app.services import oidc_login\n"
replacement = "from app.services import oidc_login\nfrom app.services.oidc_logout import get_provider_logout_url\n"
if needle not in text:
    raise SystemExit("auth route OIDC service import anchor missing")
text = text.replace(needle, replacement, 1)
old = '''@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_auth_cookie(response)
    oidc_login.clear_flow_cookie(response)
    return response
'''
new = '''@router.post("/logout")
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
'''
if old not in text:
    raise SystemExit("auth route logout anchor missing")
path.write_text(text.replace(old, new, 1))

print("OIDC explicit provider logout patch applied")
