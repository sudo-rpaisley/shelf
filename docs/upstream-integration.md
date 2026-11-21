# Fork / upstream integration

This page records the integration contract for the `sudo-rpaisley/shelf` fork,
combining fork `43e244f5` with upstream `dgahagan/shelf` at `7e0369e7`.
It is not an upstream release announcement or a production migration receipt.

**Status:** local offline qualification **PASSED** for Candidate006. Both complete,
uncapped offline runs finished against unchanged frozen inputs. This is not
staging, merge, main-branch publication, hosted-CI or production-readiness approval;
no other gate is waived.

## Local offline qualification

| Complete gate | Selection | Passed | Failures / errors / skipped |
| --- | --- | ---: | --- |
| Unit/integration | `tests --ignore=tests/e2e --ignore=tests/contract -m "not live"` | 10,564 | 0 / 0 / 0 |
| Browser | `tests/e2e -m "e2e and not live"` | 621 | 0 / 0 / 0 |

- Separate fresh disposable directories were used. External-provider traffic was
  blocked; real loopback HTTP tests were included.
- The local environment was Linux aarch64/musl, Python 3.12.13, SQLite 3.50.4,
  Chromium 156.0.8078.79 and Playwright 1.52.0. These results do not qualify
  Playwright's bundled browser or Ubuntu 24.04 hosted CI.
- Source-bound offline checks, lock/hash consistency and its negative self-test,
  the runtime dependency audit (no known vulnerabilities reported), four JavaScript
  source-model collaborators under Node 24.18.1 and an independent generated-CSS
  byte comparison passed. The license command produced an inventory, not legal
  approval. The private `.devdocs` roadmap check was not applicable in the public
  clone; that is not a skipped test in either complete suite.

Candidate005 complete offline runs remain failed: 10,399 unit passes/27 failures
and one teardown error, and 611 browser passes/eight failures. Its separate
critical gates passed 817 unit and 20 browser cases, but do not qualify the failed
complete unions.

Historical Candidate003 complete results remain 9,250 unit passes/404 failures and
521 browser passes/78 failures/12 errors; its separate 76-case critical gate passed.
Candidate004 critical results were 763 unit passes/10 failures and 18 browser
passes/two failures. These historical results are not passes for changed inputs.
No live-provider contract tests, live-database migration, deployment, image
publication, tag/release creation or workflow dispatch was performed as part of
this work. These checks are not crash/power-loss or engine-I/O durability proof.
Generated badges describe collected integrated cases, not evidence that those
cases passed. Capped attempts and collection counts are not suite passes.

## Database compatibility

- The supported admission paths are a genuinely fresh database and positively
  identified fork/common-prefix histories. An upstream-origin, mixed, unknown or
  inconsistent history is rejected before schema mutation. Matching a version
  number alone is not evidence of compatibility.
- Published fork migration identities and their history are preserved, including
  the private-state and library migrations. Version 47 is not repurposed.
  Adapted upstream operations have new identities above 66 and explicit
  provenance; upstream release numbers are not fork migration numbers.
- Schema ownership is centralized in `app/database.py`. Service `ensure_schema`
  calls validate initialized capabilities; they are not permission-granting,
  private-state-reseeding or independent migration entry points.
- Personal-state snapshots and legacy membership seeds are one-time operations
  for recognized pending histories. New accounts start clean; reading a page or
  restarting does not regrant revoked access or copy another user's history.
- There is **no transparent upstream-database conversion promise**. Do not erase
  `schema_version`, edit provenance, or relabel an upstream backup to force it
  through the fork initializer. A portable archive is a separate, previewed
  catalogue import, not a database-lineage converter or full private-state backup.

Take a consistent backup before any upgrade and rehearse it against a disposable
copy only after validation. Downgrades require the matching old source/image and
the backup from before the upgrade; running an older image on an upgraded volume
is not a rollback.

## Identity, libraries and private activity

Libraries are the catalogue permission boundary. Non-admin users need membership
in the item's library: library Viewer permits reading; library Editor permits
catalogue writes. A global Editor role alone does not grant access to every
library, and a global Viewer can be a library Editor. Global administrators retain
administration and purge authority. Feature flags, CSRF protection and item/copy
parent checks still apply.

Library Editor authority also covers bulk metadata/language edits and merges;
every extant selected parent must authorize before any mutation. Unmapped items
stay invisible on ordinary pages, including to administrators. Native account
administrators can explicitly recover them at **Settings → Library → Library
recovery**: the locked operation adds only a library mapping, preserving Trash,
copies and private history. Bearer capabilities cannot use or elevate into this
boundary; persisted account identity, role and captured session version are checked.

