"""Bounded native Continue feeds, using only a separately supplied personal token.

No persistence, catalogue credentials, remote writes, redirects, retries, artwork
or media requests. ABS shelf membership (including hide-from-Continue) comes from
personalized, not items-in-progress; /me supplies its user's exact progress.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from urllib.parse import quote, urlencode

import httpx

from app.config import MEDIA_TYPES
from app.services.source_connectors.base import contains_secret
from app.services.source_progress import ProgressError, _identity, _number, _timestamp


MAX_ENTRIES = 24
MAX_SCOPES = 8
MAX_REQUESTS = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
REQUEST_SECONDS = 15


def _object(value):
    if not isinstance(value, dict):
        raise ProgressError("malformed_response")
    return value


def _list(value, maximum=1000):
    if not isinstance(value, list) or len(value) > maximum:
        raise ProgressError("malformed_response")
    return value


def _text(value, maximum=500):
    if value is None:
        return ""
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ProgressError("malformed_response")
    return str(value)


def _rom_id(value):
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ProgressError("malformed_response")
    return str(value)


def _clock(value, *, milliseconds=False):
    """Keep the native clock; never guess a timezone for a naive provider date."""
    if value is None:
        return None, None
    if not milliseconds:
        value = _text(value, 64)
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ProgressError("malformed_response") from None
        if date.tzinfo is None:
            return value, None
    return value, _timestamp(value, milliseconds=milliseconds)


def _entry(external_id, library_id, *, kind, media_type, title, creator="", episode_id=None,
           unit=None, position=None, total=None, percent=None, clock=None, milliseconds=False,
           native_type=None, native_position=None, native_total=None, now_playing=False):
    native_clock, sort_at = _clock(clock, milliseconds=milliseconds)
    return {"external_id": external_id, "library_id": library_id, "episode_id": episode_id,
            "kind": kind, "media_type": media_type, "title": _text(title), "creator": _text(creator),
            "unit": unit, "position": position, "total": total, "percent": percent,
            "updated_at": sort_at if milliseconds else native_clock, "sort_at": sort_at,
            "native_clock": native_clock, "native_type": native_type,
            "native_position": native_position, "native_total": native_total, "now_playing": now_playing}


class Reader:
    def __init__(self, client, source, token, checkpoint):
        self.client, self.source, self.token, self.checkpoint = client, source, token, checkpoint
        self.requests, self.bytes = 0, 0

    async def get(self, path, params=None):
        self.checkpoint()
        self.requests += 1
        if self.requests > MAX_REQUESTS:
            raise ProgressError("too_large")
        url = f"{self.source['root']}/{path}"
        if params:
            url += "?" + urlencode(params)
        if contains_secret(url, [self.token]):
            raise ProgressError("invalid_token")
        header = ("X-Emby-Token" if self.source["provider"] == "jellyfin" else
                  "X-API-Key" if self.source["provider"] == "komga" else "Authorization")
        auth = f"Bearer {self.token}" if header == "Authorization" else self.token
        headers = {"Accept": "application/json", "Accept-Encoding": "identity", header: auth}
        try:
            async with asyncio.timeout(REQUEST_SECONDS):
                async with self.client.stream("GET", url, headers=headers) as response:
                    if response.status_code in {401, 403}:
                        raise ProgressError("auth_failed")
                    if 300 <= response.status_code < 400:
                        raise ProgressError("unsafe_redirect")
                    if response.status_code != 200:
                        raise ProgressError("not_found" if response.status_code == 404 else "http_error")
                    if response.headers.get("content-encoding", "identity").lower().strip() not in {"", "identity"}:
                        raise ProgressError("malformed_response")
                    length = response.headers.get("content-length", "")
                    if length.isdecimal() and int(length) > MAX_RESPONSE_BYTES:
                        raise ProgressError("too_large")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        self.bytes += len(chunk)
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES or self.bytes > MAX_TOTAL_BYTES:
                            raise ProgressError("too_large")
                        body.extend(chunk)
                        task = asyncio.current_task()
                        if task is not None and task.cancelling():
                            raise asyncio.CancelledError
            self.checkpoint()
            result = json.loads(body)
            if not isinstance(result, (dict, list)):
                raise ValueError
            return result
        except (TimeoutError, httpx.TimeoutException):
            raise ProgressError("timeout") from None
        except (httpx.HTTPError, OSError):
            raise ProgressError("unreachable") from None
        except (ValueError, UnicodeError, RecursionError):
            raise ProgressError("malformed_response") from None


async def who(reader):
    provider = reader.source["provider"]
    path = {"jellyfin": "Users/Me", "abs": "api/me", "komga": "api/v2/users/me", "romm": "api/users/me"}[provider]
    payload = _object(await reader.get(path))
    value = payload.get("Id" if provider == "jellyfin" else "id")
    identity = _rom_id(value) if provider == "romm" else _identity(value)
    if contains_secret(identity, [reader.token]):
        raise ProgressError("identity_mismatch")
    return identity, payload


def _jelly_item(data, library_id):
    data = _object(data)
    types = {"Movie": ("movie", "digital_video"), "Episode": ("episode", "tv_series"),
             "AudioBook": ("audiobook", "audiobook"), "Audio": ("track", "digital_music"),
             "Book": ("ebook", "ebook")}
    if data.get("Type") not in types:
        return None
    values = _object(data.get("UserData"))
    if type(values.get("Played")) is not bool:
        raise ProgressError("malformed_response")
    if values["Played"]:
        return None
    kind, media_type = types[data["Type"]]
    position, total = values.get("PlaybackPositionTicks"), data.get("RunTimeTicks")
    for number in (position, total):
        if number is not None and (type(number) is not int or not 0 <= number < 2**63):
            raise ProgressError("malformed_response")
    if position is not None and total is not None and position > total:
        raise ProgressError("malformed_response")
    # Book may expose a native fraction/percentage but not a playback clock.
    percent = values.get("PlayedPercentage")
    if percent is not None:
        percent = _number(percent, 100)
    elif kind != "ebook" and position is not None and total:
        percent = position * 100 / total
    creator = data.get("SeriesName") if kind == "episode" else data.get("AlbumArtist")
    return _entry(_identity(data.get("Id")), library_id, kind=kind, media_type=media_type,
                  title=data.get("Name"), creator=creator, native_type=data["Type"],
                  unit="seconds" if kind != "ebook" and position is not None else None,
                  position=position / 10_000_000 if kind != "ebook" and position is not None else None,
                  total=total / 10_000_000 if kind != "ebook" and total else None, percent=percent,
                  native_position=position, native_total=total, clock=values.get("LastPlayedDate"))


async def _jellyfin(reader, user_id, me):
    scopes = reader.source["libraries"]
    if not scopes:
        # Native root query preserves Jellyfin's own home exclusions. Selected
        # libraries instead use a ParentId query; no guessed per-item membership.
        scopes = [None]
    rows, truncated = [], len(scopes) > MAX_SCOPES
    for library_id in scopes[:MAX_SCOPES]:
        params = {"Limit": MAX_ENTRIES, "EnableUserData": "true", "EnableImages": "false",
                  "EnableTotalRecordCount": "true",
                  "IncludeItemTypes": "Movie,Episode,AudioBook,Audio,Book"}
        if library_id is not None:
            params["ParentId"] = library_id
        result = _object(await reader.get(f"Users/{quote(user_id, safe='')}/Items/Resume", params))
        items = _list(result.get("Items"), MAX_ENTRIES)
        count = result.get("TotalRecordCount")
        if type(count) is not int or count < len(items):
            raise ProgressError("malformed_response")
        truncated |= count > len(items)
        rows.extend(row for item in items if (row := _jelly_item(item, library_id)) is not None)
    return rows, truncated


async def _romm(reader, user_id, me):
    result = _object(await reader.get("api/roms", {
        "order_by": "last_played", "order_dir": "desc", "limit": MAX_ENTRIES,
        "last_played": "true", "with_char_index": "false", "with_filter_values": "false",
        "with_rom_id_index": "false", "with_total": "false",
    }))
    items = _list(result.get("items"), MAX_ENTRIES)
    rows = []
    for data in items:
        data = _object(data)
        external_id, platform_id = _rom_id(data.get("id")), _rom_id(data.get("platform_id"))
        if platform_id in reader.source["excluded"]:
            continue
        progress = _object(data.get("rom_user"))
        if _rom_id(progress.get("user_id")) != user_id or _rom_id(progress.get("rom_id")) != external_id:
            raise ProgressError("identity_mismatch")
        for key in ("hidden", "now_playing"):
            if type(progress.get(key)) is not bool:
                raise ProgressError("malformed_response")
        if progress["hidden"] or progress.get("last_played") is None:
            continue
        completion = progress.get("completion")
        if completion is not None:
            completion = _number(completion, 100)
        # RomM zero is unset, not a guessed zero-percent completion or playtime.
        rows.append(_entry(external_id, platform_id, kind="game", media_type="video_game",
                           title=data.get("name") or data.get("fs_name_no_tags") or data.get("fs_name"),
                           creator=data.get("platform_display_name") or data.get("platform_name"),
                           percent=completion if completion else None, clock=progress["last_played"],
                           now_playing=progress["now_playing"]))
    return rows, len(items) == MAX_ENTRIES


async def _komga(reader, user_id, me):
    result = _object(await reader.get("api/v1/books", {
        "read_status": "IN_PROGRESS", "sort": "readProgress.lastModified,desc", "page": 0, "size": MAX_ENTRIES,
    }))
    items = _list(result.get("content"), MAX_ENTRIES)
    if type(result.get("last")) is not bool:
        raise ProgressError("malformed_response")
    rows = []
    for data in items:
        data = _object(data)
        library_id = _identity(data.get("libraryId"))
        if library_id in reader.source["excluded"]:
            continue
        progress, media = _object(data.get("readProgress")), _object(data.get("media"))
        if type(progress.get("completed")) is not bool:
            raise ProgressError("malformed_response")
        if progress["completed"] or data.get("deleted") is True:
            continue
        position, total = progress.get("page"), media.get("pagesCount")
        if (type(position) is not int or type(total) is not int
                or not 0 < position <= total <= 10**7):
            raise ProgressError("malformed_response")
        metadata = _object(data.get("metadata"))
        media_type = "manga" if reader.source["kinds"].get(library_id) == "manga" and "manga" in MEDIA_TYPES else "comic"
        rows.append(_entry(_identity(data.get("id")), library_id, kind="book", media_type=media_type,
                           title=metadata.get("title") or data.get("name"), creator=data.get("seriesTitle"),
                           unit="pages", position=position, total=total, percent=position * 100 / total,
                           clock=progress.get("lastModified")))
    return rows, not result["last"]


async def _abs(reader, user_id, me):
    # /api/me is authenticated self; some ABS versions omit progress.userId.
    progress = {}
    for value in _list(me.get("mediaProgress"), 20000):
        value = _object(value)
        # ABS may retain orphaned legacy progress with no library item. It
        # cannot identify any shelf entity; never turn it into zero progress.
        if value.get("libraryItemId") is None:
            continue
        episode_id = value.get("episodeId")
        if episode_id not in (None, ""):
            episode_id = _identity(episode_id)
        key = (_identity(value.get("libraryItemId")), episode_id or None)
        if key in progress:
            raise ProgressError("malformed_response")
        progress[key] = value
    libraries = _list(_object(await reader.get("api/libraries")).get("libraries"))
    scopes = []
    for library in libraries:
        library_id = _identity(_object(library).get("id"))
        if library_id not in reader.source["excluded"]:
            scopes.append(library_id)
    if len(set(scopes)) != len(scopes):
        raise ProgressError("malformed_response")
    rows, truncated = [], len(scopes) > MAX_SCOPES
    for library_id in scopes[:MAX_SCOPES]:
        shelves = _list(await reader.get(f"api/libraries/{quote(library_id, safe='')}/personalized", {"limit": MAX_ENTRIES}), 30)
        for shelf in shelves:
            shelf = _object(shelf)
            if shelf.get("id") not in {"continue-listening", "continue-reading"}:
                continue
            if shelf.get("type") not in {"book", "episode"} or (shelf["id"] == "continue-reading" and shelf["type"] != "book"):
                raise ProgressError("malformed_response")
            entities = _list(shelf.get("entities"), MAX_ENTRIES)
            count = shelf.get("total")
            if type(count) is not int or count < len(entities):
                raise ProgressError("malformed_response")
            truncated |= count > len(entities)
            for data in entities:
                data = _object(data)
                external_id = _identity(data.get("id"))
                if data.get("libraryId") != library_id:
                    raise ProgressError("malformed_response")
                episode = _object(data.get("recentEpisode")) if shelf.get("type") == "episode" else None
                episode_id = _identity(episode.get("id")) if episode is not None else None
                value = progress.get((external_id, episode_id))
                if value is None:
                    # A missing progress record is not evidence of zero progress.
                    raise ProgressError("malformed_response")
                if "userId" in value and value["userId"] != user_id:
                    raise ProgressError("identity_mismatch")
                for field in ("isFinished", "hideFromContinueListening"):
                    if type(value.get(field)) is not bool:
                        raise ProgressError("malformed_response")
                if value["isFinished"] or value["hideFromContinueListening"]:
                    continue
                media = _object(data.get("media"))
                metadata = _object(media.get("metadata"))
                reading = shelf["id"] == "continue-reading"
                if reading and episode_id:
                    raise ProgressError("malformed_response")
                if reading:
                    unit, position, total = "fraction", _number(value.get("ebookProgress"), 1), 1
                else:
                    unit, position = "seconds", _number(value.get("currentTime"))
                    total = _number(value["duration"]) if value.get("duration") is not None else None
                    if total == 0:
                        total = None
                if total is not None and position > total:
                    raise ProgressError("malformed_response")
                rows.append(_entry(external_id, library_id, episode_id=episode_id,
                                   kind="episode" if episode else "ebook" if reading else "audiobook",
                                   media_type="ebook" if reading else "audiobook",
                                   title=episode.get("title") if episode else metadata.get("title"),
                                   creator=metadata.get("title") if episode else metadata.get("authorName"),
                                   unit=unit, position=position, total=total,
                                   percent=position * 100 / total if total else None,
                                   clock=value.get("lastUpdate"), milliseconds=True))
    return rows, truncated


async def fetch(reader, expected_user_id):
    actual, me = await who(reader)
    if actual != expected_user_id:
        raise ProgressError("identity_mismatch")
    rows, truncated = await {"jellyfin": _jellyfin, "abs": _abs, "komga": _komga, "romm": _romm}[
        reader.source["provider"]](reader, actual, me)
    unique = {}
    for row in rows:
        key = (row["external_id"], row["episode_id"], row["kind"])
        if key in unique and unique[key] != row:
            raise ProgressError("ambiguous_binding")
        unique[key] = row
    rows = sorted(unique.values(), key=lambda row: row["sort_at"] or "", reverse=True)
    if any(contains_secret(value, [reader.token]) for row in rows for value in row.values()):
        raise ProgressError("malformed_response")
    reader.checkpoint()
    return {"entries": rows[:MAX_ENTRIES], "truncated": truncated or len(rows) > MAX_ENTRIES}
