# Discogs enrichment

Shelf can optionally attach a **specific Discogs Release** to an item in the first-class Music catalogue.

Discogs is deliberately an enrichment source rather than Shelf's music identity system:

- **MusicBrainz Release** remains the canonical edition/release identity.
- **Discogs Release** represents the collector-selected pressing and contributes pressing-specific metadata.
- Cover Art Archive remains the music artwork source.
- Physical condition, acquisition details and provenance remain properties of Shelf's physical-copy model.

## Configure Discogs

Open `/settings/discogs` as an administrator and save a Discogs personal access token. The token is encrypted before being written to Shelf's settings database.

Alternatively set:

```text
DISCOGS_TOKEN=your-token
```

An environment token takes precedence over a saved token and is never displayed by the UI.

## Match a pressing

Open a release under **Music** and choose **Match pressing** in the Discogs section. Shelf can search exact Discogs Releases using any combination of:

- release title;
- artist;
- retail barcode;
- catalogue number.

Select the concrete pressing that matches the copy in hand. Shelf stores the Discogs Release/Master IDs plus collector-facing data such as label/catalogue number, format description, genres/styles, notes and identifiers including matrix/runout, pressing-plant IDs, label codes and rights societies when Discogs provides them.

A match can be refreshed or removed later. Removing it deletes only the Discogs enrichment; the Shelf item and its MusicBrainz identity remain untouched.
