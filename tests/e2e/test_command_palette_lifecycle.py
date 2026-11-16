"""Real native-close lifecycle regressions over the unchanged real shell helpers.

Synthetic keys deliberately force controlled schedules. They do not reconstruct
the historical retained line436 schedule or attribute it to the help/CSP change.
No dialog/focus/native method is replaced and no close event is fabricated.
"""

import json
from contextlib import contextmanager

import pytest
from playwright.sync_api import expect

from tests.e2e.test_command_palette import _open, _shell


pytestmark = pytest.mark.e2e

CSP_PROBE = """
window.__paletteLifecycleCsp = [];
document.addEventListener('securitypolicyviolation', event => {
    window.__paletteLifecycleCsp.push({
        url: document.URL, directive: event.violatedDirective,
        blockedURI: event.blockedURI, sourceFile: event.sourceFile,
        lineNumber: event.lineNumber
    });
}, {passive: true});
"""


class _CspObservedBrowser:
    """Only add a passive init script before _shell constructs/navigates its Page.

    Delegate to the real browser/context unchanged; do not replace fixture or
    browser methods, request serving, markup, native dialog APIs, or page guards.
    """

    def __init__(self, browser):
        self.browser = browser

    def new_context(self, **kwargs):
        context = self.browser.new_context(**kwargs)
        context.add_init_script(CSP_PROBE)
        return context


@contextmanager
def _observed_shell(browser):
    with _shell(_CspObservedBrowser(browser)) as (page, requests):
        yield page, requests
        violations = page.evaluate("window.__paletteLifecycleCsp")
        assert violations == [], f"Whole-page CSP violations: {violations}"
        # Returning through the original _shell also reaches its external
        # request assertion and original assert_page_clean before destruction.
    print("PALETTE_LIFECYCLE_GUARDS " + json.dumps({
        "whole_page_csp": "passed", "original_shell_page_guard": "passed",
        "original_shell_external_request_guard": "passed",
    }))


OBSERVE_LIFECYCLE = """() => {
    const d = document.querySelector('[data-command-palette-dialog]');
    const q = document.querySelector('[data-command-palette-query]');
    const l = document.querySelector('[data-command-palette-launcher]');
    const h = document.querySelector('#host-heading');
    if (!d.open || document.activeElement !== q)
        throw Error('Normal _open precondition failed');
    const mutations = [], focus = [], closes = [];
    let observedOpen = d.open;
    function target(element) {
        if (element === l) return 'launcher';
        if (element === q) return 'query';
        if (element === d) return 'dialog';
        return element.id || element.tagName;
    }
    function record(records) {
        records.forEach((mutation, index) => {
            // MutationObserver delivery may batch close/reopen/close in one
            // task. Reconstruct each new value from the next old value, not
            // the final attribute or dialog.open at callback-delivery time.
            const next = records.slice(index + 1).find(candidate =>
                candidate.target === mutation.target && candidate.attributeName === mutation.attributeName);
            const value = next ? next.oldValue : mutation.target.getAttribute(mutation.attributeName);
            if (mutation.target === d && mutation.attributeName === 'open') observedOpen = value !== null;
            mutations.push({target: target(mutation.target), attribute: mutation.attributeName,
                oldValue: mutation.oldValue, value, open: observedOpen});
        });
    }
    const observer = new MutationObserver(record);
    [d, q, l].forEach(element => observer.observe(element, {
        attributes: true, attributeOldValue: true,
        attributeFilter: ['open', 'aria-expanded', 'aria-activedescendant']
    }));
    ['focusin', 'focusout'].forEach(type => document.addEventListener(type, event => {
        focus.push({type, target: target(event.target), active: target(document.activeElement), open: d.open});
    }, {capture: true, passive: true}));
    function snapshot() {
        record(observer.takeRecords());
        return {closes: closes.slice(), mutations: mutations.slice(), focus: focus.slice(),
            closedCleanup: {
                launcher: mutations.filter(m => m.target === 'launcher' && m.attribute === 'aria-expanded' && m.value === 'false' && !m.open).length,
                query: mutations.filter(m => m.target === 'query' && m.attribute === 'aria-expanded' && m.value === 'false' && !m.open).length
            },
            falseWritesWhileOpen: mutations.filter(m => m.attribute === 'aria-expanded' && m.value === 'false' && m.open),
            active: target(document.activeElement), headingFocused: document.activeElement === h,
            documentFocused: document.hasFocus(), open: d.open,
            launcherExpanded: l.getAttribute('aria-expanded'),
            queryExpanded: q.getAttribute('aria-expanded'),
            activeDescendant: q.getAttribute('aria-activedescendant'),
            hostKeys: rf2Host.keys.slice(),
            browse: {select: rf2Host.browse.selectMode, ids: rf2Host.browse.selectedIds.slice(),
                views: rf2Host.views.slice(), clears: rf2Host.clears, filters: rf2Host.browse.filtersOpen}};
    }
    // Installed after the real app listener: these are after-cleanup records,
    // not capture-listener microtasks that might precede the app handler.
    d.addEventListener('close', event => {
        record(observer.takeRecords());
        closes.push({trusted: event.isTrusted, headingFocused: document.activeElement === h,
            active: target(document.activeElement), documentFocused: document.hasFocus(),
            open: d.open, launcherExpanded: l.getAttribute('aria-expanded'),
            queryExpanded: q.getAttribute('aria-expanded'),
            activeDescendant: q.getAttribute('aria-activedescendant'), focusCount: focus.length});
    }, {passive: true});
    window.__paletteLifecycle = {snapshot};
}"""


