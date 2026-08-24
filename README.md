# Flask + AI Agents integration

This is a minimal backend with one authentication API and an asynchronous
integration for the AI Agents Playwright project. There is no database, package
catalog, install entitlement, model, or schema layer. Authentication tokens and
run state are held in memory, so restarting Flask invalidates tokens and clears
job status history.

## Install and start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r packages/ai_agents/requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
python app.py
```

## API flow

Login and copy the returned `access_token`:

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"YOUR_CONFIGURED_PASSWORD"}' \
  http://localhost:5000/api/login
```

Start the web-testing agent. Credentials are passed only to this subprocess over
stdin and are not persisted:

```bash
curl -X POST -H "Authorization: Bearer ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"inputs":{"website_url":"https://test.example/login","username":"site-test-user","password":"RUN_ONLY_PASSWORD","routes":["/dashboard"]}}' \
  http://localhost:5000/api/ai-agents/runs
```

`routes` is optional. Omit it (or pass an empty array) to run only the generic
Playwright login check against the supplied URL. Add absolute paths such as
`["/dashboard"]` when the target uses the application's full route-testing flow.

```bash
curl -H "Authorization: Bearer ACCESS_TOKEN" http://localhost:5000/api/ai-agents/runs/RUN_ID
curl -H "Authorization: Bearer ACCESS_TOKEN" http://localhost:5000/api/ai-agents/runs/RUN_ID/logs
curl -H "Authorization: Bearer ACCESS_TOKEN" http://localhost:5000/api/ai-agents/runs/RUN_ID/artifacts
curl -X POST -H "Authorization: Bearer ACCESS_TOKEN" http://localhost:5000/api/ai-agents/runs/RUN_ID/stop
```

Each run uses `packages/ai_agents/runtime/<run-id>/`. The HTTP request returns a
run ID immediately while a supervised subprocess runs Playwright. Logs and text
artifacts are secret-redacted; paths and entrypoints are server-controlled.

## Current limitations

- Authentication is a single configured username/password with signed tokens.
- Tokens and job metadata are in memory only.
- Completed files remain in the runtime directory until cleaned operationally.
- Production should replace the local subprocess with a resource-limited
  container worker and use a proper identity provider.

## Verification

```powershell
python -m compileall -q app.py api core services packages tests
$env:PYTHONPATH = (Resolve-Path packages/ai_agents/src).Path
python -m pytest -q tests packages/ai_agents/src/ai_agents/tests
```

Tests mock the web browser and external AI behavior; no real website or API key
is required.
