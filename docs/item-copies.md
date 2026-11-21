# Physical copies

The catalogue item describes an edition; `item_copies` describes the individual
physical objects. Each copy retains its own ID, number, primary flag, barcode,
precise location, shelf order, condition, acquisition date/source/price,
provenance, notes, missing mark and deletion history. Digital availability belongs
to digital holdings, not a copy manufactured from a remote link.

This page describes the integrated fork contract, not upstream's former copy
conversion policy. See [integration status](upstream-integration.md) for pending
runtime verification.

## Two location namespaces

| Field | References |
|---|---|
| `items.location_id` | legacy flat `locations.id` |
| `item_copies.location_id` | precise `location_nodes.id` |

These IDs are **not interchangeable**, even when equal. Explicit
`legacy_location_to_node` / `node_to_legacy_location` bindings translate between
them. A child shelf may project to its bound legacy root for an item-level
summary without losing the copy's precise placement. Never join a copy's ID to
`locations`, coalesce the two integers or infer a binding from a name.

Copy controls, Shelf Fill, Arrange, labels and audits use node IDs. Item edit,
legacy Browse location filters and `/api/locations` use flat IDs;
`/api/location-tree` administers nodes. A compatibility projection must preserve
existing copy IDs, child placements, barcodes and order, not recreate copies on
every metadata update.

## Schema and write ownership

`app/database.py` owns schema, lineage admission and one-time backfills. Existing
fork foundation markers and migration history prevent a later ensure/read from
replaying a sweep. Upstream's historical "every location becomes the hierarchy"
instructions do not convert this fork's already distinct domains.

`app/services/item_copies.py` owns copy insert/update/lifecycle operations;
`holdings.sync_item` is the compatibility projection handoff. First physical
acquisition is explicit. An ordinary metadata edit must not recreate an
intentional zero-copy archive state or delete plural digital identities.
Copy creation remains restricted to eligible owned physical media.

The copy writer checks the parent item's library role under the write lock.
Routes bind a copy to the item in the URL under that same lock; global Editor
alone is not sufficient. Unknown fields and invalid locations refuse. Changing
location clears node-scoped order unless an explicit order accompanies the move.
The schema permits at most one primary, unique `(item_id, copy_number)`, and a
unique barcode across live and trashed copies.

## Adding, removing and restoring

**Add copy** creates another object without moving the existing ones. It numbers
above retained history and makes the first live copy primary. **Edit** changes
only that copy; secondary-copy movement does not relocate its siblings.

**Remove copy** uses native Trash, retaining condition, acquisition data,
provenance and barcode:

- Removing a secondary leaves the primary alone.
- Removing a primary when others remain promotes the lowest-numbered live
  survivor, then updates the item's legacy projection through explicit conversion.
- **Removing the last live copy of an owned physical item is refused.** Do not
  substitute upstream's former "clear the item's location and allow zero" rule.
  Zero-copy import/history states can exist, but are not permission to remove
  the final owned copy through ordinary controls.

Copy form refusals return HTTP **200 with an HTMX error fragment**, including
barcode collisions and last-copy refusal. The error must be displayed, not
treated as a successful removal; inaccessible collision owners are never named.

Trashing a copy demotes it while stamping `deleted_at`, so it does not retain
the primary slot. Restore makes it secondary unless no live primary remains.
Restore is refused while the parent item is in Trash: restore the parent first.
Trashing the item retains all its copy rows and hides them with the item;
permanent admin purge is distinct and irreversible. Merges must move all copy
children, including trashed ones, before removing a merged-away item.

## Labels, missing marks and reads

Generated codes use the configured prefix (`SHF` by default) and a sequence that
includes retained trashed codes. Hand-entered barcodes stay exact and
case-sensitive. Label scans identify one copy; ISBN/UPC scans identify an item
and cannot choose arbitrarily among physical copies. See
[Labels](user-guide/labels.md), [Scanning](user-guide/scanning.md) and
[Audits](user-guide/audits.md).

A missing mark is reversible, not deletion. **Found** or an appropriate scan of
that copy clears it. `copies_live` filters both copy and parent deletion and,
for an authenticated actor, library visibility. Physical-table reads used for
restore/collision/export need explicit equivalent authorization; they are not
general bypasses.

An archive restores copies only for newly created items; matched/restored items
keep their local copies rather than guessing a merge by copy number. Copy
locations must round-trip as node paths through explicit translation, not flat
IDs. Archive, audit and projection round-trips remain final verification gates.