# The contract's exact two-native-close witness; only the outer evaluate wrapper
# and indentation differ. Additional passive observers are installed beforehand.
TWO_NATIVE_CLOSES = """() => {
const d = document.querySelector('[data-command-palette-dialog]');
const q = document.querySelector('[data-command-palette-query]');
const h = document.querySelector('#host-heading');
const closes = [];
return new Promise(resolve => {
    d.addEventListener('close', () => {
        closes.push({headingFocused: document.activeElement === h,
                     activeId: document.activeElement.id,
                     documentFocused: document.hasFocus(), open: d.open});
        if (closes.length === 2) resolve(closes);
    });
    d.close();
    h.tabIndex = 0; h.focus();
    if (document.activeElement !== h) throw Error('Heading setup failed');
    h.dispatchEvent(new KeyboardEvent('keydown', {
        key:'k', ctrlKey:true, bubbles:true, cancelable:true}));
    if (!d.open || document.activeElement !== q)
        throw Error('Shortcut-open precondition failed');
    q.dispatchEvent(new KeyboardEvent('keydown', {
        key:'Escape', bubbles:true, cancelable:true}));
    if (d.open) throw Error('Escape-close precondition failed');
});
}"""


def _receipt(page, label):
    observed = page.evaluate("() => window.__paletteLifecycle.snapshot()")
    print("PALETTE_LIFECYCLE_OBSERVATION " + json.dumps({
        "label": label, "observed": observed,
        "csp": page.evaluate("window.__paletteLifecycleCsp"),
        "page_errors": list(getattr(page, "_shelf_page_errors")),
    }))
    return observed


def _assert_cleanup(observed, count):
    assert observed["closedCleanup"] == {"launcher": count, "query": count}
    assert observed["falseWritesWhileOpen"] == []
    assert observed["open"] is False
    assert observed["launcherExpanded"] == "false"
    assert observed["queryExpanded"] == "false"
    assert observed["activeDescendant"] is None
    assert observed["documentFocused"] is True
    assert all(close["trusted"] for close in observed["closes"])
    assert observed["browse"] == {"select": True, "ids": [17], "views": [], "clears": 0, "filters": False}


def _close_at_native_barrier(page):
    return page.evaluate("""() => {
        const d = document.querySelector('[data-command-palette-dialog]');
        if (!d.open) throw Error('Close precondition failed');
        return new Promise(resolve => {
            d.addEventListener('close', event => resolve({trusted: event.isTrusted,
                snapshot: window.__paletteLifecycle.snapshot()}), {once: true});
            d.close();
            if (d.open) throw Error('Native close precondition failed');
        });
    }""")


def test_two_native_closes_cleanup_once_without_an_extra_focus_transition(browser):
    with _observed_shell(browser) as (page, requests):
        _open(page)
        before = list(requests)
        page.evaluate(OBSERVE_LIFECYCLE)
        closes = page.evaluate(TWO_NATIVE_CLOSES)
        observed = _receipt(page, "two-native-closes-controlled-synthetic-keys")
        assert len(observed["closes"]) == 2
        assert all(close["trusted"] for close in observed["closes"])
        assert closes == [{"headingFocused": True, "activeId": "host-heading",
                           "documentFocused": True, "open": False}] * 2
        expect(page.locator("#host-heading")).to_be_focused()
        _assert_cleanup(observed, 1)
        assert observed["closes"][1]["focusCount"] == observed["closes"][0]["focusCount"]
        assert observed["hostKeys"] == ["k"]
        assert requests == before


def test_one_native_close_preserves_deliberately_acquired_outside_focus(browser):
    with _observed_shell(browser) as (page, requests):
        _open(page)
        before = list(requests)
        page.evaluate(OBSERVE_LIFECYCLE)
        closed = page.evaluate("""() => {
            const d = document.querySelector('[data-command-palette-dialog]');
            const h = document.querySelector('#host-heading');
            if (!d.open) throw Error('Normal-open precondition failed');
            return new Promise(resolve => {
                d.addEventListener('close', event => resolve({trusted: event.isTrusted,
                    headingFocused: document.activeElement === h,
                    documentFocused: document.hasFocus(), open: d.open}), {once: true});
                d.close();
                h.tabIndex = 0; h.focus();
                if (document.activeElement !== h) throw Error('Heading setup failed');
            });
        }""")
        observed = _receipt(page, "one-native-close-deliberate-outside-focus")
        assert closed == {"trusted": True, "headingFocused": True, "documentFocused": True, "open": False}
        expect(page.locator("#host-heading")).to_be_focused()
        _assert_cleanup(observed, 1)
        assert [event["target"] for event in observed["focus"] if event["type"] == "focusin"] == ["launcher", "host-heading"]
        assert observed["hostKeys"] == []
        assert requests == before


