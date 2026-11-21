"""Cross-filter dropdown counts for Browse.

One function, shared by `/browse` and `/api/search`, so the numbers beside the
filter options cannot disagree between the first paint and the first swap. The
filters themselves are declared in `app/browse_filters.py`.
"""

from app import browse_filters, i18n
from app.i18n import N_
from app.services import lists

# Display names for `items.source` in the Browse Source filter. The dropdown is
# built from the values actually present, so a source missing here still
# appears — under a title-cased fallback — rather than being unreachable.
SOURCE_LABELS = {
    "manual": N_("Manual"),
    "romm": "RomM",
    "komga": "Komga",
    "audiobookshelf": "Audiobookshelf",
    "musicbrainz": "MusicBrainz",
    "hardcover": "Hardcover",
    "igdb": "IGDB",
    "tmdb": "TMDb",
    "issn": "ISSN",
    "openlibrary": "Open Library",
    "oszk": "OSZK NEKTÁR",
    "porbase": "PORBASE",
    "bnf": "BnF",
    "neodb": "NeoDB",
    "google": "Google Books",
    "audible_import": N_("Audible import"),
    "photo_intake": N_("Photo Intake"),
    "store_queue": N_("Store Queue"),
    "csv_import": N_("CSV Import"),
}


def source_label(key: str) -> str:
    """An `items.source` value as a person reads it — the Browse label, or
    the same title-cased fallback the Source dropdown uses."""
    label = SOURCE_LABELS.get(key)
    return i18n.gettext(label) if label else key.replace("_", " ").title()


def filter_counts(db, values: dict, total: int, *, user_id=None, user=None) -> dict:
    """Cross-filter dropdown counts: each group is build_where minus its own filter.

    `values` is the dict `browse_filters.values_from` produced; `total` is the
    row count for the *unexcluded* where-clause, which every caller has already
    run. Both `/browse` and `/api/search` render their dropdowns from this dict,
    so the numbers cannot disagree between the first paint and the first swap.
    """
    from app.services.catalogue_security import current_actor

    actor = current_actor()
    if actor is not None:
        user, user_id = actor, int(actor["id"])
    elif user is not None:
        user_id = int(user["id"])

    def _count_where(exclude):
        where, params = browse_filters.build_where(values, exclude=exclude, user_id=user_id)
        if user is not None:
            from app.services import libraries

            return libraries.scope_where(where, params, user)
        return where, params

    type_where, type_params = _count_where("media_type_filter")
    type_counts = {
        row["media_type"]: row["c"]
        for row in db.execute(
            f"SELECT media_type, COUNT(*) as c FROM items_live i {type_where} GROUP BY media_type",
            type_params,
        ).fetchall()
    }
    type_total = sum(type_counts.values())

    own_where, own_params = _count_where("owned")
    if user_id is None:
        wishlist_sql, wishlist_params = lists.WISHLISTED_SQL, []
        neither_sql, neither_params = lists.NEITHER_SQL, []
    else:
        personal, personal_params = browse_filters.personal_wishlist_sql(user_id)
        wishlist_sql, wishlist_params = f"{personal} = 1", personal_params
        neither_sql, neither_params = f"(i.owned = 0 AND {personal} = 0)", personal_params
    # A personal wish may also be owned, so these categories are not a partition.
    # Count the unique ownership census with the same cross-filters and ACLs.
    own_row = db.execute(
        f"SELECT COUNT(*) AS ownership_total, COALESCE(SUM(i.owned = 1), 0) AS o, COALESCE(SUM({wishlist_sql}), 0) AS w, "
        f"COALESCE(SUM({neither_sql}), 0) AS n FROM items_live i {own_where}",
        [*wishlist_params, *neither_params, *own_params],
    ).fetchone()
    owned_count, wishlist_count, neither_count = own_row["o"], own_row["w"], own_row["n"]

    loc_where, loc_params = _count_where("location_filter")
    _loc_join = " AND" if loc_where else " WHERE"
    location_counts = {
        row["location_id"]: row["c"]
        for row in db.execute(
            f"SELECT location_id, COUNT(*) as c FROM items_live i {loc_where}"
            f"{_loc_join} location_id IS NOT NULL GROUP BY location_id",
            loc_params,
        ).fetchall()
    }
    no_location_count = db.execute(
        f"SELECT COUNT(*) as c FROM items_live i {loc_where}{_loc_join} location_id IS NULL",
        loc_params,
    ).fetchone()["c"]

    rs_where, rs_params = _count_where("reading_status")
    if user_id is None:
        status_sql, status_params = "i.reading_status", []
    else:
        status_sql, status_params = browse_filters.personal_reading_status_sql(user_id)
    reading_status_counts = {
        row["reading_status"]: row["c"]
        for row in db.execute(
            "SELECT reading_status, COUNT(*) as c FROM ("
            f"SELECT {status_sql} AS reading_status FROM items_live i {rs_where}"
            ") resolved WHERE reading_status IS NOT NULL AND reading_status != '' "
            "GROUP BY reading_status",
            [*status_params, *rs_params],
        ).fetchall()
    }

    src_where, src_params = _count_where("source_filter")
    _src_join = " AND" if src_where else " WHERE"
    source_counts = {
        row["source"]: row["c"]
        for row in db.execute(
            f"SELECT source, COUNT(*) as c FROM items_live i {src_where}"
            f"{_src_join} source IS NOT NULL AND source != '' "
            "GROUP BY source ORDER BY source",
            src_params,
        ).fetchall()
    }

    locations = db.execute(
        "SELECT * FROM locations ORDER BY sort_order, name"
    ).fetchall()

    return {
        "type_counts": type_counts,
        "type_total": type_total,
        "ownership_total": own_row["ownership_total"],
        "owned_count": owned_count,
        "wishlist_count": wishlist_count,
        "neither_count": neither_count,
        "location_counts": location_counts,
        "no_location_count": no_location_count,
        "reading_status_counts": reading_status_counts,
        "source_counts": source_counts,
        # Translated here, not with _() in the template: its fallback there is a
        # query-string value, and _() would %-format and mark it safe.
        "source_labels": {key: i18n.gettext(label) for key, label in SOURCE_LABELS.items()},
        "locations": locations,
        "filtered_total": total,
        "active_type": values["media_type_filter"],
        "active_owned": values["owned"],
        "active_location": values["location_filter"],
        "active_reading_status": values["reading_status"],
        "active_source": values.get("source_filter", ""),
    }
