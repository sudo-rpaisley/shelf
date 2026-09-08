from pathlib import Path

# Register the dedicated router explicitly. app/routers/__init__.py remains
# empty by architecture contract.
path = Path("app/main.py")
text = path.read_text()
old = "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, location_order, platforms, settings, sync, checkouts, valuation, hardcover, store, series, share, tags, intake, archive, shelf_fill, romm, komga, periodicals, music, related_media, personal_state, my_list\n"
new = "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, location_order, platforms, settings, sync, checkouts, valuation, hardcover, store, series, share, tags, intake, archive, shelf_fill, romm, komga, periodicals, music, related_media, personal_state, my_list, continue_home\n"
if old not in text:
    raise SystemExit("main router import anchor not found")
text = text.replace(old, new, 1)
old = "app.include_router(personal_state.router)\napp.include_router(my_list.router)"
new = "app.include_router(personal_state.router)\napp.include_router(my_list.router)\napp.include_router(continue_home.router)"
if old not in text:
    raise SystemExit("main router registration anchor not found")
path.write_text(text.replace(old, new, 1))

# Item detail links opened from Continue return to Home through the existing
# whitelist rather than echoing arbitrary `from=` values.
path = Path("app/nav.py")
text = path.read_text()
old = '''BACK_TARGETS = {
    "series": ("/series", "Back to series"),
    "stats": ("/stats", "Back to stats"),
}'''
new = '''BACK_TARGETS = {
    "home": ("/", "Back to Home"),
    "series": ("/series", "Back to series"),
    "stats": ("/stats", "Back to stats"),
}'''
if old not in text:
    raise SystemExit("nav back-target anchor not found")
path.write_text(text.replace(old, new, 1))

# Continue is lazy-loaded so Home's normal dashboard remains fast and the
# empty state adds no vertical chrome. The fragment replaces this shell.
path = Path("app/templates/home.html")
text = path.read_text()
anchor = '''    <section>
        <div class="flex items-center justify-between gap-4 mb-4">
            <div>
                <h2 class="text-lg font-semibold">Recently added</h2>'''
addition = '''    <section id="continue-section"
             class="mb-8"
             hx-get="/api/home/continue"
             hx-trigger="load"
             hx-swap="outerHTML"
             data-testid="continue-loading">
        <div class="bg-shelf-card border border-shelf-border rounded-lg p-5 text-sm text-shelf-muted">
            Loading your Continue list…
        </div>
    </section>

    <section>
        <div class="flex items-center justify-between gap-4 mb-4">
            <div>
                <h2 class="text-lg font-semibold">Recently added</h2>'''
if anchor not in text:
    raise SystemExit("home recently-added anchor not found")
path.write_text(text.replace(anchor, addition, 1))

print("Continue Series 0.37 integration applied")
