/* Contextual field capture, not a scan operation. No fetch, provider request,
   form submission or persisted state. Engines are the existing shared modules. */
(function () {
    'use strict';
    var dialog = document.querySelector('[data-add-camera-dialog]');
    if (!dialog) return;
    var title = dialog.querySelector('#add-camera-title');
    var status = dialog.querySelector('[data-camera-status]');
    var grab = dialog.querySelector('[data-camera-grab]');
    var reader = dialog.querySelector('[data-camera-reader]');
    var barcodeVideo = dialog.querySelector('[data-camera-barcode-video]');
    var photoVideo = dialog.querySelector('[data-camera-photo-video]');
    var notice = document.querySelector('[data-add-camera-notice]');
    var current = null, draining = Promise.resolve(), closing = false;
    var artwork = new WeakMap();
    var nativeCapture = 'capture' in document.createElement('input');
    var canAssign = false;
    try { canAssign = typeof DataTransfer === 'function' && !!new DataTransfer().files; } catch (error) {}
    var canModal = typeof dialog.showModal === 'function';

    function stopTracks(video) {
        var stream = video.srcObject;
        if (stream && typeof stream.getTracks === 'function') stream.getTracks().forEach(function (track) { track.stop(); });
        video.srcObject = null;
    }
    function usable(element) {
        return !!element && element.isConnected && !element.matches(':disabled') &&
            !element.closest('[hidden],[inert]') && element.getClientRects().length > 0;
    }
    function live(session) {
        return current === session && !session.retired && dialog.open &&
            session.target.isConnected && !session.target.matches(':disabled');
    }
    function close(restore) {
        var session = current;
        if (!session) return;
        current = null;
        session.retired = true; // Gate decode and grab continuations BEFORE teardown.
        grab.disabled = true;
        stopTracks(barcodeVideo);
        stopTracks(photoVideo);
        reader.querySelectorAll('video').forEach(stopTracks);
        // html5-qrcode loses its handle if stop is called during acquisition.
        // Keep that handle owned until start settles, then stop it even on a
        // late success or failure. Never reuse the closing engine or video.
        closing = true;
        draining = session.starting.catch(function () {}).then(function () {
            return session.engine ? session.engine.stop() : undefined;
        }).catch(function () {}).then(function () {
            stopTracks(barcodeVideo);
            stopTracks(photoVideo);
            reader.querySelectorAll('video').forEach(stopTracks);
            closing = false;
        });
        if (dialog.open) dialog.close();
        if (restore && document.visibilityState !== 'hidden') {
            var focus = session.kind === 'barcode' ? session.target : session.opener;
            if (!usable(focus)) focus = session.opener;
            if (usable(focus)) focus.focus();
        }
    }
    function feedback(text) { if (notice) notice.textContent = text; }
    function decode(session, code) {
        if (!live(session)) return;
        if (typeof code !== 'string' || !/^(?:[0-9]{12,13}|[0-9]{9}[0-9Xx])$/.test(code)) {
            status.textContent = dialog.dataset.invalidCode;
            return;
        }
        var method = document.getElementById(session.opener.dataset.cameraMethod);
        var form = session.target.form;
        var type = form && form.elements.namedItem('media_type');
        var isbn = /^(?:97[89][0-9]{10}|[0-9]{9}[0-9Xx])$/.test(code);
        if (isbn && type && type.value && ['book', 'audiobook', 'ebook', 'comic', 'manga'].indexOf(type.value) === -1) {
            status.textContent = dialog.dataset.wrongFormat;
            return;
        }
        if (!method || method.matches(':disabled')) return;
        method.value = isbn ? 'isbn' : 'barcode';
        method.dispatchEvent(new Event('change', {bubbles: true}));
        session.target.value = code;
        session.target.dispatchEvent(new Event('input', {bubbles: true}));
        session.target.dispatchEvent(new Event('change', {bubbles: true}));
        feedback(dialog.dataset.filled);
        close(true);
    }
    async function open(opener, kind, target) {
        if (current || closing || !canModal || !usable(target)) return;
        var session = {kind: kind, target: target, opener: opener, engine: null,
                       starting: Promise.resolve(), retired: false};
        current = session;
        title.textContent = kind === 'barcode' ? dialog.dataset.barcodeTitle : dialog.dataset.photoTitle;
        status.textContent = dialog.dataset.starting;
        grab.hidden = kind !== 'photo';
        grab.disabled = true;
        reader.hidden = barcodeVideo.hidden = photoVideo.hidden = true;
        feedback('');
        try { dialog.showModal(); } catch (error) { current = null; return; }
        // An existing continuous scan must relinquish its camera first. The
        // operation stays on that tool; this event does not submit anything.
        var stops = [];
        document.dispatchEvent(new CustomEvent('shelf:camera-open', {detail: {waitUntil: function (promise) { stops.push(promise); }}}));
        session.starting = Promise.all(stops).then(function () {
            if (!live(session)) return;
            if (kind === 'barcode') {
                session.engine = window.createBarcodeScanner({
                    html5ElId: 'add-camera-reader', videoEl: barcodeVideo,
                    html5Config: {fps: 10, qrbox: {width: 250, height: 100}, aspectRatio: 1.5},
                    onDecode: function (code) { decode(session, code); }
                });
                reader.hidden = session.engine.engine === 'zxing';
                barcodeVideo.hidden = session.engine.engine !== 'zxing';
            } else {
                photoVideo.hidden = false;
                session.engine = window.createPhotoCapture({videoEl: photoVideo});
            }
            return session.engine.start();
        });
        try {
            await session.starting;
            if (!live(session)) return;
            status.textContent = dialog.dataset.ready;
            grab.disabled = kind !== 'photo';
        } catch (error) {
            if (session.engine) await session.engine.stop();
            if (!live(session)) return;
            status.textContent = dialog.dataset.failed;
        }
    }
    grab.addEventListener('click', async function () {
        var session = current;
        if (!session || !live(session) || session.kind !== 'photo' || grab.disabled) return;
        grab.disabled = true;
        try {
            var blob = await session.engine.grab();
            if (!live(session)) return;
            if (blob.size > 10 * 1024 * 1024) throw new Error('Image too large');
            var transfer = new DataTransfer();
            transfer.items.add(new File([blob], 'cover-camera.jpg', {type: 'image/jpeg'}));
            session.target.files = transfer.files;
            session.target.dispatchEvent(new Event('change', {bubbles: true}));
            close(true);
        } catch (error) {
            if (live(session)) { status.textContent = dialog.dataset.failed; grab.disabled = false; }
        }
    });
    dialog.querySelector('[data-camera-close]').addEventListener('click', function () { close(true); });
    dialog.addEventListener('cancel', function (event) { event.preventDefault(); close(true); });
    dialog.addEventListener('close', function () { if (!dialog.open) close(true); });

    function preview(widget) {
        var state = artwork.get(widget);
        if (!state) return;
        if (state.url) URL.revokeObjectURL(state.url);
        state.url = null;
        state.image.removeAttribute('src');
        state.image.hidden = true;
        var file = state.input.files && state.input.files[0];
        var valid = !file || (file.size <= 10 * 1024 * 1024 && (!file.type || /^image\/(jpeg|png|gif|webp)$/.test(file.type)));
        state.input.setCustomValidity(valid ? '' : dialog.dataset.imageInvalid);
        state.clear.hidden = !file;
        state.status.textContent = !valid ? dialog.dataset.imageInvalid : file ? dialog.dataset.imageSelected : '';
        if (file && valid && window.URL && URL.createObjectURL) {
            state.url = URL.createObjectURL(file);
            state.image.src = state.url;
            state.image.hidden = false;
        }
    }
    function initArtwork(widget) {
        if (artwork.has(widget)) return;
        var input = widget.querySelector('[data-cover-input]'), image = widget.querySelector('[data-cover-preview]');
        var clear = widget.querySelector('[data-clear-cover]'), message = widget.querySelector('[data-cover-status]');
        if (!input || !image || !clear || !message) return;
        artwork.set(widget, {input: input, image: image, clear: clear, status: message, url: null});
        input.addEventListener('change', function () { preview(widget); });
        clear.addEventListener('click', function () {
            input.value = '';
            input.dispatchEvent(new Event('change', {bubbles: true}));
            message.textContent = dialog.dataset.imageRemoved;
        });
        preview(widget);
    }
    function enhance(root) {
        if (root.matches && root.matches('[data-artwork-input]')) initArtwork(root);
        root.querySelectorAll('[data-artwork-input]').forEach(initArtwork);
        root.querySelectorAll('[data-add-camera]').forEach(function (button) {
            button.hidden = button.dataset.addCamera === 'barcode' ? !(canModal && window.createBarcodeScanner) :
                !(nativeCapture || (canModal && canAssign && window.createPhotoCapture && navigator.mediaDevices && navigator.mediaDevices.getUserMedia));
        });
    }
    document.addEventListener('click', function (event) {
        var opener = event.target.closest('[data-add-camera]');
        if (!opener || opener.matches(':disabled')) return;
        var kind = opener.dataset.addCamera;
        var widget = kind === 'photo' && opener.closest('[data-artwork-input]');
        var target = widget ? widget.querySelector('[data-cover-input]') : document.getElementById(opener.dataset.cameraTarget);
        if (!target || target.matches(':disabled')) return;
        if (kind === 'photo' && nativeCapture) {
            var stops = [];
            document.dispatchEvent(new CustomEvent('shelf:camera-open', {detail: {waitUntil: function (promise) { stops.push(promise); }}}));
            if (stops.length) { feedback(dialog.dataset.closing); return; }
            // Use the real successful file control. No DataTransfer or JS
            // multipart rewrite is necessary for mobile / no-webcam capture.
            target.setAttribute('capture', 'environment');
            target.click();
            target.removeAttribute('capture');
            return;
        }
        open(opener, kind, target);
    });
    function release(root) {
        if (current && (root === document || root.contains(current.target))) close(false);
        var widgets = Array.from(root.querySelectorAll('[data-artwork-input]'));
        if (root.matches && root.matches('[data-artwork-input]')) widgets.push(root);
        widgets.forEach(function (widget) {
            var state = artwork.get(widget);
            if (state && state.url) { URL.revokeObjectURL(state.url); state.url = null; }
            if (state) { state.image.removeAttribute('src'); state.image.hidden = true; }
        });
    }
    window.addEventListener('pagehide', function () { release(document); });
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') close(false); });
    window.addEventListener('pageshow', function () { document.querySelectorAll('[data-artwork-input]').forEach(preview); });
    document.addEventListener('htmx:beforeCleanupElement', function (event) { release(event.target); });
    document.addEventListener('htmx:afterSwap', function (event) { enhance(event.detail.target); });
    document.addEventListener('shelf:scan-result', function (event) { enhance(event.target); });
    enhance(document);
})();
