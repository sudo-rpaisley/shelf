from pathlib import Path

# Explicitly register the account-management router.
path = Path("app/main.py")
text = path.read_text()
old = "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, location_order, platforms, settings, oidc_settings, sync, checkouts, valuation, hardcover, store, series, share, tags, intake, archive, shelf_fill, romm, komga, periodicals, music, related_media, personal_state, my_list, continue_home, attention\n"
new = "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, location_order, platforms, settings, oidc_settings, oidc_accounts, sync, checkouts, valuation, hardcover, store, series, share, tags, intake, archive, shelf_fill, romm, komga, periodicals, music, related_media, personal_state, my_list, continue_home, attention\n"
if old not in text:
    raise SystemExit("main OIDC router import anchor missing")
text = text.replace(old, new, 1)
old = "app.include_router(oidc_settings.router)\napp.include_router(sync.router)"
new = "app.include_router(oidc_settings.router)\napp.include_router(oidc_accounts.router)\napp.include_router(sync.router)"
if old not in text:
    raise SystemExit("main OIDC router registration anchor missing")
path.write_text(text.replace(old, new, 1))

# Harden existing user-management endpoints around externally managed users and
# the local recovery account.
path = Path("app/routers/auth_routes.py")
text = path.read_text()
needle = "from app.services import oidc_login\n"
replacement = "from app.services import oidc_login, oidc_accounts\n"
if needle not in text:
    raise SystemExit("auth OIDC service import anchor missing")
text = text.replace(needle, replacement, 1)

old = '''@router.get("/api/users")
async def list_users(request: Request, _=Depends(require_role("admin"))):
    with get_db() as db:
        users = db.execute(
            "SELECT id, username, display_name, role, created_at FROM users ORDER BY created_at"
        ).fetchall()
    return [dict(u) for u in users]
'''
new = '''@router.get("/api/users")
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
'''
if old not in text:
    raise SystemExit("list users anchor missing")
text = text.replace(old, new, 1)

needle = '''    if role not in ("admin", "editor", "viewer"):
        return {"ok": False, "message": "Invalid role"}

    current_user = request.state.user
'''
replacement = '''    if role not in ("admin", "editor", "viewer"):
        return {"ok": False, "message": "Invalid role"}
    if oidc_accounts.is_role_managed(
        user_id, oidc_login.get_login_config().core
    ):
        return {"ok": False, "message": "This user's role is managed by OIDC group mapping"}

    policy = get_local_login_policy()
    if policy.recovery_only and user_id == policy.break_glass_user_id and role != "admin":
        return {"ok": False, "message": "The break-glass recovery account must remain an administrator"}

    current_user = request.state.user
'''
if needle not in text:
    raise SystemExit("update role anchor missing")
text = text.replace(needle, replacement, 1)

needle = '''async def reset_user_password(
    request: Request,
    user_id: int,
    password: str = Form(...),
    _=Depends(require_role("admin")),
):
    if len(password) < 8:
'''
replacement = '''async def reset_user_password(
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
'''
if needle not in text:
    raise SystemExit("reset password anchor missing")
text = text.replace(needle, replacement, 1)

needle = '''    """Any authenticated user can change their own password."""
    user = request.state.user
    if len(new_password) < 8:
'''
replacement = '''    """Any locally authenticated account can change its own password."""
    user = request.state.user
    if _is_oidc_account(user["id"]):
        return {"ok": False, "message": "Your account is managed by OIDC and does not use a Shelf password"}
    if len(new_password) < 8:
'''
if needle not in text:
    raise SystemExit("own password anchor missing")
text = text.replace(needle, replacement, 1)

needle = '''    """Any authenticated user can update their own display name."""
    user = request.state.user
    display_name = display_name.strip()
'''
replacement = '''    """Locally managed users can update their own display name."""
    user = request.state.user
    if _is_oidc_account(user["id"]):
        return {"ok": False, "message": "Your display name is managed by your OIDC identity provider"}
    display_name = display_name.strip()
'''
if needle not in text:
    raise SystemExit("display name anchor missing")
text = text.replace(needle, replacement, 1)

needle = '''    current_user = request.state.user
    if current_user["id"] == user_id:
        return {"ok": False, "message": "Cannot delete your own account"}

    with get_db() as db:
'''
replacement = '''    current_user = request.state.user
    if current_user["id"] == user_id:
        return {"ok": False, "message": "Cannot delete your own account"}

    policy = get_local_login_policy()
    if policy.recovery_only and user_id == policy.break_glass_user_id:
        return {"ok": False, "message": "Cannot delete the configured break-glass recovery account"}

    with get_db() as db:
'''
if needle not in text:
    raise SystemExit("delete user anchor missing")
text = text.replace(needle, replacement, 1)
path.write_text(text)

