"""Default-off containment, NOT a provider licence or a grant validator.

No applicable grant has been substantiated for this adapter's catalogue queries,
persistent observations, image proxying and artwork storage. Operators must not
configure live access unless an actual applicable grant has first been obtained
and reviewed. An environment switch/reference cannot create or prove permission.
"""

import os

from app import i18n


ENABLE_ENV = "SHELF_AUDIBLE_CATALOG_ENABLED"
GRANT_ENV = "SHELF_AUDIBLE_CATALOG_GRANT_REFERENCE"
BLOCKED_MESSAGE = i18n.N_(
    "Audible provider access is blocked. No applicable provider authorisation has been established for catalogue queries, saved observations, image proxying and artwork storage. Operator opt-in is not a licence."
)


class ProviderBlocked(ValueError):
    status_code = 403

    def __init__(self):
        super().__init__(i18n.gettext(BLOCKED_MESSAGE))


def enabled():
    """Only an explicit configuration plus an audit reference passes this gate.

    This is a syntactic/configuration check only, never a determination that the
    referenced grant actually permits use. No cache, DB write, log or egress.
    """
    reference = os.environ.get(GRANT_ENV, "")
    return (os.environ.get(ENABLE_ENV) == "1" and 0 < len(reference) <= 512
            and bool(reference.strip()) and reference.isprintable())


def require_enabled():
    if not enabled():
        raise ProviderBlocked()
