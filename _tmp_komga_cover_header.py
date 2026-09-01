from pathlib import Path


def patch(path, old, new):
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old!r}")
    p.write_text(text.replace(old, new, 1))


patch(
    "app/services/komga.py",
    '                headers=_headers(api_key),\n                timeout=COVER_TIMEOUT,\n',
    '                headers={"X-API-Key": api_key, "Accept": "image/jpeg"},\n                timeout=COVER_TIMEOUT,\n',
)

patch(
    "tests/test_komga_sync.py",
    '        assert (covers.COVERS_DIR / f"{row[\'id\']}.jpg").read_bytes() == image\n',
    '        assert (covers.COVERS_DIR / f"{row[\'id\']}.jpg").read_bytes() == image\n'
    '        cover_request = respx.calls[-1].request\n'
    '        assert cover_request.headers["X-API-Key"] == KEY\n'
    '        assert cover_request.headers["Accept"] == "image/jpeg"\n',
)
