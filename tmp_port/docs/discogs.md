# Discogs enrichment

Discogs is an optional enrichment layer for first-class Music items. Shelf keeps the MusicBrainz Release as the canonical music identity and only stores a Discogs Release after a user explicitly chooses the exact pressing.

## Configure

Open **Settings → Integrations → Discogs** and save a Discogs personal access token, or set `DISCOGS_TOKEN`. Environment configuration takes precedence. Saved tokens use Shelf's normal encrypted sensitive-setting storage.

## Match a pressing

Open a Music release and choose **Match pressing** from its compact Discogs card. Search can use release title, artist, barcode and catalogue number. Shelf searches Discogs Releases rather than Masters; selecting a result fetches and stores that exact Release.

Stored enrichment includes the Discogs Release/Master IDs, label and catalogue number, format description, genres/styles, notes and collector identifiers such as matrix/runout and pressing-plant IDs.

Discogs matching never replaces the MusicBrainz release identity. Removing or refreshing Discogs data cannot remove MusicBrainz metadata. Physical condition, acquisition source, price and provenance belong to Shelf's physical-copy model, not Discogs. Cover Art Archive remains the music artwork source.
