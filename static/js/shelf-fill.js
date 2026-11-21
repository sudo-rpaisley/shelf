function shelfFillPage() {
    return {
        // The badge's fallback word; a CSP-build expression can't call t().
        get shelvedLabel() { return t('shelved'); },
        location: localStorage.getItem('shelf_fill_location') || '',
        // Existing fork kids_book choices and identities remain valid.
        mediaType: localStorage.getItem('shelf_fill_media_type') || 'auto',
        platform: localStorage.getItem('shelf_fill_platform') || '',
        cameraActive: false,
        scanPaused: false,
        scanLoading: false,
        scanResult: false,
        scanner: false,
        isZxingFallback: false,
        lastScanned: '',
        lastScanTime: 0,

        init() {
            var self = this;
            var shelfSeed = this.$el.getAttribute('data-shelf-fill-initial');
            if (shelfSeed) this.location = shelfSeed;
            var form = document.getElementById('shelf-fill-form');
            this._shelfFlow = createShelfFillFlow({
                root: this.$el, form: form, results: document.getElementById('shelf-fill-results'),
                node: function () { return document.getElementById('shelf-fill-location'); },
                active: function () { return true; }
            });
            var select = document.getElementById('shelf-fill-location');
            if (select && !Array.from(select.options).some(function (option) { return option.value === self.location; })) this.location = '';
            if (form) {
                form.addEventListener('htmx:beforeRequest', function (e) {
                    if (!self.location) {
                        e.preventDefault();
                        showToast(t('Choose a shelf location first'), 'error');
                        return false;
                    }
                });
            }
        },

        persistLocation() {
            localStorage.setItem('shelf_fill_location', this.location);
            this._shelfFlow.changed();
        },
        persistMediaType() { localStorage.setItem('shelf_fill_media_type', this.mediaType); },
        persistPlatform() { localStorage.setItem('shelf_fill_platform', this.platform); },

        async toggleCamera() {
            if (this.cameraActive) await this.stopCamera();
            else await this.startCamera();
        },

        async startCamera() {
            if (!this.location) {
                showToast(t('Choose a shelf location first'), 'error');
                return;
            }
            try {
                this.cameraActive = true;
                this.scanPaused = false;
                this.scanResult = false;
                this.scanner = window.createBarcodeScanner({
                    html5ElId: 'shelf-fill-camera-reader',
                    videoEl: 'shelf-fill-zxing-video',
                    html5Config: {fps: 10, qrbox: {width: 280, height: 100}, aspectRatio: 1.5},
                    onDecode: (decodedText) => this.onScan(decodedText)
                });
                this.isZxingFallback = this.scanner.engine === 'zxing';
                await this.$nextTick();
                await this.scanner.start();
            } catch (err) {
                this.scanner = false;
                this.cameraActive = false;
                showToast(t('Camera access failed. Check browser permissions and HTTPS.'), 'error');
            }
        },

        async stopCamera() {
            if (this.scanner) {
                await this.scanner.stop();
                this.scanner = false;
            }
            this.cameraActive = false;
            this.scanPaused = false;
            this.scanLoading = false;
            this.scanResult = false;
        },

        async resumeScanning() {
            if (this.scanLoading) return;
            this.scanResult = false;
            this.scanLoading = false;
            this.lastScanned = '';
            try { if (this.scanner) await this.scanner.resume(); } catch (err) {}
            this.scanPaused = false;
        },

        async onScan(code) {
            if (this.scanPaused || this.scanLoading || !this.location) return;
            var now = Date.now();
            if (code === this.lastScanned && now - this.lastScanTime < 3000) return;
            var context = this._shelfFlow.begin();
            if (!context) return;
            var form = document.getElementById('shelf-fill-form');
            var formData = new FormData(form);
            formData.set('isbn', code);
            formData.set('media_type', this.mediaType);
            formData.set('platform', this.platform);
            formData.set('tags', (document.getElementById('default-tags') || {}).value || '');
            this.lastScanned = code;
            this.lastScanTime = now;
            this.scanPaused = true;
            this.scanLoading = true;
            this.scanResult = false;
            try { if (this.scanner) await this.scanner.pause(); } catch (err) {}

            try {
                var card = await this._shelfFlow.scan(context, formData);
                if (!this._shelfFlow.current(context)) return;
                var outcome = scanCardOutcome(card);
                this.scanResult = {
                    ok: outcome && outcome.ok,
                    warn: outcome && outcome.warn,
                    info: outcome && outcome.info,
                    title: outcome && outcome.title,
                    label: outcome && outcome.label,
                    code: code
                };
            } catch (err) {
                this.scanResult = {title: t('Scan failed'), label: t('error'), code: code};
            } finally {
                this.scanLoading = false;
                this._shelfFlow.finish();
            }
        }
    };
}

document.addEventListener('alpine:init', function () {
    Alpine.data('shelfFillPage', shelfFillPage);
});
