from pathlib import Path

# Register the dedicated router explicitly. app/routers/__init__.py remains empty.
path = Path("app/main.py")
text = path.read_text()
old = "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, location_order, platforms, settings, sync, checkouts, valuation, hardcover, store, series, share, tags, intake, archive, shelf_fill, romm, komga, periodicals, music, related_media, personal_state, my_list, continue_home, attention\n"
new = "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, location_order, platforms, settings, oidc_settings, sync, checkouts, valuation, hardcover, store, series, share, tags, intake, archive, shelf_fill, romm, komga, periodicals, music, related_media, personal_state, my_list, continue_home, attention\n"
if old not in text:
    raise SystemExit("main router import anchor missing")
text = text.replace(old, new, 1)
old = "app.include_router(settings.router)\napp.include_router(sync.router)"
new = "app.include_router(settings.router)\napp.include_router(oidc_settings.router)\napp.include_router(sync.router)"
if old not in text:
    raise SystemExit("main router registration anchor missing")
text = text.replace(old, new, 1)
path.write_text(text)

# Expose resolved recovery/session policy to the Settings template. We project
# the break-glass username through the policy service rather than exposing the
# stored numeric account id.
path = Path("app/routers/pages.py")
text = path.read_text()
anchor = "    hideable_nav_tab_states = hideable_tab_states()\n"
insert = '''    hideable_nav_tab_states = hideable_tab_states()
    from app.oidc_policy import get_local_login_policy, get_oidc_session_hours
    oidc_login_policy = get_local_login_policy()
    oidc_session_hours = get_oidc_session_hours()
'''
if anchor not in text:
    raise SystemExit("settings view context anchor missing")
text = text.replace(anchor, insert, 1)
old = '''         "borrower_error_message": borrower_error_message,
         "missing_covers": missing_covers, "cover_queue_stats": cover_queue_stats},
    )'''
new = '''         "borrower_error_message": borrower_error_message,
         "missing_covers": missing_covers, "cover_queue_stats": cover_queue_stats,
         "oidc_login_policy": oidc_login_policy,
         "oidc_session_hours": oidc_session_hours},
    )'''
if old not in text:
    raise SystemExit("settings view return context anchor missing")
text = text.replace(old, new, 1)
path.write_text(text)

# Add OIDC configuration before ordinary account management on the Users tab.
path = Path("app/templates/settings.html")
text = path.read_text()
old = '        {% include "fragments/settings/users.html" %}\n'
new = '        {% include "fragments/settings/oidc.html" %}\n        {% include "fragments/settings/users.html" %}\n'
if old not in text:
    raise SystemExit("settings template users include anchor missing")
path.write_text(text.replace(old, new, 1))

print("OIDC settings wiring applied")
