"""Read-only catalogue queries for RomM-backed Shelf items.

This is the fork-main form of the catalogue added in the clean upstream follow-up.
The current fork stores stable RomM identity directly on ``items`` as
``romm_id``/``romm_platform_id`` rather than in ``romm_records``.  Keep that
existing persistence model authoritative and make the catalogue a pure read
surface over it.
"""

from __future__ import annotations


def _access_clause(access_sql: str) -> str:
    clean = str(access_sql or "").strip()
    return clean or "1 = 1"


def platform_summaries(
    db,
    *,
    access_sql: str = "1 = 1",
    access_params: list | None = None,
) -> list[dict]:
    """Return visible RomM platform counts keyed by stable provider id."""
    access = _access_clause(access_sql)
    rows = db.execute(
        "SELECT i.romm_platform_id AS platform_id, i.platform AS shelf_platform, "
        "COALESCE(gp.name, i.platform, i.romm_platform_id) AS platform_name, "
        "COUNT(*) AS game_count "
        "FROM items i LEFT JOIN game_platforms gp ON gp.slug = i.platform "
        "WHERE i.romm_id IS NOT NULL AND TRIM(i.romm_id) != '' "
        "AND i.romm_platform_id IS NOT NULL AND TRIM(i.romm_platform_id) != '' "
        f"AND ({access}) "
        "GROUP BY i.romm_platform_id, i.platform, gp.name "
        "ORDER BY platform_name COLLATE NOCASE, i.romm_platform_id",
        list(access_params or []),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_page(
    db,
    *,
    platform_id: str = "",
    query: str = "",
    limit: int = 60,
    offset: int = 0,
    access_sql: str = "1 = 1",
    access_params: list | None = None,
) -> tuple[list[dict], int]:
    """Fetch one stable, ACL-scoped page of RomM-backed catalogue rows."""
    where = [
        "i.romm_id IS NOT NULL",
        "TRIM(i.romm_id) != ''",
        "i.romm_platform_id IS NOT NULL",
        "TRIM(i.romm_platform_id) != ''",
        f"({_access_clause(access_sql)})",
    ]
    params: list[object] = list(access_params or [])
    clean_platform = str(platform_id or "").strip()
    clean_query = str(query or "").strip()[:200]
    if clean_platform:
        where.append("i.romm_platform_id = ?")
        params.append(clean_platform)
    if clean_query:
        where.append(
            "(i.title LIKE ? COLLATE NOCASE OR COALESCE(i.publisher, '') LIKE ? COLLATE NOCASE)"
        )
        needle = f"%{clean_query}%"
        params.extend([needle, needle])
    clause = " WHERE " + " AND ".join(where)

    total = db.execute(
        "SELECT COUNT(*) AS c FROM items i" + clause,
        params,
    ).fetchone()["c"]
    rows = db.execute(
        "SELECT i.romm_id, i.romm_platform_id AS platform_id, i.id AS item_id, "
        "i.title, i.publisher, i.publish_year, i.platform, i.cover_path, i.description, "
        "COALESCE(gp.name, i.platform, i.romm_platform_id) AS platform_name "
        "FROM items i LEFT JOIN game_platforms gp ON gp.slug = i.platform"
        + clause
        + " ORDER BY i.title COLLATE NOCASE, i.publish_year, i.romm_id LIMIT ? OFFSET ?",
        [*params, max(1, int(limit)), max(0, int(offset))],
    ).fetchall()
    return [dict(row) for row in rows], total
