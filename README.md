# Flask Login API

This backend exposes password and Google SSO login endpoints. Both flows require
the user to exist in the configured MySQL `users` table.

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
  "password": "your-password"
}
```

Configure `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, and `DB_PORT` in the
local `.env` file. For the current development setup, login passwords are read
from `users.password`. You can also set `AUTH_SECRET` and `AUTH_TOKEN_MAX_AGE`
for issued access tokens.

> Warning: `users.password` currently stores plain-text passwords for temporary
> testing only. Use secure password hashing before deploying this API.

Google SSO uses `POST /api/auth/google` with the Google Identity Services ID
token in a `credential` JSON field. Set `GOOGLE_SSO_CLIENT_ID` to the same web
client ID used by the frontend. `GOOGLE_CLIENT_ID` remains supported as a
fallback. A Google client secret is not used for ID-token verification.

Google ID-token verification allows 10 seconds of clock skew by default to
handle small differences between the Google issuer and backend clocks. Override
this with `GOOGLE_CLOCK_SKEW_SECONDS` if needed; the server clock should still be
kept synchronized.

## Verification

```powershell
python -m pytest -q -p no:cacheprovider tests
```
