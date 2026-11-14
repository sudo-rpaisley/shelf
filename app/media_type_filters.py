"""Pure, strict CSV transport for registry-backed media-format filters.

An absent/default or empty string means no predicate, including unknown legacy
types. Nonempty input is never repaired into a broader selection. Consumers
choose their refusal surface; this parser exposes only one fixed error code.
"""

from app import media_presentation


INVALID_TYPES = "invalid_types"
MAX_TYPES_CHARACTERS = 1024
MAX_TYPES_TOKENS = 32


def parse_types(value: object = "") -> tuple[str, ...]:
    """Return canonical-order types; reject malformed input before deduplication.

    The registry validates exact canonical keys via format selectors, not its
    broader family/all vocabulary or config's legacy input aliases.
    """
    if not isinstance(value, str) or len(value) > MAX_TYPES_CHARACTERS:
        raise ValueError(INVALID_TYPES)
    if not value:
        return ()
    if value.count(",") + 1 > MAX_TYPES_TOKENS:
        raise ValueError(INVALID_TYPES)
    try:
        return media_presentation.expand_selectors(tuple(f"format:{token}" for token in value.split(",")))
    except ValueError:
        raise ValueError(INVALID_TYPES) from None
