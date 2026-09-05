# Users & roles

Shelf is multi-user from the first install: one shared library, several
logins, three roles.

| Role | Can |
|---|---|
| **Admin** | Everything — settings, users, integrations, locations, borrowers, sync, bulk ops, backup/restore, logs, plus all editor rights |
| **Editor** | Add / edit / delete items, all scan modes, covers, lend and return, tags, import / export CSV and archives |
| **Viewer** | Browse, search, Series, Stats, Store Mode, set reading status on items, export CSV |

Cover controls — **Find cover**, **Upload**, **Remove cover**, **Retry
cover** — are editor+; a viewer doesn't see them on the item page at all.

The first account created in the setup wizard is an admin. There must always
be at least one admin.

## Managing users

Settings → **Users** (admin): add a local user with username, display name,
role and initial password; change a locally managed role; reset a local
password; delete a user. Deleting a user doesn't touch the catalogue — items
are shared, not owned.

Every local user can change their **own** display name and password from the
account menu in the nav.

## OpenID Connect (OIDC) single sign-on

Shelf can use any standards-compliant OpenID Connect identity provider while
keeping Shelf's existing Admin, Editor and Viewer authorisation model. OIDC is
configured in Settings → **Users**.

Shelf uses the Authorization Code flow with PKCE. Enter the provider's exact
**issuer URL**, Shelf's **client ID** and, for a confidential client, its
**client secret**. The secret is stored encrypted and is never shown again.
Register the callback URI displayed by Shelf as an allowed redirect URI at the
identity provider.

The default scopes are `openid profile email`. Your provider must also expose
the configured group claim if group-based access is enabled. The default group
claim name is `groups`; dotted paths such as `realm_access.groups` are
supported for providers that nest claims.

Use **Save & test configuration** before relying on SSO. The test retrieves
OIDC discovery metadata, verifies that the discovered issuer exactly matches
the configured issuer, validates the advertised HTTPS endpoints and checks for
Authorization Code and PKCE S256 support. A real sign-in additionally validates
the client, signed ID token and returned claims.

### Recommended migration order

For an existing Shelf installation, move to OIDC in this order:

1. Keep or create at least one strong local **Admin** account for recovery.
2. Configure the OIDC issuer, client and claims while normal local login is
   still enabled.
3. Use **Save & test configuration**.
4. Test real OIDC sign-in and the Viewer / Editor / Admin group mappings.
5. Link any existing Shelf accounts that need to keep their current Shelf
   identity instead of creating new OIDC-provisioned users.
6. Only after SSO is proven healthy, optionally change **Local sign-in and
   recovery** to **Recovery administrator only**.
7. Optionally enable provider sign-out and choose the OIDC reauthentication
   interval.

That order means an IdP outage, bad group claim or mistyped issuer cannot remove
the last known-good way into Shelf.

### Group access and Shelf roles

OIDC groups can map directly to Shelf roles. For example:

| Identity-provider group | Shelf role |
|---|---|
| `Shelf-Admins` | Admin |
| `Shelf-Editors` | Editor |
| `Shelf-Users` | Viewer |

If several mappings match, Shelf grants the highest role: Admin, then Editor,
then Viewer. Group names are matched exactly.

An optional **Required access group** is a separate hard gate. A useful setup
is to require `Shelf-Users`, then also map `Shelf-Admins` and `Shelf-Editors`
to the higher roles. A valid identity-provider account that is not in the
required group is denied before a Shelf account is created.

For the safest default, leave **If no role group matches** set to **Deny
access**. Viewer or Editor can be selected when the identity provider already
applies an equivalent access policy.

### Provisioning and role synchronisation

With automatic provisioning enabled, the first approved OIDC sign-in creates a
Shelf user. The external identity is linked using the OIDC issuer and stable
subject (`iss` + `sub`), not an email address or username. If an external
username collides with an existing local Shelf username, Shelf creates a
distinct username rather than silently linking the accounts.

With **Synchronise Shelf role on every OIDC sign-in** enabled, the identity
provider becomes the source of truth for that user's Shelf role. The local role
selector is disabled and group changes take effect at the next OIDC sign-in.
Shelf will not allow role synchronisation to demote the last administrator.

