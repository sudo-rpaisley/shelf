function editSections() {
    var requested = window.location.hash ? window.location.hash.slice(1).replace(/^edit-/, '') : '';
    if (requested === 'location') requested = 'copies';
    var allowed = ['general', 'artwork', 'series', 'identifiers', 'copies', 'media', 'tags'];
    return {
        section: allowed.indexOf(requested) !== -1 ? requested : 'general',
        show(name) {
            this.section = name;
            if (window.history && window.history.replaceState) {
                window.history.replaceState(null, '', '#edit-' + (name === 'copies' ? 'location' : name));
            }
        },
        is(name) {
            return this.section === name;
        }
    };
}

function coverDrop() {
    return {
        dragging: false,
        preview: false,
        ownedPreview: '',
        releasePreview() {
            if (this.ownedPreview) URL.revokeObjectURL(this.ownedPreview);
            this.ownedPreview = '';
        },
        applyArtworkChoice(e) {
            this.releasePreview();
            this.preview = e.detail.url || false;
        },
        destroy() {
            this.releasePreview();
        },
        handleDrop(e) {
            this.dragging = false;
            var file = e.dataTransfer.files[0];
            if (file && file.type.startsWith('image/')) {
                var dt = new DataTransfer();
                dt.items.add(file);
                this.$refs.coverInput.files = dt.files;
                // The same successful file input/change path owns drops and
                // picker uploads, including pending-choice/provenance reset.
                this.$refs.coverInput.dispatchEvent(new Event('change', {bubbles: true}));
            }
        },
        handleFile(e) {
            var file = e.target.files[0];
            this.releasePreview();
            this.preview = false;
            if (file && file.type.startsWith('image/')) {
                this.ownedPreview = URL.createObjectURL(file);
                this.preview = this.ownedPreview;
            }
        }
    };
}

function writeBarcodeField(input, value) {
    if (!input) return;
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

function applyScannedBarcode(target, decodedText, scanMode, supplementTargetId) {
    var raw = String(decodedText || '').trim();
    var digits = raw.replace(/\D/g, '');

    if (scanMode === 'periodical-carrier') {
        var carrierLength = 0;
        if (digits.length === 15 || digits.length === 18) carrierLength = 13;
        if (digits.length === 14 || digits.length === 17) carrierLength = 12;

        if (carrierLength) {
            writeBarcodeField(target, digits.slice(0, carrierLength));
            var supplementTarget = document.getElementById(supplementTargetId || '');
            writeBarcodeField(supplementTarget, digits.slice(carrierLength));
            return true;
        }

        writeBarcodeField(target, raw);
        return true;
    }

    if (scanMode === 'periodical-supplement') {
        var supplement = '';
        if (digits.length === 15 || digits.length === 18) {
            supplement = digits.slice(13);
        } else if (digits.length === 14 || digits.length === 17) {
            supplement = digits.slice(12);
        } else if (digits.length === 2 || digits.length === 5) {
            supplement = digits;
        } else {
            return false;
        }
        writeBarcodeField(target, supplement);
        return true;
    }

    writeBarcodeField(target, raw);
    return true;
}

function installBarcodeFieldScanner() {
    var modal = document.getElementById('edit-barcode-scanner');
    var closeButton = document.getElementById('edit-barcode-scanner-close');
    var html5Reader = document.getElementById('edit-barcode-camera-reader');
    var zxingContainer = document.getElementById('edit-barcode-zxing-container');
    var status = document.getElementById('edit-barcode-scanner-status');
    var buttons = document.querySelectorAll('[data-scan-barcode-target]');
    if (!modal || !closeButton || !html5Reader || !zxingContainer || !status || !buttons.length) return;

    var scanner = false;
    var target = false;
    var scanMode = '';
    var supplementTargetId = '';
    var closing = false;

    function showModal() {
        modal.classList.remove('hidden');
        modal.classList.add('flex');
    }

    function hideModal() {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
        html5Reader.classList.remove('hidden');
        zxingContainer.classList.add('hidden');
    }

    async function stopScanner() {
        if (closing) return;
        closing = true;
        var active = scanner;
        scanner = false;
        if (active) {
            try { await active.stop(); } catch (e) {}
        }
        hideModal();
        closing = false;
    }

    async function startScanner(input, mode, supplementId) {
        await stopScanner();
        target = input;
        scanMode = mode || '';
        supplementTargetId = supplementId || '';
        status.textContent = scanMode === 'periodical-supplement'
            ? 'Point the camera at the magazine barcode and include the add-on if possible.'
            : 'Point the camera at the barcode.';
        showModal();

        if (!window.createBarcodeScanner) {
            status.textContent = 'Barcode scanner could not be loaded.';
            return;
        }

        scanner = window.createBarcodeScanner({
            html5ElId: 'edit-barcode-camera-reader',
            videoEl: 'edit-barcode-zxing-video',
            html5Config: { fps: 10, qrbox: { width: 280, height: 100 }, aspectRatio: 1.5 },
            forceZxing: scanMode === 'periodical-supplement',
            onDecode: function (decodedText) {
                if (!target) return;
                if (!applyScannedBarcode(target, decodedText, scanMode, supplementTargetId)) {
                    status.textContent = 'No 2- or 5-digit add-on was detected. Try again or enter it manually.';
                    return;
                }
                var completedTarget = target;
                target = false;
                scanMode = '';
                supplementTargetId = '';
                stopScanner().then(function () {
                    completedTarget.focus();
                    completedTarget.select();
                });
            }
        });

        if (scanner.engine === 'zxing') {
            html5Reader.classList.add('hidden');
            zxingContainer.classList.remove('hidden');
        } else {
            html5Reader.classList.remove('hidden');
            zxingContainer.classList.add('hidden');
        }

        try {
            await scanner.start();
        } catch (err) {
            scanner = false;
            if (!window.isSecureContext) {
                status.textContent = 'Camera access requires HTTPS.';
            } else {
                status.textContent = 'Camera access was denied. Check browser permissions for this site.';
            }
        }
    }

    buttons.forEach(function (button) {
        button.addEventListener('click', function () {
            var input = document.getElementById(button.dataset.scanBarcodeTarget);
            if (input) {
                startScanner(
                    input,
                    button.dataset.scanBarcodeMode || '',
                    button.dataset.scanBarcodeSupplementTarget || ''
                );
            }
        });
    });

    closeButton.addEventListener('click', function () {
        target = false;
        stopScanner();
    });
    modal.addEventListener('click', function (event) {
        if (event.target === modal) {
            target = false;
            stopScanner();
        }
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && !modal.classList.contains('hidden')) {
            target = false;
            stopScanner();
        }
    });
}

