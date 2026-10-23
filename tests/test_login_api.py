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


def test_google_user_lookup_does_not_select_password(monkeypatch):
    captured = {}

    class Cursor:
        def execute(self, statement, parameters):
            captured["statement"] = " ".join(statement.split())
            captured["parameters"] = parameters

        def fetchone(self):
            return {
                "id": 1,
                "username": "tester",
                "email": "tester@example.com",
                "isActive": 1,
            }

        def close(self):
            pass

    class Connection:
        def cursor(self, *, dictionary):
            assert dictionary is True
            return Cursor()

        def close(self):
            pass

    for name in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"):
        monkeypatch.setenv(name, "test-value")
    monkeypatch.setattr(
        app_module.mysql.connector, "connect", lambda **kwargs: Connection()
    )

    user = app_module.find_user_by_email("tester@example.com")

    assert user["isActive"] == 1
    assert "password" not in captured["statement"].lower()
    assert "FROM users" in captured["statement"]
    assert captured["parameters"] == ("tester@example.com",)


def test_login(client):
    assert client.post("/api/login", json={}).status_code == 400
    assert client.post("/api/login", json=[]).status_code == 400
    assert client.post("/api/login", json={"username": "missing", "password": "anything"}).status_code == 401
    assert client.post("/api/login", json={"username": "tester", "password": "wrong"}).status_code == 401
    response = client.post(
        "/api/login",
        json={"username": "tester", "password": "test-password", "version": "1.5.0"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["access_token"]
    assert body["token_type"] == "Bearer"
    assert body["user"]["email"] == "tester@example.com"
    assert body["user"]["isConfig"] == 1


def test_login_handles_version_database_error(client, app):
    def unavailable(version):
        raise mysql.connector.Error("database unavailable")

    app.config["VERSION_CHECKER"] = unavailable

    response = client.post(
        "/api/login",
        json={"username": "tester", "password": "test-password", "version": "1.5.0"},
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "success": False,
        "message": "Version validation service is unavailable.",
    }


def test_database_fetches_valid_version_ranges(monkeypatch):
    statements = []

    class Cursor:
        def execute(self, statement):
            statements.append(" ".join(statement.split()))

        def fetchall(self):
            return [{"version_from": "1.0.0", "version_to": "2.0.0"}]

        def close(self):
            pass

    class Connection:
        def cursor(self, *, dictionary):
            assert dictionary is True
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(
        app_module.mysql.connector, "connect", lambda **kwargs: Connection()
    )

    ranges = app_module.fetch_valid_version_ranges()

    assert ranges == [{"version_from": "1.0.0", "version_to": "2.0.0"}]
    assert statements == [
        "SELECT `from` AS version_from, `to` AS version_to FROM valid_versions"
    ]


def test_version_range_comparison_is_inclusive():
    ranges = [{"version_from": "1.0.0", "version_to": "2.0.0"}]

    assert app_module.version_matches_ranges(
        app_module.Version("1.0.0"), ranges
    ) is True
    assert app_module.version_matches_ranges(
        app_module.Version("2.0.0"), ranges
    ) is True
    assert app_module.version_matches_ranges(
        app_module.Version("2.0.1"), ranges
    ) is False


def test_versions_endpoint_fetches_and_updates_only_existing_rows(client):
    response = client.get("/api/versions")

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "version": {"from": "1.0.0", "to": "2.0.0"},
    }

    response = client.post(
        "/api/versions",
        json={"from": "1.1.0", "to": "2.1.0"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "message": "Version updated successfully.",
        "version": {"from": "1.1.0", "to": "2.1.0"},
    }
    assert client.get("/api/versions").get_json() == {
        "success": True,
        "version": {"from": "1.1.0", "to": "2.1.0"},
    }


def test_update_version_endpoint_validates_payload(client):
    assert client.post("/api/versions", json=[]).status_code == 400
    assert client.post(
        "/api/versions",
        json={"from": "not-a-version", "to": "2.0.0"},
    ).status_code == 400

    response = client.post(
        "/api/versions",
        json={"from": "2.0.0", "to": "1.0.0"},
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "success": False,
        "message": "from version cannot be greater than to version.",
    }


def test_update_version_endpoint_does_not_create_a_missing_row(client, app):
    app.config["VERSION_FETCHER"] = lambda: None
    app.config["VERSION_UPDATER"] = lambda version_from, version_to: False

    response = client.post(
        "/api/versions",
        json={"from": "1.0.0", "to": "2.0.0"},
    )

    assert response.status_code == 404
    assert response.get_json() == {
        "success": False,
        "message": "Version entry not found.",
    }
    assert client.get("/api/versions").status_code == 404


def test_database_update_version_never_inserts(monkeypatch):
    statements = []

    class Cursor:
        def execute(self, statement, parameters=None):
            statements.append((" ".join(statement.split()), parameters))

        def fetchone(self):
            return {"my_row_id": 4}

        def close(self):
            pass

    class Connection:
        def __init__(self):
            self.committed = False
            self.rolled_back = False

        def cursor(self, *, dictionary):
            assert dictionary is True
            return Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(
        app_module.mysql.connector,
        "connect",
        lambda **kwargs: connection,
    )

    updated = app_module.update_version("1.0.0", "2.0.0")

    assert updated is True
    assert connection.committed is True
    assert [statement for statement, _ in statements] == [
        (
            "SELECT my_row_id FROM valid_versions ORDER BY my_row_id "
            "LIMIT 1 FOR UPDATE"
        ),
        (
            "UPDATE valid_versions SET `from` = %s, `to` = %s "
            "WHERE my_row_id = %s"
        ),
    ]
    assert all("INSERT" not in statement for statement, _ in statements)
    assert statements[0][1] is None
    assert statements[1][1] == ("1.0.0", "2.0.0", 4)


def test_login_requires_active_user_and_matching_version(app):
    client = app.test_client()

    missing_version = client.post(
        "/api/login", json={"username": "tester", "password": "test-password"}
    )
    assert missing_version.status_code == 400

    app.config["VERSION_CHECKER"] = lambda version: False
    mismatch = client.post(
        "/api/login",
        json={"username": "tester", "password": "test-password", "version": "2.0.1"},
    )
    assert mismatch.status_code == 403
    assert mismatch.get_json() == {
        "success": False,
        "message": "Version not supported",
    }

    app.config["USER_LOOKUP_BY_USERNAME"] = lambda username: {
        "id": 1,
        "username": username,
        "email": "tester@example.com",
        "password": "test-password",
        "isActive": 0,
        "isAdmin": 0,
    }
    inactive = client.post(
        "/api/login",
        json={"username": "tester", "password": "test-password", "version": "1.5.0"},
    )
    assert inactive.status_code == 403
    assert inactive.get_json() == {
        "success": False,
        "message": "This account is inactive. Please contact an administrator.",
    }


def test_login_accepts_password_hash(app):
    hashed_user = {
        "id": 3,
        "username": "hashed",
        "email": "hashed@example.com",
        "password": None,
        "password_hash": generate_password_hash("secret-password"),
        "isActive": 1,
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
        "/api/login",
        json={"username": "hashed", "password": "secret-password", "version": "1.5.0"},
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
            "isDataMigration": False,
            "isMobile": False,
            "isDeploy": False,
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
            "isDataMigration": False,
            "isMobile": False,
            "isDeploy": False,
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
            "isConfig": False,
            "isFsNxt": False,
            "isSapFsNxt": False,
            "isDataMigration": False,
            "isDeploy": False,
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
            "isDataMigration": True,
            "isMobile": True,
            "isDeploy": True,
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
        "isDataMigration": 1,
        "isMobile": 1,
        "isDeploy": 1,
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
            "isDataMigration": 0,
            "isMobile": 1,
            "isDeploy": 0,
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
            "isDataMigration": 0,
            "isMobile": 1,
            "isDeploy": 0,
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
        True,
        True,
        False,
    )

    assert (user_id, created) == (7, False)
    assert connection.committed is True
    update_statement, update_parameters = statements[1]
    assert "SET username = %s, email = %s, full_name = %s" in update_statement
    assert "isDataMigration = %s, isMobile = %s, isDeploy = %s" in update_statement
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
        1,
        1,
        0,
        "2026-10-12 12:00:00",
        7,
    )


def test_database_upsert_inserts_mobile_and_deploy_flags_for_new_user(monkeypatch):
    statements = []

    class Cursor:
        lastrowid = 8

        def execute(self, statement, parameters):
            statements.append((" ".join(statement.split()), parameters))

        def fetchone(self):
            return None

        def close(self):
            pass

    class Connection:
        def cursor(self, dictionary=False):
            assert dictionary is True
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            raise AssertionError("The successful upsert must not be rolled back")

        def close(self):
            pass

    monkeypatch.setattr(
        app_module.mysql.connector, "connect", lambda **kwargs: Connection()
    )
    monkeypatch.setattr(
        app_module, "get_current_ist_time", lambda: "2026-10-12 12:00:00"
    )

    user_id, created = app_module.upsert_user(
        "Mobile User",
        "mobile@example.com",
        "Mobile User",
        True,
        False,
        True,
        False,
        True,
        False,
        False,
        True,
        True,
    )

    assert (user_id, created) == (8, True)
    insert_statement, insert_parameters = statements[1]
    assert "isDataMigration, isMobile, isDeploy" in insert_statement
    assert insert_parameters == (
        "Mobile User",
        "mobile@example.com",
        "Mobile User",
        1,
        0,
        1,
        0,
        1,
        0,
        0,
        1,
        1,
        "2026-10-12 12:00:00",
        "2026-10-12 12:00:00",
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
            "isDataMigration": False,
            "isMobile": False,
            "isDeploy": False,
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
            "isDataMigration": True,
            "isMobile": True,
            "isDeploy": True,
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
            "isDataMigration": True,
            "isMobile": True,
            "isDeploy": True,
        },
    )

    assert response.status_code == 503


