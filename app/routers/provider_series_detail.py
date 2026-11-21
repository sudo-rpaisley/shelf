"""Anchored provider metadata projection; no endpoint replacement."""

import json

from app.services import provider_series


def _provider_row_is_anchored(row: dict, visible_items: list[dict]) -> bool:
    provider = row.get("provider")
    if provider == "komga":
        provider_id = str(row.get("provider_series_id") or "")
        return any(str(i.get("source") or "").casefold() == "komga"
                   and str(i.get("komga_series_id") or "") == provider_id for i in visible_items)
    if provider == "audiobookshelf":
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            return False
        library_id = metadata.get("library_id") if isinstance(metadata, dict) else None
        return any(i.get("abs_id") and (library_id is None or str(i.get("abs_library_id") or "") == str(library_id))
                   for i in visible_items)
    if provider == "romm":
        return any(i.get("romm_id") for i in visible_items)
    return False


def attach_provider_metadata(db, context: dict, *, komga_series_id=None) -> None:
    kwargs = {"provider": "komga", "provider_series_id": komga_series_id} if komga_series_id else {}
    rows = [r for r in provider_series.for_series(db, context["name"], **kwargs)
            if _provider_row_is_anchored(r, context.get("items") or [])]
    description, source = provider_series.preferred_description(rows, context.get("media_type"))
    context.update(provider_metadata=rows, provider_description=description, provider_description_source=source)
