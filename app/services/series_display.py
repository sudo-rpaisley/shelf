"""Presentation helpers shared by series-oriented library surfaces.

Shelf stores series membership directly on items.  These helpers deliberately
stay small and source-agnostic: they turn positions into local gap hints and
choose human-friendly terminology without changing catalogue data.

The Komga volume heuristic is intentionally conservative.  When a canonical
Digital Comic series contains multiple Komga series IDs, Komga has usually
split ComicInfo ``Volume`` values into generated series such as ``One Piece
(21)``.  Shelf canonicalises those names back to ``One Piece`` during sync, so
multiple source-series IDs are a strong signal that the resulting entries are
volumes rather than individual issues.  A later explicit series-unit field can
override this heuristic without changing callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class SeriesUnit:
    singular: str
    plural: str
    position_prefix: str


ISSUES = SeriesUnit("issue", "issues", "#")
VOLUMES = SeriesUnit("volume", "volumes", "Vol. ")


def find_gaps(positions: Iterable[object]) -> list[int]:
    """Return missing whole-numbered positions from 1 to the highest position.

    Fractional positions are ignored for gap math: they can represent specials
    or novellas between numbered entries and should neither create nor fill an
    integer gap.
    """
    ints: set[int] = set()
    for position in positions:
        if position is None:
            continue
        try:
            value = float(position)
        except (TypeError, ValueError):
            continue
        if value.is_integer() and value >= 1:
            ints.add(int(value))
    if not ints:
        return []
    return [number for number in range(1, max(ints) + 1) if number not in ints]


def infer_series_unit(items: Iterable[Mapping[str, object]]) -> SeriesUnit:
    """Choose ``issue(s)`` or ``volume(s)`` for a displayed series.

    Books are conventionally volumes.  Comic-like series default to issues,
    except for the Komga pattern described in this module's docstring where
    several distinct Komga series IDs have been canonicalised into one Shelf
    series; those entries are volumes.
    """
    rows = list(items)
    if not rows:
        return VOLUMES

    media_types = {str(row.get("media_type") or "") for row in rows}
    comic_like = bool(media_types) and media_types <= {"comic", "digital_comic"}
    if not comic_like:
        return VOLUMES

    komga_series_ids = {
        str(row.get("komga_series_id") or "").strip()
        for row in rows
        if str(row.get("source") or "").lower() == "komga"
        and str(row.get("komga_series_id") or "").strip()
    }
    if len(komga_series_ids) > 1:
        return VOLUMES
    return ISSUES


def count_label(count: int, unit: SeriesUnit) -> str:
    """Return a correctly pluralised count such as ``1 issue`` or ``3 volumes``."""
    noun = unit.singular if count == 1 else unit.plural
    return f"{count} {noun}"
