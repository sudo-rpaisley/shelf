function scanPage() {
    return {
        mode: localStorage.getItem('shelf_scan_mode') || 'add',
        // This is a hint for NEW input, not an existing item's type. Keep
        // legacy kids_book rows distinct; only the device hint becomes book.
        // Other deliberate choices (including book) must not become Auto.
        mediaType: (function () {
            var value = localStorage.getItem('shelf_media_type') || 'auto';
            if (value === 'kids_book') {
                value = 'book';
                localStorage.setItem('shelf_media_type', value);
            }
            return value;
        })(),
        platform: localStorage.getItem('shelf_platform') || '',
        location: localStorage.getItem('shelf_location') || '',
        // Inventory/audits select location_nodes, unlike flat add/move.
        auditLocation: localStorage.getItem('shelf_audit_location') || '',
        shelfLocation: localStorage.getItem('shelf_fill_location') || '',
        readingStatus: (function () {
            var v = localStorage.getItem('shelf_reading_status') || '';
            return ['', 'want_to_read', 'reading', 'read'].indexOf(v) === -1 ? '' : v;
        })(),
        // The default-status words for the current media type, and whether
        // that type carries a status at all (refreshStatusLabels).
        statusLabelSets: {},
        statusWant: t('Want to'),
        statusDoing: t('In progress'),
        statusDone: t('Finished'),
        statusTypeOk: true,
        statusDisabled: false,
        borrowerId: '',
        cameraActive: false,
        scanPaused: false,
        scanLoading: false,
        scanResult: false,
        scanner: false,
        lastScanned: '',
        lastScanTime: 0,
        // The open audit session's id ('' when none), mirrored to
        // localStorage so a reload resumes it. The strip is the authority:
        // syncAuditFromStrip() overwrites this from every fragment it renders.
        auditId: (function() {
            try { return localStorage.getItem('shelf_audit_id') || ''; } catch (e) { return ''; }
        })(),
        isZxingFallback: false,
        manualOpen: false,
        recentScansRequest: 0,

        modes: [
            {id: 'add', label: t('Add')},
            {id: 'shelf_fill', label: t('Shelf Fill')},
            {id: 'wishlist', label: t('Wishlist')},
            {id: 'lend', label: t('Lend')},
            {id: 'return', label: t('Return')},
            {id: 'move', label: t('Move')},
            {id: 'inventory', label: t('Inventory')},
            {id: 'lookup', label: t('Lookup')},
            {id: 'quick_rate', label: t('Quick Rate')},
        ],

        // Alpine's CSP build can't call t() in an expression: the scan form's
        // mode- and type-dependent wording comes from these getters.
        get locationLabel() {
            return this.mode === 'move' ? t('Target Location') : this.mode === 'inventory' ? t('Auditing Location') : t('Location');
        },
        get barcodeOnly() {
            return ['lend', 'return', 'move', 'inventory', 'lookup', 'quick_rate'].indexOf(this.mode) !== -1;
        },
        get barcodeLabel() { return this.barcodeOnly ? t('Barcode') : t('ISBN / UPC'); },
        get barcodePlaceholder() {
            if (this.barcodeOnly) return t('Scan barcode...');
            if (this.mediaType === 'auto') return t('Scan any barcode...');
            return this.mediaType === 'video_game' ? t('Scan UPC barcode...') : t('Scan or type ISBN...');
        },
        get titlePlaceholder() {
            return {auto: t('Title or author...'), video_game: t('Game title...'), dvd: t('Movie title...')}[this.mediaType] ||
                t('Book title or author...');
        },
        get resultsHeading() {
            return {add: t('Recently Added'), wishlist: t('Added to Wishlist'), lend: t('Checked Out'), 'return': t('Returned'),
                    move: t('Moved'), inventory: t('Scanned Items'), lookup: t('Lookup Results'),
                    quick_rate: t('Marked as finished')}[this.mode] || t('Results');
        },

        get modeConfig() {
            var configs = {
                add: {heading: t('Add Items'), description: t('Scan barcodes to add items to your collection.')},
                shelf_fill: {heading: t('Shelf Filling'), description: t('Pick a precise shelf or sub-location, then scan items in the order they sit. New items are added; existing items are moved.')},
                wishlist: {heading: t('Add to Wishlist'), description: t('Scan to save items you want but haven\'t bought yet.')},
                lend: {heading: t('Lend Items'), description: t('Select a borrower, then scan items to check them out.')},
                'return': {heading: t('Return Items'), description: t('Scan items to check them back in.')},
                move: {heading: t('Move Items'), description: t('Select a target location, then scan items to move them.')},
                inventory: {heading: t('Inventory Audit'), description: t('Select a location and scan every item you find there. Start an audit to get a report.')},
                lookup: {heading: t('Lookup'), description: t('Scan to check if an item is in your collection. No changes are made.')},
                quick_rate: {heading: t('Quick Rate'), description: t('Scan to mark items as read or completed.')},
            };
            return configs[this.mode] || configs.add;
        },

        loadRecentScans(m) {
            // Unresolved cards and saved IDs outlive mode switches. History is
            // a snapshot, not permission to discard a paused Shelf Fill scan.
            if (this._shelfFlow && this._shelfFlow.hasContinuations()) return;
            var self = this;
            var request = ++this.recentScansRequest;
            var results = document.getElementById('scan-results');
            var firstResult = results.firstChild;
            fetch('/api/recent-scans?mode=' + encodeURIComponent(m))
                .then(function(r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.text();
                })
                .then(function(html) {
                    // A newer history load or an inserted scan card makes this snapshot stale.
                    if (request === self.recentScansRequest && results.firstChild === firstResult &&
                        !(self._shelfFlow && self._shelfFlow.hasContinuations())) {
                        results.innerHTML = html;
                        htmx.process(results);
                    }
                })
                .catch(function() {
                    showToast(t('Could not load recent scans'), 'error');
                });
        },

        toggleManual() {
            this.manualOpen = !this.manualOpen;
        },

        setMode(m) {
            this.mode = m;
            if (this._shelfFlow) this._shelfFlow.changed();
            localStorage.setItem('shelf_scan_mode', m);
            this.loadRecentScans(m);
            if (m === 'inventory') this.refreshAudit();
            var si = document.getElementById('title-search-input');
            if (si) si.value = '';
            var sr = document.getElementById('title-search-results');
            if (sr) sr.innerHTML = '';
            this.updateShelfFillVisibility();
        },

        // @change handlers (CSP build: no localStorage/document in templates)
        persistMediaType() {
            localStorage.setItem('shelf_media_type', this.mediaType);
            var si = document.getElementById('title-search-input');
            if (si) si.value = '';
            var sr = document.getElementById('title-search-results');
            if (sr) sr.innerHTML = '';
        },

        persistLocation() {
            localStorage.setItem('shelf_location', this.location);
        },
        persistAuditLocation() {
            localStorage.setItem('shelf_audit_location', this.auditLocation);
        },

        persistShelfLocation() {
            var select = document.getElementById('shelf-fill-location');
            this.shelfLocation = select ? select.value : '';
            localStorage.setItem('shelf_fill_location', this.shelfLocation);
            if (this._shelfFlow) this._shelfFlow.changed();
        },

        persistPlatform() {
            localStorage.setItem('shelf_platform', this.platform);
        },

        updateShelfFillVisibility() {
            var wrap = document.getElementById('shelf-fill-location-wrap');
            if (wrap) wrap.style.display = this.mode === 'shelf_fill' ? '' : 'none';
        },

        installShelfFillPicker() {
            var self = this;
            var form = document.querySelector('form[hx-post="/api/scan"]');
            if (!form || document.getElementById('shelf-fill-picker-host')) return;

            var host = document.createElement('div');
            host.id = 'shelf-fill-picker-host';
            var card = form.querySelector('.bg-shelf-card');
            if (card) form.insertBefore(host, card);
            else form.appendChild(host);

            fetch('/api/shelf-fill/location-picker')
                .then(function(r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.text();
                })
                .then(function(html) {
                    host.innerHTML = html;
                    var select = document.getElementById('shelf-fill-location');
                    if (select) {
                        select.value = self.shelfLocation;
                        if (select.value !== self.shelfLocation) {
                            self.shelfLocation = '';
                            localStorage.removeItem('shelf_fill_location');
                        }
                        select.addEventListener('change', function() {
                            self.persistShelfLocation();
                        });
                    }
                    self.updateShelfFillVisibility();
                    if (self._shelfFlow) self._shelfFlow.changed();
                })
                .catch(function() {
                    host.innerHTML = '';
                    if (self.mode === 'shelf_fill') {
                        showToast('Could not load shelf locations', 'error');
                    }
                });
        },

        persistReadingStatus() {
            localStorage.setItem('shelf_reading_status', this.readingStatus);
        },

        // Auto gets the neutral words; a type outside the map has no status,
        // so the control is disabled and keeps the neutral words.
        refreshStatusLabels() {
            var sets = this.statusLabelSets;
            var words = sets[this.mediaType] || sets.auto ||
                [t('Want to'), t('In progress'), t('Finished')];
            this.statusTypeOk = this.mediaType === 'auto' || !!sets[this.mediaType];
            if (!this.statusTypeOk) words = sets.auto || [t('Want to'), t('In progress'), t('Finished')];
            this.statusWant = words[0];
            this.statusDoing = words[1];
            this.statusDone = words[2];
            this.statusDisabled = (this.mode !== 'add' && this.mode !== 'wishlist') || !this.statusTypeOk;
        },

        // Client-side validation before form submit
        init() {
            var self = this;

            // Lending off (or any feature whose scan_modes the registry
            // disabled) removes its modes from the row. Read through the
            // root's data attribute, tolerating its absence or an empty
            // value (G91: no throw inside init()). If the mode restored
            // from localStorage above is no longer offered, fall back to
            // 'add' the same way the manual-panel normalization below
            // does — this also tidies any other unknown stored value,
            // since an id that matches nothing in `this.modes` is caught
            // by the same check regardless of why it is unknown.
            var offEl = document.querySelector('[data-off-modes]');
            var offModes = (offEl && offEl.dataset.offModes) ? offEl.dataset.offModes.split(' ') : [];
            this.modes = this.modes.filter(function(m) { return offModes.indexOf(m.id) === -1; });
            // An audit report's Add link seeds Add mode and a code on the
            // root. A seeded mode wins over the stored one; the code is
            // put in the input and never submitted. Absent or empty is fine.
            var seedEl = document.querySelector('[data-seed-mode]');
            if (seedEl && seedEl.dataset.seedMode === 'add') {
                this.mode = 'add';
                try { localStorage.setItem('shelf_scan_mode', 'add'); } catch (err) { /* private mode */ }
            }
            var shelfSeed = this.$el.getAttribute('data-shelf-fill-initial');
            if (shelfSeed) {
                this.shelfLocation = shelfSeed;
                this.mode = 'shelf_fill';
                localStorage.setItem('shelf_fill_location', shelfSeed);
                localStorage.setItem('shelf_scan_mode', this.mode);
            }
            var codeEl = document.querySelector('[data-seed-code]');
            var seedCode = codeEl ? (codeEl.dataset.seedCode || '') : '';
            if (!this.modes.some(function(m) { return m.id === self.mode; })) {
                this.mode = 'add';
                localStorage.setItem('shelf_scan_mode', 'add');
            }

            // The manual panel's open state is seeded server-side from
            // ?add=manual, so there is no URL parsing here and no
            // open-then-populate flash.
            var seed = document.querySelector('[data-manual-open]');
            this.manualOpen = !!(seed && seed.dataset.manualOpen === 'true');

            // Default-status words per media type, from the root (G91: absence
            // or a bad value never throws). A watch, not a hook in
            // persistMediaType(): auto-detect and restore change it too.
            var labelsEl = document.querySelector('[data-status-labels]');
            try {
                this.statusLabelSets = JSON.parse((labelsEl && labelsEl.dataset.statusLabels) || '{}') || {};
            } catch (err) {
                this.statusLabelSets = {};
            }

            // ...but `mode` is restored from localStorage above, and six of
            // the eight modes hide the panel AND its only toggle. Without
            // this, every returning user whose last mode was Lookup follows
            // Home's "Add by hand" link and sees nothing change. Normalize
            // before Alpine paints. A seeded 'wishlist' is preserved:
            // arriving by deep link must not move someone off wishlist mode.
            if (this.manualOpen && this.mode !== 'add' && this.mode !== 'wishlist') {
                this.mode = 'add';
                localStorage.setItem('shelf_scan_mode', 'add');
            }
            if (!shelfSeed) this.loadRecentScans(this.mode);

            if (seedCode) {
                var isbnInput = document.getElementById('isbn-input');
                if (isbnInput) {
                    isbnInput.value = seedCode;
                    isbnInput.focus();
                }
            }

            // Both inputs to the default-status control change from more than
            // one place (setMode, auto-detect, the restore above), so watch them.
            this.refreshStatusLabels();
            this.$watch('mediaType', function () { self.refreshStatusLabels(); });
            this.$watch('mode', function () { self.refreshStatusLabels(); });

            // One delegated listener for every way in (#120). A button in a
            // swapped-in search-result fragment has no reliable Alpine scope
            // and the CSP forbids inline onclick, so each way in is a plain
            // button carrying three data attributes and no JS of its own —
            // and it survives every HTMX swap.
            document.addEventListener('click', function (e) {
                var btn = e.target.closest ? e.target.closest('[data-manual-add]') : null;
                if (!btn) return;
                e.preventDefault();
                self.manualOpen = true;
                window.dispatchEvent(new CustomEvent('shelf:manual-add', {
                    detail: {
                        title: btn.dataset.manualAddTitle || '',
                        media_type: btn.dataset.manualAddType || ''
                    }
                }));
            });
            var form = document.querySelector('form[hx-post="/api/scan"]');
            this._shelfFlow = createShelfFillFlow({
                root: this.$el, form: form, results: document.getElementById('scan-results'),
                node: function () { return document.getElementById('shelf-fill-location'); },
                active: function () { return self.mode === 'shelf_fill'; }
            });
            this.installShelfFillPicker();
            if (form) {
                form.addEventListener('htmx:beforeRequest', function(e) {
                    if (self.mode === 'lend' && !self.borrowerId) {
                        e.preventDefault();
                        showToast(t('Select a borrower first'), 'error');
                        return false;
                    }
                    if ((self.mode === 'move' && !self.location) || (self.mode === 'inventory' && !self.auditLocation)) {
                        e.preventDefault();
                        showToast(t('Select a location first'), 'error');
                        return false;
                    }
                    if (self.mode === 'shelf_fill' && !self.shelfLocation) {
                        e.preventDefault();
                        showToast('Select a shelf location first', 'error');
                        return false;
                    }
                });
                form.addEventListener('htmx:configRequest', function(e) {
                    if (self.mode === 'shelf_fill') {
                        e.detail.path = '/api/shelf-fill/scan';
                    }
                });
            }

            this.updateShelfFillVisibility();

            // One listener for the audit strip (G6: afterSwap, not
            // afterSettle). (a) A strip fragment landed: sync auditId from it.
            // (b) A scan card landed in Inventory mode with an audit open —
            // typed form or a legacy card's repost, both /api/scan — so the
            // strip's counts are stale. The camera path refreshes itself.
            document.body.addEventListener('htmx:afterSwap', function(e) {
                var detail = e.detail || {};
                var target = detail.target;
                if (target && target.id === 'audit-strip') {
                    self.syncAuditFromStrip(target);
                    return;
                }
                var path = detail.requestConfig && detail.requestConfig.path;
                if (path === '/api/scan' && self.mode === 'inventory' && self.auditId) {
                    self.refreshAudit();
                }
            });
            var strip = document.getElementById('audit-strip');
            if (strip) {
                strip.addEventListener('click', function(e) {
                    if (e.target.closest && e.target.closest('[data-audit-leave]')) self.leaveAudit();
                });
            }
            if (this.mode === 'inventory') this.refreshAudit();
        },

        // The selected location's name, for the whole-library strip: there
        // every scan files the book at this location, so it must stay in view
        // as the user walks from room to room (QA audit-sessions Obs 1).
        get scanLocationName() {
            var id = this.auditLocation;
            var select = document.getElementById('inventory-location');
            if (!id || !select) return 'no location';
            for (var i = 0; i < select.options.length; i++) {
                if (select.options[i].value === id) return select.options[i].textContent.trim();
            }
            return 'no location';
        },

        // Ask the server for the strip: the open session's summary, or the
        // start-or-join view when auditId is empty, finished or deleted.
        refreshAudit() {
            var url = '/api/audits/summary?audit_id=' + encodeURIComponent(this.auditId || '');
            htmx.ajax('GET', url, {target: '#audit-strip', swap: 'innerHTML'});
        },

        syncAuditFromStrip(strip) {
            var root = strip.firstElementChild;
            if (!root || !root.dataset) return;
            var id = root.dataset.auditId || '';
            var auditLocation = root.dataset.auditLocation || '';
            this.auditId = id;
            try {
                if (id) localStorage.setItem('shelf_audit_id', id);
                else localStorage.removeItem('shelf_audit_id');
            } catch (e) { /* private mode */ }
            // A location audit takes scans only at its own location, so
            // starting, joining or resuming one selects it — through state,
            // so x-model keeps the select in step.
            if (id && auditLocation && this.auditLocation !== auditLocation) {
                this.auditLocation = auditLocation;
                this.persistAuditLocation();
            }
        },

        // Leave: this device stops scanning into the audit; the audit stays open.
        leaveAudit() {
            this.auditId = '';
            try { localStorage.removeItem('shelf_audit_id'); } catch (e) { /* private mode */ }
            this.refreshAudit();
        },

        async toggleCamera() {
            if (this.cameraActive) {
                await this.stopCamera();
            } else {
                await this.startCamera();
            }
        },

        async startCamera() {
            try {
                this.cameraActive = true;
                this.scanPaused = false;
                this.scanResult = false;

                this.scanner = window.createBarcodeScanner({
                    html5ElId: 'camera-reader',
                    videoEl: 'zxing-video',
                    html5Config: { fps: 10, qrbox: { width: 280, height: 100 }, aspectRatio: 1.5 },
                    onDecode: (decodedText) => this.onScan(decodedText)
                });
                this.isZxingFallback = this.scanner.engine === 'zxing';
                this.scanLoading = this.isZxingFallback;

                // Let the paired x-show containers settle before the engine
                // grabs its target element.
                await this.$nextTick();

                await this.scanner.start();
                this.scanLoading = false;
            } catch (err) {
                this.scanner = false;
                this.cameraActive = false;
                this.scanLoading = false;
                if (!window.isSecureContext) {
                    showToast(t('Camera requires HTTPS. Open Shelf through an HTTPS reverse proxy, or leave SHELF_TLS on.'), 'error');
                } else {
                    showToast(t('Camera access denied. Check browser permissions for this site.'), 'error');
                }
            }
        },

        async stopCamera() {
            if (this.scanner) {
                await this.scanner.stop();
                this.scanner = false;
            }
            this.cameraActive = false;
            this.scanPaused = false;
            this.scanResult = false;
        },

        async resumeScanning() {
            if (this.scanLoading) return;
            this.scanResult = false;
            this.scanLoading = false;
            this.lastScanned = '';
            try {
                if (this.scanner) await this.scanner.resume();
            } catch (err) {}
            this.scanPaused = false;
        },

        async onScan(code) {
            if (this.scanPaused || this.scanLoading) return;

            // Client-side validation
            if (this.mode === 'lend' && !this.borrowerId) {
                showToast(t('Select a borrower first'), 'error');
                return;
            }
            if ((this.mode === 'move' && !this.location) || (this.mode === 'inventory' && !this.auditLocation)) {
                showToast(t('Select a location first'), 'error');
                return;
            }
            if (this.mode === 'shelf_fill' && !this.shelfLocation) {
                showToast('Select a shelf location first', 'error');
                return;
            }

            var now = Date.now();
            if (code === this.lastScanned && now - this.lastScanTime < 3000) return;

            // Snapshot before pausing the camera or any other await. The
            // precise node never comes from the later live selection.
            var scanMode = this.mode;
            var shelfContext = scanMode === 'shelf_fill' ? this._shelfFlow.begin() : null;
            if (scanMode === 'shelf_fill' && !shelfContext) return;
            var form = document.querySelector('form[hx-post="/api/scan"]');
            var formData = new FormData(form);
            formData.set('isbn', code);
            formData.set('mode', scanMode);
            if (scanMode === 'lend') formData.set('borrower_id', this.borrowerId);

            this.lastScanned = code;
            this.lastScanTime = now;

            // Pause scanner immediately
            this.scanPaused = true;
            this.scanLoading = true;
            this.scanResult = false;
            if (this.scanner) {
                try { await this.scanner.pause(); } catch(e) {}
            }

            // Beep
            try {
                var ctx = new (window.AudioContext || window.webkitAudioContext)();
                var osc = ctx.createOscillator();
                var gain = ctx.createGain();
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.frequency.value = 880;
                gain.gain.value = 0.3;
                osc.start();
                osc.stop(ctx.currentTime + 0.1);
            } catch(e) {}

            // Submit scan via fetch so we can capture the result
            try {
                var card;
                if (shelfContext) card = await this._shelfFlow.scan(shelfContext, formData);
                else {
                    var resp = await fetch('/api/scan', { method: 'POST', headers: { 'X-CSRF-Token': window.csrfToken() }, body: formData });
                    if (!resp.ok) throw new Error('HTTP ' + resp.status);
                    var html = await resp.text();
                    var results = document.getElementById('scan-results');
                    results.insertAdjacentHTML('afterbegin', html);
                    card = results.firstElementChild;
                    if (card) htmx.process(card);
                }
                var outcome = scanCardOutcome(card) || {};
                if (shelfContext && !this._shelfFlow.current(shelfContext)) return;

                // Both legacy cards require the user's interaction with the
                // card below the camera — an ambiguous choice, or the five
                // typed digits. Stop scanning so another frame cannot enqueue
                // a second unresolved choice and swap the card out from
                // under the user.
                if (outcome && (outcome.status === 'legacy_ambiguous' || outcome.status === 'legacy_incomplete')) {
                    if (this.scanner) {
                        try { await this.scanner.stop(); } catch (e) {}
                        this.scanner = false;
                    }
                    this.cameraActive = false;
                    this.scanPaused = false;
                    this.scanLoading = false;
                    this.scanResult = false;
                    showToast(outcome.status === 'legacy_ambiguous' ? t('Choose the matching book below') : t('Type the five digits below'), 'warning');
                    return;
                }

                this.scanResult = {
                    ok: outcome.ok,
                    warn: outcome.warn,
                    info: outcome.info,
                    label: outcome.label || 'done',
                    title: outcome.title,
                    authors: outcome.authors,
                    cover: outcome.cover ? outcome.cover.replace(/^\//, '') : null,
                    isbn: code
                };

                // The server recorded the scan into the open audit; refresh
                // the strip's counts (the typed path does this on afterSwap).
                if (scanMode === 'inventory' && this.auditId) this.refreshAudit();
            } catch (err) {
                this.scanResult = { ok: false, warn: false, info: false, label: t('error'), title: t('Scan failed'), isbn: code };
            } finally {
                this.scanLoading = false;
                if (shelfContext) this._shelfFlow.finish();
            }
        }
    }
}

// CSP build has no global fallback — register so x-data="scanPage" resolves.
document.addEventListener('alpine:init', function () {
    Alpine.data('scanPage', scanPage);
});
