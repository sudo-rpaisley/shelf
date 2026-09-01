# Triggered after the workflow file is present on the feature branch.
from pathlib import Path


def replace(path, old, new):
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"pattern not found in {path}: {old[:80]!r}")
    p.write_text(text.replace(old, new, 1))


# Server-render the current background job snapshot into Settings so returning
# to the page never depends on the first polling request succeeding.
replace(
    "app/routers/pages.py",
    "    from app.services import cover_queue\n",
    "    from app.services import cover_queue, sync_jobs\n",
)
replace(
    "app/routers/pages.py",
    "    for k in SENSITIVE_KEYS:\n        if k in settings:\n            settings[k] = \"\"\n    return request.app.state.templates.TemplateResponse(\n",
    "    for k in SENSITIVE_KEYS:\n        if k in settings:\n            settings[k] = \"\"\n\n    # Seed detached integration-sync progress into the first Settings render.\n    # The browser still polls for fresh values, but a transient failed/429\n    # reconnect can no longer make an active job look idle after navigation.\n    abs_sync_job = sync_jobs.get_status(\"audiobookshelf\")\n    komga_sync_job = sync_jobs.get_status(\"komga\")\n\n    return request.app.state.templates.TemplateResponse(\n",
)
replace(
    "app/routers/pages.py",
    '         "borrower_error_message": borrower_error_message,\n         "missing_covers": missing_covers, "cover_queue_stats": cover_queue_stats},\n',
    '         "borrower_error_message": borrower_error_message,\n         "missing_covers": missing_covers, "cover_queue_stats": cover_queue_stats,\n         "abs_sync_job": abs_sync_job, "komga_sync_job": komga_sync_job},\n',
)

# Add server-seeded state attributes to both integration cards.
replace(
    "app/templates/fragments/settings/integrations.html",
    '         data-abs-saved="{{ \'1\' if secrets_present[\'abs_token\'] else \'\' }}">\n',
    '         data-abs-saved="{{ \'1\' if secrets_present[\'abs_token\'] else \'\' }}"\n'
    '         data-sync-state="{{ abs_sync_job.get(\'state\', \'idle\') }}"\n'
    '         data-sync-current="{{ abs_sync_job.get(\'current\', 0) }}"\n'
    '         data-sync-total="{{ abs_sync_job.get(\'total\', 0) }}"\n'
    '         data-sync-title="{{ abs_sync_job.get(\'title\', \'\') | e }}">\n',
)
replace(
    "app/templates/fragments/settings/komga.html",
    '         data-komga-saved="{{ \'1\' if secrets_present[\'komga_api_key\'] else \'\' }}"\n         data-testid="komga-sync-card">\n',
    '         data-komga-saved="{{ \'1\' if secrets_present[\'komga_api_key\'] else \'\' }}"\n'
    '         data-sync-state="{{ komga_sync_job.get(\'state\', \'idle\') }}"\n'
    '         data-sync-current="{{ komga_sync_job.get(\'current\', 0) }}"\n'
    '         data-sync-total="{{ komga_sync_job.get(\'total\', 0) }}"\n'
    '         data-sync-title="{{ komga_sync_job.get(\'title\', \'\') | e }}"\n'
    '         data-testid="komga-sync-card">\n',
)

