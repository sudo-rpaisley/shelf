import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATABASE_PATH = DATA_DIR / "shelf.db"
COVERS_DIR = DATA_DIR / "covers"

MEDIA_TYPES = {
    "book": "Book",
    "kids_book": "Kids Book",
    "audiobook": "Audiobook",
    "ebook": "eBook",
    "magazine": "Magazine",
    "dvd": "DVD / Blu-ray",
    "vinyl": "Vinyl",
    "cassette": "Cassette",
    "cd": "CD",
    "digital_music": "Digital Music",
    "music_other": "Other Music Format",
    "comic": "Comic / Graphic Novel",
    "digital_comic": "Digital Comic",
    "manga": "Manga",
    "digital_manga": "Digital Manga",
    "video_game": "Video Game",
    "digital_game": "Digital Game",
}

# Music is a first-class media family. Keep this declaration beside
# MEDIA_TYPES so routes/templates/services can share one membership test
# instead of scattering near-identical tuples across the application.
MUSIC_MEDIA_TYPES = frozenset({
    "vinyl",
    "cassette",
    "cd",
    "digital_music",
    "music_other",
})

# Shelf's user-facing library sections are broader than storage formats.
# Keep family membership here so Home, Collection filters, statistics and
# future integrations all agree on where a media type belongs. Tuples are
# deliberately ordered for predictable display and query construction.
MEDIA_FAMILIES = {
    "books": {
        "label": "Books",
        "types": ("book", "kids_book", "ebook"),
    },
    "magazines": {
        "label": "Magazines",
        "types": ("magazine",),
    },
    "comics": {
        "label": "Comics",
        "types": ("comic", "digital_comic"),
    },
    "manga": {
        "label": "Manga",
        "types": ("manga", "digital_manga"),
    },
    "music": {
        "label": "Music",
        "types": ("vinyl", "cassette", "cd", "digital_music", "music_other"),
    },
    "film": {
        "label": "Film & TV",
        "types": ("dvd",),
    },
    "games": {
        "label": "Games",
        "types": ("video_game", "digital_game"),
    },
    "audiobooks": {
        "label": "Audiobooks",
        "types": ("audiobook",),
    },
}

# The book-shaped family: media types that are read, can carry ISBN identity,
# and participate in series/reading workflows. Digital comics belong here for
# validation and controls even though they live in the Comics browse family.
BOOK_MEDIA_TYPES = frozenset({
    "book", "kids_book", "audiobook", "ebook", "comic", "digital_comic"
})

# Seed data — runtime platform list comes from game_platforms table
GAME_PLATFORMS = {
    "atari2600": "Atari 2600",
    "atari5200": "Atari 5200",
    "atari7800": "Atari 7800",
    "nes": "NES",
    "snes": "SNES",
    "n64": "Nintendo 64",
    "gamecube": "GameCube",
    "wii": "Wii",
    "wiiu": "Wii U",
    "switch": "Nintendo Switch",
    "gameboy": "Game Boy",
    "gba": "Game Boy Advance",
    "nds": "Nintendo DS",
    "3ds": "Nintendo 3DS",
    "genesis": "Sega Genesis",
    "saturn": "Sega Saturn",
    "dreamcast": "Dreamcast",
    "ps1": "PlayStation",
    "ps2": "PlayStation 2",
    "ps3": "PlayStation 3",
    "ps4": "PlayStation 4",
    "ps5": "PlayStation 5",
    "psp": "PSP",
    "vita": "PS Vita",
    "xbox": "Xbox",
    "xbox360": "Xbox 360",
    "xboxone": "Xbox One",
    "xboxsx": "Xbox Series X/S",
    "pc": "PC",
    "other": "Other",
}

# --- Photo-intake tiling / cost estimation -------------------------------
# Per-model ingest caps: the resolution the provider actually feeds the model.
# Anthropic high-res models (Opus 4.7+, Sonnet 5, Fable 5) accept up to 2576px
# on the long edge (~3.75MP); older models downscale to 1568px (~1.15MP).
# Matched by substring against the configured model id.
ANTHROPIC_HIGHRES_MODELS = ("opus-4-7", "opus-4-8", "sonnet-5", "fable-5")
ANTHROPIC_HIGHRES_CAP = {"long_edge": 2576, "max_pixels": 3_750_000}
ANTHROPIC_STANDARD_CAP = {"long_edge": 1568, "max_pixels": 1_150_000}
OLLAMA_DEFAULT_INGEST_LONG_EDGE = 1024  # gemma3 crops at 896px; qwen2.5vl is dynamic
# OpenAI-compatible endpoints downscale to ~2048px on the long edge for
# high-detail vision. Operator-tunable since compatible servers vary.
OPENAI_DEFAULT_INGEST_LONG_EDGE = 2048

