"""Redacted, local-only health for the existing singleton integrations.

These are not rows in the multi-source catalogue: identities, configuration and
credentials stay with ABS/Komga/RomM. Health describes the last sync attempt,
not whether a remote server is reachable now. ``statuses`` uses only its
caller's connection and never logs, decrypts through a logging accessor, or
creates a key file. ``run_tracked`` must be called outside a write transaction;
its two short transactions never surround the underlying sync.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

import httpx

from app import config, crypto
from app.database import get_db, set_setting


_PROVIDERS = {
    "abs": ("Audiobookshelf", "abs_sync", "abs_url", "abs_token"),
    "komga": ("Komga", "komga", "komga_url", "komga_api_key"),
    "romm": ("RomM", "romm", "romm_url", "romm_token"),
}
_STATUSES = frozenset({"never", "ok", "partial", "error"})
_ERROR_CODES = frozenset({
    "connector_error", "incomplete", "malformed_response", "unexpected_error",
    "timeout", "unreachable", "http_error", "auth_failed", "forbidden",
    "rate_limited", "interrupted",
})
_COMPLETED_COUNTS = (
    "added", "created", "adopted", "updated", "unchanged", "skipped", "in_trash",
)
_Result = TypeVar("_Result")


def _read(db, key: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"] or "") if row else ""


def _key(provider: str, field: str) -> str:
    return f"connection.{provider}.{field}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: str) -> str | None:
    """Normalize timestamps rather than passing arbitrary saved text to a DTO."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _credential_present(raw: str) -> bool:
    """Check a saved credential without logging or creating an encryption key.

    ``decrypt_value`` logs even on some reads (G3). Use the same cipher and
    existing key, but do not call its logging/auto-create accessors here.
    """
    if not raw.strip():
        return False
    if not crypto.is_fernet_token(raw):
        return True  # The integration also accepts legacy plaintext settings.
    try:
        secret = (crypto._cached_encryption_key
                  or os.environ.get("SHELF_ENCRYPTION_KEY", "").strip()
                  or crypto._read_keyfile("encryption.key"))
        if not secret:
            return False
        return bool(crypto._fernet(secret).decrypt(raw.encode()).decode().strip())
    except Exception:
        return False


def _configured(db, provider: str, url_key: str, credential_key: str) -> bool:
    url = _read(db, url_key)
    if provider == "abs":
        # Only ABS has env overrides in the existing integrations (G15).
        url = config.get_setting_value(url_key, url)
        if config.is_env_override(credential_key):
            return bool(url.strip() and config.get_setting_value(credential_key).strip())
    return bool(url.strip()) and _credential_present(_read(db, credential_key))


def statuses(db) -> list[dict]:
    """Return safe legacy source DTOs using only ``db``, including live counts.

    Keys/providers are ``abs``, ``komga`` and ``romm``. ``last_sync_at`` is the
    last complete success, retained across failures/partial runs. There is no
    backfill from ``abs_last_sync``: historically it also stamped failed runs.
    """
    counts = {
        "abs": db.execute(
            "SELECT COUNT(*) FROM items_live WHERE abs_id IS NOT NULL AND abs_id != ''"
        ).fetchone()[0],
        "komga": db.execute(
            "SELECT COUNT(*) FROM komga_records r JOIN items_live i ON i.id = r.item_id"
        ).fetchone()[0],
        "romm": db.execute(
            "SELECT COUNT(*) FROM romm_records r JOIN items_live i ON i.id = r.item_id"
        ).fetchone()[0],
    }
    rows = []
    for provider, (name, feature, url_key, credential_key) in _PROVIDERS.items():
        status = _read(db, _key(provider, "last_status"))
        status = status if status in _STATUSES else "never"
        error = _read(db, _key(provider, "last_error"))
        rows.append({
            "key": provider,
            "provider": provider,
            "name": name,
            "enabled": _read(db, f"feature.{feature}") != "0",
            "configured": _configured(db, provider, url_key, credential_key),
            "records_count": counts[provider],
            "last_attempt_at": _timestamp(_read(db, _key(provider, "last_attempt_at"))),
            "last_sync_at": _timestamp(_read(db, _key(provider, "last_sync_at"))),
            "last_status": status,
            "last_error": (error if error in _ERROR_CODES else "connector_error")
                          if status in {"error", "partial"} else None,
            "capabilities": {"sync": True, "open": True, "remote_write": False,
                             "scheduled_sync": provider == "abs"},
            "settings_path": "/settings",
        })
    return rows


