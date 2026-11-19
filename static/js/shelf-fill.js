function shelfFillPage() {
    return {
        location: localStorage.getItem('shelf_fill_location') || '',
        // `kids_book` no longer exists as an option; normalize a cached
        // reading of it to `book` and correct the stored value.
        mediaType: (function () {
            var v = localStorage.getItem('shelf_fill_media_type') || 'auto';
            if (v === 'kids_book') {
                v = 'book';
                localStorage.setItem('shelf_fill_media_type', v);
            }
            return v;
        })(),
        platform: localStorage.getItem('shelf_fill_platform') || '',
        cameraActive: false,
        scanPaused: false,
        scanLoading: false,
        scanResult: false,
        scanner: false,
        cameraStarting: false,
        cameraStopping: false,
        cameraGeneration: 0,
        isZxingFallback: false,
        lastScanned: '',
        lastScanTime: 0,

        init() {
            var self = this;
            window.addEventListener('pagehide', function () { self.stopCamera(); });
            document.addEventListener('visibilitychange', function () {
                if (document.visibilityState === 'hidden') self.stopCamera();
            });
            var form = document.getElementById('shelf-fill-form');
            if (form) {
                form.addEventListener('htmx:beforeRequest', function (e) {
                    if (!self.location) {
                        e.preventDefault();
                        showToast('Choose a shelf location first', 'error');
                        return false;
                    }
                });
            }
        },

        persistLocation() { localStorage.setItem('shelf_fill_location', this.location); },
        persistMediaType() { localStorage.setItem('shelf_fill_media_type', this.mediaType); },
        persistPlatform() { localStorage.setItem('shelf_fill_platform', this.platform); },

        async toggleCamera() {
            if (this.cameraActive) await this.stopCamera();
            else await this.startCamera();
        },

        async startCamera() {
            if (this.cameraActive || this.cameraStarting || this.cameraStopping) return;
            if (!this.location) {
                showToast('Choose a shelf location first', 'error');
                return;
            }
            var generation = ++this.cameraGeneration, scanner = null, pending = null;
            try {
                this.cameraActive = true;
                this.scanPaused = false;
                this.scanResult = false;
                scanner = window.createBarcodeScanner({
                    html5ElId: 'shelf-fill-camera-reader',
                    videoEl: 'shelf-fill-zxing-video',
                    html5Config: {fps: 10, qrbox: {width: 280, height: 100}, aspectRatio: 1.5},
                    onDecode: (decodedText) => {
                        if (this.cameraActive && generation === this.cameraGeneration) this.onScan(decodedText);
                    }
                });
                this.scanner = scanner;
                this.isZxingFallback = scanner.engine === 'zxing';
                pending = this.$nextTick().then(() => {
                    if (this.cameraActive && generation === this.cameraGeneration) return scanner.start();
                });
                this.cameraStarting = pending;
                await pending;
            } catch (err) {
                if (scanner) await scanner.stop();
                if (generation !== this.cameraGeneration) return;
                this.scanner = false;
                this.cameraActive = false;
                showToast('Camera access failed. Check browser permissions and HTTPS.', 'error');
            } finally {
                if (this.cameraStarting === pending) this.cameraStarting = false;
            }
        },

        async stopCamera() {
            ++this.cameraGeneration;
            var scanner = this.scanner, pending = this.cameraStarting;
            this.scanner = false;
            this.cameraActive = false;
            this.scanPaused = false;
            this.scanLoading = false;
            this.scanResult = false;
            if (!scanner) return this.cameraStopping || undefined;
            document.querySelectorAll('#shelf-fill-camera-reader video, #shelf-fill-zxing-video').forEach(function (video) {
                if (video.srcObject) video.srcObject.getTracks().forEach(function (track) { track.stop(); });
            });
            var stopping = Promise.resolve(pending).catch(function () {}).then(function () { return scanner.stop(); });
            this.cameraStopping = stopping;
            await stopping;
            if (this.cameraStopping === stopping) this.cameraStopping = false;
        },

        async resumeScanning() {
            if (!this.scanner || !this.cameraActive || this.cameraStarting || this.cameraStopping) return;
            var generation = this.cameraGeneration;
            this.scanResult = false;
            this.scanLoading = false;
            this.lastScanned = '';
            try {
                var pending = this.scanner.resume();
                this.cameraStarting = pending;
                await pending;
            } catch (err) {}
            finally { if (this.cameraStarting === pending) this.cameraStarting = false; }
            if (generation === this.cameraGeneration) this.scanPaused = false;
        },

        async onScan(code) {
            if (this.scanPaused || !this.location) return;
            var generation = this.cameraGeneration;
            var now = Date.now();
            if (code === this.lastScanned && now - this.lastScanTime < 3000) return;
            this.lastScanned = code;
            this.lastScanTime = now;
            this.scanPaused = true;
            this.scanLoading = true;
            this.scanResult = false;
            var form = document.getElementById('shelf-fill-form');
            var formData = new FormData(form);
            formData.set('isbn', code);
            formData.set('location_id', this.location);
            formData.set('media_type', this.mediaType);
            formData.set('platform', this.platform);
            formData.set('tags', (document.getElementById('default-tags') || {}).value || '');
            try { if (this.scanner) await this.scanner.pause(); } catch (err) {}
            if (generation !== this.cameraGeneration) return;
            try {
                var resp = await fetch('/api/shelf-fill/scan', {
                    method: 'POST',
                    headers: {'X-CSRF-Token': window.csrfToken()},
                    body: formData
                });
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                var html = await resp.text();
                var results = document.getElementById('shelf-fill-results');
                results.insertAdjacentHTML('afterbegin', html);
                if (results.firstElementChild) htmx.process(results.firstElementChild);
                if (generation !== this.cameraGeneration) return;

                var tmp = document.createElement('div');
                tmp.innerHTML = html;
                var outcome = scanCardOutcome(tmp.querySelector('.scan-result'));
                this.scanResult = {
                    ok: outcome && outcome.ok,
                    warn: outcome && outcome.warn,
                    info: outcome && outcome.info,
                    title: outcome && outcome.title,
                    label: outcome && outcome.label,
                    code: code
                };
            } catch (err) {
                if (generation !== this.cameraGeneration) return;
                this.scanResult = {title: 'Scan failed', label: 'error', code: code};
            }
            this.scanLoading = false;
        }
    };
}

document.addEventListener('alpine:init', function () {
    Alpine.data('shelfFillPage', shelfFillPage);
});
