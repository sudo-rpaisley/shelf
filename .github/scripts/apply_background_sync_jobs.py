from pathlib import Path
import re


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:80]!r}")
    text = text.replace(old, new, 1)
    p.write_text(text)


# Audiobookshelf: add detached job start/status endpoints while retaining the
# existing direct and SSE endpoints for API/backwards compatibility.
replace_once(
    "app/routers/sync.py",
    "from app.services import audiobookshelf\n",
    "from app.services import audiobookshelf, sync_jobs\n",
)
replace_once(
    "app/routers/sync.py",
    '@router.post("/audiobookshelf")\nasync def sync_audiobookshelf(request: Request):\n',
    '''@router.get("/audiobookshelf/job")\nasync def audiobookshelf_job_status():\n    """Return the current/most recent detached Audiobookshelf sync job."""\n    return sync_jobs.get_status("audiobookshelf")\n\n\n@router.post("/audiobookshelf/job")\nasync def start_audiobookshelf_job():\n    """Start ABS sync on the server and return without holding the browser open."""\n    with get_db() as db:\n        abs_url_val = get_setting(db, "abs_url")\n        abs_token_val = get_setting(db, "abs_token")\n\n    if not abs_url_val or not abs_token_val:\n        return {"state": "error", "error": "Audiobookshelf URL and API token must be configured in Settings"}\n    url_err = _validate_abs_url(abs_url_val)\n    if url_err:\n        return {"state": "error", "error": url_err}\n\n    async def runner(on_progress):\n        return await audiobookshelf.sync(\n            abs_url_val, abs_token_val, on_progress=on_progress\n        )\n\n    return sync_jobs.start("audiobookshelf", runner, source="manual")\n\n\n@router.post("/audiobookshelf")\nasync def sync_audiobookshelf(request: Request):\n''',
)

# Komga equivalent.
replace_once(
    "app/routers/komga.py",
    "from app.services import komga\n",
    "from app.services import komga, sync_jobs\n",
)
replace_once(
    "app/routers/komga.py",
    '@router.post("")\nasync def sync_now():\n',
    '''@router.get("/job")\nasync def komga_job_status():\n    """Return the current/most recent detached Komga sync job."""\n    return sync_jobs.get_status("komga")\n\n\n@router.post("/job")\nasync def start_komga_job():\n    """Start Komga sync on the server and return without holding the browser open."""\n    with get_db() as db:\n        url = get_setting(db, "komga_url")\n        api_key = get_setting(db, "komga_api_key")\n    if not url or not api_key:\n        return {"state": "error", "error": "Komga URL and API key must be configured in Settings"}\n    url_error = _validate_url(url)\n    if url_error:\n        return {"state": "error", "error": url_error}\n\n    async def runner(on_progress):\n        return await komga.sync(url, api_key, on_progress=on_progress)\n\n    return sync_jobs.start("komga", runner, source="manual")\n\n\n@router.post("")\nasync def sync_now():\n''',
)

