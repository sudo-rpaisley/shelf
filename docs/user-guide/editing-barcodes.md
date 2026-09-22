# Editing retail barcodes

Shelf's item editor can correct both book ISBNs and retail UPC/EAN identifiers without performing another metadata lookup.

For DVDs, CDs, comics and physical video games, the **Identifiers** section shows a **UPC / EAN** field. Type the printed code or choose **Scan UPC / EAN** to use the same camera engine as the main Scan page. A camera read only fills and selects the field; it does not save the item or contact a metadata provider.

UPC-A values are stored in Shelf's canonical 13-digit EAN form, so the same product scanned as UPC-A or EAN-13 keeps one identity. A value you type or scan is checked: invalid check digits and Bookland 978/979 ISBN barcodes are rejected from the UPC field. A stored UPC / EAN you leave alone is left as it is even when it would not pass that check, with a note under the field telling you so. The normal per-media-type duplicate guard remains in force.

Magazine issue carrier/add-on editing is intentionally handled by the Periodicals feature rather than storing issue identity in the globally unique `items.upc` column.