Bearer API tokens remain read-only `GET /api/v1/` capabilities. A token's owning
account determines read visibility, but an administrator account or library Editor
membership never lifts the token above its viewer capability cap.

Status, dates, completion history, progress, rating, favourites, personal wishlist
and notes belong to the signed-in user. They are not shared item metadata.
The **shared acquisition wishlist** (`wishlisted`, backed by lists) is separate
from the **personal wishlist** (`wishlist`, in user state): a user can personally
wish for an item the household already owns. Collections organize the catalogue;
they and physical locations are not substitute security boundaries.

The fork retains My List, Continue / Up Next, provider-backed series details,
multiple series memberships, Collections and precise physical placements alongside
upstream's feature profiles, music releases, periodicals, audits, labels, release
radar, price alerts and reversible Trash. These surfaces are included in the
complete offline unit/browser qualification, not a live-provider validation.

Hidden live or trashed identifier collisions must refuse without revealing the
blocking title, identifier or library, restoring it, or moving it into another
library. Merges must retain all children or refuse conflicting private, library,
series or connector identities atomically. Ordinary deletion uses Trash; no
destructive boot-time `kids_book` retirement or twin consolidation is performed.
Existing legacy media IDs remain visible and usable, rather than being silently
hidden or rewritten to satisfy upstream's former conversion policy.

New `kids_book` input uses the canonical book category. Archive matching prefers
an exact existing legacy identity, including Trash, before considering a guarded
canonical book fallback. This never consolidates existing book/kids-book twins.
The editor also follows native legacy-ISBN compatibility: an unchanged pre-existing
junk ISBN may be retained during an unrelated edit; a new or changed invalid ISBN
is rejected before writes, and explicit clearing remains supported.

## Copies and two location domains

`items.location_id` names a legacy flat **locations** row.
`item_copies.location_id` names a precise **location_nodes** row. These are
different namespaces even when their integers happen to match. Conversion uses
explicit legacy/node bindings, not integer coalescing or a matching display name.

Existing copy IDs, barcodes, order, acquisition metadata and precise child-node
placements are preserved. Physical/digital eligibility remains deliberate;
digital availability is not manufactured into a physical copy. Ordinary copy
removal moves it to Trash, but removing the last live copy of an owned physical
item is refused. Copy form failures remain HTTP **200 HTMX error fragments**,
not silent success and not an upstream-style blanket 400 response. Barcode
collision messages must not disclose an inaccessible item.
Correctable stale/wrong/missing child operations on an authorized parent use the
same complete 200 error fragment/toast with no catalogue effects. A missing or
hidden parent remains 404; a visible read-only mutation remains 403. Trusted
maintenance can update/reindex Trash metadata without restoring it or creating
copies; ordinary HTTP Trash editing remains refused.

Merging reprojects the finalized primary copy into the legacy seam before metadata
or ownership promotion. It must not relocate a retained copy, erase its shelf
order, or manufacture a placeholder for an intentionally zero-copy archive.

## Connector identity

One canonical transaction/writer reconciles exact provider attachments across
inline IDs, modern sidecar records and `digital_holdings`. Plural attached
identities are not deleted to fit a single inline field. Exact local/provider
identity takes priority over ISBN adoption; ambiguous kind, platform, library or
ownership translation is refused rather than guessed from a title. Only one
scheduler per connector may write. A provider's library ID is not a Shelf
permission-library ID.

New Komga and RomM imports are digital. Exact historical physical/manual
attachments remain attached without takeover or retyping. Provider-owned cleanup
moves excluded items to Trash once; adopted items detach, and retained holdings
protect the item. RomM cover requests stay on the configured origin, never send
credentials externally, and do not follow cover redirects. Cover downloads stage
off-target, recheck identity/liveness/authority under the publication lock, and
retain file recovery until the real outer transaction outcome.

Hardcover status links and reading updates are account-owned. Scheduled pulls
require a configured, still-valid owner; an unconfigured legacy schedule may use
only a positively sole account. A multi-account installation must choose an owner
explicitly. Refused pulls do not receive a success timestamp.

## Imports, Store and Browse

Imports and exports must obey library visibility and preserve actor ownership of
personal activity. A catalogue archive is not an export of every user's private
history, and file-local IDs do not grant access to a receiving library. Use a
database backup for full-instance recovery. Inspect an archive before sharing:
catalogue notes, loans, provenance, covers and deleted catalogue records can be
sensitive even when credentials are absent. Archive/CSV privacy and location
round-trip checks are included in the passed offline qualification.