# Scheduled ABS/Komga runs use the same registry so Settings can display them
# and manual presses cannot start a duplicate provider sync.
replace_once(
    "app/main.py",
    "    from app.services import audiobookshelf\n",
    "    from app.services import audiobookshelf, sync_jobs\n",
)
replace_once(
    "app/main.py",
    '''            if abs_url_val and abs_token_val:\n                await audiobookshelf.sync(abs_url_val, abs_token_val)\n                with get_db() as db:\n                    db.execute(\n                        "INSERT INTO settings (key, value) VALUES ('abs_last_sync', ?) "\n                        "ON CONFLICT(key) DO UPDATE SET value = ?",\n                        (str(now), str(now)),\n                    )\n                logger.info("Periodic Audiobookshelf sync completed")\n''',
    '''            if abs_url_val and abs_token_val:\n                async def runner(on_progress):\n                    return await audiobookshelf.sync(\n                        abs_url_val, abs_token_val, on_progress=on_progress\n                    )\n\n                started = sync_jobs.start(\n                    "audiobookshelf", runner, source="scheduled"\n                )\n                if not started.get("started"):\n                    continue\n                final = await sync_jobs.wait("audiobookshelf")\n                if final["state"] != "completed":\n                    logger.warning(\n                        "Periodic Audiobookshelf sync did not complete: %s",\n                        final.get("error") or final["state"],\n                    )\n                    continue\n                finished = str(time.time())\n                with get_db() as db:\n                    db.execute(\n                        "INSERT INTO settings (key, value) VALUES ('abs_last_sync', ?) "\n                        "ON CONFLICT(key) DO UPDATE SET value = ?",\n                        (finished, finished),\n                    )\n                logger.info("Periodic Audiobookshelf sync completed")\n''',
)
replace_once(
    "app/main.py",
    "    from app.services import komga as komga_service\n",
    "    from app.services import komga as komga_service, sync_jobs\n",
)
replace_once(
    "app/main.py",
    '''            if komga_url_val and komga_api_key:\n                result = await komga_service.sync(komga_url_val, komga_api_key)\n                if result.get("error"):\n                    logger.warning("Periodic Komga sync failed: %s", result["error"])\n                    continue\n                with get_db() as db:\n                    db.execute(\n                        "INSERT INTO settings (key, value) VALUES ('komga_last_sync', ?) "\n                        "ON CONFLICT(key) DO UPDATE SET value = ?",\n                        (str(now), str(now)),\n                    )\n                logger.info("Periodic Komga sync completed")\n''',
    '''            if komga_url_val and komga_api_key:\n                async def runner(on_progress):\n                    return await komga_service.sync(\n                        komga_url_val, komga_api_key, on_progress=on_progress\n                    )\n\n                started = sync_jobs.start("komga", runner, source="scheduled")\n                if not started.get("started"):\n                    continue\n                final = await sync_jobs.wait("komga")\n                if final["state"] != "completed":\n                    logger.warning(\n                        "Periodic Komga sync did not complete: %s",\n                        final.get("error") or final["state"],\n                    )\n                    continue\n                finished = str(time.time())\n                with get_db() as db:\n                    db.execute(\n                        "INSERT INTO settings (key, value) VALUES ('komga_last_sync', ?) "\n                        "ON CONFLICT(key) DO UPDATE SET value = ?",\n                        (finished, finished),\n                    )\n                logger.info("Periodic Komga sync completed")\n''',
)
replace_once(
    "app/main.py",
    '''    if cover_task is not None:\n        cover_task.cancel()\n''',
    '''    if cover_task is not None:\n        cover_task.cancel()\n    from app.services import sync_jobs\n    await sync_jobs.cancel_all()\n''',
)

