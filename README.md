# Flask login API

This backend now exposes authentication only. Web Testing runs, logs, stopping,
artifacts, and report downloads execute locally in the Electron frontend and do
not call Flask.

## Install and start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

The only application API is:

```text
POST /api/login
```

## Verification

```powershell
python -m compileall -q app.py core tests
python -m pytest -q tests
```