Portable archive version 3 carries explicit node paths, legacy bindings and copy
provenance; v1/v2 readers remain supported. Operational audit missing marks are
not portable catalogue metadata: a newly imported copy is not marked missing
until a local audit establishes it. Export does not clear the source's marks or
overwrite a matched receiving copy's local history. Each export owns a separate
0600 temporary file. Cover-file recovery remains owned by the actual outer
database transaction, not merely the import savepoint.

Critical resource journals require the managed connection's protected outcome
capability, not just ordinary callback lists. Cover publication and archive cover
writes share a process-safe directory fence acquired before reading preimages.
Critical undo precedes native rollback; the retained fence also protects recovery
when a failed commit has already unlocked SQLite. Ordinary cache callbacks remain
post-unlock. Local recovery failures stay rollback-only rather than being silently
finalized by an unrelated commit; recovery errors retain their backups and surface.
The stable lock file is outside the served covers directory and must not be
unlinked while writers may use it. All shared destination writers must cooperate;
old non-cooperating processes require quiescence before switching versions.
Legacy direct helpers fence only their final file write, not a later SQL update.
These contracts do not claim crash/power-loss or failing-filesystem recovery proof.

CSV physical placement exports the actual primary-copy NODE path and resolves
existing paths with explicit FLAT projection, preserving secondary copies.
An existing/restored zero-copy item can retain a representable mapped-root FLAT
placement; a precise child/unmapped path is refused rather than silently flattened
or represented by a fabricated copy. Unknown nested hierarchies are not invented.

Inventory uses native audit sessions for durable copy-by-copy accounting. The
retained `/api/inventory/missing` compatibility report is read-only, takes a NODE
ID and an item-level `scanned_ids` set, and uses the same scoped expected census.
One named item accounts for all its expected copies at that node; reading the
report does not move, mark or create copies or record an audit.

Database restore validates and migrates staging before replacing live pages.
Sensitive settings are normalized under existing destination keys, imported
database signing-key material is removed, and restored cookie/API capabilities
are invalidated before installation. Unknown ciphertext is retained and reported
for credential re-entry. Restore never creates or replaces external key files.
A unique durable pre-restore snapshot is retained; native SQLite backup preserves
WAL transaction semantics rather than replacing a database file under open WAL
connections. Process-interruption checks are not a power-loss durability claim.

Store keeps strict invalid-input behavior: an invalid code returns **invalid**
and inserts no item. It does not create an `Unreadable barcode` placeholder.
Valid-code responses retain their client `code`, ownership and queue semantics;
offline queue, feature-gate and error-display browser checks passed in the
complete offline run.

Browse has **one actual search input**, responsive at all widths. Filters,
bookmarks, keyboard focus, long-title truncation and narrow-screen accessibility
must work without a second hidden search field or dummy markup.

Core 977 assistance and confirmation remain available with optional features off;
successful confirmation must lead to the saved core item when Periodicals is off.
Provider schedule configuration remains reachable while its connector is off,
without starting a job. Optional browse/use controls remain gated or use safe
core Browse fallbacks. ISSN lookup defaults to portal-first fallback; an explicit
endpoint override stays single-target, including on failure.

Native automatic 977 scanning uses only the ISSN Portal publication resolver and
does not spend UPC lookups on a valid serial. The fork's full magazine-assistance
ladder—Google Books, ISSN Portal, Crossref, then UPC—belongs to an explicit barcode
assistance action on **Magazines**, not an automatic extra lookup in core Scan.
Both retain exact carrier/add-on context and leave concrete issue identity for
confirmation. A barcode-derived ISSN hint is not a provider-confirmed publication
identity. Optional magazine assistance remains editor/library/feature guarded;
native core confirmation remains usable with optional features disabled.

## Build, support and publication

Fork CI is read-only: no auto-restamp, bot commit or push-back job. Authors
generate and commit `static/css/app.css`, the service-worker `SW_VERSION` and
README badge values in the fork PR that changes their inputs. Regenerate after
reconciling source; never invent a digest or test count. Collection counts are not
test passes. CI checks the candidate rather than repairing it after merge.

The official `dangahagan/shelf` images, attestations and upstream releases belong
to `dgahagan/shelf`. This fork does not publish those images and does not enable
an upstream Docker publishing workflow. Use a separate data directory when trying
fork source; never point an upstream image at an integrated-fork database on the
assumption that release numbers imply compatibility.

Fork support is in [SUPPORT.md](../SUPPORT.md); report vulnerabilities privately
using [SECURITY.md](../SECURITY.md). Historical entries in the
[changelog](../CHANGELOG.md), [GOTCHAS](../GOTCHAS.md) and upstream upgrade notes
are retained as origin-qualified history, not permission to reinstate upstream
CI writers, destructive conversions, invalid Store placeholders or zero-copy
removal policy.
