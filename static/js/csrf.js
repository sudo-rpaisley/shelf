// Read the csrf_token cookie. Used by HTMX hook below and by raw fetch() callers.
window.csrfToken = function() {
    const match = document.cookie.split('; ').find(r => r.startsWith('csrf_token='));
    return match ? decodeURIComponent(match.split('=')[1]) : '';
};
// Attach CSRF token to every HTMX state-mutating request (double-submit cookie)
document.addEventListener('htmx:configRequest', function(evt) {
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(evt.detail.verb.toUpperCase())) {
        const match = document.cookie.split('; ').find(r => r.startsWith('csrf_token='));
        if (match) {
            evt.detail.headers['X-CSRF-Token'] = match.split('=')[1];
        }
    }
});
// Inject _csrf hidden field into plain HTML POST forms (non-HTMX). Runs on
// DOMContentLoaded and again on every htmx:load, because a fragment swapped in
// later (e.g. the lazy-loaded Discogs panel) carries forms the first pass
// never saw — and a plain form without the field is a 403.
function injectCsrfFields(root) {
    const csrfValue = window.csrfToken();
    if (!csrfValue || !root || !root.querySelectorAll) return;
    const selector = 'form[method="post"], form[method="POST"]';
    const forms = Array.from(root.querySelectorAll(selector));
    if (root.matches && root.matches(selector)) forms.push(root);
    forms.forEach(function(form) {
        if (!form.querySelector('input[name="_csrf"]')) {
            var input = document.createElement('input');
            input.type = 'hidden'; input.name = '_csrf'; input.value = csrfValue;
            form.appendChild(input);
        }
    });
}
document.addEventListener('DOMContentLoaded', function() { injectCsrfFields(document); });
document.addEventListener('htmx:load', function(evt) { injectCsrfFields(evt.detail.elt); });
