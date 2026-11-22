"""Navigation tab registry and per-request visibility resolution.

The nav renders on every page, so the settings it depends on are read once
and cached at module level; every settings write path that can change a
nav-relevant value calls `invalidate_cache()`.

Visibility is presentation only for manual hiding and the `requires` check —
a hidden tab's route keeps serving. A **disabled feature** is not presentation:
it gates the tab's routes too (`app.features.require_feature`), so dropping
its tab here is just the nav agreeing with what the route already refuses.
Role gating is the other exception: it mirrors the security rules the
routers already enforce and always wins over the visibility config.
"""

import json
import logging

from app.features import FEATURES, feature_enabled
from app.i18n import N_

logger = logging.getLogger(__name__)

# Feature ownership remains centralized even for the fork's grouped navigation.
_TAB_FEATURE = {tab: key for key, feature in FEATURES.items() for tab in feature.nav_tabs}

# `group` controls presentation in base.html without changing route access.
# Primary destinations stay visible; creation tools sit under Add; specialist
# and reporting pages sit under More; admin pages live with the account menu.
NAV_TABS = [
    {"key": "home", "label": N_("Home"), "path": "/", "group": "primary"},
    {"key": "browse", "label": N_("Browse"), "path": "/browse", "group": "primary"},
    {"key": "my_list", "label": N_("My List"), "path": "/my-list", "group": "primary"},
    {"key": "collections", "label": N_("Collections"), "path": "/collections", "group": "primary"},
    {"key": "series", "label": N_("Series"), "path": "/series", "group": "primary"},
    {"key": "discover", "label": N_("Discover"), "path": "/discover", "group": "primary",
     "requires": "hardcover"},
    {"key": "scan", "label": N_("Scan"), "path": "/scan", "group": "add",
     "roles": ("admin", "editor")},
    {"key": "intake", "label": N_("Intake"), "path": "/intake", "group": "add",
     "roles": ("admin", "editor"), "requires": "vision"},
    {"key": "shelf-fill", "label": N_("Shelf Fill"), "path": "/shelf-fill", "group": "add",
     "roles": ("admin", "editor")},
    {"key": "locations", "label": N_("Locations"), "path": "/locations", "group": "more"},
    {"key": "music", "label": N_("Music"), "path": "/music", "group": "more"},
    {"key": "periodicals", "label": N_("Periodicals"), "path": "/periodicals", "group": "more"},
    {"key": "store", "label": N_("Store"), "path": "/store", "group": "more"},
    {"key": "stats", "label": N_("Stats"), "path": "/stats", "group": "more"},
    {"key": "settings", "label": N_("Settings"), "path": "/settings", "group": "account",
     "roles": ("admin",)},
    {"key": "logs", "label": N_("Logs"), "path": "/logs", "group": "account",
     "roles": ("admin",)},
    {"key": "trash", "label": N_("Trash"), "path": "/trash", "roles": ("admin", "editor"),
      "group": "account", "menu": "account", "heading": N_("Library")},
    {"key": "labels", "label": N_("Labels"), "path": "/labels", "roles": ("admin", "editor"),
      "group": "account", "menu": "account", "heading": N_("Library")},
    {"key": "audits", "label": N_("Audits"), "path": "/audits", "roles": ("admin", "editor"),
      "group": "account", "menu": "account", "heading": N_("Library")},
    {"key": "duplicates", "label": N_("Duplicates"), "path": "/duplicates", "roles": ("admin",),
      "group": "account", "menu": "account", "heading": N_("Library")},
]

# Home, Browse, Collections and the page that controls visibility must stay reachable.
ALWAYS_VISIBLE = ("home", "browse", "collections", "settings")

HIDEABLE_TABS = [t for t in NAV_TABS if t["key"] not in ALWAYS_VISIBLE]
HIDEABLE_KEYS = frozenset(t["key"] for t in HIDEABLE_TABS)

# Every settings key the nav depends on, read together so one cold read
# covers a whole page render.
NAV_SETTING_KEYS = (
    "vision_provider",
    "anthropic_api_key",
    "openai_api_key",
    "hardcover_token",
    "nav_hidden_tabs",
)

_cached_settings: dict[str, str] | None = None


def invalidate_cache() -> None:
    """Drop the cached settings so the next render re-reads them."""
    global _cached_settings
    _cached_settings = None


def _nav_settings() -> dict[str, str]:
    """Nav-relevant settings, with env overrides applied and secrets decrypted.

    Read through `get_setting` rather than the raw table so an env-provided
    token counts as configured and `hardcover_token` is decrypted.
    """
    global _cached_settings
    if _cached_settings is None:
        from app.database import get_db, get_setting
        try:
            with get_db() as db:
                _cached_settings = {k: get_setting(db, k) for k in NAV_SETTING_KEYS}
        except Exception:
            # A nav that can't read settings must not take the page down;
            # fall back to "nothing configured, nothing hidden" for this
            # render and try again on the next one.
            logger.warning("Could not read nav settings; falling back to defaults", exc_info=True)
            return {k: "" for k in NAV_SETTING_KEYS}
    return _cached_settings


def _is_configured(requirement: str, settings: dict[str, str]) -> bool:
    """Whether an integration is set up enough for its tab to do anything.

    Mirrors what each feature itself enforces before running: the vision
    backends' hard requirements (`app/services/vision.py`) and the presence
    of a Hardcover token.
    """
    if requirement == "vision":
        provider = (settings.get("vision_provider") or "").strip()
        if provider == "anthropic":
            return bool((settings.get("anthropic_api_key") or "").strip())
        if provider == "openai":
            return bool((settings.get("openai_api_key") or "").strip())
        if provider == "ollama":
            return True  # URL and model both have defaults
        return False
    if requirement == "hardcover":
        return bool((settings.get("hardcover_token") or "").strip())
    return True  # an unknown requirement never hides a tab


