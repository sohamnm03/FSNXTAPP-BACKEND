import mysql.connector


VALID_LOG = {
    "username": "tester",
    "client": "desktop-app",
    "TC": "TC-123",
    "path": r"C:\data\report.csv",
    "lane": "lane-1",
}


def test_get_logs_returns_all_logs_for_admin(client):
    response = client.get("/api/logs?username=admin")

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "logs": [
            {
                "my_row_id": 2,
                "username": "other",
                "client": "web-app",
                "TC": "TC-456",
                "path": r"C:\data\other.csv",
                "created_at": "2026-09-09T12:30:00",
                "lane": "lane-2",
            },
            {
                "my_row_id": 1,
                "username": "tester",
                "client": "desktop-app",
                "TC": "TC-123",
                "path": r"C:\data\report.csv",
                "created_at": "2026-09-09T10:30:00",
                "lane": "lane-1",
            },
        ],
    }


def test_get_logs_returns_only_matching_logs_for_non_admin(client):
    response = client.get("/api/logs?username=tester")

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "logs": [
            {
                "my_row_id": 1,
                "username": "tester",
                "client": "desktop-app",
                "TC": "TC-123",
                "path": r"C:\data\report.csv",
                "created_at": "2026-09-09T10:30:00",
                "lane": "lane-1",
            },
        ],
    }


def test_get_logs_requires_known_username(client):
    assert client.get("/api/logs").status_code == 400
    assert client.get("/api/logs?username=").status_code == 400
    assert client.get("/api/logs?username=missing").status_code == 404


def test_get_logs_handles_database_error(client, app):
    def unavailable(_username):
        raise mysql.connector.Error("database unavailable")

    app.config["LOGS_FOR_USER_FETCHER"] = unavailable

    response = client.get("/api/logs?username=tester")

    assert response.status_code == 503


def test_create_log(client, app):
    response = client.post("/api/logs", json=VALID_LOG)

    assert response.status_code == 201
    assert response.get_json() == {
        "success": True,
        "message": "Log created successfully.",
    }
    assert app.config["INSERTED_LOGS"] == [VALID_LOG]


def test_create_log_rejects_invalid_or_missing_fields(client):
    assert client.post("/api/logs", json=[]).status_code == 400
    assert client.post("/api/logs", json={}).status_code == 400

    for field in VALID_LOG:
        payload = {**VALID_LOG, field: ""}
        assert client.post("/api/logs", json=payload).status_code == 400


def test_create_log_rejects_values_longer_than_columns(client):
    limits = {"username": 100, "client": 100, "TC": 100, "path": 500, "lane": 100}

    for field, limit in limits.items():
        payload = {**VALID_LOG, field: "x" * (limit + 1)}
        response = client.post("/api/logs", json=payload)
        assert response.status_code == 400
        assert field in response.get_json()["message"]


def test_create_log_handles_database_error(client, app):
    def fail_insert(*_args):
        raise mysql.connector.Error("database unavailable")

    app.config["LOG_INSERTER"] = fail_insert

    response = client.post("/api/logs", json=VALID_LOG)

    assert response.status_code == 503
    assert response.get_json() == {
        "success": False,
        "message": "Log service is unavailable.",
    }
