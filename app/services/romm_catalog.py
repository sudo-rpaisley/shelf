"""Read-only catalogue queries for RomM-backed Shelf items.

All exact sidecars are visible, plus legacy inline/holding identities not yet
represented by a sidecar. A plural record is never flattened to one inline ID.
"""

from __future__ import annotations

_RECORDS = """WITH records(romm_id, item_id, platform_id) AS (
    SELECT romm_id, item_id, platform_id FROM romm_records
    UNION
    SELECT i.romm_id, i.id, i.romm_platform_id FROM items_live i
    WHERE i.romm_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM romm_records rr WHERE rr.romm_id = i.romm_id
    )
    UNION
    SELECT dh.external_id, dh.item_id, dh.library_id FROM digital_holdings dh
    WHERE dh.provider = 'romm' AND NOT EXISTS (
        SELECT 1 FROM romm_records rr WHERE rr.romm_id = dh.external_id
    ) AND NOT EXISTS (SELECT 1 FROM items_live i WHERE i.romm_id = dh.external_id)
) """


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
        _RECORDS + "SELECT r.platform_id, i.platform AS shelf_platform, "
        "COALESCE(gp.name, i.platform, r.platform_id) AS platform_name, "
        "COUNT(*) AS game_count "
        "FROM records r JOIN items_live i ON i.id = r.item_id LEFT JOIN game_platforms gp ON gp.slug = i.platform "
        "WHERE r.romm_id IS NOT NULL AND TRIM(r.romm_id) != '' "
        "AND r.platform_id IS NOT NULL AND TRIM(r.platform_id) != '' "
        f"AND ({access}) "
        "GROUP BY r.platform_id, i.platform, gp.name "
        "ORDER BY platform_name COLLATE NOCASE, r.platform_id",
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
        "r.romm_id IS NOT NULL",
        "TRIM(r.romm_id) != ''",
        "r.platform_id IS NOT NULL",
        "TRIM(r.platform_id) != ''",
        f"({_access_clause(access_sql)})",
    ]
    params: list[object] = list(access_params or [])
    clean_platform = str(platform_id or "").strip()
    clean_query = str(query or "").strip()[:200]
    if clean_platform:
        where.append("r.platform_id = ?")
        params.append(clean_platform)
    if clean_query:
        where.append(
            "(i.title LIKE ? COLLATE NOCASE OR COALESCE(i.publisher, '') LIKE ? COLLATE NOCASE)"
        )
        needle = f"%{clean_query}%"
        params.extend([needle, needle])
    clause = " WHERE " + " AND ".join(where)

    total = db.execute(
        _RECORDS + "SELECT COUNT(*) AS c FROM records r JOIN items_live i ON i.id = r.item_id" + clause,
        params,
    ).fetchone()["c"]
    rows = db.execute(
        _RECORDS + "SELECT r.romm_id, r.platform_id, i.id AS item_id, "
        "i.title, i.publisher, i.publish_year, i.platform, i.cover_path, i.media_type, i.description, "
        "COALESCE(gp.name, i.platform, r.platform_id) AS platform_name "
        "FROM records r JOIN items_live i ON i.id = r.item_id LEFT JOIN game_platforms gp ON gp.slug = i.platform"
        + clause
        + " ORDER BY i.title COLLATE NOCASE, i.publish_year, r.romm_id LIMIT ? OFFSET ?",
        [*params, max(1, min(250, int(limit))), max(0, int(offset))],
    ).fetchall()
    return [dict(row) for row in rows], total
