function scanPage() {
    return {
        mode: localStorage.getItem('shelf_scan_mode') || 'add',
        // Auto for a *new* user. A stored value is deliberately never
        // migrated: "book" is also what someone who scans books chose, and
        // reinterpreting that as "no choice" is guessing at intent. The
        // barcode rule (§1) is what reaches those users instead.
        mediaType: localStorage.getItem('shelf_media_type') || 'auto',
        platform: localStorage.getItem('shelf_platform') || '',
        location: localStorage.getItem('shelf_location') || '',
        shelfLocation: localStorage.getItem('shelf_fill_location') || '',
        borrowerId: '',
        cameraActive: false,
        scanPaused: false,
        scanLoading: false,
        scanResult: false,
        scanner: false,
        lastScanned: '',
        lastScanTime: 0,
        inventoryScannedIds: [],
        isZxingFallback: false,

        modes: [
            {id: 'add', label: 'Add'},
            {id: 'shelf_fill', label: 'Shelf Fill'},
            {id: 'wishlist', label: 'Wishlist'},
            {id: 'lend', label: 'Lend'},
            {id: 'return', label: 'Return'},
            {id: 'move', label: 'Move'},
            {id: 'inventory', label: 'Inventory'},
            {id: 'lookup', label: 'Lookup'},
            {id: 'quick_rate', label: 'Quick Rate'},
        ],

        get modeConfig() {
            var configs = {
                add: {heading: 'Add Items', description: 'Scan barcodes to add items to your collection.'},
                shelf_fill: {heading: 'Shelf Filling', description: 'Pick a precise shelf or sub-location, then scan items in the order they sit. New items are added; existing items are moved.'},
                wishlist: {heading: 'Add to Wishlist', description: 'Scan to save items you want but haven\'t bought yet.'},
                lend: {heading: 'Lend Items', description: 'Select a borrower, then scan items to check them out.'},
                'return': {heading: 'Return Items', description: 'Scan items to check them back in.'},
                move: {heading: 'Move Items', description: 'Select a target location, then scan items to move them.'},
                inventory: {heading: 'Inventory Audit', description: 'Select a location and scan every item you find there. Then check for missing items.'},
                lookup: {heading: 'Lookup', description: 'Scan to check if an item is in your collection. No changes are made.'},
                quick_rate: {heading: 'Quick Rate', description: 'Scan to mark items as read or completed.'},
            };
            return configs[this.mode] || configs.add;
        },

        loadRecentScans(m) {
            fetch('/api/recent-scans?mode=' + encodeURIComponent(m))
                .then(function(r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.text();
                })
                .then(function(html) {
                    document.getElementById('scan-results').innerHTML = html;
                })
                .catch(function() {
                    showToast('Could not load recent scans', 'error');
                });
        },

        setMode(m) {
            this.mode = m;
            localStorage.setItem('shelf_scan_mode', m);
            document.getElementById('inventory-missing').innerHTML = '';
            this.inventoryScannedIds = [];
            this.loadRecentScans(m);
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

        persistShelfLocation() {
            var select = document.getElementById('shelf-fill-location');
            this.shelfLocation = select ? select.value : '';
            localStorage.setItem('shelf_fill_location', this.shelfLocation);
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
                })
                .catch(function() {
                    host.innerHTML = '';
                    if (self.mode === 'shelf_fill') {
                        showToast('Could not load shelf locations', 'error');
                    }
                });
        },

        async placeDeferredResult(html) {
            if (this.mode !== 'shelf_fill' || !this.shelfLocation || !html) return;

            var tmp = document.createElement('div');
            tmp.innerHTML = html;
            var incoming = tmp.querySelector('.scan-result');
            if (!incoming) return;
            var status = incoming.getAttribute('data-scan-status') || '';
            if (status !== 'added' && status !== 'duplicate') return;

            var link = incoming.querySelector('a[href^="/item/"]');
            if (!link) return;
            var match = link.getAttribute('href').match(/\/item\/(\d+)/);
            if (!match) return;
            var itemId = parseInt(match[1]);

            var body = new FormData();
            body.set('item_id', itemId);
            body.set('location_node_id', this.shelfLocation);
            try {
                var resp = await fetch('/api/shelf-fill/place', {
                    method: 'POST',
                    headers: {'X-CSRF-Token': window.csrfToken()},
                    body: body
                });
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                var replacement = await resp.text();
                var cards = document.querySelectorAll('#scan-results .scan-result');
                for (var i = 0; i < cards.length; i++) {
                    var cardLink = cards[i].querySelector('a[href="/item/' + itemId + '"]');
                    var cardStatus = cards[i].getAttribute('data-scan-status') || '';
                    if (cardLink && (cardStatus === 'added' || cardStatus === 'duplicate')) {
                        cards[i].outerHTML = replacement;
                        var first = document.querySelector('#scan-results .scan-result');
                        if (first) htmx.process(first);
                        break;
                    }
                }
            } catch (err) {
                showToast('Item was added, but could not be placed on the selected shelf', 'error');
            }
        },

        // Client-side validation before form submit
        init() {
            var self = this;
            var form = document.querySelector('form[hx-post="/api/scan"]');
            this.installShelfFillPicker();
            if (form) {
                form.addEventListener('htmx:beforeRequest', function(e) {
                    if (self.mode === 'lend' && !self.borrowerId) {
                        e.preventDefault();
                        showToast('Select a borrower first', 'error');
                        return false;
                    }
                    if ((self.mode === 'move' || self.mode === 'inventory') && !self.location) {
                        e.preventDefault();
                        showToast('Select a location first', 'error');
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

            // Metadata misses and magazine scans can render a second-step form.
            // Once that form eventually creates or resolves an item, place it
            // on the still-selected precise shelf without asking again.
            document.body.addEventListener('htmx:afterRequest', function(e) {
                if (self.mode !== 'shelf_fill' || !e.detail.successful) return;
                var el = e.detail.elt;
                if (!el || !el.closest || !el.closest('#scan-results')) return;
                var xhr = e.detail.xhr;
                if (xhr && xhr.responseText) self.placeDeferredResult(xhr.responseText);
            });

            this.updateShelfFillVisibility();
        },

        async showMissing() {
            var form = new FormData();
            form.set('location_id', this.location);
            form.set('scanned_ids', this.inventoryScannedIds.join(','));
            try {
                var resp = await fetch('/api/inventory/missing', {
                    method: 'POST',
                    headers: {'X-CSRF-Token': window.csrfToken()},
                    body: form
                });
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                var html = await resp.text();
                document.getElementById('inventory-missing').innerHTML = html;
            } catch (e) {
                showToast('Could not check missing items', 'error');
            }
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
                if (location.protocol !== 'https:' && location.hostname !== 'localhost') {
                    showToast('Camera requires HTTPS. Access Shelf via https:// and accept the certificate.', 'error');
                } else {
                    showToast('Camera access denied. Check browser permissions for this site.', 'error');
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
            this.scanResult = false;
            this.scanLoading = false;
            this.lastScanned = '';
            try {
                if (this.scanner) await this.scanner.resume();
            } catch (err) {}
            this.scanPaused = false;
        },

        async onScan(code) {
            if (this.scanPaused) return;

            // Client-side validation
            if (this.mode === 'lend' && !this.borrowerId) {
                showToast('Select a borrower first', 'error');
                return;
            }
            if ((this.mode === 'move' || this.mode === 'inventory') && !this.location) {
                showToast('Select a location first', 'error');
                return;
            }
            if (this.mode === 'shelf_fill' && !this.shelfLocation) {
                showToast('Select a shelf location first', 'error');
                return;
            }

            var now = Date.now();
            if (code === this.lastScanned && now - this.lastScanTime < 3000) return;
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
            var form = document.querySelector('form[hx-post="/api/scan"]');
            var formData = new FormData(form);
            formData.set('isbn', code);
            formData.set('mode', this.mode);
            if (this.mode === 'lend') formData.set('borrower_id', this.borrowerId);
            var endpoint = this.mode === 'shelf_fill' ? '/api/shelf-fill/scan' : '/api/scan';
            try {
                var resp = await fetch(endpoint, { method: 'POST', headers: { 'X-CSRF-Token': window.csrfToken() }, body: formData });
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                var html = await resp.text();

                // Insert result into the scan results list
                var results = document.getElementById('scan-results');
                results.insertAdjacentHTML('afterbegin', html);
                htmx.process(results.firstElementChild);

                // Parse the result to show in overlay. scanCardOutcome (app.js)
                // reads the card's own data-scan-* attributes; it is the same
                // reader the typed/Enter toast uses, so the two paths cannot
                // drift apart the way two class-substring parsers did.
                var tmp = document.createElement('div');
                tmp.innerHTML = html;
                var outcome = scanCardOutcome(tmp.querySelector('.scan-result'));

                this.scanResult = {
                    ok: outcome.ok,
                    warn: outcome.warn,
                    label: outcome.label || 'done',
                    title: outcome.title,
                    authors: outcome.authors,
                    cover: outcome.cover ? outcome.cover.replace(/^\//, '') : null,
                    isbn: code
                };

                // Track item IDs for inventory mode
                if (this.mode === 'inventory') {
                    var linkEl = tmp.querySelector('a[href^="/item/"]');
                    if (linkEl) {
                        var match = linkEl.getAttribute('href').match(/\/item\/(\d+)/);
                        if (match) this.inventoryScannedIds.push(parseInt(match[1]));
                    }
                }
            } catch (err) {
                this.scanResult = { ok: false, warn: false, label: 'error', title: 'Scan failed', isbn: code };
            }
            this.scanLoading = false;
        }
    }
}

// CSP build has no global fallback — register so x-data="scanPage" resolves.
document.addEventListener('alpine:init', function () {
    Alpine.data('scanPage', scanPage);
});