def test_delete_user_by_email(client, app):
    assert client.delete("/api/users", json=[]).status_code == 400
    assert client.delete("/api/users", json={}).status_code == 400
    assert client.delete("/api/users", json={"email": 123}).status_code == 400
    assert client.delete(
        "/api/users", json={"email": "x" * 256}
    ).status_code == 400

    response = client.delete(
        "/api/users", json={"email": " TESTER@EXAMPLE.COM "}
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "message": "User deleted successfully.",
        "email": "TESTER@EXAMPLE.COM",
    }
    assert app.config["DELETED_USERS"] == ["TESTER@EXAMPLE.COM"]
    assert client.get("/api/users").get_json()["users"] == []


def test_delete_user_returns_not_found(client):
    response = client.delete(
        "/api/users", json={"email": "missing@example.com"}
    )

    assert response.status_code == 404
    assert response.get_json() == {
        "success": False,
        "message": "User not found.",
    }


def test_delete_user_handles_database_conflict(client, app):
    def conflict(email):
        raise mysql.connector.IntegrityError("user is referenced")

    app.config["USER_DELETER"] = conflict

    response = client.delete(
        "/api/users", json={"email": "tester@example.com"}
    )

    assert response.status_code == 409
    assert response.get_json()["success"] is False


def test_delete_user_handles_database_error(client, app):
    def unavailable(email):
        raise mysql.connector.Error("database unavailable")

    app.config["USER_DELETER"] = unavailable

    response = client.delete(
        "/api/users", json={"email": "tester@example.com"}
    )

    assert response.status_code == 503