# Downscale factor at or above which the "what the model sees" preview and
# the tiling offer appear. Below it the single-image path runs unchanged.
TILING_THRESHOLD = 1.5

# Absolute source long-edge (pixels) below which a photo is flagged low-res,
# separate from the tiling decision. A 1080p video-track grab (1920) and a
# messaging-app-recompressed library photo fall below it; a native phone
# still (>=3000) sits above it. This is absolute source pixels, not a ratio
# to the provider cap -- a ratio would double-fire with the tiling card in
# the [1.5, 2) factor band and mis-fire on the Anthropic high-res 2576 cap
# and on operator-raised Ollama caps.
LOW_RES_LONG_EDGE = 2400

# Directional overlap: vertical cut lines bisect spines, so they get generous
# overlap; horizontal cuts run between shelf rows and need little.
TILE_OVERLAP_X = 0.12  # fraction of tile width
TILE_OVERLAP_Y = 0.05  # fraction of tile height

# Above this tile count, submit per-tile and dedup in code instead of one
# multi-image request (keeps request size and merge quality manageable).
MAX_TILES_PER_REQUEST = 16

# Image input tokens ~= (w * h) / 750, capped per image at the model max.
IMAGE_TOKEN_DIVISOR = 750
ANTHROPIC_HIGHRES_IMAGE_TOKEN_CAP = 4784
ANTHROPIC_STANDARD_IMAGE_TOKEN_CAP = 1600

# USD per million tokens (input, output), matched by substring on model id.
# Output dominates cost and scales with book count, not tile count.
VISION_PRICING = {
    "fable-5": (10.00, 50.00),
    "opus": (5.00, 25.00),
    "sonnet": (3.00, 15.00),
    "haiku": (1.00, 5.00),
}
VISION_PRICING_DEFAULT = (5.00, 25.00)
PROMPT_OVERHEAD_TOKENS = 500  # unified spine+cover prompt + JSON schema scaffolding
TOKENS_PER_BOOK = 60  # ~1 JSON row, e.g. {"title": "Dune", "authors": "Frank Herbert", "isbn": "9780441172719", "source": "read"}
EXPECTED_BOOKS_PER_MEGAPIXEL = 8  # rough spine density for output estimate
EXPECTED_BOOKS_MIN = 20
EXPECTED_BOOKS_MAX = 200

# Minimum seconds between requests to a given outbound host, enforced by
# app.services.outbound.acquire(). A host that is absent means "no pacing"
# (interval 0.0) — that is the correct entry for LAN/self-hosted targets and
# for hosts that publish no limit, not an oversight.
#
# Read this table as `app.config.HOST_RATE_LIMITS` at call time. A
# `from app.config import HOST_RATE_LIMITS` freezes the reference at import
# and breaks test overrides (the "Config import trap" in CLAUDE.md); tests
# override single hosts with monkeypatch.setitem.
HOST_RATE_LIMITS = {
    "openlibrary.org": 0.25,
    "covers.openlibrary.org": 0.25,
    "www.googleapis.com": 0.1,
    "api.hardcover.app": 0.2,
    "api.igdb.com": 0.25,
    "id.twitch.tv": 0.25,
    "api.themoviedb.org": 0.2,
    "musicbrainz.org": 1.0,
    "api.discogs.com": 1.0,
}

# Common outbound timeout used by provider clients.
HTTP_TIMEOUT = 20.0

# Environment variables that can override stored sensitive settings.
SECRET_ENV_VARS = {
    "hardcover_token": "HARDCOVER_TOKEN",
    "google_books_api_key": "GOOGLE_BOOKS_API_KEY",
    "tmdb_api_key": "TMDB_API_KEY",
    "igdb_client_id": "IGDB_CLIENT_ID",
    "igdb_client_secret": "IGDB_CLIENT_SECRET",
    "audiobookshelf_api_key": "AUDIOBOOKSHELF_API_KEY",
    "komga_api_key": "KOMGA_API_KEY",
    "romm_token": "ROMM_TOKEN",
    "musicbrainz_contact": "MUSICBRAINZ_CONTACT",
    "discogs_token": "DISCOGS_TOKEN",
}


def get_setting_value(key: str, stored_value: str | None = None) -> str:
    """Return env override for a setting, or its stored value/default."""
    env_name = SECRET_ENV_VARS.get(key)
    if env_name:
        env_value = os.environ.get(env_name)
        if env_value is not None:
            return env_value
    return stored_value or ""
