"""Bounded marker ownership in a Node DOM double, not browser/layout proof."""

import json
import subprocess

import pytest

from tests.test_command_palette import DOM, NODE_DOUBLE, ROOT, render_component


def _logic(setup, assertions, *, empty=False):
    anchor = "vm.runInNewContext(source, sandbox);"
    assert NODE_DOUBLE.count(anchor) == 1
    program = NODE_DOUBLE.replace(anchor, setup + "\n" + anchor, 1)
    completed = subprocess.run(
        ["node", "-e", program + assertions + "\nconsole.log('fallback marker logic only');"],
        input=json.dumps({"tree": DOM(render_component(empty=empty)).root.serialize()}),
        capture_output=True, text=True, timeout=25, cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "fallback marker logic only" in completed.stdout


def test_unavailable_marker_is_idempotent_does_not_change_disclosures_and_clears_on_real_recovery():
    _logic(r"""
const originalShowModal = Element.prototype.showModal;
Element.prototype.showModal = undefined;
const beforeDetails = document.querySelectorAll('details').map(node => node.open);
const beforeAnchors = document.querySelectorAll('a').map(node => [node.textContent, node.getAttribute('href')]);
""", r"""
assert.equal(nav.getAttribute('data-nav-palette-fallback'), 'unavailable');
assert.notEqual(root.getAttribute('data-command-palette-enhanced'), 'true');
assert.equal(launch.hidden, true);
assert.equal(root.querySelector('[data-command-palette-fallback]').hidden, false);
for (let i=0; i<3; i++) vm.runInNewContext(source, sandbox);
assert.equal(nav.getAttribute('data-nav-palette-fallback'), 'unavailable');
assert.deepEqual(document.querySelectorAll('details').map(node => node.open), beforeDetails);
assert.deepEqual(document.querySelectorAll('a').map(node => [node.textContent, node.getAttribute('href')]), beforeAnchors);
assert.equal((window.listeners.keydown || []).length, 0);
Element.prototype.showModal = originalShowModal;
vm.runInNewContext(source, sandbox);
assert.equal(nav.hasAttribute('data-nav-palette-fallback'), false);
assert.equal(root.getAttribute('data-command-palette-enhanced'), 'true');
for (let i=0; i<3; i++) vm.runInNewContext(source, sandbox);
assert.equal((window.listeners.keydown || []).length, 1);
assert.deepEqual(document.querySelectorAll('details').map(node => node.open), beforeDetails);
""")


def test_successful_initialization_removes_only_the_owned_unavailable_marker():
    _logic(r"""
document.querySelector('[data-media-navigation]').setAttribute('data-nav-palette-fallback', 'unavailable');
""", r"""
assert.equal(root.getAttribute('data-command-palette-enhanced'), 'true');
assert.equal(nav.hasAttribute('data-nav-palette-fallback'), false);
""")


@pytest.mark.parametrize("part", ("dialog", "query", "launcher"))
def test_incomplete_component_is_not_mislabeled_as_missing_native_capability(part):
    setup = ("Element.prototype.showModal = undefined;\n"
             f"document.querySelector('[data-command-palette-{part}]').remove();")
    _logic(setup, r"""
assert.equal(nav.hasAttribute('data-nav-palette-fallback'), false);
assert.notEqual(root.getAttribute('data-command-palette-enhanced'), 'true');
""")


def test_unavailable_empty_navigation_never_gains_an_anchor_or_guessed_destination():
    _logic(r"""
Element.prototype.showModal = undefined;
const beforeAnchors = document.querySelectorAll('a').map(node => node.getAttribute('href'));
""", r"""
assert.equal(nav.getAttribute('data-nav-palette-fallback'), 'unavailable');
assert.equal(nav.querySelector('[data-nav-primary="browse"]'), null);
assert.deepEqual(document.querySelectorAll('a').map(node => node.getAttribute('href')), beforeAnchors);
""", empty=True)