# Surface provider/recovery state in the existing Users list and disable actions
# that the server will reject. Server checks remain authoritative.
path = Path("app/templates/fragments/settings/users.html")
text = path.read_text()
needle = '''                        <span class="font-medium text-shelf-text" x-text="u.display_name || u.username"></span>
                        <span class="text-shelf-muted text-sm ml-1" x-text="'@' + u.username"></span>
'''
replacement = '''                        <span class="font-medium text-shelf-text" x-text="u.display_name || u.username"></span>
                        <span class="text-shelf-muted text-sm ml-1" x-text="'@' + u.username"></span>
                        <span class="text-xs text-shelf-muted ml-2" x-text="u.auth_provider"></span>
                        <span x-show="u.role_managed" class="text-xs text-shelf-accent2 ml-2">role synced</span>
                        <span x-show="u.break_glass" class="text-xs text-shelf-warning ml-2">recovery</span>
'''
if needle not in text:
    raise SystemExit("users identity badge anchor missing")
text = text.replace(needle, replacement, 1)
text = text.replace(
    '                    <select :value="u.role" @change="updateRole(u.id, $event.target.value)"\n',
    '                    <select :value="u.role" @change="updateRole(u.id, $event.target.value)" :disabled="u.role_managed || u.break_glass"\n',
    1,
)
text = text.replace(
    '                    <button @click="resetPassword(u.id)" class="text-shelf-muted hover:text-shelf-text text-sm transition-colors" title="Reset password">\n',
    '                    <button x-show="u.auth_provider === \'Local\'" @click="resetPassword(u.id)" class="text-shelf-muted hover:text-shelf-text text-sm transition-colors" title="Reset password">\n',
    1,
)
text = text.replace(
    '                    <button @click="deleteUser(u.id, u.display_name || u.username)" class="text-shelf-error/60 hover:text-shelf-error text-sm transition-colors" title="Delete user">\n',
    '                    <button @click="deleteUser(u.id, u.display_name || u.username)" :disabled="u.break_glass" class="text-shelf-error/60 hover:text-shelf-error text-sm transition-colors" title="Delete user">\n',
    1,
)
path.write_text(text)

# Add explicit linking UI and fixed-code status messaging to the OIDC fragment.
path = Path("app/templates/fragments/settings/oidc.html")
text = path.read_text()
anchor = '''    <div class="grid grid-cols-1 xl:grid-cols-2 gap-6">
'''
card = '''    {% set oidc_account_status = request.query_params.get('oidc_account_status') %}
    {% if oidc_account_status == 'linked' %}
    <div class="bg-shelf-success/10 border border-shelf-success/40 text-shelf-success text-sm rounded-lg px-4 py-2" role="status">Existing Shelf account linked to OIDC.</div>
    {% elif oidc_account_status in ('not_configured', 'invalid', 'missing_user', 'recovery_account', 'already_linked', 'subject_used', 'last_local_admin') %}
    <div class="bg-shelf-error/10 border border-shelf-error/30 text-shelf-error text-sm rounded-lg px-4 py-2" role="alert">
        {% if oidc_account_status == 'not_configured' %}Configure the OIDC issuer and Client ID before linking an account.
        {% elif oidc_account_status == 'missing_user' %}The selected Shelf user was not found.
        {% elif oidc_account_status == 'recovery_account' %}The break-glass recovery administrator cannot be linked to OIDC.
        {% elif oidc_account_status == 'already_linked' %}That Shelf account already has an external identity.
        {% elif oidc_account_status == 'subject_used' %}That OIDC subject is already linked to another Shelf account.
        {% elif oidc_account_status == 'last_local_admin' %}Keep at least one separate local administrator before linking this account.
        {% else %}The account link request was invalid.{% endif %}
    </div>
    {% endif %}

    <div class="bg-shelf-card rounded-xl border border-shelf-border p-6">
        <h2 class="text-lg font-semibold">Link an existing Shelf account</h2>
        <p class="text-sm text-shelf-muted mt-1 mb-4">Bind a local account to the provider's stable OIDC <code>sub</code> value. Shelf never infers identity from a matching username or email address.</p>
        <form method="POST" action="/api/settings/oidc/accounts/link-existing" class="grid grid-cols-1 md:grid-cols-3 gap-4 items-end">
            <div>
                <label for="oidc_link_username" class="block text-sm font-medium text-shelf-muted mb-1">Shelf username</label>
                <input id="oidc_link_username" name="shelf_username" type="text" maxlength="128" required autocomplete="off"
                       class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text">
            </div>
            <div>
                <label for="oidc_link_subject" class="block text-sm font-medium text-shelf-muted mb-1">OIDC subject (sub)</label>
                <input id="oidc_link_subject" name="oidc_subject" type="text" maxlength="512" required autocomplete="off"
                       class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text">
            </div>
            <div>
                <label for="oidc_link_email" class="block text-sm font-medium text-shelf-muted mb-1">Provider email <span class="font-normal">(optional)</span></label>
                <input id="oidc_link_email" name="oidc_email" type="email" maxlength="320" autocomplete="off"
                       class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text">
            </div>
            <div class="md:col-span-3">
                <button type="submit" class="px-4 py-2 bg-shelf-hover hover:bg-shelf-border text-shelf-text rounded-lg text-sm border border-shelf-border">Link existing account</button>
            </div>
        </form>
    </div>

'''
if anchor not in text:
    raise SystemExit("OIDC account UI anchor missing")
path.write_text(text.replace(anchor, card + anchor, 1))

print("OIDC account management patch applied")
