# Deploy notes: FS Sprint passes, rules and team files

This release adds FS Sprint's pass/renewal sign-in, the rules bundles and the team files to
`fsnxt-app-function`.

From the **first** deploy, writes to users and versions (`POST`/`DELETE /api/users`,
`POST /api/versions`) need an **admin pass**. Anonymous writes are closed for good.
Only the anonymous **reads** of the installed 0.3.22 stay open, behind `LEGACY_OPEN_READS`
(default on).

The owner manages users and the allowed versions from the **new** FS Sprint build. It gets a pass
once this function is deployed: sign in with Google as an admin (`users.isAdmin = 1` or listed in
`ADMIN_EMAILS`). Roles & Auth in 0.3.22, and the new build's password-admin path (no pass), are
refused for writes from this deploy on.

## New app settings

| Setting | Default | Meaning |
|---|---|---|
| `AUTH_SECRET` (existing) | — | Also signs the passes. **Must be at least 32 characters.** While it is missing, `development-only-change-me` or shorter than 32 characters, no pass is issued or accepted (sign-in answers without `session`; pass routes answer 503), and the function logs this at startup. Set a fresh value before the deploy (below). |
| `FS_STORAGE_CONNECTION_STRING` | (none) | Connection string of the storage account that holds rules, team files and renewal tickets. Preferred. |
| `FS_STORAGE_ACCOUNT_URL` | (none) | Only if no connection string: `https://<account>.blob.core.windows.net`, used with the function's managed identity. Needs `azure-identity` added to `requirements.txt` and the role below. |
| `ADMIN_EMAILS` | (empty) | Fallback admin emails, separated by `,` or `;`. Admin = `users.isAdmin = 1` OR listed here. Put the owner's Google email here so he can never lock himself out. |
| `PASS_TTL_SECONDS` | `5400` | Pass lifetime (90 minutes). Clamped to 300..86400. |
| `TICKET_MAX_DAYS` | `7` | Renewal lifetime, absolute from the Google sign-in. Rotation never extends it. |
| `FS_AUTH_CONTAINER` | `fs-auth` | Container for renewal records (`families/` and `tickets/`; only SHA-256 hashes of tickets). |
| `FS_RULES_CONTAINER` | `fs-rules` | Container for the nine rules bundles (`<name>.bin`). |
| `FS_FILES_CONTAINER` | `fs-files` | Container for the team files (`/api/files`). |
| `LEGACY_OPEN_READS` | `1` | On (`1/true/yes/on`, any case): the anonymous reads of the installed 0.3.22 still work, which are `GET /api/users` and `POST /api/logs`. Off (`0/false/no/off`): those need a pass. **Any other value counts as off** (fail safe) and is logged. The effective mode is logged at startup. Writes need an admin pass either way. `GET /api/versions` stays open either way, because the sign-in screen reads it. |

`LEGACY_OPEN_API` (from an earlier draft of this release) is not read any more. If it is set, the
function logs a warning; remove it.

