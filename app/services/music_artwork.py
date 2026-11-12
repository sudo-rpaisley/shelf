"""Music artwork candidates stay separate from release/pressing identity."""

from urllib.parse import urlsplit

from app.services import discogs, discogs_selection, manual_cover, music_matching, musicbrainz, provider_result


def context(db, item):
    result = dict(item)
    release = db.execute("SELECT musicbrainz_release_id, musicbrainz_release_group_id FROM music_releases WHERE item_id = ?",
                         (result["id"],)).fetchone()
    if release:
        result.update(dict(release))
    result["discogs_release_id"] = discogs_selection.get_selected_release_id(db, result["id"])
    return result


async def release_candidates(release, client):
    """Exact-release scans first, clearly labelled album-art fallback second."""
    result = await musicbrainz.cover_art(release.get("musicbrainz_release_id") or "", client)
    candidates = []
    if result.found:
        for image in result.payload:
            label = " · ".join(filter(None, (release.get("title"), release.get("release_date"),
                                            release.get("catalog_number"), ", ".join(image.get("types") or []))))
            candidates.append({**image, "label": label[:300] or "Release artwork"})
    group = release.get("musicbrainz_release_group_id")
    if group and not any(image.get("front") for image in candidates):
        fallback = await musicbrainz.cover_art(group, client, group=True)
        if fallback.found:
            candidates.extend({**image, "label": "Album artwork — another edition"} for image in fallback.payload)
        elif not candidates:
            return fallback
    return provider_result.found("coverartarchive", candidates[:24]) if candidates else result


async def search(item, query, client, *, creds):
    if creds.get("music_disabled"):
        return provider_result.no_match("musicbrainz")
    item = dict(item)
    typed = query.strip() != (item.get("title") or "").strip()
    candidates, outcomes = [], []
    releases = []
    if not typed and item.get("musicbrainz_release_id"):
        releases = [item]
    else:
        result = await music_matching.search(client, title=query, artist="" if typed else item.get("authors") or "",
                                             barcode="" if typed else item.get("upc") or "", format=item.get("media_type") or "")
        outcomes.append(result)
        if result.found:
            releases = result.payload[:4]
    for release in releases:
        release_id = release.get("musicbrainz_release_id")
        if not release_id:
            continue
        result = await release_candidates(release, client)
        outcomes.append(result)
        if result.found:
            for image in result.payload:
                candidates.append(image)
        if len(candidates) >= 12:
            break
    if not typed and item.get("discogs_release_id") and creds.get("discogs_token"):
        result = await discogs.lookup_release(item["discogs_release_id"], client, token=creds["discogs_token"])
        outcomes.append(result)
        if result.found:
            candidates.extend(result.payload.get("images") or [])
    unique = {candidate["url"]: candidate for candidate in reversed(candidates)}
    if unique:
        return provider_result.found("musicbrainz", list(reversed(list(unique.values())))[:24])
    return provider_result.combine([provider_result.no_match(r.provider) if r.found else r for r in outcomes],
                                   provider="musicbrainz")


async def download(item_id, url):
    from app.services import covers
    if not covers.is_allowed_cover_url(url):
        return None
    caa = urlsplit(url).hostname != "i.discogs.com"
    return await manual_cover.download(item_id, url, unique=True, caa=caa)