# Settings JS: replace browser-owned EventSource jobs with detached server jobs
# and a 2-second status poll. init() reattaches after normal navigation.
js_path = Path("static/js/components-settings.js")
js = js_path.read_text()
helper_anchor = "document.addEventListener('alpine:init', function () {\n"
helpers = r'''// Long integration syncs belong to the Shelf server, not to the browser
// page that happened to start them.  Polling is intentionally modest (2s) so
// a user can navigate away and later return without hitting the API rate limit.
function applyBackgroundSyncState(self, data) {
    if (!data || !data.state) return;
    self.syncing = data.state === 'running';
    self.syncCurrent = Number(data.current || 0);
    self.syncTotal = Number(data.total || 0);
    self.syncLastTitle = data.title || '';
    if (Array.isArray(data.recent)) {
        self.syncLog = data.recent.map(function (entry) {
            var copy = {i: entry.i, t: entry.t, s: entry.s};
            if (typeof self.statusClass === 'function') copy.statusClass = self.statusClass(entry.s);
            return copy;
        });
    }
    if (data.state === 'completed') {
        self.result = data.stats || {};
    } else if (data.state === 'error' || data.state === 'cancelled') {
        self.result = {error: data.error || 'Sync stopped'};
    }
}

function pollBackgroundSync(self, url) {
    if (self._syncJobTimer) clearTimeout(self._syncJobTimer);
    fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
            applyBackgroundSyncState(self, data);
            if (data.state === 'running') {
                self._syncJobTimer = setTimeout(function () { pollBackgroundSync(self, url); }, 2000);
            }
        })
        .catch(function () {
            if (self.syncing) {
                self._syncJobTimer = setTimeout(function () { pollBackgroundSync(self, url); }, 3000);
            }
        });
}

function startBackgroundSync(self, url) {
    if (self._syncJobTimer) clearTimeout(self._syncJobTimer);
    self.syncing = true;
    self.result = false;
    self.syncCurrent = 0;
    self.syncTotal = 0;
    self.syncLastTitle = '';
    self.syncLog = [];
    self.showSyncLog = false;
    fetch(url, {method: 'POST', headers: {'X-CSRF-Token': window.csrfToken()}})
        .then(function (r) { return r.json(); })
        .then(function (data) {
            applyBackgroundSyncState(self, data);
            if (data.state === 'running') {
                self._syncJobTimer = setTimeout(function () { pollBackgroundSync(self, url); }, 1000);
            }
        })
        .catch(function () {
            self.syncing = false;
            self.result = {error: 'Failed to start background sync'};
        });
}

'''
if helper_anchor not in js:
    raise SystemExit("Alpine init anchor not found")
js = js.replace(helper_anchor, helpers + helper_anchor, 1)

js = js.replace(
    """                this.absSaved = this.$el.dataset.absSaved === '1';\n            },\n""",
    """                this.absSaved = this.$el.dataset.absSaved === '1';\n                pollBackgroundSync(this, '/api/sync/audiobookshelf/job');\n            },\n""",
    1,
)
js = js.replace(
    """                this.komgaSaved = this.$el.dataset.komgaSaved === '1';\n            },\n""",
    """                this.komgaSaved = this.$el.dataset.komgaSaved === '1';\n                pollBackgroundSync(this, '/api/sync/komga/job');\n            },\n""",
    1,
)

abs_pattern = re.compile(
    r"            startSync\(\) \{\n                if \(!this\.absSyncReady\) return;.*?\n            \}\n(?=        \};\n    \}\);\n\n    // settings\.html — Audiobookshelf library selection)",
    re.S,
)
js, n = abs_pattern.subn(
    """            startSync() {\n                if (!this.absSyncReady) return;\n                startBackgroundSync(this, '/api/sync/audiobookshelf/job');\n            }\n""",
    js,
    count=1,
)
if n != 1:
    raise SystemExit(f"ABS startSync replacement count {n}")

komga_pattern = re.compile(
    r"            startSync\(\) \{\n                if \(!this\.syncReady\) return;.*?\n            \}\n(?=        \};\n    \}\);\n\n    // settings\.html — Komga library selection)",
    re.S,
)
js, n = komga_pattern.subn(
    """            startSync() {\n                if (!this.syncReady) return;\n                startBackgroundSync(this, '/api/sync/komga/job');\n            }\n""",
    js,
    count=1,
)
if n != 1:
    raise SystemExit(f"Komga startSync replacement count {n}")

js_path.write_text(js)

# Make the behaviour explicit in both cards without introducing new CSS classes.
for template in (
    "app/templates/fragments/settings/komga.html",
    "app/templates/fragments/settings/integrations.html",
):
    p = Path(template)
    text = p.read_text()
    if "Syncing..." in text:
        text = text.replace("Syncing...", "Syncing in background...")
    elif "Syncing&hellip;" in text:
        text = text.replace("Syncing&hellip;", "Syncing in background&hellip;")
    p.write_text(text)

print("background sync job wiring applied")
