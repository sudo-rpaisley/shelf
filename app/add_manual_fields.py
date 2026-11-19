"""Finite optional manual metadata; never provider identity, history or SQL policy."""

from datetime import date
import math
import re

from app.config import STATUS_MEDIA_TYPES
from app.services import isbn
from app.services.write_targets import ItemValueError


TEXT_FIELDS = {"subtitle": 200, "description": 10000, "notes": 10000, "narrator": 200}
INTEGER_FIELDS = ("page_count", "duration_mins")
DECIMAL_FIELDS = ("series_position", "manual_value")
DATE_FIELDS = ("date_started", "date_finished")
EXTRA_FIELDS = (*TEXT_FIELDS, *INTEGER_FIELDS, *DECIMAL_FIELDS, *DATE_FIELDS, "asin")
MULTILINE_FIELDS = frozenset({"description", "notes"})


def text_value(raw, maximum, *, multiline=False):
    if not isinstance(raw, str) or any(
        (ord(char) < 32 and not (multiline and char in "\r\n\t")) or 127 <= ord(char) < 160 for char in raw
    ):
        raise ItemValueError("A manual field contains invalid text or is too long")
    if multiline:
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    if len(raw) > maximum:
        raise ItemValueError("A manual field contains invalid text or is too long")
    return raw


def extra_values(form, media_type):
    values = {}
    for name in EXTRA_FIELDS:
        raw = form.get(name, "")
        if not isinstance(raw, str):
            raise ItemValueError("Manual metadata must be text")
        if not raw:
            continue
        if name in {"narrator", "duration_mins", "asin"} and media_type != "audiobook":
            raise ItemValueError("Audio details require an audiobook format")
        if name in DATE_FIELDS and media_type not in STATUS_MEDIA_TYPES:
            raise ItemValueError("Progress dates do not apply to this format")
        if name in TEXT_FIELDS:
            value = text_value(raw, TEXT_FIELDS[name], multiline=name in MULTILINE_FIELDS)
        elif name in INTEGER_FIELDS:
            if not re.fullmatch(r"[0-9]{1,9}", raw):
                raise ItemValueError("Pages and duration must be nonnegative whole numbers")
            value = int(raw)
        elif name in DECIMAL_FIELDS:
            if len(raw) > 40 or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", raw):
                raise ItemValueError("Series position or value is not a finite number")
            value = float(raw)
            if not math.isfinite(value) or abs(value) > 10**12 or (name == "manual_value" and value < 0):
                raise ItemValueError("Series position or value is outside the supported range")
        elif name in DATE_FIELDS:
            try:
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw) or date.fromisoformat(raw).isoformat() != raw:
                    raise ValueError
            except ValueError:
                raise ItemValueError("Use a valid date in YYYY-MM-DD format") from None
            value = raw
        else:
            value = isbn.clean_asin(raw)
            if value is None:
                raise ItemValueError("An ASIN must contain ten letters or digits")
        values[name] = value
    return tuple(values.items())


def extra_dict(values):
    if not isinstance(values, tuple) or any(not isinstance(pair, tuple) or len(pair) != 2 for pair in values):
        raise ItemValueError("Invalid manual metadata")
    result = dict(values)
    if len(result) != len(values) or set(result) - set(EXTRA_FIELDS):
        raise ItemValueError("Invalid manual metadata fields")
    return result
