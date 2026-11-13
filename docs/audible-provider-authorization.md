# Audible catalogue prototype: permission blocker

**R7 is not complete. Direct provider access is blocked by default.** No
applicable grant has been substantiated for this adapter's direct Audible API
queries, permanent metadata observations, image proxying and local artwork
storage. Public reachability, an ASIN match, technical host allowlists, a
successful historical response or an operator opt-in are **not a licence**.

The retained code and mocked tests are technical preparation, not an authorised
provider integration. The routing allowlist is not a ten-market coverage claim.
There is no automatic fallback, Audnexus switch, scraping, account login, audio
download, DRM handling or restriction bypass.

## Containment configuration

The provider-specific egress gate is independent of **Connected library** and is
closed unless **both** process environment values are explicitly configured:

| Variable | Default | Meaning |
|---|---|---|
| `SHELF_AUDIBLE_CATALOG_ENABLED` | unset/off | Only the exact value `1` passes the configuration check |
| `SHELF_AUDIBLE_CATALOG_GRANT_REFERENCE` | unset | Nonempty, printable audit reference, at most 512 characters; never echoed to the browser or sent to the provider |

**Do not configure live enablement without first obtaining and reviewing an
actual applicable grant.** The reference is only an operator audit pointer: code
cannot verify that a contract exists or covers this use. A fabricated reference,
general retail terms, endpoint access, API credentials alone or a switch do not
establish permission. No generally available applicable grant is asserted here.

Before any future enablement, the operator must verify the grant's scope for the
exact API/CDN access method, intended marketplaces, persistent observations,
proxying, storage, retention, redistribution and applicable use restrictions.
If a grant requires another access method, authentication or other conditions,
this prototype must not be enabled unchanged. Keep both values unset while that
review remains unresolved. There is intentionally no review-page enable button.

With the gate closed, lookup, preview-image GET and Apply refuse even previously
signed tickets. Direct service lookups, downloads and staging are also guarded;
configuration is checked again around outbound waits and before returning or
storing provider results. No provider fallback is attempted. A request already
on the wire cannot be recalled; removing configuration blocks subsequent hops
and rejects its result rather than storing/applying it.

Local ASIN storage/editing, local file import and existing catalogue records are
not disabled or erased. Other Connected library functionality is not switched
off. Existing saved observations remain local/read-only; this containment does
not settle their retention rights. Review any earlier stored provider material
against applicable rights without treating a backup or this gate as permission.

## Verification boundary

Technical tests explicitly request a disposable **mock-only** authorisation
fixture and stub or deny network operations. Its fictional audit reference is
not a grant and must never be used for live requests. Separate default-off tests
assert zero outbound work, blocked review controls and cached-ticket refusal.
No new live calls, provider contacts, signups or credential acquisition are part
of this containment change. Earlier reachability evidence does not establish
permission, present compatibility, image rights or regional coverage.
