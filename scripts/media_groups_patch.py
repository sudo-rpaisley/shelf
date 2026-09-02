from pathlib import Path
import re


def replace(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:80]!r}")
    p.write_text(text.replace(old, new, 1))


def regex_replace(path: str, pattern: str, replacement: str) -> None:
    p = Path(path)
    text = p.read_text()
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f"pattern matched {count} times in {path}: {pattern!r}")
    p.write_text(updated)


# Router registration.
replace(
    "app/main.py",
    "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, platforms, settings, sync, komga, romm, checkouts, valuation, hardcover, store, series, share, tags, intake, archive",
    "from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, platforms, settings, sync, komga, romm, related, checkouts, valuation, hardcover, store, series, share, tags, intake, archive",
)
replace(
    "app/main.py",
    "app.include_router(romm.router)\napp.include_router(checkouts.router)",
    "app.include_router(romm.router)\napp.include_router(related.router)\napp.include_router(checkouts.router)",
)

# Item pages now read the whole connected media group instead of only direct edges.
replace(
    "app/routers/pages.py",
    '''        # Linked items (different formats of the same work)\n        linked_items = db.execute(\n            "SELECT i.id, i.title, i.media_type, i.abs_id, i.komga_id, i.romm_id FROM item_links il "\n            "JOIN items i ON (i.id = CASE WHEN il.item_a_id = ? THEN il.item_b_id ELSE il.item_a_id END) "\n            "WHERE il.item_a_id = ? OR il.item_b_id = ?",\n            (item_id, item_id, item_id),\n        ).fetchall()\n''',
    '''        # Related media is a transitive group: A↔B↔C means every member sees\n        # the complete set, not only its immediate item_links neighbours.\n        from app.services import media_groups\n        linked_items = media_groups.related_items(db, item_id)\n''',
)
replace(
    "app/routers/pages.py",
    '''        game_platforms = get_game_platforms(db)\n\n        from app.routers.tags import get_item_tags, get_all_tags\n''',
    '''        game_platforms = get_game_platforms(db)\n\n        # Enrich related rows once for the unified Related media panel. Provider\n        # deep links still use each integration's public/browser URL setting.\n        abs_link_map = {entry["id"]: entry["abs_url"] for entry in linked_abs_items}\n        komga_link_map = {entry["id"]: entry["komga_url"] for entry in linked_komga_items}\n        romm_link_map = {entry["id"]: entry["romm_url"] for entry in linked_romm_items}\n        related_media = []\n        for related_item in linked_items:\n            data = dict(related_item)\n            data["media_label"] = MEDIA_TYPES.get(data["media_type"], data["media_type"])\n            data["platform_label"] = (\n                game_platforms.get(data["platform"], data["platform"])\n                if data.get("platform") else None\n            )\n            data["provider_url"] = None\n            data["provider_name"] = None\n            if data["id"] in abs_link_map:\n                data["provider_url"] = abs_link_map[data["id"]]\n                data["provider_name"] = "Audiobookshelf"\n            elif data["id"] in komga_link_map:\n                data["provider_url"] = komga_link_map[data["id"]]\n                data["provider_name"] = "Komga"\n            elif data["id"] in romm_link_map:\n                data["provider_url"] = romm_link_map[data["id"]]\n                data["provider_name"] = "RomM"\n            data["manual_linked"] = media_groups.has_manual_group_edge(\n                db, item_id, data["id"]\n            )\n            related_media.append(data)\n\n        all_group_items = [dict(item)] + [dict(row) for row in linked_items]\n        related_formats = []\n        related_game_platforms = []\n        for group_item in all_group_items:\n            label = MEDIA_TYPES.get(group_item["media_type"], group_item["media_type"])\n            if label not in related_formats:\n                related_formats.append(label)\n            if (\n                group_item["media_type"] in ("video_game", "digital_game")\n                and group_item.get("platform")\n            ):\n                platform_label = game_platforms.get(\n                    group_item["platform"], group_item["platform"]\n                )\n                if platform_label not in related_game_platforms:\n                    related_game_platforms.append(platform_label)\n\n        from app.routers.tags import get_item_tags, get_all_tags\n''',
)
replace(
    "app/routers/pages.py",
    '''            "linked_items": linked_items,\n            "linked_abs_items": linked_abs_items,\n''',
    '''            "linked_items": linked_items,\n            "related_media": related_media,\n            "related_formats": related_formats,\n            "related_game_platforms": related_game_platforms,\n            "linked_abs_items": linked_abs_items,\n''',
)

# Replace the three separate "Also in ..." blocks plus one-hop link chips with
# one richer transitive group panel. Current-item Open in provider buttons stay.
regex_replace(
    "app/templates/item_detail.html",
    r'''\n                <!-- Also in Audiobookshelf \(linked digital copy, direct deep link\) -->.*?\n                <!-- Reading Status -->''',
    '''\n                {% include "fragments/related_media.html" %}\n\n                <!-- Reading Status -->''',
)

# Provider syncs rebuild the natural family group once per batch. This groups
# multiple audiobook editions and RomM platform ports without N² pair links.
regex_replace(
    "app/services/audiobookshelf.py",
    r'''def _auto_link_items\(\):.*\Z''',
    '''def _auto_link_items():\n    """Group book, ebook and audiobook representations of the same work."""\n    from app.services import media_groups\n    with get_db() as db:\n        media_groups.auto_link_family(db, "book")\n''',
)
regex_replace(
    "app/services/komga.py",
    r'''def _auto_link_items\(\):.*\Z''',
    '''def _auto_link_items():\n    """Group physical and digital comic representations of the same work."""\n    from app.services import media_groups\n    with get_db() as db:\n        media_groups.auto_link_family(db, "comic")\n''',
)
regex_replace(
    "app/services/romm.py",
    r'''def _auto_link_items\(\) -> None:.*\Z''',
    '''def _auto_link_items() -> None:\n    """Group same-title game representations across platforms and formats."""\n    from app.services import media_groups\n    with get_db() as db:\n        media_groups.auto_link_family(db, "game")\n''',
)

# RomM previously pinned same-title, different-platform games as unrelated.
# Cross-platform variants are now intentionally one media group.
replace(
    "tests/test_romm_sync.py",
    '''@respx.mock\ndef test_same_title_different_platform_is_not_linked(db):\n    _insert_item(db, title="Chrono Trigger", isbn=None,\n                 media_type="video_game", platform="ps1")\n    db.execute("COMMIT")\n    _mock_library()\n    respx.get(f"{ROMM}/assets/romm/resources/snes/cover/chrono.jpg").mock(\n        return_value=httpx.Response(404))\n    asyncio.run(sync(ROMM, TOKEN))\n    assert db.execute("SELECT COUNT(*) AS c FROM item_links").fetchone()["c"] == 0\n''',
    '''@respx.mock\ndef test_same_title_different_platform_is_grouped(db):\n    physical_id = _insert_item(db, title="Chrono Trigger", isbn=None,\n                               media_type="video_game", platform="ps1")\n    db.execute("COMMIT")\n    _mock_library()\n    respx.get(f"{ROMM}/assets/romm/resources/snes/cover/chrono.jpg").mock(\n        return_value=httpx.Response(404))\n    asyncio.run(sync(ROMM, TOKEN))\n    digital_id = db.execute(\n        "SELECT id FROM items WHERE media_type='digital_game'"\n    ).fetchone()["id"]\n    link = db.execute("SELECT item_a_id, item_b_id FROM item_links").fetchone()\n    assert {link["item_a_id"], link["item_b_id"]} == {physical_id, digital_id}\n''',
)

print("Media-group wiring applied")
