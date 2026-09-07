from pathlib import Path

# Migration 31 is the authoritative schema change. Do not also bake the new
# column into MIGRATION_TABLES: legacy-test fixtures intentionally create the
# post-migration helper tables while recording only older schema versions, and
# the later migration must still have a real ALTER to apply.
db_path = Path("app/database.py")
db = db_path.read_text()
old_db = "    position_order     INTEGER DEFAULT NULL,\n"
if db.count(old_db) != 1:
    raise SystemExit(f"expected one MIGRATION_TABLES position_order line, found {db.count(old_db)}")
db_path.write_text(db.replace(old_db, "", 1))

# Wait on the actual auto-order POST instead of networkidle after a click.
e2e_path = Path("tests/e2e/test_location_order.py")
e2e = e2e_path.read_text()
old_e2e = """    authed_page.locator(\"[data-auto-order='series']\").click()\n    authed_page.wait_for_load_state(\"networkidle\")\n"""
new_e2e = """    with authed_page.expect_response(\n        lambda response: f\"/api/locations/{location_id}/auto-order\" in response.url\n        and response.request.method == \"POST\"\n    ):\n        authed_page.locator(\"[data-auto-order='series']\").click()\n"""
if e2e.count(old_e2e) != 1:
    raise SystemExit(f"expected one E2E networkidle block, found {e2e.count(old_e2e)}")
e2e_path.write_text(e2e.replace(old_e2e, new_e2e, 1))
