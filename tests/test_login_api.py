import app as app_module


def test_google_verifier_allows_small_clock_skew(monkeypatch):
    captured = {}

    def verify(token, request, audience, *, clock_skew_in_seconds):
        captured.update(
            token=token,
            request=request,
            audience=audience,
            clock_skew_in_seconds=clock_skew_in_seconds,
        )
        return {"sub": "google-user-id"}

    monkeypatch.setattr(app_module.google_id_token, "verify_oauth2_token", verify)

    claims = app_module.verify_google_credential("credential", "client-id")

    assert claims == {"sub": "google-user-id"}
    assert captured["token"] == "credential"
    assert captured["audience"] == "client-id"
    assert captured["clock_skew_in_seconds"] == 10


def test_login(client):
    assert client.post("/api/login", json={}).status_code == 400
    assert client.post("/api/login", json=[]).status_code == 400
    assert client.post("/api/login", json={"username": "missing", "password": "anything"}).status_code == 401
    assert client.post("/api/login", json={"username": "tester", "password": "wrong"}).status_code == 401
    response = client.post("/api/login", json={"username": "tester", "password": "test-password"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["access_token"]
    assert body["token_type"] == "Bearer"
    assert body["user"]["email"] == "tester@example.com"


def test_google_login(client):
    assert client.post("/api/auth/google", json={}).status_code == 400
    assert client.post(
        "/api/auth/google", json={"credential": "invalid-token"}
    ).status_code == 401
    unavailable = client.post(
        "/api/auth/google", json={"credential": "network-error"}
    )
    assert unavailable.status_code == 503
    assert unavailable.get_json()["message"] == (
        "Google verification service is unavailable. Please try again."
    )
    assert client.post(
        "/api/auth/google", json={"credential": "unverified-token"}
    ).status_code == 401

    response = client.post(
        "/api/auth/google", json={"credential": "valid-token"}
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["access_token"]
    assert body["user"]["email"] == "tester@example.com"


VALID_DESKTOP_PAYLOAD = {
    "code": "valid-code",
    "codeVerifier": "verifier",
    "redirectUri": "http://127.0.0.1:54231/",
    "nonce": "expected-nonce",
}


def test_google_desktop_login_rejects_missing_fields(client):
    assert client.post("/api/auth/google/desktop", json={}).status_code == 400


def test_google_desktop_login_rejects_non_loopback_redirect(client):
    payload = {**VALID_DESKTOP_PAYLOAD, "redirectUri": "https://evil.example/"}
    assert client.post("/api/auth/google/desktop", json=payload).status_code == 400


def test_google_desktop_login_rejects_bad_authorization_code(client):
    payload = {**VALID_DESKTOP_PAYLOAD, "code": "invalid-code"}
    assert client.post("/api/auth/google/desktop", json=payload).status_code == 401


def test_google_desktop_login_rejects_nonce_mismatch(client):
    payload = {**VALID_DESKTOP_PAYLOAD, "nonce": "wrong-nonce"}
    assert client.post("/api/auth/google/desktop", json=payload).status_code == 401


def test_google_desktop_login_succeeds(client):
    response = client.post("/api/auth/google/desktop", json=VALID_DESKTOP_PAYLOAD)
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["access_token"]
    assert body["user"]["email"] == "tester@example.com"
