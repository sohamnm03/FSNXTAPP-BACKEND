# Flask Login API

This backend exposes one endpoint: `POST /api/login`.

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
  "username": "admin",
  "password": "password123"
}
```

The defaults are intended for local development only. Override `AUTH_USERNAME`,
`AUTH_PASSWORD`, `AUTH_SECRET`, and `AUTH_TOKEN_MAX_AGE` in a local `.env` file.

## Verification

```powershell
python -m pytest -q -p no:cacheprovider tests
```
