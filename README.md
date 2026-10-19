# Flask Login API

This backend exposes password and Google SSO login endpoints. All login flows
require an active user in the configured MySQL `users` table and a submitted app
version that is inside an inclusive range in the `valid_versions` table.

## Install and start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

The server runs at `http://127.0.0.1:5000`. Send JSON credentials to the login
endpoint:

```json
{
  "username": "your-username",
  "password": "your-password",
  "version": "1.5.0"
}
```

The version ranges are read from the existing `valid_versions` table:

```sql
SELECT my_row_id, `from`, `to`
FROM valid_versions;
```

The API uses only `from` and `to`; `my_row_id` remains the table's primary key.
The backticks are required because `from` is a SQL keyword. Ranges are
inclusive, so a row containing `1.0.0` through `2.0.0` accepts `1.0.0`,
`1.5.0`, and `2.0.0`. Versions must be strings in a standard dotted format
such as `1.5.0`.

Version validation happens inside the existing login endpoints. A version
outside every configured range returns HTTP 403:

```json
{
  "success": false,
  "message": "Version not supported"
}
```

Configure `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, and `DB_PORT` in the
local `.env` file. Google sign-in reads the user's identity, activity, and access
flags from `users`; it does not require a `users.password` column. You can also
set `AUTH_SECRET` and `AUTH_TOKEN_MAX_AGE` for issued access tokens.

Google SSO uses `POST /api/auth/google` with the Google Identity Services ID
token in a `credential` JSON field and the app version in a `version` field.
Set `GOOGLE_SSO_CLIENT_ID` to the same web client ID used by the frontend.
`GOOGLE_CLIENT_ID` remains supported as a fallback. A Google client secret is
not used for ID-token verification.

Google ID-token verification allows 10 seconds of clock skew by default to
handle small differences between the Google issuer and backend clocks. Override
this with `GOOGLE_CLOCK_SKEW_SECONDS` if needed; the server clock should still be
kept synchronized.

## Verification

```powershell
python -m pytest -q -p no:cacheprovider tests
```

## Deploy to Azure

The app runs on Azure Functions (Flex Consumption, Linux, Python 3.12) as
`fsnxt-app-function` in resource group `FS_ERP`, via `function_app.py`, which
wraps the Flask app with `azure.functions.WsgiFunctionApp`.

### One-time setup (per machine)

```powershell
winget install -e --id Microsoft.AzureCLI
winget install -e --id Microsoft.Azure.FunctionsCoreTools
winget install -e --id Python.Python.3.12
```

Close and reopen your terminal so PATH picks up the new tools, then log in
once:

```powershell
az login
```

Confirm you're pointed at the right account/subscription with
`az account show`.

### Deploying

From the project root:

```powershell
.\deploy.ps1
```

This validates `requirements.txt` and the Functions entry point locally
before publishing, then runs `func azure functionapp publish
fsnxt-app-function --python`. Run it any time you want to push updated code.

### App settings (set once, not part of deployment)

`DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_PORT`, `AUTH_SECRET`, and
`GOOGLE_SSO_CLIENT_ID` must be configured directly on the Function App — they
live in Azure, not in the deployed code, and `local.settings.json`/`.env`
never get uploaded. Set or update them with:

```powershell
az functionapp config appsettings set -n fsnxt-app-function -g FS_ERP --settings `
  DB_HOST=<value> DB_USER=<value> DB_PASSWORD=<value> DB_NAME=<value> DB_PORT=3306 `
  AUTH_SECRET=<value> GOOGLE_SSO_CLIENT_ID=<value>
```