# Seed Alpine synchronously from the HTML snapshot and make reconnect polling
# resilient to transient 429/network failures. Five-second polling leaves ample
# headroom under Shelf's 60 requests/minute per-IP API limiter even if both
# integrations are running simultaneously.
replace(
    "static/js/components-settings.js",
    "            if (data.state === 'running') {\n                self._syncJobTimer = setTimeout(function () { pollBackgroundSync(self, url); }, 2000);\n            }\n        })\n        .catch(function () {\n            if (self.syncing) {\n                self._syncJobTimer = setTimeout(function () { pollBackgroundSync(self, url); }, 3000);\n            }\n        });\n",
    "            if (data.state === 'running') {\n                self._syncJobTimer = setTimeout(function () { pollBackgroundSync(self, url); }, 5000);\n            }\n        })\n        .catch(function () {\n            // A fresh Settings component begins with syncing=false. Previously\n            // one failed/429 reconnect therefore stopped polling forever and\n            // hid a still-running job. Always retry a failed status read.\n            self._syncJobTimer = setTimeout(function () { pollBackgroundSync(self, url); }, 5000);\n        });\n",
)
replace(
    "static/js/components-settings.js",
    "            init() {\n                this.absUrl = this.$el.dataset.absUrl || '';\n                this.absUrlPresent = this.$el.dataset.absUrlPresent === '1';\n                this.absSaved = this.$el.dataset.absSaved === '1';\n                pollBackgroundSync(this, '/api/sync/audiobookshelf/job');\n            },\n",
    "            init() {\n                this.absUrl = this.$el.dataset.absUrl || '';\n                this.absUrlPresent = this.$el.dataset.absUrlPresent === '1';\n                this.absSaved = this.$el.dataset.absSaved === '1';\n                applyBackgroundSyncState(this, {\n                    state: this.$el.dataset.syncState || 'idle',\n                    current: Number(this.$el.dataset.syncCurrent || 0),\n                    total: Number(this.$el.dataset.syncTotal || 0),\n                    title: this.$el.dataset.syncTitle || '',\n                    recent: []\n                });\n                pollBackgroundSync(this, '/api/sync/audiobookshelf/job');\n            },\n",
)
replace(
    "static/js/components-settings.js",
    "            init() {\n                this.komgaUrl = this.$el.dataset.komgaUrl || '';\n                this.komgaUrlPresent = this.$el.dataset.komgaUrlPresent === '1';\n                this.komgaSaved = this.$el.dataset.komgaSaved === '1';\n                pollBackgroundSync(this, '/api/sync/komga/job');\n            },\n",
    "            init() {\n                this.komgaUrl = this.$el.dataset.komgaUrl || '';\n                this.komgaUrlPresent = this.$el.dataset.komgaUrlPresent === '1';\n                this.komgaSaved = this.$el.dataset.komgaSaved === '1';\n                applyBackgroundSyncState(this, {\n                    state: this.$el.dataset.syncState || 'idle',\n                    current: Number(this.$el.dataset.syncCurrent || 0),\n                    total: Number(this.$el.dataset.syncTotal || 0),\n                    title: this.$el.dataset.syncTitle || '',\n                    recent: []\n                });\n                pollBackgroundSync(this, '/api/sync/komga/job');\n            },\n",
)

# Regression: the Settings response itself carries an already-running Komga
# job, so a return navigation has progress before the first status poll.
test = Path("tests/test_sync_jobs.py")
text = test.read_text()
addition = '''\n\ndef test_settings_render_seeds_running_komga_progress(admin_client, monkeypatch):\n    real_get_status = sync_jobs.get_status\n\n    def fake_status(provider):\n        if provider == "komga":\n            return {\n                "provider": "komga",\n                "source": "manual",\n                "state": "running",\n                "current": 546,\n                "total": 2576,\n                "title": "The Deluge Drivers",\n                "item_status": "updated",\n                "started_at": 1.0,\n                "updated_at": 2.0,\n                "finished_at": None,\n                "stats": {},\n                "error": None,\n                "recent": [],\n            }\n        return real_get_status(provider)\n\n    monkeypatch.setattr(sync_jobs, "get_status", fake_status)\n    html = admin_client.get("/settings").text\n\n    assert 'data-sync-state="running"' in html\n    assert 'data-sync-current="546"' in html\n    assert 'data-sync-total="2576"' in html\n    assert 'data-sync-title="The Deluge Drivers"' in html\n'''
if "test_settings_render_seeds_running_komga_progress" not in text:
    test.write_text(text + addition)