def test_database_delete_removes_matching_email(monkeypatch):
    statements = []

    class Cursor:
        rowcount = 1

        def execute(self, statement, parameters):
            statements.append((" ".join(statement.split()), parameters))

        def close(self):
            pass

    class Connection:
        def __init__(self):
            self.committed = False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            raise AssertionError("The successful delete must not be rolled back")

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(app_module.mysql.connector, "connect", lambda **kwargs: connection)

    deleted = app_module.delete_user_by_email("tester@example.com")

    assert deleted is True
    assert connection.committed is True
    assert statements == [
        (
            "DELETE FROM users WHERE LOWER(email) = LOWER(%s) LIMIT 1",
            ("tester@example.com",),
        )
    ]


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
                "isDataMigration": 1,
                "isMobile": 1,
                "isDeploy": 1,
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
        "/api/auth/google", json={"credential": "valid-token", "version": "1.5.0"}
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


def test_google_login_rejects_mismatched_version(client):
    client.application.config["VERSION_CHECKER"] = lambda version: False
    response = client.post(
        "/api/auth/google",
        json={"credential": "valid-token", "version": "2.0.1"},
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "success": False,
        "message": "Version not supported",
    }


VALID_DESKTOP_PAYLOAD = {
    "code": "valid-code",
    "codeVerifier": "verifier",
    "redirectUri": "http://127.0.0.1:54231/",
    "nonce": "expected-nonce",
    "version": "1.5.0",
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


def test_google_desktop_login_rejects_mismatched_version(client):
    client.application.config["VERSION_CHECKER"] = lambda version: False
    response = client.post(
        "/api/auth/google/desktop",
        json={**VALID_DESKTOP_PAYLOAD, "version": "2.0.1"},
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "success": False,
        "message": "Version not supported",
    }


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
        "/api/login",
        json={"username": "tester", "password": "test-password", "version": "1.5.0"},
    )
    body = login.get_json()
    assert body["user"]["isDev"] == 1
    assert body["user"]["isTesting"] == 0
    assert body["user"]["isConfig"] == 1
    assert body["user"]["isFsNxt"] == 1
    assert body["user"]["isSapFsNxt"] == 0
    assert body["user"]["isDataMigration"] == 1
    assert body["user"]["isMobile"] == 1
    assert body["user"]["isDeploy"] == 1
    assert body["user"]["modules"] == ["development"]

    me = client.get(
        "/api/me", headers={"Authorization": "Bearer " + body["access_token"]}
    )
    assert me.status_code == 200
    assert me.get_json()["user"]["isConfig"] == 1
    assert me.get_json()["user"]["isFsNxt"] == 1
    assert me.get_json()["user"]["isSapFsNxt"] == 0
    assert me.get_json()["user"]["isDataMigration"] == 1
    assert me.get_json()["user"]["isMobile"] == 1
    assert me.get_json()["user"]["isDeploy"] == 1
    assert me.get_json()["user"]["modules"] == ["development"]

    assert client.get("/api/me").status_code == 401
    assert client.get(
        "/api/me", headers={"Authorization": "Bearer bad"}
    ).status_code == 401
