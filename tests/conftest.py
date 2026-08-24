from __future__ import annotations

import json, time
from pathlib import Path
import pytest
from app import create_app

RUNNER = '''
import time
from pathlib import Path
def run_package(run_id, inputs, output_dir):
    print(f"password={inputs.get('password', '')}", flush=True)
    if inputs.get("mode") == "sleep": time.sleep(3)
    if inputs.get("mode") == "fail": raise RuntimeError(f"failed {inputs.get('password', '')}")
    Path(output_dir, "report.html").write_text("<html>ok</html>", encoding="utf-8")
    return {"status": "completed", "summary": {"mode": inputs.get("mode", "success")}, "password": inputs.get("password", "")}
'''


@pytest.fixture
def app(tmp_path):
    package = tmp_path / "ai_agents"; (package / "runtime").mkdir(parents=True)
    (package / "runner.py").write_text(RUNNER, encoding="utf-8")
    manifest = {
        "id": "ai_agents", "name": "AI Agents", "version": "1.0.0", "description": "test",
        "entrypoint": "packages.ai_agents.runner:run_package", "enabled": True, "timeout_seconds": 1,
        "required_inputs": [
            {"name": "website_url", "type": "string", "format": "url"},
            {"name": "username", "type": "string"}, {"name": "password", "type": "string"}
        ],
        "optional_inputs": [{"name": "routes", "type": "array"}, {"name": "mode", "type": "string"}],
    }
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return create_app({
        "TESTING": True, "AUTH_SECRET": "test-signing-secret", "AUTH_USERNAME": "tester",
        "AUTH_PASSWORD": "test-password", "AI_AGENTS_PACKAGE_PATH": str(package), "RUN_TIMEOUT_SECONDS": 10,
    })


@pytest.fixture
def client(app): return app.test_client()


@pytest.fixture
def auth(client):
    response = client.post("/api/login", json={"username": "tester", "password": "test-password"})
    return {"Authorization": f"Bearer {response.get_json()['access_token']}"}


@pytest.fixture
def inputs():
    return {"website_url": "https://example.test/login", "username": "site-user", "password": "run-secret", "routes": ["/dashboard"]}


def wait_for(client, auth, run_id, terminal=True):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        response = client.get(f"/api/ai-agents/runs/{run_id}", headers=auth)
        data = response.get_json()
        if not terminal or data["status"] in {"completed", "failed", "stopped", "timed_out"}: return data
        time.sleep(.05)
    raise AssertionError("run did not finish")
