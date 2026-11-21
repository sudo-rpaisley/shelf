"""The camera error classifier asks the browser, never the URL.

`http://127.0.0.1` and `http://localhost` are secure contexts, and behind a
TLS-terminating proxy the page's own scheme is the proxy's, so a check on
`location.protocol` misreports a permission denial as "requires HTTPS". The
E2E test in tests/e2e/test_scan.py covers the Scan page at runtime; this pins
every site, including the two on the item edit page.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HTTPS_TOAST = "Camera requires HTTPS"


def _first_party_sources(root: Path = _ROOT):
    yield from sorted((root / "static/js").glob("*.js"))
    yield from sorted((root / "app/templates").rglob("*.html"))


def find_violations(root: Path = _ROOT) -> list[str]:
    violations = []
    for path in _first_party_sources(root):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root)
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r"location\.protocol", line):
                violations.append(f"{rel}:{lineno}: uses location.protocol — ask window.isSecureContext")
        if _HTTPS_TOAST in text and "window.isSecureContext" not in text:
            violations.append(f"{rel}: shows '{_HTTPS_TOAST}' without consulting window.isSecureContext")
    return violations


def test_camera_classifier_asks_the_browser():
    violations = find_violations()
    assert not violations, "\n" + "\n".join(violations)


def test_every_https_toast_site_is_found():
    """Guard the guard: the three known classifier sites stay in scope."""
    counts = {
        p.name: p.read_text(encoding="utf-8").count("window.isSecureContext")
        for p in _first_party_sources()
    }
    assert counts["scan.js"] >= 1
    assert counts["item_edit.js"] >= 2


def test_lint_catches_the_old_heuristic(tmp_path):
    (tmp_path / "static/js").mkdir(parents=True)
    (tmp_path / "app/templates").mkdir(parents=True)
    (tmp_path / "static/js/bad.js").write_text(
        "if (location.protocol !== 'https:' && location.hostname !== 'localhost') {\n"
        "  showToast('Camera requires HTTPS.', 'error');\n"
        "}\n"
    )
    (tmp_path / "static/js/ok.js").write_text(
        "if (!window.isSecureContext) { showToast('Camera requires HTTPS.', 'error'); }\n"
    )
    violations = find_violations(tmp_path)
    assert len(violations) == 2
    assert all("bad.js" in v for v in violations)
