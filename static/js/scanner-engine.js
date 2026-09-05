// Shared barcode-scanner engine, consumed by the scan page and the store PWA.
//
// Framework-free on purpose: the store shell must run offline and loads no
// client-side framework at all. This module owns engine selection,
// construction and teardown only — decode dedupe, toasts and page state stay with the caller.
//
// Contract:
//   window.createBarcodeScanner({ html5ElId, videoEl, html5Config, onDecode, forceZxing? })
//     -> { engine: 'zxing'|'html5-qrcode', start(), stop(), pause(), resume() }
//
//   start() and stop() return Promises under both strategies. start() rejects
//   on a start failure — the page handles the error UX; strategies never touch
//   page state and never raise toasts. stop()/pause()/resume() never reject,
//   and are safe no-ops when the engine is idle.
(function () {
    'use strict';

    // @zxing/browser UMD exports as ZXingBrowser (not ZXing).
    // DecodeHintType isn't re-exported — use the numeric enum values
    // from @zxing/library: POSSIBLE_FORMATS=2, TRY_HARDER=3.
    // ResultMetadataType is likewise not re-exported; UPC_EAN_EXTENSION=7.
    var HINT_POSSIBLE_FORMATS = 2;
    var HINT_TRY_HARDER = 3;
    var META_UPC_EAN_EXTENSION = 7;
    var CONFIRM_WINDOW_MS = 1800;

    function isIosDevice() {
        var ua = navigator.userAgent;
        return /iPad|iPhone|iPod/.test(ua) || (ua.includes('Macintosh') && 'ontouchend' in document);
    }

    function resolveEl(ref) {
        return typeof ref === 'string' ? document.getElementById(ref) : ref;
    }

    // Camera decoders occasionally produce a single plausible-but-wrong EAN
    // while the phone is moving or autofocus is settling. Do not submit that
    // first frame. A code must be seen twice, unchanged, within a short window.
    // USB/keyboard scans do not pass through this engine and are unaffected.
    function createDecodeGuard(onDecode) {
        var candidate = '';
        var candidateAt = 0;

        return {
            accept: function (decodedText) {
                var now = Date.now();
                if (decodedText === candidate && now - candidateAt <= CONFIRM_WINDOW_MS) {
                    candidate = '';
                    candidateAt = 0;
                    onDecode(decodedText);
                    return;
                }
                candidate = decodedText;
                candidateAt = now;
            },
            reset: function () {
                candidate = '';
                candidateAt = 0;
            }
        };
    }

    function createHtml5Engine(opts) {
        var scanner = false;

        return {
            engine: 'html5-qrcode',

            start: function () {
                scanner = new Html5Qrcode(opts.html5ElId);
                return scanner.start(
                    { facingMode: 'environment' },
                    opts.html5Config,
                    function (decodedText) { opts.onDecode(decodedText); },
                    function () { /* per-frame decode misses are normal */ }
                );
            },

            stop: function () {
                if (!scanner) return Promise.resolve();
                var handle = scanner;
                scanner = false;
                return Promise.resolve()
                    .then(function () { return handle.stop(); })
                    .catch(function () {});
            },

            pause: function () {
                return Promise.resolve()
                    .then(function () { if (scanner) return scanner.pause(true); })
                    .catch(function () {});
            },

            resume: function () {
                return Promise.resolve()
                    .then(function () { if (scanner) return scanner.resume(); })
                    .catch(function () {});
            }
        };
    }

    function zxingDecodedText(result) {
        var text = result.getText();
        try {
            var metadata = result.getResultMetadata && result.getResultMetadata();
            var extension = metadata && metadata.get ? metadata.get(META_UPC_EAN_EXTENSION) : null;
            // EAN/UPC add-ons are either two or five digits. ZXing decodes the
            // carrier and extension separately; concatenate them before handing
            // the value to Shelf so camera scans follow the same server path as
            // USB scanners that already emit carrier+supplement as one string.
            if (typeof extension === 'string' && /^(?:\d{2}|\d{5})$/.test(extension)) {
                return text + extension;
            }
        } catch (e) {
            // Metadata is optional; a perfectly good carrier scan must still
            // succeed when a browser/device does not expose the extension.
        }
        return text;
    }

    function createZxingEngine(opts) {
        var reader = false;
        var controls = false;
        var paused = false;

        function stop() {
            if (controls) {
                try { controls.stop(); } catch (e) {}
                controls = false;
            }
            // BrowserMultiFormatReader in @zxing/browser 0.1.5 has no reset();
            // teardown is the IScannerControls handle from decodeFromConstraints,
            // already stopped above.
            reader = false;
            return Promise.resolve();
        }

        function start() {
            // Single stop -> start: callers may restart a live stream (resume).
            return stop().then(function () {
                var formats = ZXingBrowser.BarcodeFormat;
                var hints = new Map();
                hints.set(HINT_POSSIBLE_FORMATS, [
                    formats.EAN_13,
                    formats.UPC_A,
                    formats.EAN_8,
                    formats.UPC_E
                ]);
                hints.set(HINT_TRY_HARDER, true);

                reader = new ZXingBrowser.BrowserMultiFormatReader(hints, {
                    delayBetweenScanAttempts: 100,
                    delayBetweenScanSuccess: 250
                });

                var constraints = {
                    audio: false,
                    video: {
                        width: { ideal: 1920 },
                        height: { ideal: 1080 },
                        facingMode: { ideal: 'environment' }
                    }
                };

                paused = false;
                return reader.decodeFromConstraints(constraints, resolveEl(opts.videoEl), function (result) {
                    if (result && !paused) opts.onDecode(zxingDecodedText(result));
                });
            }).then(function (handle) {
                controls = handle;
            });
        }

        return {
            engine: 'zxing',
            start: start,
            stop: stop,

            // ZXing has no native pause: gate the decode callback instead.
            pause: function () {
                paused = true;
                return Promise.resolve();
            },

            resume: function () {
                if (!controls) {
                    paused = false;
                    return Promise.resolve();
                }
                return start().catch(function () {});
            }
        };
    }

    function guardedEngine(engine, guard) {
        return {
            engine: engine.engine,
            start: function () {
                guard.reset();
                return engine.start();
            },
            stop: function () {
                guard.reset();
                return engine.stop();
            },
            pause: function () {
                guard.reset();
                return engine.pause();
            },
            resume: function () {
                guard.reset();
                return engine.resume();
            }
        };
    }

    window.createBarcodeScanner = function (opts) {
        var guard = createDecodeGuard(opts.onDecode);
        var guardedOpts = Object.assign({}, opts, { onDecode: guard.accept });

        // Keep the original html5-qrcode scanner on Android and other
        // non-iOS devices. Besides being more reliable for the long 977 EAN
        // carrier in real use, it restores the compact preview, shaded mask and
        // the small 280x100 framing rectangle configured by scan.js.
        //
        // iOS retains ZXing, where that engine is the established compatibility
        // path. A caller may explicitly opt into ZXing for a targeted operation
        // such as reading UPC_EAN_EXTENSION metadata from a magazine add-on;
        // this does not change the normal Android carrier-scanning path.
        var engine = (opts.forceZxing || isIosDevice())
            ? createZxingEngine(guardedOpts)
            : createHtml5Engine(guardedOpts);
        return guardedEngine(engine, guard);
    };
})();