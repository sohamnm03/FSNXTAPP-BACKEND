def test_login(client):
    assert client.post("/api/login", json={}).status_code == 400
    assert client.post("/api/login", json={"username": "tester", "password": "wrong"}).status_code == 401
    response = client.post("/api/login", json={"username": "tester", "password": "test-password"})
    assert response.status_code == 200
    assert response.get_json()["token_type"] == "Bearer"


def test_ai_agents_api_is_not_exposed(client):
    assert client.post("/api/ai-agents/runs", json={}).status_code == 404
