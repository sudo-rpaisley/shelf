from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise RuntimeError(f"anchor not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


replace_once(
    "app/config.py",
    '    "digital_comic": "Digital Comic",\n    "video_game": "Video Game",',
    '    "digital_comic": "Digital Comic",\n    "video_game": "Video Game",\n    "digital_game": "Digital Game",',
)
replace_once(
    "app/config.py",
    '    "komga_url": "KOMGA_URL",\n    "komga_api_key": "KOMGA_API_KEY",',
    '    "komga_url": "KOMGA_URL",\n    "komga_api_key": "KOMGA_API_KEY",\n    "romm_url": "ROMM_URL",\n    "romm_api_token": "ROMM_API_TOKEN",',
)
replace_once(
    "app/crypto.py",
    '        "komga_api_key",\n        "anthropic_api_key",',
    '        "komga_api_key",\n        "romm_api_token",\n        "anthropic_api_key",',
)
replace_once(
    "app/database.py",
    '    (27, "Index Komga item IDs", "CREATE INDEX IF NOT EXISTS idx_items_komga_id ON items(komga_id)"),\n)',
    '    (27, "Index Komga item IDs", "CREATE INDEX IF NOT EXISTS idx_items_komga_id ON items(komga_id)"),\n'
    '    (28, "Add romm_id column", "ALTER TABLE items ADD COLUMN romm_id TEXT DEFAULT NULL"),\n'
    '    (29, "Add romm_platform_id column", "ALTER TABLE items ADD COLUMN romm_platform_id TEXT DEFAULT NULL"),\n'
    '    (30, "Index RomM item IDs", "CREATE INDEX IF NOT EXISTS idx_items_romm_id ON items(romm_id)"),\n)',
)
replace_once(
    ".env.example",
    "# GOOGLE_BOOKS_API_KEY=\n",
    "# GOOGLE_BOOKS_API_KEY=\n\n"
    "# Optional RomM integration. ROMM_URL is the server-side/API address Shelf can\n"
    "# reach; ROMM_API_TOKEN is a RomM Client API Token with roms.read and\n"
    "# platforms.read scopes. A browser/public URL can also be set in Settings.\n"
    "# ROMM_URL=http://romm:80\n"
    "# ROMM_API_TOKEN=\n",
)

replace_once(
    "app/routers/settings.py",
    '    "komga_url",\n    "komga_api_key",\n    "isbndb_api_key",',
    '    "komga_url",\n    "komga_api_key",\n    "romm_url",\n    "romm_api_token",\n    "isbndb_api_key",',
)
replace_once(
    "app/routers/settings.py",
    '            if key in ("abs_url", "komga_url"):\n',
    '            if key in ("abs_url", "komga_url", "romm_url"):\n',
)

replace_once(
    "app/main.py",
    "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, platforms, settings, sync, komga, checkouts, valuation, hardcover, store, series, share, tags, intake, archive",
    "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, platforms, settings, sync, komga, romm, checkouts, valuation, hardcover, store, series, share, tags, intake, archive",
)
periodic = '''async def _periodic_romm_sync():
    """Background task: run RomM sync on schedule if configured."""
    from app.services import romm as romm_service, sync_jobs
    from app.database import get_setting

    intervals = {"daily": 86400, "weekly": 604800}

    while True:
        await asyncio.sleep(300)
        try:
            with get_db() as db:
                row = db.execute(
                    "SELECT value FROM settings WHERE key = 'romm_sync_interval'"
                ).fetchone()
                interval = row["value"] if row else "off"
                if interval == "off":
                    continue
                last = db.execute(
                    "SELECT value FROM settings WHERE key = 'romm_last_sync'"
                ).fetchone()
                now = time.time()
                if last and last["value"]:
                    if now - float(last["value"]) < intervals.get(interval, 86400):
                        continue
                romm_url_val = get_setting(db, "romm_url")
                romm_token = get_setting(db, "romm_api_token")

            if romm_url_val and romm_token:
                async def runner(on_progress):
                    return await romm_service.sync(
                        romm_url_val, romm_token, on_progress=on_progress
                    )
                started = sync_jobs.start("romm", runner, source="scheduled")
                if not started.get("started"):
                    continue
                final = await sync_jobs.wait("romm")
                if final["state"] != "completed":
                    logger.warning(
                        "Periodic RomM sync did not complete: %s",
                        final.get("error") or final["state"],
                    )
                    continue
                finished = str(time.time())
                with get_db() as db:
                    db.execute(
                        "INSERT INTO settings (key, value) VALUES ('romm_last_sync', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = ?",
                        (finished, finished),
                    )
                logger.info("Periodic RomM sync completed")
        except Exception:
            logger.exception("Periodic RomM sync failed")


'''
replace_once(
    "app/main.py",
    "\n\nasync def _periodic_hardcover_sync():",
    "\n\n" + periodic + "async def _periodic_hardcover_sync():",
)
replace_once(
    "app/main.py",
    "    komga_task = asyncio.create_task(_periodic_komga_sync())\n    hc_task = asyncio.create_task(_periodic_hardcover_sync())",
    "    komga_task = asyncio.create_task(_periodic_komga_sync())\n    romm_task = asyncio.create_task(_periodic_romm_sync())\n    hc_task = asyncio.create_task(_periodic_hardcover_sync())",
)
replace_once(
    "app/main.py",
    "    komga_task.cancel()\n    hc_task.cancel()",
    "    komga_task.cancel()\n    romm_task.cancel()\n    hc_task.cancel()",
)
replace_once(
    "app/main.py",
    "app.include_router(komga.router)\napp.include_router(checkouts.router)",
    "app.include_router(komga.router)\napp.include_router(romm.router)\napp.include_router(checkouts.router)",
)

replace_once(
    "app/routers/pages.py",
    '    komga_url_present = bool(settings.get("komga_url")) or "komga_url" in env_overrides\n',
    '    komga_url_present = bool(settings.get("komga_url")) or "komga_url" in env_overrides\n'
    '    romm_url_present = bool(settings.get("romm_url")) or "romm_url" in env_overrides\n',
)
replace_once(
    "app/routers/pages.py",
    '    abs_sync_job = sync_jobs.get_status("audiobookshelf")\n    komga_sync_job = sync_jobs.get_status("komga")',
    '    abs_sync_job = sync_jobs.get_status("audiobookshelf")\n    komga_sync_job = sync_jobs.get_status("komga")\n    romm_sync_job = sync_jobs.get_status("romm")',
)
replace_once(
    "app/routers/pages.py",
    '         "komga_url_present": komga_url_present,\n',
    '         "komga_url_present": komga_url_present,\n         "romm_url_present": romm_url_present,\n',
)
replace_once(
    "app/routers/pages.py",
    '         "abs_sync_job": abs_sync_job, "komga_sync_job": komga_sync_job},',
    '         "abs_sync_job": abs_sync_job, "komga_sync_job": komga_sync_job,\n         "romm_sync_job": romm_sync_job},',
)
replace_once(
    "app/routers/pages.py",
    '"SELECT i.id, i.title, i.media_type, i.abs_id, i.komga_id FROM item_links il "',
    '"SELECT i.id, i.title, i.media_type, i.abs_id, i.komga_id, i.romm_id FROM item_links il "',
)
romm_detail = '''        # RomM browser URLs use the public root when configured while sync
        # traffic continues to use romm_url.
        romm_url = None
        linked_romm_items = []
        romm_url_val = get_setting(db, "romm_url")
        if romm_url_val:
            from app.services.romm import get_browser_url as get_romm_browser_url
            if item["romm_id"]:
                romm_url = get_romm_browser_url(romm_url_val, item["romm_id"])
            linked_romm_items = [
                {"id": li["id"], "media_type": li["media_type"],
                 "romm_url": get_romm_browser_url(romm_url_val, li["romm_id"])}
                for li in linked_items if li["romm_id"]
            ]

'''
replace_once(
    "app/routers/pages.py",
    "\n        # Hardcover token check\n",
    "\n" + romm_detail + "        # Hardcover token check\n",
)
replace_once(
    "app/routers/pages.py",
    '            "linked_komga_items": linked_komga_items,\n            "komga_url": komga_url,',
    '            "linked_komga_items": linked_komga_items,\n            "komga_url": komga_url,\n            "linked_romm_items": linked_romm_items,\n            "romm_url": romm_url,',
)

replace_once(
    "app/templates/fragments/settings/integrations.html",
    '    {% include "fragments/settings/komga.html" %}\n\n    <!-- Hardcover -->',
    '    {% include "fragments/settings/komga.html" %}\n\n    {% include "fragments/settings/romm.html" %}\n\n    <!-- Hardcover -->',
)
romm_button = '''
                <!-- RomM -->
                {% if romm_url %}
                <div class="mb-4">
                    <a href="{{ romm_url }}" target="_blank" rel="noopener"
                       class="inline-flex items-center gap-1 px-3 py-1.5 bg-shelf-accent/20 text-shelf-accent2 rounded-lg text-sm hover:bg-shelf-accent/30 transition-colors">
                        &#127918; Open in RomM
                    </a>
                </div>
                {% endif %}
'''
replace_once(
    "app/templates/item_detail.html",
    "\n                <!-- Hardcover Sync -->",
    "\n" + romm_button + "\n                <!-- Hardcover Sync -->",
)
romm_link = '''
                <!-- Also in RomM (linked digital copy) -->
                {% if not romm_url and linked_romm_items %}
                <div class="mb-4">
                    {% for lr in linked_romm_items %}
                    <a href="{{ lr.romm_url }}" target="_blank" rel="noopener"
                       class="inline-flex items-center gap-1 px-3 py-1.5 bg-shelf-accent/20 text-shelf-accent2 rounded-lg text-sm hover:bg-shelf-accent/30 transition-colors mr-2">
                        &#127918; Also in RomM ({{ media_types.get(lr.media_type, lr.media_type) }})
                    </a>
                    {% endfor %}
                </div>
                {% endif %}
'''
replace_once(
    "app/templates/item_detail.html",
    "\n                <!-- Linked Items (other formats) -->",
    "\n" + romm_link + "\n                <!-- Linked Items (other formats) -->",
)

js = '''
    // settings.html — RomM sync card
    Alpine.data('rommSync', function () {
        return {
            syncing: false, result: false, status: false, testing: false, showHelp: false,
            rommUrl: '', rommToken: '', rommSaved: false, rommUrlPresent: false,
            syncCurrent: 0, syncTotal: 0, syncLastTitle: '', syncLog: [], showSyncLog: false,
            init() {
                this.rommUrl = this.$el.dataset.rommUrl || '';
                this.rommUrlPresent = this.$el.dataset.rommUrlPresent === '1';
                this.rommSaved = this.$el.dataset.rommSaved === '1';
                applyBackgroundSyncState(this, {
                    state: this.$el.dataset.syncState || 'idle',
                    current: Number(this.$el.dataset.syncCurrent || 0),
                    total: Number(this.$el.dataset.syncTotal || 0),
                    title: this.$el.dataset.syncTitle || '', recent: []
                });
                pollBackgroundSync(this, '/api/sync/romm/job');
            },
            get testReady() { return Boolean((this.rommUrl || this.rommUrlPresent) && (this.rommToken || this.rommSaved)); },
            get syncReady() { return Boolean(this.rommUrlPresent && this.rommSaved); },
            get syncLabel() {
                if (this.syncReady) return 'Sync Now';
                if (this.rommUrl || this.rommToken) return 'Save your settings to sync';
                return 'Enter URL and token to sync';
            },
            get syncPct() { return (this.syncTotal ? Math.round(this.syncCurrent / this.syncTotal * 100) : 0) + '%'; },
            get syncProgress() { return this.syncCurrent + ' / ' + this.syncTotal; },
            get syncWidth() { return 'width:' + (this.syncTotal ? (this.syncCurrent / this.syncTotal * 100) : 0) + '%'; },
            get syncLogLabel() { return this.showSyncLog ? 'Hide details' : 'Show details (' + this.syncLog.length + ' items)'; },
            statusClass(status) {
                if (status === 'added') return 'text-shelf-success';
                if (status === 'updated') return 'text-shelf-accent2';
                return 'text-shelf-muted';
            },
            testRomm() {
                if (!this.testReady) return;
                this.testing = true; this.status = false;
                fetch('/api/sync/romm/test', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.csrfToken() },
                    body: JSON.stringify({ url: this.rommUrl, token: this.rommToken })
                }).then(r => r.json())
                  .then(d => { this.status = d; this.testing = false; })
                  .catch(() => { this.status = { ok: false, message: 'Connection failed' }; this.testing = false; });
            },
            startSync() {
                if (!this.syncReady) return;
                startBackgroundSync(this, '/api/sync/romm/job');
            }
        };
    });

    Alpine.data('rommPlatforms', function () {
        return {
            platforms: false, loading: false, error: false, saving: false,
            cleaning: false, cleanResult: false,
            excludedIds() { return this.platforms.filter(p => !p.included).map(p => p.id); },
            get hasExcluded() { return Boolean(this.platforms && this.excludedIds().length); },
            get cleanResultLabel() {
                if (!this.cleanResult) return '';
                return 'Removed ' + this.cleanResult.deleted + ' synced items; detached ' + this.cleanResult.detached + ' existing Shelf items.';
            },
            platformCountLabel(platform) {
                return '(Digital Game · ' + platform.rom_count + ' ROM' + (platform.rom_count === 1 ? '' : 's') + ')';
            },
            togglePlatform(id) {
                var platform = this.platforms.find(p => p.id === id);
                if (platform) platform.included = !platform.included;
            },
            loadPlatforms() {
                this.loading = true; this.error = false;
                fetch('/api/sync/romm/platforms').then(r => r.json())
                    .then(d => { if (d.ok) this.platforms = d.platforms; else this.error = d.message; this.loading = false; })
                    .catch(() => { this.error = 'Failed to load platforms'; this.loading = false; });
            },
            savePlatforms() {
                this.saving = true;
                fetch('/api/sync/romm/platforms', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': window.csrfToken()}, body: JSON.stringify({excluded: this.excludedIds()})})
                    .then(r => r.json()).then(d => { this.saving = false; if (d.ok) showToast('Platform selection saved'); else showToast(d.message || 'Save failed', 'error'); })
                    .catch(() => { this.saving = false; showToast('Save failed', 'error'); });
            },
            cleanup() {
                if (!confirm('Remove RomM-synced Shelf items from unchecked platforms? RomM itself is not touched.')) return;
                this.cleaning = true; this.cleanResult = false;
                fetch('/api/sync/romm/platforms', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': window.csrfToken()}, body: JSON.stringify({excluded: this.excludedIds()})})
                    .then(() => fetch('/api/sync/romm/platforms/cleanup', {method: 'POST', headers: {'X-CSRF-Token': window.csrfToken()}}))
                    .then(r => r.json()).then(d => { this.cleaning = false; this.cleanResult = d; if (d.ok) showToast('RomM cleanup complete'); })
                    .catch(() => { this.cleaning = false; showToast('Cleanup failed', 'error'); });
            }
        };
    });
'''
replace_once(
    "static/js/components-settings.js",
    "\n    // settings.html — Hardcover card (test / import / export)\n",
    "\n" + js + "\n    // settings.html — Hardcover card (test / import / export)\n",
)

# Digital Game should show the same game-specific platform controls as a
# physical Video Game. Keep these replacements intentionally narrow.
for path in ("app/templates/item_edit.html", "app/templates/fragments/scan_result.html"):
    p = Path(path)
    text = p.read_text()
    text = text.replace("item.media_type == 'video_game'", "item.media_type in ('video_game', 'digital_game')")
    text = text.replace("media_type == 'video_game'", "media_type in ('video_game', 'digital_game')")
    p.write_text(text)

print("RomM patch applied")