def test_stale_native_close_is_inert_while_reopened_and_cleanup_rearms(browser):
    with _observed_shell(browser) as (page, requests):
        _open(page)
        before = list(requests)
        page.evaluate(OBSERVE_LIFECYCLE)
        reopened = page.evaluate("""() => {
            const d = document.querySelector('[data-command-palette-dialog]');
            const q = document.querySelector('[data-command-palette-query]');
            const h = document.querySelector('#host-heading');
            if (!d.open || document.activeElement !== q) throw Error('Normal-open precondition failed');
            return new Promise(resolve => {
                let beforeEvent;
                d.addEventListener('close', event => resolve({trusted: event.isTrusted,
                    beforeEvent, afterEvent: window.__paletteLifecycle.snapshot()}), {once: true});
                d.close();
                h.tabIndex = 0; h.focus();
                if (document.activeElement !== h) throw Error('Heading setup failed');
                h.dispatchEvent(new KeyboardEvent('keydown', {key: 'k', ctrlKey: true, bubbles: true, cancelable: true}));
                if (!d.open || document.activeElement !== q) throw Error('Reopen precondition failed');
                beforeEvent = window.__paletteLifecycle.snapshot();
            });
        }""")
        observed = _receipt(page, "stale-native-close-while-reopened")
        assert reopened["trusted"] is True
        assert observed["open"] is True
        assert observed["active"] == "query"
        assert observed["launcherExpanded"] == observed["queryExpanded"] == "true"
        assert observed["activeDescendant"] is not None
        assert observed["closedCleanup"] == {"launcher": 0, "query": 0}
        assert observed["falseWritesWhileOpen"] == []
        assert observed["focus"] == reopened["beforeEvent"]["focus"]
        assert observed["closes"] == reopened["afterEvent"]["closes"]
        closed = _close_at_native_barrier(page)
        assert closed["trusted"] is True
        first = _receipt(page, "reopened-session-closed-once")
        _assert_cleanup(first, 1)
        expect(page.locator("#host-heading")).to_be_focused()
        _open(page)
        expect(page.locator("[data-command-palette-query]")).to_have_attribute("aria-expanded", "true")
        assert _close_at_native_barrier(page)["trusted"] is True
        second = _receipt(page, "subsequent-opening-rearmed")
        _assert_cleanup(second, 2)
        assert len(second["closes"]) == 3
        expect(page.locator("[data-command-palette-launcher]")).to_be_focused()
        assert requests == before


@pytest.mark.parametrize("target", ["opener", "launcher", "browse", "main"])
def test_native_close_reaches_each_focus_fallback_independently(browser, target):
    with _observed_shell(browser) as (page, requests):
        before = list(requests)
        page.locator("#host-heading").evaluate("e => {e.tabIndex = 0; e.focus();}")
        page.keyboard.press("Control+k")
        expect(page.locator("[data-command-palette-query]")).to_be_focused()
        page.evaluate(OBSERVE_LIFECYCLE)
        if target != "opener":
            page.locator("#host-heading").evaluate("e => e.remove()")
        if target in {"browse", "main"}:
            page.locator("[data-command-palette-launcher]").evaluate("e => e.remove()")
        if target == "main":
            page.locator('[data-nav-primary="browse"]').evaluate("e => e.remove()")
        assert _close_at_native_barrier(page)["trusted"] is True
        observed = _receipt(page, "independent-fallback-" + target)
        _assert_cleanup(observed, 1)
        assert len(observed["closes"]) == 1
        selector = {"opener": "#host-heading", "launcher": "[data-command-palette-launcher]",
                    "browse": '[data-nav-primary="browse"]', "main": "main"}[target]
        expect(page.locator(selector)).to_be_focused()
        if target == "main":
            expect(page.locator("main")).to_have_attribute("tabindex", "-1")
            page.locator("#host-query").focus()
            expect(page.locator("main")).not_to_have_attribute("tabindex")
        assert requests == before


def test_native_main_fallback_does_not_own_a_preexisting_tabindex(browser):
    with _observed_shell(browser) as (page, requests):
        _open(page)
        before = list(requests)
        page.evaluate(OBSERVE_LIFECYCLE)
        page.locator("main").evaluate("e => e.tabIndex = 0")
        page.locator("[data-command-palette-launcher]").evaluate("e => e.remove()")
        page.locator('[data-nav-primary="browse"]').evaluate("e => e.remove()")
        assert _close_at_native_barrier(page)["trusted"] is True
        observed = _receipt(page, "main-preexisting-tabindex")
        _assert_cleanup(observed, 1)
        expect(page.locator("main")).to_be_focused()
        expect(page.locator("main")).to_have_attribute("tabindex", "0")
        page.locator("#host-query").focus()
        expect(page.locator("main")).to_have_attribute("tabindex", "0")
        assert requests == before