// Legacy invalid-but-unchanged identifiers are preserved by the modern server
// edit funnel. Keep fields successful: omitting an unchanged ISBN would make
// its meaning depend on whether a route treats absence as preserve or clear.
document.addEventListener('DOMContentLoaded', function () {
    installBarcodeFieldScanner();
});

// Native ISBN/UPC scanners keep their validation and feedback; the generic
// scanner above additionally handles the fork's periodical carrier/add-on.
function isbnCamera() {
    return {
        cameraActive: false,
        scanner: false,
        isZxingFallback: false,
        accepted: false,
        status: t('Point the camera at a 978 or 979 ISBN barcode.'),

        async startCamera() {
            if (this.cameraActive) return;
            this.cameraActive = true;
            this.accepted = false;
            this.status = t('Starting camera…');
            try {
                await this.$nextTick();
                this.scanner = window.createBarcodeScanner({
                    html5ElId: 'edit-isbn-camera-reader',
                    videoEl: 'edit-isbn-zxing-video',
                    html5Config: { fps: 10, qrbox: { width: 280, height: 100 }, aspectRatio: 1.5 },
                    onDecode: (decodedText) => this.acceptDecoded(decodedText)
                });
                this.isZxingFallback = this.scanner.engine === 'zxing';
                await this.$nextTick();
                await this.scanner.start();
                this.status = t('Point the camera at a 978 or 979 ISBN barcode.');
            } catch (err) {
                if (this.scanner) await this.scanner.stop();
                this.scanner = false;
                this.cameraActive = false;
                this.isZxingFallback = false;
                if (!window.isSecureContext) {
                    showToast(t('Camera requires HTTPS. Open Shelf through an HTTPS reverse proxy, or leave SHELF_TLS on.'), 'error');
                } else {
                    showToast(t('Camera access denied. Check browser permissions for this site.'), 'error');
                }
            }
        },

        async stopCamera() {
            if (this.scanner) await this.scanner.stop();
            this.scanner = false;
            this.cameraActive = false;
            this.isZxingFallback = false;
        },

        acceptDecoded(decodedText) {
            if (this.accepted) return;
            var digits = String(decodedText || '').replace(/\D/g, '');
            if (digits.length !== 13 || (digits.slice(0, 3) !== '978' && digits.slice(0, 3) !== '979')) {
                this.status = t('That barcode is not a 978/979 ISBN. Try again.');
                return;
            }
            var input = document.getElementById('isbn');
            if (!input) return;
            this.accepted = true;
            input.value = digits;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            this.stopCamera().then(function () {
                input.focus();
                input.select();
            });
        }
    };
}

function validEan13(code) {
    if (!/^\d{13}$/.test(code)) return false;
    var total = 0;
    for (var i = 0; i < 12; i += 1) total += parseInt(code[i], 10) * (i % 2 ? 3 : 1);
    return ((10 - (total % 10)) % 10) === parseInt(code[12], 10);
}

