/* Native navigation owns all I/O. This optional enhancement only manages
 * applicability and permanently retires actions prepared for an older intent. */
(() => {
    'use strict';

    document.querySelectorAll('[data-testid="add-media"]').forEach((root) => {
        const search = root.querySelector('[data-testid="add-media-search"]');
        if (!search) return;

        const control = (name) => search.elements.namedItem(name);
        const musicTypes = new Set(['vinyl', 'cassette', 'cd', 'digital_music']);
        const bookTypes = new Set(['book', 'audiobook', 'ebook', 'comic', 'manga']);
        const lookupTypes = new Set([...musicTypes, ...bookTypes, 'dvd', 'video_game']);
        const intentNames = new Set(['mode', 'media_type', 'method', 'provider', 'q', 'artist', 'title_hint', 'platform', 'country']);
        const renderedValues = new Map(Array.from(intentNames, (name) => {
            const element = control(name);
            const selected = element.options && Array.from(element.options).find((option) => option.defaultSelected);
            const value = element.options ? (selected || element.options[0]).value : element.defaultValue;
            return [name, value];
        }));
        let retired = false;

        function retirePreparedActions() {
            if (retired) return;
            retired = true;
            root.querySelectorAll('form[data-add-prepared]').forEach((form) => {
                form.querySelectorAll('input, select, textarea, button, fieldset').forEach((element) => {
                    element.disabled = true;
                });
                // Also block implicit submission and requestSubmit on a stale form.
                form.addEventListener('submit', (event) => event.preventDefault());
            });
            const notice = root.querySelector('[data-add-invalidated]');
            if (notice && root.querySelector('form[data-add-prepared]')) {
                notice.textContent = root.dataset.invalidatedMessage;
                notice.hidden = false;
            }
        }

        function applyApplicability() {
            const type = control('media_type').value;
            const music = musicTypes.has(type);
            const provider = control('provider');
            const providerGroup = search.querySelector('[data-add-provider]');
            providerGroup.hidden = !music;
            Array.from(provider.options).forEach((option) => {
                option.disabled = option.value !== 'auto' && !music;
            });
            // Non-music still submits the one allowed explicit provider policy.
            if (!music) provider.value = 'auto';

            Array.from(control('method').options).forEach((option) => {
                if (!option.value) return;
                option.disabled = Boolean(type) && (
                    (option.value === 'isbn' && !bookTypes.has(type)) ||
                    (option.value === 'catalog_number' && !music) ||
                    (['title', 'barcode'].includes(option.value) && !lookupTypes.has(type))
                );
            });
            const methodSelect = control('method');
            if (methodSelect.selectedOptions.length && methodSelect.selectedOptions[0].disabled) {
                methodSelect.value = '';
            }
            const method = methodSelect.value;

            search.querySelectorAll('[data-add-types], [data-add-methods]').forEach((group) => {
                const types = (group.dataset.addTypes || '').split(' ').filter(Boolean);
                const methods = (group.dataset.addMethods || '').split(' ').filter(Boolean);
                const applicable = (!types.length || types.includes(type)) && (!methods.length || methods.includes(method));
                group.hidden = !applicable;
                group.querySelectorAll('input, select, textarea').forEach((element) => {
                    element.disabled = !applicable;
                });
            });

            const query = control('q');
            const label = search.querySelector('[data-add-query-label]');
            const labels = {
                title: label.dataset.titleLabel,
                isbn: label.dataset.isbnLabel,
                barcode: label.dataset.barcodeLabel,
                catalog_number: label.dataset.catalogNumberLabel,
                manual: label.dataset.manualLabel,
            };
            label.textContent = labels[method] || label.dataset.defaultLabel;
            query.maxLength = method === 'catalog_number' ? 100 : ['isbn', 'barcode'].includes(method) ? 32 : 200;
            query.required = ['isbn', 'barcode', 'catalog_number'].includes(method) || (method === 'title' && !music);
            control('country').maxLength = provider.value === 'discogs' ? 80 : 2;
        }

        function intentChanged(event) {
            if (!intentNames.has(event.target.name)) return;
            retirePreparedActions();
            applyApplicability();
        }

        search.addEventListener('input', intentChanged);
        search.addEventListener('change', intentChanged);
        // A reset is also an intent change; it must never revive an old action.
        search.addEventListener('reset', () => {
            retirePreparedActions();
            queueMicrotask(checkRestoredIntent);
        });
        root.querySelectorAll('form[data-add-prepared]').forEach((form) => {
            form.addEventListener('submit', (event) => {
                // Compare before normalization can hide an eventless edit.
                if (Array.from(renderedValues).some(([name, value]) => control(name).value !== value)) {
                    retirePreparedActions();
                }
                if (retired) event.preventDefault();
            }, true);
        });
        function checkRestoredIntent() {
            if (!root.isConnected) return;
            // History/autofill can restore values without input/change events.
            if (Array.from(renderedValues).some(([name, value]) => control(name).value !== value)) {
                retirePreparedActions();
            }
            applyApplicability();
        }
        window.addEventListener('pageshow', checkRestoredIntent);
        checkRestoredIntent();
    });
})();
