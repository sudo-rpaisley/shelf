"""Read-only contexts shared by the unified workspace and compatible tool URLs."""

from app import features
from app.config import MEDIA_TYPES, STATUS_MEDIA_TYPES, status_labels
from app.database import get_all_settings, get_db, get_game_platforms
from app.services import item_template, locations as location_svc


TOOLS = frozenset({"add", "scan", "photo", "shelf"})
SCAN_MODES = ("add", "wishlist", "lend", "return", "move", "inventory", "lookup", "quick_rate")


def status_label_sets():
    def words(labels):
        return [labels.want, labels.doing, labels.done]
    return {"auto": words(status_labels(None)),
            **{kind: words(status_labels(kind)) for kind in sorted(STATUS_MEDIA_TYPES)}}


def scan_context(*, add="", from_="", mode="", code=""):
    try:
        candidate = int(from_)
    except (TypeError, ValueError):
        candidate = 0
    if not 0 < candidate <= 2**63 - 1:
        candidate = 0
    disabled = features.disabled_scan_modes()
    allowed = set(SCAN_MODES) - set(disabled.split())
    seed_mode = mode if mode in allowed else ""
    # Only the existing audit Add link may seed a barcode. Never submit it.
    seed_code = code if seed_mode == "add" and len(code) <= 64 else ""
    with get_db() as db:
        locations = db.execute("SELECT * FROM locations ORDER BY sort_order, name").fetchall()
        platforms = get_game_platforms(db)
        borrowers = db.execute("SELECT * FROM borrowers ORDER BY name").fetchall()
        prefill = {name: "" if value is None else value for name, value in
                   ((item_template.copyable_fields(db, candidate) if candidate > 0 else None) or {}).items()}
    return {"media_types": MEDIA_TYPES, "game_platforms": platforms,
            "locations": locations, "borrowers": borrowers,
            "manual_open": add == "manual", "manual_prefill": prefill,
            "off_scan_modes": disabled, "seed_mode": seed_mode, "seed_code": seed_code,
            "status_label_sets": status_label_sets()}


def photo_context():
    with get_db() as db:
        locations = db.execute("SELECT * FROM locations ORDER BY sort_order, name").fetchall()
        provider = get_all_settings(db).get("vision_provider") or ""
    return {"locations": locations, "vision_provider": provider, "media_types": MEDIA_TYPES}


def shelf_context():
    with get_db() as db:
        locations = location_svc.location_tree(db)
        platforms = get_game_platforms(db)
    return {"locations": locations, "game_platforms": platforms, "media_types": MEDIA_TYPES}
