# Connected library experiment

This work lives on the experimental `new-test` branch, starting at
`100219d` (the current Shelf baseline plus the Related Media refresh fix).
It is not a release announcement or an upstream design commitment.

## Goal

Make Shelf a single front door to a mixed physical and digital library:
find an item, see its editions and availability, and open it in the service
that actually holds it. Keep the existing catalogue, copies, integrations,
and administrative Browse usable throughout.

## Implementation areas

The following code is present on this branch; “implemented” here does not mean
released or universally compatible. The canonical setup/behaviour reference is the
[connected-library user guide](user-guide/connected-library.md).

| Area | Implemented evidence | Limits |
|---|---|---|
| Unified Library | `routers/library.py`, `services/library_catalog.py`, `services/format_groups.py`, `templates/library.html` | Format-only stacks, same-member filters, same-holding source/availability, full-stack counts before pagination, local series and account-owned saved views. Browse stays separate; no remote search |
| Available here | `routers/availability.py`, `services/availability.py`, `templates/fragments/available_here.html` | Actual live copies plus cached legacy/new holdings, not live reachability. Adaptations and Trash cannot bridge stacks; ownership alone is not a copy |
| Read-only sources | `routers/sources.py`, `services/sources.py`, `services/source_links.py`, `services/source_connectors/{base,opds,jellyfin}.py` | Multi-instance OPDS 1/2 (safe generic/Calibre and open-access acquisitions) and selected Jellyfin types. Manual `links` has no test/sync and supports admin Remove link only. Bounded enumeration, complete-only absence reconciliation, config-change invalidation, no new scheduler/playback/progress sync. Legacy singleton health appears alongside new sources |
| Needs Attention and connection review | `routers/library_attention.py`, `services/library_attention.py`, `services/connection_suggestions.py` | Local quality/source queues, bounded evidence proposals, explicit accept/dismiss/restore with stale-evidence checks. Acceptance adds a format link only; Related Media unlink is undo |
| Personal activity | `routers/personal_library.py`, `services/personal_library.py`, `templates/activity.html` | Account/item-local status, progress, rating, favourites, notes/history, Continue and explicit My List/Planned Up Next. State-owned recency survives merges/reset history. No shared-state/Hardcover mutation, household isolation or cross-format progress conversion; shared legacy status still permits viewers |
| Metadata refresh safety | `services/metadata_policy.py`, `services/sources.py`, ABS/Komga/RomM persistence paths, `services/item_merge.py` | Validated new-source descriptive projection, bounded searchable provider IDs, instance/hashed-identity provenance and three-way refresh. Known edits/clears and unknown nonempty legacy baselines are preserved. Merged observations do not grant losing-source authority over kept manual fields |
| Saved metadata review | `services/metadata_review.py`, `routers/metadata_review.py`, `templates/fragments/metadata_provenance.html`, `routers/pages.py` | Registered/included item panel; viewer GET `/api/library/items/{item_id}/metadata`, editor POST `/api/library/items/{item_id}/metadata/resolve` with `keep_local`/`follow_source`, full saved-row fingerprint checks and read-only identity fields |
| Cover decisions | `services/cover_sync_policy.py`, `routers/cover_sync_policy.py`, `routers/items_covers.py`, ABS/Komga/RomM cover paths, `templates/item_detail.html` | Deliberate removal and explicit Allow missing-cover sync again have a separate durable decision, not queue dismissal. Source-fill guards recheck before writing, manual choice remains possible, and the existing book-cover queue remains a separate path |
| Navigation, schema and feature gates | `app/nav.py`, `app/features.py`, `app/database.py`, `app/main.py` | Standard includes `connected_library`; migration 58 preserves minimal upgrades and saved choices, migration 59 adds personal-state event order. Feature off hides/refuses without data deletion; private HTML defaults to no-store, Store remains separate |

### Persistence and recovery boundary

`MIGRATION_TABLES` owns eight additive tables: `source_instances`,
`source_records`, `user_item_state`, `user_item_events`, `library_saved_views`,
`connection_dismissals`, `item_metadata_provenance` and `cover_sync_blocks`.
Migration 59 adds state-owned `last_event_id`, default 0 rather than an inference
from history. The tables start empty rather than converting singleton sources
or shared status. Full database backup retains
them; covers and matching external encryption/signing keys must be preserved
separately. Credentials are encrypted with the existing external key and are
never rendered/exported as raw secrets.

The portable collection archive intentionally excludes all eight tables and
therefore the new connection config, credentials, bindings, personal
state/history (including ratings), saved views, connection dismissals, metadata
provenance and cover-sync suppression/opt-in decisions.
It is not a personal/source portability mechanism. Back up before experimenting;
disable Connected library in Settings → Features to hide it without deleting
data. All imported catalogue entries remain visible to all Shelf users in the
existing shared library.

## Invariants

- A format link is not an adaptation. Do not collapse all Related Media edges
  into equivalent-format groups.
- Never automatically merge records from title similarity. Uncertain matches
  require review; actions must be reversible.
- Only complete successful source enumeration may mark an unseen remote
  record unavailable. A timeout must not delete catalogue entries.
- Remote metadata is provenance, not permission to overwrite local edits.
- Credentials are encrypted at rest, never rendered or exported as raw secrets,
  and are not included in browser-facing links. A database backup retains the
  ciphertext, not its external key.
- All catalogue and copy reads use the existing live views. Background sync
  must not resurrect Trash.
- Roles, CSRF, optional-feature gates, CSP, and local-network integrations
  keep their existing guarantees.
- Preserve the single-container FastAPI/HTMX/SQLite architecture. No new
  mandatory external service or frontend rewrite.

## Verification and limits

Final local verification passed **7515 unit/integration tests**, **384 browser
tests**, `make checks-fast`, dependency consistency and staged whitespace
checks. The implementation stayed frozen throughout both full suites. See the
[verification record](connected-library-verification.md) for commands, earlier
failures/fixes and intentional exclusions. The private roadmap map was not
available. Local Alpine Python 3.14 / fallback Playwright 1.65.1 / Chromium 155
is not the pinned Python 3.12 / Playwright 1.52.0 CI runtime.

Metadata-review routes are registered in `app/main.py`/the feature registry and
their context/fragment is included on the item page. Sources' page/API routers
are registered explicitly, not through a combined `sources.router`. Registration
is also covered by the local route, feature and browser checks.

Source adapters can be checked against representative local fixtures;
compatibility with a particular live server cannot be claimed without testing
that server. The guide records the implemented adapter limits; native playback,
remote progress and commercial-service compatibility were not inferred from
these tests.

This branch does not promise every commercial provider, DRM handling,
playback/transcoding, or cross-format position conversion. Digital Video and
TV Series are distinct catalogue types, not physical DVDs. Supported sources
and intentional limits are documented in the guide; none is a universal
commercial-library connector or an OPDS-serving/hosted-reader implementation.
