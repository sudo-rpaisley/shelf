"""Regression coverage for the top-right account menu."""

from html.parser import HTMLParser


class _DOM(HTMLParser):
    """Locate real element spans without template comments or sibling leakage."""

    _VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.html = html
        self.nodes = []
        self.stack = []
        self.offsets = [0]
        for line in html.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.feed(html)
        self.close()

    def _offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        start = self._offset()
        node = {"tag": tag, "attrs": dict(attrs), "start": start,
                "end": start + len(self.get_starttag_text()),
                "parent": self.stack[-1] if self.stack else None}
        self.nodes.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                self.stack[index]["end"] = self.html.index(">", self._offset()) + 1
                del self.stack[index:]
                return

    def testid(self, value):
        matches = [n for n in self.nodes if n["attrs"].get("data-testid") == value]
        assert len(matches) == 1, (value, len(matches))
        return matches[0]


def _section(html: str, start: str, end: str) -> str:
    """Smallest actual DOM region containing the two named data-testid nodes.

    The same id twice selects precisely that element, including nested forms.
    Distinct ids select their closest common ancestor (desktop Add/More).
    """
    dom = _DOM(html)
    first, last = dom.testid(start), dom.testid(end)
    node = first
    while not (node["start"] <= last["start"] and last["end"] <= node["end"]):
        node = node["parent"]
        assert node is not None, (start, end)
    return html[node["start"]:node["end"]]


def test_admin_account_menu_contains_settings_logs_and_sign_out(admin_client, db):
    html = admin_client.get("/browse").text

    assert 'data-testid="account-menu-button"' in html
    assert 'data-testid="account-menu-panel"' in html
    assert 'data-testid="account-profile-action"' in html

    account = _section(html, "account-menu-panel", "account-menu-panel")
    assert 'href="/settings"' in account
    assert 'data-nav-tab="settings"' in account
    assert 'href="/logs"' in account
    assert 'data-nav-tab="logs"' in account
    assert 'action="/logout"' in account
    assert "Sign out" in account


def test_account_admin_actions_are_not_in_mobile_library_menu(admin_client, db):
    html = admin_client.get("/browse").text
    mobile = _section(html, "nav-menu-panel", "nav-menu-panel")

    assert 'href="/browse"' in mobile
    assert 'href="/settings"' not in mobile
    assert 'href="/logs"' not in mobile


def test_viewer_account_menu_omits_admin_destinations(viewer_client, db):
    html = viewer_client.get("/browse").text
    account = _section(html, "account-menu-panel", "account-menu-panel")

    assert "Account" in account
    assert "Keyboard shortcuts" in account
    assert "Sign out" in account
    assert 'href="/settings"' not in account
    assert 'href="/logs"' not in account


def test_account_menu_uses_csp_registered_component(admin_client, db):
    html = admin_client.get("/browse").text
    assert 'x-data="accountMenu"' in html
    assert "Alpine.data('accountMenu'" in open("static/js/components.js", encoding="utf-8").read()

    desktop = _section(html, "nav-add-button", "nav-more-button")
    assert 'data-testid="nav-add-button"' in desktop
    assert 'data-testid="nav-add-panel"' in desktop
    assert 'data-testid="nav-more-button"' in desktop
    assert 'data-testid="nav-more-panel"' in desktop
    assert '<details' not in desktop
    assert desktop.count('x-data="navMenu"') == 2

    account = _section(html, "account-menu-panel", "account-menu-panel")
    assert 'data-testid="account-shortcuts-action"' in account
    assert 'title="Keyboard shortcuts (?)"' not in html
    assert 'id="shortcut-modal" role="dialog"' in html
    assert 'hidden flex items-center justify-center' in html


def test_dom_regions_do_not_leak_adjacent_account_controls():
    html = ('<nav><div data-testid="nav-menu-panel"><div><a href="/browse">Browse</a></div></div>'
            '<div data-testid="account-menu-panel"><form action="/logout">'
            '<input type="hidden" name="_csrf" value="csrf-value"><button>Sign out</button>'
            '</form><a href="/settings">Settings</a></div></nav>')
    mobile = _section(html, "nav-menu-panel", "nav-menu-panel")
    account = _section(html, "account-menu-panel", "account-menu-panel")
    assert 'href="/browse"' in mobile
    assert 'href="/settings"' not in mobile
    assert 'action="/logout"' not in mobile
    assert 'href="/settings"' in account
    assert 'name="_csrf"' in account
    assert 'href="/browse"' not in account
