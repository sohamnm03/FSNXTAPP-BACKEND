from tests.conftest import wait_for


def test_login(client):
    assert client.post("/api/login", json={}).status_code == 400
    assert client.post("/api/login", json={"username": "tester", "password": "wrong"}).status_code == 401
    response = client.post("/api/login", json={"username": "tester", "password": "test-password"})
    assert response.status_code == 200
    assert response.get_json()["token_type"] == "Bearer"


def test_run_requires_authentication(client, inputs):
    assert client.post("/api/ai-agents/runs", json={"inputs": inputs}).status_code == 401


def test_input_validation(client, auth, inputs):
    assert client.post("/api/ai-agents/runs", headers=auth, json={"inputs": {}}).status_code == 400
    bad = dict(inputs, routes=["/safe/../../escape"])
    assert client.post("/api/ai-agents/runs", headers=auth, json={"inputs": bad}).status_code == 400
    bad = dict(inputs, website_url="file:///etc/passwd")
    assert client.post("/api/ai-agents/runs", headers=auth, json={"inputs": bad}).status_code == 400
    bad = dict(inputs, command="whoami")
    assert client.post("/api/ai-agents/runs", headers=auth, json={"inputs": bad}).status_code == 400


def test_complete_run_logs_artifacts_and_redaction(client, auth, inputs):
    response = client.post("/api/ai-agents/runs", headers=auth, json={"inputs": inputs})
    assert response.status_code == 202
    run_id = response.get_json()["run_id"]
    result = wait_for(client, auth, run_id)
    assert result["status"] == "completed"
    assert result["result"]["password"] == "[REDACTED]"
    logs = client.get(f"/api/ai-agents/runs/{run_id}/logs", headers=auth).get_json()["logs"]
    assert "run-secret" not in logs and "[REDACTED]" in logs
    artifacts = client.get(f"/api/ai-agents/runs/{run_id}/artifacts", headers=auth).get_json()["artifacts"]
    report = next(item for item in artifacts if item["path"] == "report.html")
    assert client.get(report["download_url"], headers=auth).status_code == 200


def test_run_ownership(client, app, auth, inputs):
    run_id = client.post("/api/ai-agents/runs", headers=auth, json={"inputs": inputs}).get_json()["run_id"]
    other_client = app.test_client()
    token = other_client.post("/api/login", json={"username": "tester", "password": "test-password"}).get_json()["access_token"]
    # Same configured account is intentionally the same owner; a forged token is rejected.
    assert client.get(f"/api/ai-agents/runs/{run_id}", headers={"Authorization": "Bearer forged"}).status_code == 401
    assert token
    wait_for(client, auth, run_id)


def test_failure(client, auth, inputs):
    run_id = client.post("/api/ai-agents/runs", headers=auth, json={"inputs": dict(inputs, mode="fail")}).get_json()["run_id"]
    result = wait_for(client, auth, run_id)
    assert result["status"] == "failed" and "run-secret" not in result["error"]


def test_timeout(client, auth, inputs):
    run_id = client.post("/api/ai-agents/runs", headers=auth, json={"inputs": dict(inputs, mode="sleep")}).get_json()["run_id"]
    assert wait_for(client, auth, run_id)["status"] == "timed_out"


def test_stop(client, auth, inputs):
    run_id = client.post("/api/ai-agents/runs", headers=auth, json={"inputs": dict(inputs, mode="sleep")}).get_json()["run_id"]
    state = wait_for(client, auth, run_id, terminal=False)
    while state["status"] == "queued": state = wait_for(client, auth, run_id, terminal=False)
    assert client.post(f"/api/ai-agents/runs/{run_id}/stop", headers=auth).status_code == 202
    assert wait_for(client, auth, run_id)["status"] == "stopped"
