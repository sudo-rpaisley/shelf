"""CSV import error-contract bridge for the upstream-0.34 integration."""

import csv
import io
import re

from fastapi import Depends, Request

from app.auth import require_role
from app.routers import items_csv
from app.services import reading_imports


_original_import_csv = items_csv.import_csv

items_csv.router.routes[:] = [
    route
    for route in items_csv.router.routes
    if not (
        getattr(route, "path", None) == "/api/import/csv"
        and "POST" in (getattr(route, "methods", None) or set())
    )
]


def _row_context(raw: bytes) -> dict[int, dict]:
    """Normalised source rows keyed by the user-visible CSV row number."""
    try:
        content = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        if reader.fieldnames:
            reader.fieldnames = [
                field.strip().lower().replace(" ", "_") for field in reader.fieldnames
            ]
        fmt = reading_imports.detect_format(reader.fieldnames)
        normalize = reading_imports.NORMALIZERS[fmt]
        return {number: normalize(row) for number, row in enumerate(reader, start=2)}
    except Exception:
        return {}


@items_csv.router.post("/import/csv")
async def integrated_import_csv(
    request: Request,
    _=Depends(require_role("admin")),
):
    form = await request.form()
    mode = form.get("mode", "skip")
    if mode not in ("skip", "update"):
        return {
            "error": "Invalid import mode",
            "imported": 0,
            "skipped": 0,
            "errors": [],
        }

    csv_file = form.get("file")
    row_context = {}
    if csv_file is not None and hasattr(csv_file, "read") and hasattr(csv_file, "seek"):
        try:
            raw = await csv_file.read()
            row_context = _row_context(raw)
            await csv_file.seek(0)
        except Exception:
            row_context = {}

    result = await _original_import_csv(request, _=request.state.user)
    if not isinstance(result, dict):
        return result

    repaired = []
    for message in result.get("errors") or []:
        match = re.match(r"Row (\d+): invalid ISBN$", message)
        if match:
            number = int(match.group(1))
            raw_isbn = (row_context.get(number) or {}).get("isbn")
            if raw_isbn:
                message = f"Row {number}: Invalid ISBN: {raw_isbn}"
        else:
            match = re.match(r"Row (\d+): invalid media_type$", message)
            if match:
                number = int(match.group(1))
                media_type = (row_context.get(number) or {}).get("media_type")
                if media_type:
                    message = f"Row {number}: Unknown media type: {media_type!r}"
        repaired.append(message)

    result["errors"] = repaired
    return result
