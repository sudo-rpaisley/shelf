# Item copies design spike

This branch is an internal design spike for upstream issue dgahagan/shelf#97.
It is **not** an upstream-ready implementation yet.

## Decisions being tested

- A catalogue item may have zero, one, or many physical-copy rows.
- `items` remains the shared catalogue record. Copy rows hold object-specific facts.
- Existing `items.owned` remains independent. Copies do not redefine wishlist/ownership semantics.
- Existing flat `locations` are reused for this first model; hierarchical locations are a later concern.
- Migration/backfill must not infer physical/digital status from `media_type`.
- An existing owned item is automatically projected only when it already has an explicit `items.location_id`.
- One copy may be marked primary solely as a compatibility seam for the legacy item-level location field.
- At most one primary copy is allowed per item.
- Copy numbers are unique only within an item; two copies of the same item may share a location.
- A local copy barcode/accession label is globally unique.
- Copy-specific lending, hierarchical location order, valuation, and service-backed digital holdings are deliberately out of scope.

## Proposed copy fields

- `item_id`
- `copy_number`
- existing flat `location_id`
- `condition`
- `notes`
- `acquired_date`
- `acquisition_source`
- `acquisition_price`
- `provenance`
- `copy_barcode`
- `is_primary` compatibility flag
- timestamps

The roadmap mentions per-copy edition information. This spike does not add an edition field yet because Shelf's current catalogue row already represents an edition through ISBN and other shared metadata. That boundary needs maintainer agreement before it is encoded in a migration.

## What a real PR would still need

1. Append-only migrations plus fresh-schema parity in `app/database.py`.
2. Integration with the central `item_write` funnel so legacy flat location updates and the primary copy cannot drift.
3. An item-detail/editor surface for adding and editing copies.
4. Changelog and user documentation.
5. Full unit, integration, CSS and E2E validation.

The design spike intentionally stops before those steps until the upstream issue settles the model.