function validUpcA(code) {
    if (!/^\d{12}$/.test(code)) return false;
    var total = 0;
    for (var i = 0; i < 11; i += 1) total += parseInt(code[i], 10) * (i % 2 ? 1 : 3);
    return ((10 - (total % 10)) % 10) === parseInt(code[11], 10);
}

function canonicalEditUpc(raw) {
    var digits = String(raw || '').replace(/\D/g, '');
    if (digits.length === 12 && validUpcA(digits)) return '0' + digits;
    if (digits.length === 13 && digits.slice(0, 3) !== '978' && digits.slice(0, 3) !== '979' && validEan13(digits)) return digits;
    return '';
}

function upcCamera() {
    return {
        cameraActive: false,
        scanner: false,
        isZxingFallback: false,
        accepted: false,
        status: t('Point the camera at a UPC-A or EAN-13 retail barcode.'),

        async startCamera() {
            if (this.cameraActive) return;
            this.cameraActive = true;
            this.accepted = false;
            this.status = t('Starting camera…');
            try {
                await this.$nextTick();
                this.scanner = window.createBarcodeScanner({
                    html5ElId: 'edit-upc-camera-reader',
                    videoEl: 'edit-upc-zxing-video',
                    html5Config: { fps: 10, qrbox: { width: 280, height: 100 }, aspectRatio: 1.5 },
                    onDecode: (decodedText) => this.acceptDecoded(decodedText)
                });
                this.isZxingFallback = this.scanner.engine === 'zxing';
                await this.$nextTick();
                await this.scanner.start();
                this.status = t('Point the camera at a UPC-A or EAN-13 retail barcode.');
            } catch (err) {
                if (this.scanner) await this.scanner.stop();
                this.scanner = false;
                this.cameraActive = false;
                this.isZxingFallback = false;
                if (!window.isSecureContext) {
                    showToast(t('Camera requires HTTPS. Open Shelf through an HTTPS reverse proxy, or leave SHELF_TLS on.'), 'error');
                } else {
                    showToast(t('Camera access denied. Check browser permissions for this site.'), 'error');
                }
            }
        },

        async stopCamera() {
            if (this.scanner) await this.scanner.stop();
            this.scanner = false;
            this.cameraActive = false;
            this.isZxingFallback = false;
        },

        acceptDecoded(decodedText) {
            if (this.accepted) return;
            var canonical = canonicalEditUpc(decodedText);
            if (!canonical) {
                this.status = t('That is not a valid UPC-A / EAN-13 retail barcode. Try again.');
                return;
            }
            var input = document.getElementById('upc');
            if (!input) return;
            this.accepted = true;
            input.value = canonical;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            this.stopCamera().then(function () {
                input.focus();
                input.select();
            });
        }
    };
}

function updateEditSectionVisibility(root) {
    var mediaSelect = root.querySelector('#media_type');
    if (!mediaSelect) return;
    var mediaType = mediaSelect.value;
    root.querySelectorAll('[data-media-types]').forEach(function (element) {
        var supported = (element.dataset.mediaTypes || '').split(/\s+/).filter(Boolean);
        var alwaysVisible = element.dataset.alwaysVisible === 'true';
        element.hidden = !alwaysVisible && supported.indexOf(mediaType) === -1;
    });
}

document.addEventListener('DOMContentLoaded', function () {
    var root = document.querySelector('[data-item-edit-sections]');
    if (!root) return;
    var mediaSelect = root.querySelector('#media_type');
    updateEditSectionVisibility(root);
    if (mediaSelect) {
        mediaSelect.addEventListener('change', function () {
            updateEditSectionVisibility(root);
        });
    }
});

// The edit form's two ownership checkboxes (#125). Owned and wishlisted are
// independent, except that an owned item cannot be on the wishlist: the
// wishlist box is disabled while "I own this" is checked, and checking it
// clears the wishlist box. Initial state comes from data attributes, read
// synchronously (G2).
function ownershipBoxes() {
    return {
        owned: false,
        wishlisted: false,
        init() {
            this.owned = this.$el.dataset.owned === '1';
            this.wishlisted = this.$el.dataset.wishlisted === '1';
            this.$watch('owned', (value) => {
                if (value) this.wishlisted = false;
            });
        }
    };
}

// CSP build has no global fallback — register so x-data components resolve.
document.addEventListener('alpine:init', function () {
    Alpine.data('editSections', editSections);
    Alpine.data('coverDrop', coverDrop);
    Alpine.data('isbnCamera', isbnCamera);
    Alpine.data('upcCamera', upcCamera);
    Alpine.data('ownershipBoxes', ownershipBoxes);
});