def result_health(stats: object) -> tuple[str, str | None]:
    """Classify existing sync return shapes without storing any provider text.

    Counted library failures matter even when no candidates were returned.
    Intentional skips/Trash are completed decisions, not remote failures.
    Warnings or an explicitly incomplete result never count as full success.
    """
    if not isinstance(stats, dict):
        return "error", "malformed_response"
    if (stats.get("error") or stats.get("ok") is False
            or stats.get("success") is False or stats.get("status") == "error"):
        return "error", "connector_error"
    incomplete = (stats.get("warnings") or stats.get("partial")
                  or stats.get("complete") is False or stats.get("status") == "partial")
    if stats.get("errors"):
        completed = any(
            isinstance(stats.get(key), (int, float)) and stats[key] > 0
            for key in _COMPLETED_COUNTS
        )
        return (("partial", "incomplete") if completed or incomplete
                else ("error", "connector_error"))
    if incomplete:
        return "partial", "incomplete"
    return "ok", None


def _exception_code(exc: BaseException) -> str:
    # Provider exceptions sometimes wrap a transport failure. Inspect types,
    # never exception messages, URLs, request headers or response bodies.
    for _ in range(4):
        if isinstance(exc, asyncio.CancelledError):
            return "interrupted"
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.HTTPStatusError):
            return {401: "auth_failed", 403: "forbidden", 429: "rate_limited"}.get(
                exc.response.status_code, "http_error"
            )
        if isinstance(exc, httpx.TransportError):
            return "unreachable"
        if isinstance(exc, httpx.HTTPError):
            return "http_error"
        if exc.__cause__ is None:
            break
        exc = exc.__cause__
    return "unexpected_error"


def _record_result(provider: str, attempt: str, status: str, error: str | None) -> None:
    finished_at = _now()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if status == "ok":
            previous = _timestamp(_read(db, _key(provider, "last_sync_at")))
            if previous is None or finished_at > previous:
                set_setting(db, _key(provider, "last_sync_at"), finished_at)
        # Overlapping manual/scheduled runs must not let an older attempt
        # overwrite the latest attempt's outcome. Check under the lock (G18).
        if _read(db, _key(provider, "last_attempt_at")) == attempt:
            set_setting(db, _key(provider, "last_status"), status)
            set_setting(db, _key(provider, "last_error"), error or "")


async def run_tracked(
    provider: str,
    sync_callable: Callable[..., Awaitable[_Result]],
    *args: Any,
    **kwargs: Any,
) -> _Result:
    """Track a sync outside its transactions, returning its result unchanged.

    All positional/keyword arguments (including progress/context callbacks)
    are forwarded only to the callable, never saved. Exceptions are recorded
    as fixed safe codes and re-raised; cancellation is an interrupted attempt.
    """
    if provider not in _PROVIDERS:
        raise ValueError("Unknown legacy source provider")
    attempt = _now()
    with get_db() as db:
        set_setting(db, _key(provider, "last_attempt_at"), attempt)
    try:
        stats = await sync_callable(*args, **kwargs)
    except (Exception, asyncio.CancelledError) as exc:
        try:
            _record_result(provider, attempt, "error", _exception_code(exc))
        except Exception as health_error:
            # Keep the sync failure primary even if health storage also fails;
            # expose the storage failure as its cause, never silently drop it.
            raise exc from health_error
        raise
    status, error = result_health(stats)
    _record_result(provider, attempt, status, error)
    return stats
