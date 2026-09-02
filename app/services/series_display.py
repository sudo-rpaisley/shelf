"""Presentation helpers shared by series-oriented library surfaces.

Shelf stores series membership directly on items. These helpers deliberately
stay small: they turn positions into local gap hints and choose safe,
human-friendly terminology without changing catalogue data.

Komga-backed Digital Comics use neutral ``item`` wording here. Komga's series
identity is authoritative, but Shelf does not yet store an explicit semantic
unit saying whether a given Komga series contains issues, manga volumes,
omnibuses, or another kind of book. Guessing from series IDs caused Shelf to
reinterpret the source catalogue, so precise issue/volume wording is reserved
for data Shelf actually understands.
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
ITEMS = SeriesUnit("item", "items", "#")


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
    """Choose safe wording for a displayed series.

    Ordinary book-like series are conventionally volumes. Shelf-managed comic
    series use issues. Komga-backed Digital Comics remain neutral until an
    explicit source/unit field is added; series identity alone cannot tell us
    whether Komga's books are issues or volumes.
    """
    rows = list(items)
    if not rows:
        return VOLUMES

    media_types = {str(row.get("media_type") or "") for row in rows}
    comic_like = bool(media_types) and media_types <= {"comic", "digital_comic"}
    if not comic_like:
        return VOLUMES

    if any(
        str(row.get("source") or "").lower() == "komga"
        for row in rows
    ):
        return ITEMS
    return ISSUES


def count_label(count: int, unit: SeriesUnit) -> str:
    """Return a correctly pluralised count such as ``1 issue`` or ``3 items``."""
    noun = unit.singular if count == 1 else unit.plural
    return f"{count} {noun}"
