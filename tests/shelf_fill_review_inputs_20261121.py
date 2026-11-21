"""Standalone Jinja/owned-AST input checks. No application imports or startup.

This checks rendered continuation/receipt contracts and source-level transaction
containment, NOT actual FastAPI dispatch, SQLite writers or authorization. The
separate route regression source must run after the parent's freeze.

Run: /tmp/shelf-main-integration-20261120/venv/bin/python tests/shelf_fill_review_inputs_20261121.py
"""

import ast
from html.parser import HTMLParser
import os
from pathlib import Path
from types import SimpleNamespace
import traceback

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("SHELF_FILL_REVIEW_SOURCE_ROOT", ROOT))
# The shared CSRF include is an existing interface, read but never edited here.
ENV = Environment(loader=FileSystemLoader([SOURCE / "app/templates", ROOT / "app/templates"]),
                  autoescape=select_autoescape(["html"]))
ENV.globals.update({"_": lambda message, **values: message % values if values else message,
                    "cover_shape": lambda media: "portrait",
                    "status_labels": lambda media: SimpleNamespace(done_lower="read")})


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.select = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"attrs": attrs, "fields": {}}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and attrs.get("name"):
            self.current["fields"][attrs["name"]] = attrs.get("value", "")
        elif tag == "select" and self.current is not None:
            self.select = attrs.get("name")
        elif tag == "option" and self.current is not None and self.select:
            if self.select not in self.current["fields"] or "selected" in attrs:
                self.current["fields"][self.select] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None
        elif tag == "select":
            self.select = None


def render(name, **context):
    return ENV.get_template("fragments/" + name + ".html").render(
        request=SimpleNamespace(cookies={"csrf_token": "actual-cookie-token"}),
        csrf_token="must-not-use-stale-context-token", **context,
    )


def shared():
    return {"location_id": 7, "selected_location_id": 7, "location_node_id": 41,
            "shelf_fill_origin": "scan", "library_id": 2, "tags": "Signed; Freely typed",
            "reading_status": "read", "mode": "add", "locations": [{"id": 7, "name": "Flat root"}]}


def assert_fields(form, **fields):
    for key, value in fields.items():
        assert form["fields"].get(key) == str(value), (form, key, value)


def native_csrf(form):
    assert form["attrs"].get("method") == "post", form
    assert_fields(form, _csrf="actual-cookie-token")


def test_native_scan_continuations_have_cookie_csrf_and_distinct_node():
    for status in ("legacy_incomplete", "legacy_ambiguous", "in_trash", "saved_unplaced"):
        html = render("scan_result", status=status, isbn="barcode", title="Saved title", item_id=917,
                      media_type="book", message="placement refused", deleted_at="2026-01-01",
                      legacy_candidates=[{"isbn13": "9780306406157", "title": "Candidate"}],
                      scan_action="/api/shelf-fill/scan", **shared())
        forms = Forms(html).forms
        assert forms, status
        for form in forms:
            native_csrf(form)
            assert_fields(form, location_node_id=41, shelf_fill_origin="scan")
        if status == "saved_unplaced":
            assert 'data-saved-item-id="917"' in html
            assert 'data-scan-status="saved_unplaced"' in html
            assert_fields(forms[0], item_id=917)
            assert forms[0]["attrs"]["action"] == "/api/shelf-fill/place"
            assert "isbn" not in forms[0]["fields"], "retry must not re-Add"


def test_periodical_forms_preserve_defaults_and_native_post():
    html = render("periodical_scan_result", candidate={"publication_title": "Monthly <title>"},
                  raw_barcode="977095361611504", **shared())
    search, confirm = Forms(html).forms
    for form in (search, confirm):
        assert_fields(form, location_id=7, location_node_id=41, shelf_fill_origin="scan", library_id=2,
                      tags="Signed; Freely typed", reading_status="read", mode="add")
    native_csrf(confirm)
    assert confirm["attrs"]["action"] == "/api/periodicals/confirm"
    assert "Monthly &lt;title&gt;" in html
    ordinary = render("periodical_scan_result", candidate={}, raw_barcode="977095361611504", mode="add")
    confirm = Forms(ordinary).forms[1]
    native_csrf(confirm)
    assert "hx-post" not in confirm["attrs"], "ordinary confirmation still uses its native redirect"


