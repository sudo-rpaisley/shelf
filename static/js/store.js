/* Store Mode — offline "do I own this?" checks. See .devdocs/archive/completed/PWA_STORE_MODE.md.
 *
 * Catalogue snapshots belong to an authenticated owner AND login session.
 * Queues survive that owner's later logins; unowned legacy queues are preserved
 * separately, never silently assigned to the next person using this browser.
 */
(function () {
    'use strict';

    // app.js's t(): Store Mode loads no app.js, so the same lookup lives here.
    var catalog = null;
    function t(message) {
        if (catalog === null) {
            var el = document.getElementById('i18n-catalog');
            try { catalog = el ? JSON.parse(el.textContent) : {}; } catch (e) { catalog = {}; }
        }
        return catalog[message] || message;
    }

    var ownerId = '';
    var pageSession = '';
    var sessionTag = '';
    var DATA_KEY = '';
    var AUTH_KEY = '';
    var accessEpoch = '';
    var dataGeneration = 0;
    var QUEUE_KEY = 'shelf-store-queue';
    var QUEUE_OWNER_KEY = 'shelf-store-queue-owner';
    var OWNER_QUEUE_PREFIX = 'shelf-store-queue-owner:';
    var CACHE_MAX_AGE = 24 * 60 * 60 * 1000;
    var fetchedAt = null;     // null means no full catalogue; clock zero is valid
    var queueVerifiedAt = Object.create(null); // independently bounded per-code acknowledgements
    var storageReady = false;
    var sessionBlocked = false;
    var legacyPreserved = false;
    var canSync = false;
    var queueDatabase = null;

    var index = {};            // normalized code -> {title, authors, owned}
    var dataMeta = { generated_at: null, count: 0 };
    var scanner = null;
    var lastScan = { code: null, at: 0 };
    var deferredInstall = null;
    var currentCode = null;    // the code the verdict card is showing
    var verdictGeneration = 0;
    var inputGeneration = 0;

    function $(id) { return document.getElementById(id); }

    function csrfToken() {
        var m = document.cookie.split('; ').find(function (r) { return r.indexOf('csrf_token=') === 0; });
        return m ? decodeURIComponent(m.split('=')[1]) : '';
    }

    function normalizeCode(raw) {
        return (raw || '').toUpperCase().replace(/[^0-9X]/g, '');
    }

    function sameSession() {
        return !!ownerId && !!pageSession && csrfToken() === pageSession;
    }

    function ownsQueue() {
        try {
            return !sessionBlocked && storageReady && sameSession()
                && (localStorage.getItem(AUTH_KEY) || '') === accessEpoch
                && localStorage.getItem(QUEUE_OWNER_KEY) === ownerId;
        }
        catch (e) { return false; }
    }

    function sessionWarning(message) {
        var warning = $('store-session-warning');
        warning.textContent = message;
        warning.classList.remove('hidden');
    }

    function blockSession() {
        sessionBlocked = true;
        dataGeneration++;
        index = {};
        queueVerifiedAt = Object.create(null);
        dataMeta = {generated_at: null, count: 0};
        fetchedAt = null;
        $('status-line').removeAttribute('aria-busy');
        $('status-line').textContent = t('Library checks unavailable. Reload and sign in again. Your saved scans have been kept.');
        $('verdict').classList.add('hidden');
        $('queue-list').textContent = '';
        $('queue-count').textContent = '0';
        $('queue-section').classList.add('hidden');
        ['camera-toggle', 'check-button', 'sync-now'].forEach(function (id) {
            var button = document.querySelector('[data-testid="' + id + '"]');
            if (button) button.disabled = true;
        });
        if (scanner) { scanner.stop().catch(function () {}); scanner = null; }
        sessionWarning(t('Your sign-in changed. Reload Store Mode before using this account. Saved scans have been kept for their original owner.'));
    }

    // All tabs use the SAME readwrite transaction scope, including browsers
    // without Web Locks. Its request callback runs the whole synchronous
    // localStorage read/modify/write before the transaction releases its lock.
    // No awaits/network work inside it, no lease timeouts, and no unlocked
    // fallback. A failed transaction leaves the input available for retry.
    // localStorage remains the established queue format, not a second writer.
    function withQueueLock(operation) {
        if (!queueDatabase) {
            queueDatabase = new Promise(function (resolve, reject) {
                if (!window.indexedDB) { reject(new Error('Safe queue storage is unavailable')); return; }
                var request = window.indexedDB.open('shelf-store-coordination', 1);
                request.onupgradeneeded = function () { request.result.createObjectStore('queue-lock'); };
                request.onerror = function () { reject(request.error || new Error('Could not open safe queue storage')); };
                request.onblocked = function () { reject(new Error('Safe queue storage is blocked by another page')); };
                request.onsuccess = function () {
                    var db = request.result;
                    db.onversionchange = function () { db.close(); queueDatabase = null; };
                    resolve(db);
                };
            }).catch(function (error) { queueDatabase = null; throw error; });
        }
        return queueDatabase.then(function (db) {
            return new Promise(function (resolve, reject) {
                var transaction = db.transaction('queue-lock', 'readwrite');
                var result;
                var failure;
                transaction.oncomplete = function () { resolve(result); };
                transaction.onabort = transaction.onerror = function () {
                    reject(failure || transaction.error || new Error('Could not save the queue safely'));
                };
                var request = transaction.objectStore('queue-lock').get('exclusive');
                request.onsuccess = function () {
                    try { result = operation(); }
                    catch (error) { failure = error; transaction.abort(); }
                };
            });
        });
    }

    function queueEntryId() {
        if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
        return Date.now().toString(36) + ':' + Math.random().toString(36).slice(2) + ':' + Math.random().toString(36).slice(2);
    }

    function parseQueue(raw) {
        var queue = JSON.parse(raw || '[]');
        if (!Array.isArray(queue) || queue.some(function (entry) { return !entry || typeof entry.isbn !== 'string'; })) {
            throw new Error('Unreadable saved queue');
        }
        return queue;
    }

    function activateQueue() {
        return withQueueLock(function () {
            if (!sameSession() || (localStorage.getItem(AUTH_KEY) || '') !== accessEpoch) throw new Error('Sign-in changed');
            // The active array keeps the established queue interface. Its owner
            // marker is mandatory; inactive owners retain a separate saved array.
            // Copy to the old owner's namespace BEFORE replacing the active array.
            // If any storage operation fails, stop rather than discard a scan.
            var previous = localStorage.getItem(QUEUE_OWNER_KEY);
            if (previous !== ownerId) {
                var raw = localStorage.getItem(QUEUE_KEY);
                if (raw !== null && raw !== '[]') {
                    if (previous && /^[1-9][0-9]*$/.test(previous)) {
                        localStorage.setItem(OWNER_QUEUE_PREFIX + previous, raw);
                    } else {
                        var preserved = 'shelf-store-queue-unassigned:' + Date.now() + ':' + Math.random().toString(36).slice(2);
                        localStorage.setItem(preserved, raw);
                        legacyPreserved = true;
                    }
                } else if (previous && /^[1-9][0-9]*$/.test(previous)) {
                    localStorage.setItem(OWNER_QUEUE_PREFIX + previous, '[]');
                }
                var saved = localStorage.getItem(OWNER_QUEUE_PREFIX + ownerId);
                // A failed write must not leave another owner's array under the
                // previous owner's marker. Both archives are intact at this point.
                localStorage.setItem(QUEUE_OWNER_KEY, 'switching');
                localStorage.setItem(QUEUE_KEY, saved || '[]');
                localStorage.setItem(QUEUE_OWNER_KEY, ownerId);
            }
            // Assign durable identities under the same lock as every later write.
            // Never cap/truncate legacy overflow or remove an unacknowledged scan.
            var queue = parseQueue(localStorage.getItem(QUEUE_KEY));
            var changed = false;
            var ids = Object.create(null);
            queue.forEach(function (entry) {
                if (typeof entry.id !== 'string' || !entry.id || ids[entry.id]) {
                    entry.id = queueEntryId(); changed = true;
                }
                ids[entry.id] = true;
            });
            if (changed) localStorage.setItem(QUEUE_KEY, JSON.stringify(queue));
            storageReady = true;
        });
    }

    async function refuseAccess() {
        // A login redirect / permission refusal is not an offline failure.
        // Fence outstanding work in THIS page immediately, then publish a
        // revision under the queue lock so other tabs cannot acknowledge old
        // batches or restore old snapshots even if the CSRF cookie is unchanged.
        blockSession();
        try {
            await withQueueLock(function () {
                if (!sameSession() || (localStorage.getItem(AUTH_KEY) || '') !== accessEpoch) return;
                localStorage.removeItem(DATA_KEY);
                localStorage.setItem(AUTH_KEY, queueEntryId());
            });
        } catch (error) {
            sessionWarning(t('Sign-in could not be verified and browser storage is unavailable. Library checks are blocked. Saved scans have not been removed.'));
        }
    }

    async function fingerprintSession() {
        // Never persist the CSRF token itself in the catalogue snapshot.
        // Without WebCrypto, online document-memory checks still work; no
        // persistent catalogue snapshot is read or written.
        if (!window.crypto || !window.crypto.subtle) return '';
        var bytes = new TextEncoder().encode(pageSession);
        var digest = await window.crypto.subtle.digest('SHA-256', bytes);
        return Array.from(new Uint8Array(digest)).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
    }

    // --- Data cache -------------------------------------------------------

    function buildIndex(data) {
        index = Object.create(null);
        queueVerifiedAt = Object.create(null);
        (data.items || []).forEach(function (item) {
            (item.codes || []).forEach(function (code) {
                index[code] = item;
            });
        });
        dataMeta = { generated_at: data.generated_at, count: data.count || 0 };
    }

    function expireCatalogue() {
        var now = Date.now();
        var age = now - fetchedAt;
        var expired = false;
        if (fetchedAt !== null && (age < 0 || age > CACHE_MAX_AGE)) {
            // One queue acknowledgement must not renew an older catalogue,
            // nor lose its own independently verified remaining lifetime.
            Object.keys(index).forEach(function (code) {
                if (!Object.prototype.hasOwnProperty.call(queueVerifiedAt, code)) delete index[code];
            });
            dataMeta = {generated_at: null, count: 0}; fetchedAt = null;
            expired = true;
        }
        Object.keys(queueVerifiedAt).forEach(function (code) {
            var verifiedAge = now - queueVerifiedAt[code];
            if (verifiedAge < 0 || verifiedAge > CACHE_MAX_AGE) {
                delete queueVerifiedAt[code]; delete index[code]; expired = true;
            }
        });
        if (expired) $('verdict').classList.add('hidden');
    }

    function loadLocalData() {
        if (!ownsQueue() || !sessionTag) return;
        try {
            var raw = localStorage.getItem(DATA_KEY);
            if (!raw) return;
            var saved = JSON.parse(raw);
            var age = Date.now() - saved.fetched_at;
            if (saved.owner !== ownerId || saved.session !== sessionTag || (saved.access_epoch || '') !== accessEpoch
                || typeof saved.fetched_at !== 'number' || !Number.isFinite(age) || age < 0 || age > CACHE_MAX_AGE || !validCatalogue(saved.data)) return;
            fetchedAt = saved.fetched_at;
            buildIndex(saved.data);
        } catch (e) { /* corrupted cache — refresh will replace it */ }
    }

    function refusedResponse() {
        var error = new Error('Sign-in or server response could not be verified');
        error.refused = true;
        return error;
    }

    async function storeJSON(response, endpoint) {
        var type = response.headers.get('Content-Type') || '';
        // These API endpoints never redirect. In particular, fetch follows an
        // invalid session's 303 to /login and otherwise reports ok + HTML.
        if (response.redirected || response.type === 'opaqueredirect'
            || (response.url && (new URL(response.url).pathname !== endpoint || new URL(response.url).origin !== window.location.origin))
            || response.headers.get('HX-Redirect')
            || !/^application\/(?:[a-z0-9.+-]+\+)?json(?:\s*;|\s*$)/i.test(type)
            || response.status === 401 || response.status === 403) throw refusedResponse();
        if (!response.ok) throw new Error(response.status);
        try { return await response.json(); }
        catch (error) {
            // Response.json also rejects if a genuine connection failure or
            // abort interrupts its body stream. Only malformed JSON is an
            // unexpected server document; transport errors retain valid cache.
            if (error.name === 'SyntaxError') throw refusedResponse();
            throw error;
        }
    }

    function validCatalogue(data) {
        return data && Number.isInteger(data.count) && data.count >= 0
            && typeof data.generated_at === 'string' && Number.isFinite(Date.parse(data.generated_at))
            && Array.isArray(data.items) && data.count === data.items.length && data.items.every(function (item) {
                return item && typeof item.title === 'string' && typeof item.owned === 'boolean'
                    && (item.authors === null || typeof item.authors === 'string')
                    && Array.isArray(item.codes) && item.codes.every(function (code) { return typeof code === 'string' && !!normalizeCode(code); });
            });
    }

    function refreshData() {
        if (!ownsQueue()) { blockSession(); return Promise.resolve(); }
        if (!navigator.onLine) return Promise.resolve();
        var generation = ++dataGeneration;
        $('status-line').setAttribute('aria-busy', 'true');
        return fetch('/api/store/data', { cache: 'no-store', headers: { 'Accept': 'application/json' } })
            .then(function (r) {
                return storeJSON(r, '/api/store/data');
            })
            .then(function (data) {
                if (generation !== dataGeneration) return;
                if (!ownsQueue()) { blockSession(); return; }
                if (!validCatalogue(data)) throw refusedResponse();
                return withQueueLock(function () {
                    if (generation !== dataGeneration) return;
                    if (!ownsQueue()) { blockSession(); return; }
                    fetchedAt = Date.now();
                    if (sessionTag) {
                        try { localStorage.setItem(DATA_KEY, JSON.stringify({owner: ownerId, session: sessionTag, access_epoch: accessEpoch, fetched_at: fetchedAt, data: data})); }
                        catch (e) { sessionWarning(t('The catalogue is available in this page, but could not be saved for offline use. Your queued scans are kept.')); }
                    }
                    buildIndex(data);
                    updateStatus();
                });
            })
            .catch(function (error) {
                if (generation !== dataGeneration) return;
                if (!ownsQueue()) { blockSession(); return; }
                // An authenticated refusal may mean membership was revoked.
                // A network failure, however, must not erase offline data.
                if (error.refused) {
                    return refuseAccess();
                }
                updateStatus();
            }).finally(function () {
                if (generation === dataGeneration) $('status-line').removeAttribute('aria-busy');
            });
    }

    // --- Queue ------------------------------------------------------------

    function getQueue() {
        if (!ownsQueue()) return [];
        return parseQueue(localStorage.getItem(QUEUE_KEY));
    }

    function enqueue(code) {
        return withQueueLock(function () {
            if (!ownsQueue()) { blockSession(); throw new Error('Sign-in changed'); }
            var q = getQueue();
            if (q.some(function (e) { return e.isbn === code; })) return false;
            q.push({ id: queueEntryId(), isbn: code, at: new Date().toISOString() });
            localStorage.setItem(QUEUE_KEY, JSON.stringify(q));
            return true;
        }).then(function (added) { renderQueue(); return added; });
    }

    // One flush in flight at a time. A caller that arrives while it is out
    // gets the same promise; a scan made mid-flight is enqueued and picked up
    // by the next round. Each distinct code is attempted once per flush, so
    // invalid/unmatched responses remain queued without looping or starving
    // a later valid batch. Storage failures also settle and unlock the flight.
    var inflight = null;

    function flushQueue() {
        if (inflight) return inflight;
        if (!ownsQueue()) { blockSession(); return Promise.resolve(); }
        if (!canSync) {
            $('sync-hint').textContent = t('Catalogue editing access is required to sync. Your queued scans are kept for this account.');
            $('sync-hint').classList.remove('hidden');
            return Promise.resolve();
        }
        if (!navigator.onLine) return Promise.resolve();
        $('sync-hint').classList.add('hidden');
        $('sync-now').disabled = true;
        $('sync-now').setAttribute('aria-busy', 'true');

        var totals = { invalid: 0, restored: 0, rounds: 0, attempted: Object.create(null) };
        inflight = Promise.resolve().then(async function () {
            while (ownsQueue() && navigator.onLine) {
                var q = getQueue().filter(function (entry) { return !totals.attempted[normalizeCode(entry.isbn)]; });
                if (!q.length) break;
                var batch = q.slice(0, 50);
                batch.forEach(function (entry) { totals.attempted[normalizeCode(entry.isbn)] = true; });
                await flushRound(batch, totals);
                totals.rounds++;
            }
        }).catch(function () {
            if (ownsQueue() && $('sync-hint').classList.contains('hidden')) {
                $('sync-hint').textContent = t('Sync did not finish. Unsynced scans have been kept; retry when ready.');
                $('sync-hint').classList.remove('hidden');
            }
        }).finally(function () {
            inflight = null;
            $('sync-now').removeAttribute('aria-busy');
            $('sync-now').disabled = !ownsQueue() || !canSync;
            if (!ownsQueue()) blockSession();
            else {
                try {
                    if (totals.rounds) showSyncResult(totals.invalid, totals.restored);
                    updateStatus();
                } catch (error) {
                    sessionWarning(t('Saved scans could not be opened safely. They have not been removed. Reload after checking browser storage.'));
                }
            }
        });
        return inflight;
    }

    // Send one batch and index what the server says about each code. The
    // server echoes the string sent as `code` and the row's real `owned` on
    // every result. Only confirmed saved/existing results leave the queue.
    // Invalid/refused responses are not saved and remain retryable.
    async function flushRound(batch, totals) {
        var isbns = batch.map(function (e) { return e.isbn; });
        var data;
        try {
            var response = await fetch('/api/store/queue', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF-Token': pageSession },
                body: JSON.stringify({ isbns: isbns }),
            });
            data = await storeJSON(response, '/api/store/queue');
            if (!data || !Array.isArray(data.results)) throw refusedResponse();
        } catch (error) {
            if (error.refused && ownsQueue()) {
                await refuseAccess();
                $('sync-hint').textContent = t('Sign in and verify Store Mode access before syncing. Your queued scans are kept.');
                $('sync-hint').classList.remove('hidden');
            }
            throw error;
        }
        return withQueueLock(function () {
            if (!ownsQueue()) { blockSession(); return 0; }
            var handled = Object.create(null);
            var answers = Object.create(null);
            var batchIds = Object.create(null);
            batch.forEach(function (entry) { if (entry.id) batchIds[entry.id] = true; });
            var sent = batch.map(function (entry) { return normalizeCode(entry.isbn); });
            data.results.forEach(function (res) {
                if (!res || typeof res !== 'object') return;
                var key = normalizeCode(res.code || res.isbn);
                if (!key || sent.indexOf(key) === -1) return;
                // The strict writer saved NOTHING: do not drain or index it.
                if (res.status === 'invalid') {
                    totals.invalid++;
                    return;
                }
                if (['wishlisted', 'added_bare', 'duplicate', 'restored'].indexOf(res.status) === -1) return;
                if (typeof res.owned !== 'boolean') return;
                if (res.status === 'restored') totals.restored++;
                var entry = {
                    title: res.title || ('ISBN ' + res.isbn),
                    authors: null,
                    owned: !!res.owned,
                };
                handled[key] = true;
                answers[key] = entry;
                var isbnKey = normalizeCode(res.isbn);
                if (isbnKey) answers[isbnKey] = entry;
            });
            var before = getQueue();
            // Remove only the exact entries sent in THIS batch, not a newer
            // scan of the same code enqueued by a different tab after its own
            // successful acknowledgement. The read and removal share a lock.
            var after = before.filter(function (e) { return !(batchIds[e.id] && handled[normalizeCode(e.isbn)]); });
            if (after.length !== before.length) localStorage.setItem(QUEUE_KEY, JSON.stringify(after));
            if (Object.keys(answers).length) {
                dataGeneration++; // a manifest started before this save is stale
                $('status-line').removeAttribute('aria-busy');
                expireCatalogue();
                var verifiedAt = Date.now();
                Object.keys(answers).forEach(function (key) {
                    index[key] = answers[key];
                    queueVerifiedAt[key] = verifiedAt;
                });
            }
            renderQueue();
            // The card still showing a code this round answered now shows
            // the server's answer. A card moved on to another code is left
            // alone (G39: prove ownership before writing).
            if (currentCode && handled[currentCode] && index[currentCode]) {
                renderVerdict(currentCode);
            }
            return before.length - after.length;
        });
    }

    // Report rejected codes truthfully; the strict fork writer inserts no row.
    function showSyncResult(invalid, restored) {
        var el = $('sync-result');
        if (!el) return;
        var parts = [];
        if (invalid) {
            parts.push(t('{count} invalid barcodes were not saved. They remain queued for correction or retry.').replace('{count}', invalid));
        }
        if (restored) {
            parts.push(restored === 1
                ? t('1 restored from Trash.')
                : t('{count} restored from Trash.').replace('{count}', restored));
        }
        if (!parts.length) { el.classList.add('hidden'); el.textContent = ''; return; }
        el.textContent = parts.join(' ');
        el.classList.remove('hidden');
    }

    function renderQueue() {
        if (!ownsQueue()) { blockSession(); return; }
        var q = getQueue();
        $('queue-section').classList.toggle('hidden', q.length === 0);
        $('queue-count').textContent = q.length;
        var list = $('queue-list');
        list.textContent = '';
        // Bound rendered feedback, not saved scans. Every entry remains in
        // storage and participates in later 50-code sync batches.
        q.slice(0, 200).forEach(function (e) {
            var li = document.createElement('li');
            li.textContent = e.isbn;
            list.appendChild(li);
        });
        var overflow = $('queue-overflow');
        if (overflow) {
            overflow.classList.toggle('hidden', q.length <= 200);
            overflow.textContent = q.length > 200 ? t('Showing the first 200 scans. All saved scans remain queued and will sync.') : '';
        }
    }

    // --- Verdict ----------------------------------------------------------

    async function showVerdict(rawCode) {
        if (!ownsQueue()) { blockSession(); return false; }
        expireCatalogue();
        var code = normalizeCode(rawCode);
        if (!code) return;
        var generation = ++verdictGeneration;
        currentCode = code;
        if (renderVerdict(code)) return true;

        var note = $('verdict-note');
        note.textContent = t('Saving scan on this device…');
        var added;
        try { added = await enqueue(code); }
        catch (error) {
            if (generation === verdictGeneration) note.textContent = t('This scan was not saved. Keep the code in the input and retry.');
            throw error;
        }
        if (!ownsQueue()) { blockSession(); return false; }
        if (added && navigator.onLine) flushQueue();
        // A later camera/manual scan owns the card now. Saving this scan must
        // not repaint that newer card or clear a newer value from the input.
        if (generation !== verdictGeneration) return true;
        if (!added) {
            note.textContent = t('Already queued for your wishlist.');
        } else {
            note.textContent = navigator.onLine
                ? t('Added to sync queue — syncing now…')
                : t('Queued — will be added to your wishlist when back online.');
        }
        return true;
    }

    // Paint the card for `code` from the index. No queueing, no flush —
    // safe to call again when a flush answers the code on screen. Returns
    // whether the code is in the library.
    function renderVerdict(code) {
        var card = $('verdict');
        var label = $('verdict-label');
        var title = $('verdict-title');
        var authors = $('verdict-authors');
        var note = $('verdict-note');

        card.classList.remove('hidden', 'border-shelf-success', 'border-shelf-warning', 'border-shelf-border',
                              'bg-shelf-success/10', 'bg-shelf-warning/10', 'bg-shelf-card');
        label.classList.remove('text-shelf-success', 'text-shelf-warning', 'text-shelf-muted');
        title.textContent = ''; authors.textContent = ''; note.textContent = '';

        var hit = index[code];
        if (hit && hit.owned) {
            card.classList.add('border-shelf-success', 'bg-shelf-success/10');
            label.classList.add('text-shelf-success');
            label.textContent = t('OWNED');
            title.textContent = hit.title;
            authors.textContent = hit.authors || '';
        } else if (hit) {
            card.classList.add('border-shelf-warning', 'bg-shelf-warning/10');
            label.classList.add('text-shelf-warning');
            label.textContent = t('ON WISHLIST');
            title.textContent = hit.title;
            authors.textContent = hit.authors || '';
        } else {
            card.classList.add('border-shelf-border', 'bg-shelf-card');
            label.classList.add('text-shelf-muted');
            label.textContent = fetchedAt !== null ? t('NOT IN LIBRARY') : t('LIBRARY UNAVAILABLE');
            title.textContent = t('ISBN {code}').replace('{code}', code);
        }
        return !!hit;
    }

    // --- Camera -----------------------------------------------------------

    function toggleCamera() {
        if (!ownsQueue()) { blockSession(); return; }
        var btn = $('camera-toggle');
        var readerEl = $('reader');
        var zxingEl = $('store-zxing-container');
        if (scanner) {
            scanner.stop().catch(function () {}).then(function () {
                scanner = null;
                readerEl.classList.add('hidden');
                zxingEl.classList.add('hidden');
                btn.textContent = t('Scan with Camera');
            });
            return;
        }
        scanner = window.createBarcodeScanner({
            html5ElId: 'reader',
            videoEl: 'store-zxing-video',
            html5Config: { fps: 10, qrbox: { width: 240, height: 140 } },
            onDecode: function (decoded) {
                var now = Date.now();
                if (decoded === lastScan.code && now - lastScan.at < 3000) return;
                lastScan = { code: decoded, at: now };
                if (navigator.vibrate) navigator.vibrate(60);
                var input = $('isbn-input');
                var inputValue = input.value;
                var inputVersion = inputGeneration;
                var admission = showVerdict(decoded);
                var scanVersion = verdictGeneration;
                admission.catch(function () {
                    if (!ownsQueue()) { blockSession(); return; }
                    // Recovery owns only an unchanged, originally empty input.
                    // A newer edit/scan (even edit then clear) keeps ownership.
                    if (!inputValue && input.value === inputValue && inputGeneration === inputVersion && verdictGeneration === scanVersion) {
                        input.value = decoded;
                        inputGeneration++;
                    }
                    sessionWarning(t('Scan {code} was not saved. Retry this code after checking browser storage. Your manual entry has been kept.').replace('{code}', function () { return decoded; }));
                });
            }
        });
        var activeEl = scanner.engine === 'zxing' ? zxingEl : readerEl;
        activeEl.classList.remove('hidden');
        btn.textContent = t('Stop Camera');
        scanner.start().catch(function () {
            readerEl.classList.add('hidden');
            zxingEl.classList.add('hidden');
            btn.textContent = t('Scan with Camera');
            scanner = null;
            var note = $('verdict-note');
            var card = $('verdict');
            card.classList.remove('hidden');
            card.classList.add('border-shelf-border');
            note.textContent = t('Camera unavailable — use manual entry.');
        });
    }

    // --- PWA install ------------------------------------------------------
    // Chrome never reliably shows its own install banner; capture the event
    // and surface a button instead. Browsers without the event (iOS Safari,
    // already-installed, untrusted origin) simply never reveal the button.

    window.addEventListener('beforeinstallprompt', function (e) {
        e.preventDefault();
        deferredInstall = e;
        var btn = $('install-app');
        if (btn) btn.classList.remove('hidden');
    });

    window.addEventListener('appinstalled', function () {
        deferredInstall = null;
        var btn = $('install-app');
        if (btn) btn.classList.add('hidden');
    });

    function promptInstall() {
        if (!deferredInstall) return;
        var evt = deferredInstall;
        deferredInstall = null;
        $('install-app').classList.add('hidden');
        evt.prompt();
    }

    // --- Status -----------------------------------------------------------

    function updateStatus() {
        if (sessionBlocked || !ownsQueue()) { blockSession(); return; }
        expireCatalogue();
        var online = navigator.onLine;
        var dot = $('net-dot');
        dot.classList.toggle('bg-shelf-success', online);
        dot.classList.toggle('bg-shelf-error', !online);
        dot.classList.remove('bg-shelf-muted');

        var parts = [];
        parts.push(dataMeta.count > 0 ? t('{count} titles cached').replace('{count}', dataMeta.count) : t('No library data cached yet'));
        if (dataMeta.generated_at) {
            parts.push(t('synced {when}').replace('{when}', new Date(dataMeta.generated_at).toLocaleString()));
        }
        parts.push(online ? t('online') : t('offline'));
        $('status-line').textContent = parts.join(' · ');
        renderQueue();
    }

    // --- Init -------------------------------------------------------------

    document.addEventListener('DOMContentLoaded', async function () {
        ownerId = document.body.dataset.storeUserId || '';
        pageSession = document.body.dataset.storeSession || '';
        canSync = document.body.dataset.storeCanSync === '1';
        $('sync-now').disabled = !canSync;
        if (!/^[1-9][0-9]*$/.test(ownerId) || !sameSession()) { blockSession(); return; }
        DATA_KEY = 'shelf-store-data-owner:' + ownerId;
        AUTH_KEY = 'shelf-store-auth-owner:' + ownerId;
        try {
            accessEpoch = localStorage.getItem(AUTH_KEY) || '';
            await activateQueue();
            getQueue(); // Refuse corrupt storage before installing handlers.
            sessionTag = await fingerprintSession();
            if (!ownsQueue()) { blockSession(); return; }
        } catch (e) {
            blockSession();
            sessionWarning(t('Saved scans could not be opened safely. They have not been removed. Reload after checking browser storage.'));
            return;
        }
        loadLocalData();
        updateStatus();
        if (legacyPreserved) sessionWarning(t('An older queue without an identified owner has been preserved separately. It will not be synced into this account automatically.'));

        $('isbn-input').addEventListener('input', function () { inputGeneration++; });
        $('manual-form').addEventListener('submit', async function (e) {
            e.preventDefault();
            var v = $('isbn-input').value;
            if (!v.trim()) return;
            var inputVersion = inputGeneration;
            try {
                if (await showVerdict(v) && $('isbn-input').value === v && inputGeneration === inputVersion) {
                    $('isbn-input').value = ''; inputGeneration++;
                }
            }
            catch (err) { sessionWarning(t('This scan could not be saved. Keep the code in the input and retry after checking browser storage.')); }
        });
        $('camera-toggle').addEventListener('click', toggleCamera);
        $('sync-now').addEventListener('click', function () { flushQueue(); });
        $('install-app').addEventListener('click', promptInstall);

        window.addEventListener('online', function () { updateStatus(); flushQueue().then(refreshData); });
        window.addEventListener('offline', updateStatus);
        window.addEventListener('storage', function () {
            if (!ownsQueue()) blockSession();
            else {
                try { renderQueue(); }
                catch (error) { sessionWarning(t('Saved scans could not be opened safely. They have not been removed. Reload after checking browser storage.')); }
            }
        });
        window.addEventListener('pageshow', function () { if (!ownsQueue()) blockSession(); });
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') {
                if (!ownsQueue()) blockSession();
                else if (navigator.onLine) refreshData();
            }
        });

        flushQueue().then(refreshData, refreshData);

        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js').catch(function () {
                // Untrusted cert or unsupported browser — page still works online
            });
        }
    });
})();
