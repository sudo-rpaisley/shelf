"""Synchronous finite-logic controls using the original, unmodified Node double.

Native queued close events and blur behavior are covered in the additive browser
module, not inferred from this double's synchronous close implementation.
"""

import pytest

from tests.test_command_palette import _node, render_component


def test_synchronous_close_cleans_state_and_rearms_each_opening():
    _node(render_component(), r"""
const heading = document.querySelector('main').children.find(element => element.tagName === 'H1');
heading.tabIndex = 0;
for (let opening = 0; opening < 2; opening++) {
    heading.focus();
    emit(heading, 'keydown', {key: 'k', ctrlKey: true});
    assert.equal(dialog.open, true);
    assert.equal(document.activeElement, input);
    assert.equal(launch.getAttribute('aria-expanded'), 'true');
    assert.equal(input.getAttribute('aria-expanded'), 'true');
    assert.ok(input.hasAttribute('aria-activedescendant'));
    key('Escape');
    assert.equal(dialog.open, false);
    assert.equal(document.activeElement, heading);
    assert.equal(launch.getAttribute('aria-expanded'), 'false');
    assert.equal(input.getAttribute('aria-expanded'), 'false');
    assert.equal(input.hasAttribute('aria-activedescendant'), false);
}
assert.deepEqual(navigations, []);
""")


def test_synchronous_close_preserves_deliberately_acquired_visible_outside_focus():
    _node(render_component(), r"""
open();
const heading = document.querySelector('main').children.find(element => element.tagName === 'H1');
heading.tabIndex = 0;
heading.focus();
dialog.close();
assert.equal(document.activeElement, heading);
assert.equal(launch.getAttribute('aria-expanded'), 'false');
assert.equal(input.getAttribute('aria-expanded'), 'false');
assert.equal(input.hasAttribute('aria-activedescendant'), false);
assert.deepEqual(navigations, []);
""")


@pytest.mark.parametrize("target", ["opener", "launcher", "browse", "main"])
def test_synchronous_close_reaches_each_visible_fallback_independently(target):
    _node(render_component(), r"""
const heading = document.querySelector('main').children.find(element => element.tagName === 'H1');
const primary = nav.querySelector('[data-nav-primary="browse"]');
const main = document.querySelector('main');
heading.tabIndex = 0;
heading.focus();
emit(heading, 'keydown', {key: 'k', ctrlKey: true});
assert.equal(dialog.open, true);
assert.equal(document.activeElement, input);
if (data.target !== 'opener') heading.remove();
if (['browse', 'main'].includes(data.target)) launch.remove();
if (data.target === 'main') primary.remove();
key('Escape');
const expected = {opener: heading, launcher: launch, browse: primary, main}[data.target];
assert.equal(document.activeElement, expected);
assert.equal(dialog.open, false);
assert.equal(launch.getAttribute('aria-expanded'), 'false');
assert.equal(input.getAttribute('aria-expanded'), 'false');
assert.equal(input.hasAttribute('aria-activedescendant'), false);
if (data.target === 'main') assert.equal(main.getAttribute('tabindex'), '-1');
assert.deepEqual(navigations, []);
""", target=target)


@pytest.mark.parametrize("unavailable", ["hidden", "inert", "aria-hidden", "removed"])
def test_synchronous_close_does_not_preserve_unavailable_outside_targets(unavailable):
    _node(render_component(), r"""
open();
const heading = document.querySelector('main').children.find(element => element.tagName === 'H1');
heading.tabIndex = 0;
heading.focus();
if (data.unavailable === 'removed') heading.remove();
else heading.setAttribute(data.unavailable, data.unavailable === 'aria-hidden' ? 'true' : '');
dialog.close();
assert.equal(document.activeElement, launch);
assert.equal(launch.getAttribute('aria-expanded'), 'false');
assert.equal(input.getAttribute('aria-expanded'), 'false');
assert.deepEqual(navigations, []);
""", unavailable=unavailable)


def test_synchronous_main_fallback_preserves_an_existing_tabindex():
    _node(render_component(), r"""
open();
const main = document.querySelector('main');
main.setAttribute('tabindex', '0');
launch.remove();
nav.querySelector('[data-nav-primary="browse"]').remove();
key('Escape');
assert.equal(document.activeElement, main);
assert.equal(main.getAttribute('tabindex'), '0');
assert.equal(input.getAttribute('aria-expanded'), 'false');
assert.deepEqual(navigations, []);
""")