OIDC accounts do not use Shelf passwords. Local password login, password reset
and local profile-name changes are blocked for an OIDC identity; make those
changes in the identity provider instead.

### Linking an existing Shelf account

If a person already has a Shelf account, use **Link an existing account to
OIDC** instead of relying on JIT provisioning. The administrator supplies:

- the existing Shelf username;
- the provider's stable OIDC subject (`sub`); and
- optionally the identity's email address as metadata.

Shelf deliberately does **not** auto-link by matching username or email. Those
values can change or collide and are not proof of identity. The stable subject
is bound to the configured issuer and becomes the external identity key.

Linking immediately turns that Shelf account into an OIDC-managed identity:
password login is blocked and its currently issued local Shelf sessions are
invalidated. Shelf refuses to link the final remaining local administrator or
the administrator currently designated as the break-glass recovery account.

### Local sign-in and break-glass recovery

Shelf has two local-login policies:

- **Normal local sign-in** — local username/password accounts remain available
  alongside the OIDC button.
- **Recovery administrator only** — the normal password form is hidden from the
  main login screen and only one explicitly selected local Admin may authenticate
  with a Shelf password. A small recovery link exposes that form when needed.

The second mode is the closest Shelf comes to "SSO only". Shelf intentionally
does **not** offer a mode with no local recovery path.

The selected break-glass account must remain a local Admin. While the recovery
policy is active Shelf prevents that account from being demoted, deleted or
linked to OIDC. If the stored recovery account ever becomes invalid — for
example after an unexpected database edit — Shelf fails open to normal local
login rather than risking a permanent lockout. Disabling OIDC also restores
normal local login automatically.

Give the break-glass account a strong unique password and reserve it for
recovery rather than everyday use.

### Authentik example

Authentik works with Shelf as a normal OIDC provider. A typical deployment uses
three Authentik groups named `Shelf-Users`, `Shelf-Editors` and
`Shelf-Admins`, with `Shelf-Users` also configured as Shelf's required access
group. Register the callback URI displayed on Shelf's OIDC settings card in the
Authentik provider, then copy the provider's exact issuer URL, client ID and
client secret into Shelf.

After configuration, test at least one account at each role and then remove or
change a test user's group membership to confirm that the next OIDC sign-in
updates or denies access as intended.

## Sessions

Local login sets an HTTP-only, secure cookie with a 7-day JWT that refreshes
while you're active, so a device you use regularly stays logged in.

OIDC sessions are intentionally different: they have a fixed, non-sliding
reauthentication ceiling. The default is **24 hours**; an administrator can
choose 1 hour, 8 hours, 24 hours, 3 days or 7 days in Settings, and the backend
accepts whole-hour values from 1 through 168. When the ceiling is reached Shelf
sends the user through the provider again, which refreshes access and group/role
claims. An existing provider SSO session will normally make that reauthentication
transparent.

Changing a local password invalidates that local user's existing Shelf
sessions; a synced OIDC role change also invalidates existing Shelf sessions for
that user. Login attempts are rate-limited per IP.

### Provider sign-out

Shelf always clears its own session when **Log out** is chosen. An administrator
can additionally enable **Sign out at the identity provider when supported**.
For an OIDC-authenticated session, Shelf then uses the provider's discovered
`end_session_endpoint` and asks it to return to Shelf's login page.

Provider sign-out is deliberately best-effort. Some identity providers require
an `id_token_hint` for complete RP-initiated logout; Shelf does not retain the
provider ID token after sign-in. If the provider does not advertise a logout
endpoint, requires a hint, or is unavailable, Shelf's own logout still succeeds
and the user returns to the Shelf login page. Local Shelf sessions never trigger
provider logout.

## The log viewer

**Logs** in the nav (admin) tails Shelf's application log in the browser:
auth events (logins, failures, role changes), sync runs, metadata and cover
lookups, errors. Handy for "why didn't that cover load" without touching
`docker compose logs`.

## What's per-user today

Your authentication identity, display name and role are user-specific. Reading
status, tags, wishlist and everything else are shared across the household.
Per-user personal state is tracked in the Shelf roadmap in issue #102.