# What each requirement needs, for the settings UI's explanatory hint.
REQUIREMENT_LABELS = {
    "vision": "a vision provider",
    "hardcover": "a Hardcover token",
}


def hideable_tab_states(settings: dict[str, str] | None = None) -> list[dict]:
    """Each hideable tab with both inputs to its visibility.

    -> {"key", "label", "hidden", "available", "requirement_label",
        "feature_off", "feature_label"}

    `available` is the auto-hide input (integration configured or not);
    `hidden` is the manual-hide input (the settings checkbox). The two are
    independent — a tab can be checked visible but still unavailable, or
    manually hidden despite being fully configured. `feature_off` is a third,
    independent input: whether the tab's owning feature (if any) is turned
    off in Features — a disabled feature's tab reads as turned off there
    rather than as visible, regardless of `hidden`/`available`. Callers
    combine these as needed; this just reports them so the UI can explain
    the difference.
    """
    if settings is None:
        settings = _nav_settings()
    hidden = hidden_keys(settings)
    states = []
    for tab in HIDEABLE_TABS:
        requires = tab.get("requires")
        available = _is_configured(requires, settings) if requires else True
        feature = _TAB_FEATURE.get(tab["key"])
        feature_off = bool(feature) and not feature_enabled(feature)
        states.append({
            "key": tab["key"],
            "label": tab["label"],
            "hidden": tab["key"] in hidden,
            "available": available,
            "requirement_label": REQUIREMENT_LABELS.get(requires, ""),
            "feature_off": feature_off,
            "feature_label": FEATURES[feature].label if feature else "",
        })
    return states


def hidden_keys(settings: dict[str, str] | None = None) -> set[str]:
    """The manually hidden tab keys, tolerating absent or malformed values."""
    raw = (settings if settings is not None else _nav_settings()).get("nav_hidden_tabs")
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("nav_hidden_tabs is not valid JSON; ignoring it")
        return set()
    if not isinstance(parsed, list):
        return set()
    return {k for k in parsed if isinstance(k, str) and k in HIDEABLE_KEYS}


def can_edit_catalogue(user: dict | None) -> bool:
    """Mirror library authority for presentation without promoting bearer caps."""
    if not user or user.get("api_token_id"):
        return False
    if user.get("role") == "admin":
        return True
    if not user.get("id"):
        return False
    from app.database import get_db
    from app.services import libraries

    try:
        with get_db() as db:
            return any(
                libraries.has_library_role(db, user, library_id, "editor")
                for library_id in libraries.accessible_library_ids(db, user)
            )
    except Exception:
        logger.warning("Could not resolve catalogue navigation authority", exc_info=True)
        return False


def visible_tabs(user: dict | None, *, can_edit: bool | None = None) -> list[dict]:
    """The tabs this user should see: role AND integration AND not hidden."""
    settings = _nav_settings()
    hidden = hidden_keys(settings)
    role = (user or {}).get("role")
    if can_edit is None:
        can_edit = can_edit_catalogue(user)
    tabs = []
    for tab in NAV_TABS:
        roles = tab.get("roles")
        if roles == ("admin", "editor"):
            if not can_edit:
                continue
        elif roles and role not in roles:
            continue
        requires = tab.get("requires")
        if requires and not _is_configured(requires, settings):
            continue
        feature = _TAB_FEATURE.get(tab["key"])
        if feature and not feature_enabled(feature):
            continue
        if tab["key"] in hidden:
            continue
        tabs.append({
            "key": tab["key"],
            "label": tab["label"],
            "path": tab["path"],
            "group": tab.get("group", "primary"),
            "menu": tab.get("menu", "account" if tab.get("group") == "account" else ""),
            "heading": tab.get("heading", N_("Administration") if tab.get("group") == "account" else ""),
        })
    return tabs


# Where the item-detail "back" link goes when arriving from somewhere other
# than Browse. This is a whitelist, not an echo: `back_target` is the only
# way a `?from=` value may influence emitted HTML, and any key not listed
# here — including anything crafted to look like a URL or path — silently
# degrades to the default with no `from=` param emitted at all.
BACK_TARGETS = {
    "home": ("/", N_("Back to Home")),
    "music": ("/music", N_("Back to music")),
    "series": ("/series", N_("Back to series")),
    "stats": ("/stats", N_("Back to stats")),
    "my_list": ("/my-list", N_("Back to My List")),
}
DEFAULT_BACK_TARGET = ("/browse", N_("Back to collection"))


def back_target(key: str | None) -> dict:
    """Resolve a `?from=` value to a safe back-navigation target.

    Returns `{"key": ..., "path": ..., "label": ...}`. A valid key is
    normalized and echoed back so templates can propagate it further; any
    other value (absent, unknown, or hostile — `//evil.com`,
    `javascript:alert(1)`, `../../etc`, etc.) resolves to the `/browse`
    default with `key` set to `""`, so callers never render it into a URL.
    """
    if isinstance(key, str) and key in BACK_TARGETS:
        path, label = BACK_TARGETS[key]
        return {"key": key, "path": path, "label": label}
    path, label = DEFAULT_BACK_TARGET
    return {"key": "", "path": path, "label": label}
