import app as app_module
import mysql.connector
from werkzeug.security import generate_password_hash


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
    assert body["user"]["isConfig"] == 1


def test_login_accepts_password_hash(app):
    hashed_user = {
        "id": 3,
        "username": "hashed",
        "email": "hashed@example.com",
        "password": None,
        "password_hash": generate_password_hash("secret-password"),
        "isAdmin": 1,
    }
    app.config["USER_LOOKUP_BY_USERNAME"] = (
        lambda username: hashed_user if username == hashed_user["username"] else None
    )
    client = app.test_client()

    assert client.post(
        "/api/login", json={"username": "hashed", "password": "wrong"}
    ).status_code == 401

    response = client.post(
        "/api/login", json={"username": "hashed", "password": "secret-password"}
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["user"]["email"] == "hashed@example.com"
    assert body["user"]["isAdmin"] == 1


def test_upsert_user_creates_new_user(client, app):
    assert client.post("/api/users", json=[]).status_code == 400
    assert client.post("/api/users", json={}).status_code == 400
    assert client.post(
        "/api/users",
        json={
            "username": "tester",
            "email": "tester@example.com",
            "full_name": "x" * 256,
            "isActive": True,
            "isDev": False,
            "isTesting": False,
            "isConfig": False,
            "isFsNxt": False,
            "isSapFsNxt": False,
        },
    ).status_code == 400
    assert client.post(
        "/api/users",
        json={
            "username": "tester",
            "email": "tester@example.com",
            "full_name": "Tester User",
            "isActive": True,
            "isDev": False,
            "isTesting": False,
            "isFsNxt": False,
            "isSapFsNxt": False,
        },
    ).status_code == 400

    response = client.post(
        "/api/users",
        json={
            "username": "new-user",
            "email": "new@example.com",
            "full_name": "New User",
            "isActive": True,
            "isDev": True,
            "isTesting": False,
            "isConfig": True,
            "isFsNxt": True,
            "isSapFsNxt": False,
        },
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["success"] is True
    assert body["message"] == "User created successfully."
    assert body["user"] == {
        "id": 2,
        "username": "new-user",
        "email": "new@example.com",
        "full_name": "New User",
        "isActive": 1,
        "isDev": 1,
        "isTesting": 0,
        "isConfig": 1,
        "isFsNxt": 1,
        "isSapFsNxt": 0,
    }
    assert app.config["UPSERTED_USERS"][0] == body["user"]


def test_upsert_user_updates_existing_email_and_accepts_database_flags(client):
    response = client.post(
        "/api/users",
        json={
            "username": "updated-tester",
            "email": "tester@example.com",
            "full_name": "Updated Tester",
            "isActive": 0,
            "isDev": 1,
            "isTesting": 1,
            "isConfig": 0,
            "isFsNxt": 0,
            "isSapFsNxt": 1,
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "message": "User updated successfully.",
        "user": {
            "id": 1,
            "username": "updated-tester",
            "email": "tester@example.com",
            "full_name": "Updated Tester",
            "isActive": 0,
            "isDev": 1,
            "isTesting": 1,
            "isConfig": 0,
            "isFsNxt": 0,
            "isSapFsNxt": 1,
        },
    }


def test_database_upsert_updates_every_payload_field_for_existing_email(monkeypatch):
    statements = []

    class Cursor:
        def execute(self, statement, parameters):
            statements.append((" ".join(statement.split()), parameters))

        def fetchone(self):
            return {"id": 7}

        def close(self):
            pass

    class Connection:
        def __init__(self):
            self.committed = False

        def cursor(self, dictionary=False):
            assert dictionary is True
            return Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            raise AssertionError("The successful upsert must not be rolled back")

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(app_module.mysql.connector, "connect", lambda **kwargs: connection)
    monkeypatch.setattr(
        app_module, "get_current_ist_time", lambda: "2026-10-12 12:00:00"
    )

    user_id, created = app_module.upsert_user(
        "Adil",
        "adil.a@fourthsignal.com",
        "Adil Ansari",
        True,
        False,
        False,
        False,
        True,
        False,
    )

    assert (user_id, created) == (7, False)
    assert connection.committed is True
    update_statement, update_parameters = statements[1]
    assert "SET username = %s, email = %s, full_name = %s" in update_statement
    assert update_parameters == (
        "Adil",
        "adil.a@fourthsignal.com",
        "Adil Ansari",
        1,
        0,
        0,
        0,
        1,
        0,
        "2026-10-12 12:00:00",
        7,
    )


def test_upsert_user_accepts_full_name_camel_case(client, app):
    response = client.post(
        "/api/users",
        json={
            "username": "new-user",
            "email": "new@example.com",
            "fullName": "New User",
            "isActive": True,
            "isDev": False,
            "isTesting": False,
            "isConfig": False,
            "isFsNxt": False,
            "isSapFsNxt": False,
        },
    )

    assert response.status_code == 201
    assert app.config["UPSERTED_USERS"][0]["full_name"] == "New User"


def test_upsert_user_handles_database_conflict(client, app):
    def duplicate_user(*args):
        raise mysql.connector.IntegrityError("duplicate")

    app.config["USER_UPSERTER"] = duplicate_user

    response = client.post(
        "/api/users",
        json={
            "username": "tester",
            "email": "tester@example.com",
            "full_name": "Tester User",
            "isActive": True,
            "isDev": False,
            "isTesting": False,
            "isConfig": True,
            "isFsNxt": True,
            "isSapFsNxt": False,
        },
    )

    assert response.status_code == 409


def test_upsert_user_handles_database_error(client, app):
    def unavailable(*args):
        raise mysql.connector.Error("database unavailable")

    app.config["USER_UPSERTER"] = unavailable

    response = client.post(
        "/api/users",
        json={
            "username": "tester",
            "email": "tester@example.com",
            "full_name": "Tester User",
            "isActive": True,
            "isDev": False,
            "isTesting": False,
            "isConfig": True,
            "isFsNxt": True,
            "isSapFsNxt": False,
        },
    )

    assert response.status_code == 503


def test_legacy_user_update_endpoints_are_removed(client):
    assert client.patch("/api/users/modules", json={}).status_code == 404
    assert client.patch("/api/users/active", json={}).status_code == 404


def test_get_users(client):
    response = client.get("/api/users")

    assert response.status_code == 200
    body = response.get_json()
    assert body == {
        "success": True,
        "users": [
            {
                "username": "tester",
                "email": "tester@example.com",
                "full_name": "Tester User",
                "isActive": 1,
                "isDev": 0,
                "isTesting": 0,
                "isConfig": 1,
                "isFsNxt": 1,
                "isSapFsNxt": 0,
                "modules": [],
                "created_at": "2026-09-09T10:30:00",
                "updated_at": "2026-09-09T11:45:00",
            }
        ],
    }


def test_get_users_handles_database_error(client, app):
    def unavailable():
        raise mysql.connector.Error("database unavailable")

    app.config["USERS_FETCHER"] = unavailable

    response = client.get("/api/users")

    assert response.status_code == 503


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
    assert body["user"]["isAdmin"] == 0


def test_google_login_rejects_inactive_user(app):
    app.config["USER_LOOKUP_BY_EMAIL"] = lambda email: {
        "id": 1,
        "username": "tester",
        "email": email,
        "isActive": 0,
        "isAdmin": 0,
    }

    response = app.test_client().post(
        "/api/auth/google", json={"credential": "valid-token"}
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "success": False,
        "message": "This account is inactive. Please contact an administrator.",
    }


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
    assert body["user"]["isAdmin"] == 0


def test_google_desktop_login_rejects_inactive_user(app):
    app.config["USER_LOOKUP_BY_EMAIL"] = lambda email: {
        "id": 1,
        "username": "tester",
        "email": email,
        "isActive": False,
        "isAdmin": 0,
    }

    response = app.test_client().post(
        "/api/auth/google/desktop", json=VALID_DESKTOP_PAYLOAD
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "success": False,
        "message": "This account is inactive. Please contact an administrator.",
    }


def test_login_and_me_report_module_access(client):
    login = client.post(
        "/api/login", json={"username": "tester", "password": "test-password"}
    )
    body = login.get_json()
    assert body["user"]["isDev"] == 1
    assert body["user"]["isTesting"] == 0
    assert body["user"]["isConfig"] == 1
    assert body["user"]["isFsNxt"] == 1
    assert body["user"]["isSapFsNxt"] == 0
    assert body["user"]["modules"] == ["development"]

    me = client.get(
        "/api/me", headers={"Authorization": "Bearer " + body["access_token"]}
    )
    assert me.status_code == 200
    assert me.get_json()["user"]["isConfig"] == 1
    assert me.get_json()["user"]["isFsNxt"] == 1
    assert me.get_json()["user"]["isSapFsNxt"] == 0
    assert me.get_json()["user"]["modules"] == ["development"]

    assert client.get("/api/me").status_code == 401
    assert client.get(
        "/api/me", headers={"Authorization": "Bearer bad"}
    ).status_code == 401
