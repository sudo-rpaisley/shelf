# Locations

Shelf can organise physical media in nested locations instead of forcing every place into one flat list.

The fork keeps **two location domains**: item-level `items.location_id` refers
to flat `locations`; each copy's `location_id` refers to precise `location_nodes`.
They are not interchangeable IDs. A node hierarchy and its copy placement can
remain precise even when an item summary shows a bound legacy root.

A location may be as broad or as specific as you need. For example:

- `Living Room`
- `Living Room / Bookcase`
- `Living Room / Bookcase / Shelf 1`
- `Bedroom / Shelf 1`

The same label can therefore appear beneath different parents: `Shelf 1` in the living room is a different place from `Shelf 1` in the bedroom.

## Create a nested location

Open **Settings → Library → Locations**. Enter the new location label and, if it belongs inside another location, choose it as the parent. Leave the parent at **Top level** to create a top-level location.

There is no fixed room/bookcase/shelf structure. Any level can be omitted and nesting can be as deep as your collection needs.

## Move or rename a location

Existing locations can be renamed or moved beneath another parent from the same Settings card. Shelf rewrites the displayed full path for that location and all of its descendants in one transaction, so existing item assignments continue to point at the same location records.

The **Parent** list on a location's edit form offers only the places it can go inside: the location itself and everything nested beneath it are left out, because a location cannot be moved inside its own contents. Shelf still refuses such a move if one is submitted some other way.

## Delete a location

A location that still contains child locations cannot be deleted. Move or delete the children first.

Deleting a leaf node retains its copies and clears their node placement. Legacy
flat-location administration is a separate operation; it must not interpret an
equal integer as the same node or silently drop copy metadata.

## Finding what else is here

Browse's legacy Location filter uses the item's flat location projection, not
every precise copy node. For the objects on an exact shelf, use that node's
Arrange/copy view. Do not interpret a legacy-filter result as a recursive
physical inventory report. Final link/filter parity needs integration verification.

## Compatibility

`location_tree.legacy_location_to_node` and `node_to_legacy_location` use explicit
bindings. `/api/locations` remains legacy administration; `/api/location-tree`
administers nodes. No name-based guess, ID coalescing or wholesale upstream tree
conversion is performed. See [Physical copies](../item-copies.md) and
[Integration](../upstream-integration.md).

## Arrange a physical shelf

Positions are assigned as you scan in [Shelf Fill](shelf-fill.md); this page is
where you change them afterwards.

Each location can have an explicit order for the physical copies stored directly
there. In **Settings → Library → Locations**, choose **Arrange** beside a room,
bookcase or shelf. Authorized library editors can drag copies into their real left-to-right (or
top-to-bottom) order and save it.

Shelf can also auto-order that location by title, creator, series position,
release date/year or periodical issue. When optional Periodicals or Music
metadata is installed, the richer issue/release dates are used automatically;
otherwise the core catalogue fields provide the fallback order.

Ordering is copy-specific, so two copies of the same title can sit next to each
other and retain distinct positions.
