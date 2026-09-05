# Users & roles

Shelf is multi-user: one shared catalogue can have several logins with three
site roles.

| Role | Can |
|---|---|
| **Admin** | Everything — settings, users, integrations, locations, borrowers, sync, bulk ops, backup/restore, logs, plus all editor rights |
| **Editor** | Add / edit / delete catalogue items, all scan modes, covers, lend and return, tags, import / export CSV and archives, plus their own personal media state |
| **Viewer** | Browse, search, Series, Stats, Store Mode, export CSV, and manage their own status, progress, rating, favourite, wishlist and personal notes |

Cover controls — **Find cover**, **Upload**, **Remove cover**, **Retry
cover** — are editor+; a viewer doesn't see them on the item page at all.

The first account created in the setup wizard is an admin. There must always
be at least one admin.

## Managing users

Settings → **Users** (admin): add a user with username, display name, role
and initial password; change a role; reset a password; delete a user.
Deleting a user doesn't delete shared catalogue items. Account-specific media
state is tied to that user and is removed with the account.

Every local user changes their **own** display name and password from the
account menu in the nav.

## Shared catalogue vs personal state

Shelf keeps the catalogue shared while storing personal activity separately
for each account.

**Shared catalogue data** includes title and edition metadata, physical and
digital holdings, physical copies and locations, lending, tags and collections.
Changing shared catalogue data therefore requires editor or admin access.

**Personal media state** belongs only to the signed-in account:

- reading / watching / listening / playing status;
- start and finish dates and completion history;
- progress, including a value, optional total and unit;
- rating;
- favourite;
- personal wishlist; and
- private personal notes.

A viewer can change their own personal state without gaining permission to
edit the shared catalogue. One user's status, rating, wishlist, progress or
notes never replaces another user's values.

On upgrade, accounts that already exist receive a one-time snapshot of the
old shared reading status, wishlist intent and reading history. Users created
after that migration start with clean personal state and do not inherit another
account's legacy activity.

Goodreads and StoryGraph imports also treat reading state as personal to the
account performing the import. In duplicate `skip` mode, Shelf can leave an
existing catalogue record untouched while still importing that user's status.

## Sessions

Login sets an HTTP-only, secure cookie with a 7-day JWT that refreshes while
you're active, so a device you use regularly stays logged in. **Log out**
from the account menu; to force every device out, change your password.
Login attempts are rate-limited per IP.

OIDC/SSO work is being developed separately; OIDC sessions have their own
revalidation behaviour.

## The log viewer

**Logs** in the nav (admin) tails Shelf's application log in the browser:
auth events (logins, failures, role changes), sync runs, metadata and cover
lookups, errors. Handy for "why didn't that cover load" without touching
`docker compose logs`.

## Library-specific permissions

Site roles are currently global. First-class Shelf libraries with per-user
`None / Viewer / Editor` access are planned separately in
[issue #106](https://github.com/sudo-rpaisley/shelf/issues/106). That work will
make libraries the catalogue security boundary without turning Collections or
physical locations into permission containers.
