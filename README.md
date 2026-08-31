# Flask Login API

This backend exposes one endpoint: `POST /api/login`. Credentials are checked
against the `users` table in the configured MySQL database.

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

## Verification

```powershell
python -m pytest -q -p no:cacheprovider tests
```