def test_magazine_search_select_and_confirmation_keep_new_defaults():
    html = render("magazine_issue_form", carrier_ean="9770953616115", supplement="04", title="Monthly",
                  **shared())
    search, confirm = Forms(html).forms
    for form in (search, confirm):
        assert_fields(form, carrier_ean="9770953616115", barcode_supplement="04", mode="add",
                      location_node_id=41, shelf_fill_origin="scan", library_id=2,
                      tags="Signed; Freely typed", reading_status="read")
    assert_fields(search, location_id=7)
    assert_fields(confirm, location_id=7)
    native_csrf(confirm)
    for for_scan in (True, False):
        html = render("magazine_search_results", for_scan=for_scan,
                      results=[{"google_volume_id": "exact-id", "title": "Monthly"}],
                      carrier_ean="9770953616115", barcode_supplement="04", **shared())
        form = Forms(html).forms[0]
        assert_fields(form, google_volume_id="exact-id", carrier_ean="9770953616115", barcode_supplement="04",
                      location_id=7, location_node_id=41, shelf_fill_origin="scan", mode="add", library_id=2,
                      tags="Signed; Freely typed", reading_status="read")
        if not for_scan:
            native_csrf(form)


def test_native_assisted_periodical_selection_has_a_real_get_form_with_captured_defaults():
    html = render("periodical_scan_result", candidate={}, raw_barcode="977095361611504",
                  native_assisted_candidates=[{"google_volume_id": "exact-id", "title": "Monthly"}], **shared())
    form = next(f for f in Forms(html).forms if f["attrs"].get("action") == "/api/periodicals/assist/select")
    assert form["attrs"]["method"] == "get"
    assert_fields(form, volume_id="exact-id", raw_barcode="977095361611504", location_id=7,
                  location_node_id=41, shelf_fill_origin="scan", library_id=2,
                  tags="Signed; Freely typed", reading_status="read", mode="add")


def test_empty_numeric_get_fields_are_omitted_not_submitted_as_invalid_empty_ints():
    html = render("magazine_issue_form", carrier_ean="9770953616115", supplement="04", mode="add")
    assert not {"location_id", "location_node_id", "library_id"} & Forms(html).forms[0]["fields"].keys()
    html = render("magazine_search_results", results=[{"google_volume_id": "exact-id", "title": "Monthly"}],
                  for_scan=True, mode="add")
    assert not {"location_id", "location_node_id", "library_id"} & Forms(html).forms[0]["fields"].keys()


def test_only_actual_placement_receipts_claim_item_destination_and_position():
    html = render("scan_result", status="moved", item_id=917, title="<script>not executable</script>",
                  shelf_fill_placed=True, position_order=3, location_path="Room > Shelf", **shared())
    assert 'data-scan-item-id="917"' in html
    assert 'data-shelf-fill-placed-node="41"' in html
    assert 'data-testid="shelf-fill-position"' in html and "#3" in html
    assert "<script>not executable</script>" not in html
    ordinary = render("scan_result", status="moved", item_id=917, title="Ordinary Move", message="Elsewhere")
    assert "data-shelf-fill-placed-node" not in ordinary
    assert "shelf-fill-position" not in ordinary
    refusal = render("scan_result", status="error", isbn="invalid", message="Nothing saved")
    assert "data-scan-item-id" not in refusal and "data-saved-item-id" not in refusal


def test_owned_transaction_containment_and_explicit_origin_validator():
    for filename, function_name in (("periodicals.py", "confirm_periodical_issue"),
                                    ("magazines.py", "add_magazine_issue")):
        tree = ast.parse((SOURCE / "app/routers" / filename).read_text())
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == function_name)
        scopes = [node for node in ast.walk(function) if isinstance(node, ast.With) and
                  any(isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Attribute) and
                      i.context_expr.func.attr == "_write_scope" for i in node.items)]
        assert len(scopes) == 1
        assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_place_item"
                   for n in ast.walk(scopes[0])), "placement must be inside issue transaction"
    tree = ast.parse((SOURCE / "app/routers/shelf_fill.py").read_text())
    validator = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "continuation_fields")
    namespace = {}
    exec(compile(ast.Module(body=[validator], type_ignores=[]), "owned-continuation-validator", "exec"), namespace)
    validate = namespace["continuation_fields"]
    assert validate(None, "") == {}
    assert validate(41, "scan")["location_node_id"] == 41
    assert validate(41, "shelf_fill")["shelf_fill_origin"] == "shelf_fill"
    for node, origin in ((None, "scan"), (0, "scan"), (-1, "shelf_fill"), (41, "https://evil.invalid"), (41, "")):
        try:
            validate(node, origin)
        except ValueError:
            pass
        else:
            raise AssertionError((node, origin))
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "scan_isbn"
                   for n in ast.walk(tree)), "never call a decorated scan route directly"


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print("PASS", test.__name__)
        except Exception:
            failed += 1
            print("FAIL", test.__name__)
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} standalone Jinja/owned-AST checks passed")
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