Changing `AUTH_SECRET` ends every pass at once. Colleagues on the new build renew with their
ticket automatically. 0.3.22 colleagues are not affected (they don't use passes).

Example (values are placeholders):

```powershell
python -c "import secrets;print(secrets.token_urlsafe(48))"   # a fresh AUTH_SECRET, never reuse an old one
az functionapp config appsettings set -n fsnxt-app-function -g FS_ERP --settings `
  AUTH_SECRET="<the value printed above>" `
  FS_STORAGE_CONNECTION_STRING="<connection string>" `
  ADMIN_EMAILS="<owner's google email>" LEGACY_OPEN_READS=1
```

## Storage account (one time)

Use a **separate** StorageV2 account, not the one in `AzureWebJobsStorage`. Settings:

- **Anonymous blob access: off** (`allowBlobPublicAccess=false`). All containers private.
- **Minimum TLS version: 1.2.**
- **Network:** reachable from the function. Do **not** disable public network access unless the
  function has VNet integration and a private endpoint/service endpoint is set up for this account.
- **Shared-key access: leave enabled** when using `FS_STORAGE_CONNECTION_STRING` (the connection
  string uses the account key). Only disable it if you switch to `FS_STORAGE_ACCOUNT_URL` + managed
  identity.
- Recommended: Blob versioning and 30-day soft delete for blobs and containers.
- **Lifecycle rule** for renewal records: delete base blobs in `fs-auth` with prefixes `tickets/`
  and `families/` when not modified for more than `TICKET_MAX_DAYS + 1` days (8 by default). With
  versioning on, the same rule must also **delete previous versions** older than 1 day. Otherwise
  every rotation leaves a version behind.

Create the three private containers once. The function never creates containers itself. A missing
container answers 503 `FS Sprint storage is unavailable.`, and its name is written only to the
function log.

```powershell
az storage container create --account-name <account> --name fs-auth  --auth-mode login
az storage container create --account-name <account> --name fs-rules --auth-mode login
az storage container create --account-name <account> --name fs-files --auth-mode login
```

Access for the function is one of these:

- the account's connection string in `FS_STORAGE_CONNECTION_STRING`;
- with `FS_STORAGE_ACCOUNT_URL`: the function app's managed identity with the role **Storage Blob
  Data Contributor** on the account.

## What the deploy uploads

`.funcignore` keeps out of the package:

- virtual environments (`.venv*`, `.deploy-venv`);
- `tests/` and `local_runner.py` (test-only code with a test sign-in);
- `__pycache__`, `.pytest_cache`, `.env`, `local.settings.json`;
- `deploy.ps1` and the `*.md` files.

`function_app.py`, `app.py`, `fs_*.py`, `host.json` and `requirements.txt` are what runs.

## Cut-over order (matches the plan's owner steps)

1. Create the storage account and containers as above. Set a **fresh `AUTH_SECRET`** (at least 32
   characters, from the command above) and the other app settings, with `LEGACY_OPEN_READS=1`.
2. Deploy this code (`.\deploy.ps1`). 0.3.22 colleagues keep signing in and reading users. Their
   Roles & Auth writes are refused from now on (C1).
3. **Smoke test** (below), right away, against the real account.
4. In the FS Sprint repo: `npm run pack:all`, then `npm run rules:upload` (admin Google sign-in;
   check that no bundle is reported "missing"), then `npm run storage:upload`. `rules:upload` is
   safe right after the smoke test: anonymous writes are already closed, so nobody can give
   themselves rules access.
5. Release the new FS Sprint version; run `npm run storage:upload` again.
6. Raise Allowed FS Sprint Versions "From" to the new version (Roles & Auth in the new build).
7. Set `LEGACY_OPEN_READS=0` (closes the anonymous `GET /api/users` and `POST /api/logs`).
8. Remove colleagues from the GitHub data repository; retire the `fs-sprint` function.

## Smoke test right after the first deploy

Against the real function and storage account:

1. In the **new** build, sign in with Google as the owner. A pass comes back (Roles & Auth works,
   no "server needs an update").
2. `GET /api/rules` lists (empty before the first upload is fine).
3. One `npm run rules:upload`. Then `GET /api/rules` shows the nine bundles with etags.
4. `/api/files`: `PUT` a test file, `GET` it back, `PUT` again with `If-None-Match: *` → **412**,
   `DELETE` it.
5. A 0.3.22 colleague signs in with Google: still works, and their user list loads.
6. A file name with `%` (for example `smoke/100%25 test.md`): put, get and list show the same
   name. This rules out double decoding.

If any step fails, set the settings back or roll forward (below). Don't let colleagues start on the
new build until it passes.

## Rollback

- **Before the new FS Sprint build ships** (steps 1–4): redeploying the previous commit is safe.
  The new routes disappear. Nothing in MySQL changed (no schema change; `isAdmin` values written by
  admins stay and are harmless; open version ends stay as `0.0.0` / `9999.9999.9999`, which the old
  code reads as a wide range). Blob data stays.
- **After the new build ships** (step 5 on): redeploying the old code leaves new-build colleagues
  with **no pass and no rules**. They cannot work. From that point the safe paths are only:
  - **roll forward** (fix and redeploy this code), or
  - **settings toggles** on this code: `LEGACY_OPEN_READS=1` reopens the anonymous reads (it takes
    effect on the next restart).
- To end all sessions at once: change `AUTH_SECRET` (passes) and delete the `families/` blobs in
  `fs-auth` (renewals). Everyone signs in with Google again.

## Notes

- Passes are re-checked against the `users` row on every call (one MySQL lookup per call). A
  deactivated or deleted user is refused at once (401), and their renewal is refused (403).
- Renewal: each Google sign-in is one family. Only its latest ticket renews, plus the previous one
  for 2 minutes after its first rotation (a lost answer). Re-using the previous ticket inside the
  grace replaces the successor, so there is never more than one live successor. Any older or
  replaced ticket, or the previous one after the grace, revokes the whole family (copy detected).
  Sign-out revokes the family.
- Storage calls have a small budget (connect 3 s, read 10 s, 1 retry). Conditional writes are
  never retried blindly (a lost first answer would come back as a false 412). Such a write
  answers 503 instead of a false conflict. A Blob outage never breaks sign-in: the answer
  then has a pass without a renewal ticket.
- `local_runner.py` is a developer tool (in-memory data, a test-only sign-in). Azure never imports
  it, `.funcignore` keeps it out of the package, and it refuses to start inside an Azure
  Functions host.

## Follow-up (not in this release)

- **M2:** one MySQL connection per call, no pool. A sync burst multiplies connections. Add a pool
  or a short per-process cache of user rows (about 60 s), and cap the Flex Consumption
  `maximumInstanceCount` so instances × connections stays under MySQL `max_connections`.
