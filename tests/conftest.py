from __future__ import annotations

from datetime import datetime

import pytest
from google.auth.exceptions import TransportError
from app import create_app


@pytest.fixture
def app():
    inserted_logs = []
    upserted_users = []
    deleted_users = []
    users = [
        {
            "id": 1,
            "username": "tester",
            "email": "tester@example.com",
            "full_name": "Tester User",
            "isActive": 1,
            "isAdmin": 0,
            "isDev": 0,
            "isTesting": 0,
            "isConfig": 1,
            "isFsNxt": 1,
            "isSapFsNxt": 0,
            "created_at": datetime(2026, 9, 9, 10, 30, 0),
            "updated_at": datetime(2026, 9, 9, 11, 45, 0),
        }
    ]
    log_users = [
        *users,
        {
            "username": "admin",
            "email": "admin@example.com",
            "full_name": "Admin User",
            "isActive": 1,
            "isAdmin": 1,
            "created_at": datetime(2026, 9, 8, 10, 30, 0),
            "updated_at": datetime(2026, 9, 8, 11, 45, 0),
        },
    ]
    logs = [
        {
            "my_row_id": 2,
            "username": "other",
            "client": "web-app",
            "TC": "TC-456",
            "path": r"C:\data\other.csv",
            "created_at": datetime(2026, 9, 9, 12, 30, 0),
            "lane": "lane-2",
        },
        {
            "my_row_id": 1,
            "username": "tester",
            "client": "desktop-app",
            "TC": "TC-123",
            "path": r"C:\data\report.csv",
            "created_at": datetime(2026, 9, 9, 10, 30, 0),
            "lane": "lane-1",
        },
    ]
    user = {
        "id": 1,
        "username": "tester",
        "email": "tester@example.com",
        "password": "test-password",
        "isActive": 1,
        "isAdmin": 0,
        "isDev": 1,
        "isTesting": 0,
        "isConfig": 1,
        "isFsNxt": 1,
        "isSapFsNxt": 0,
    }
    def verify_google_token(credential, audience, clock_skew_in_seconds):
        assert audience.endswith(".apps.googleusercontent.com")
        assert clock_skew_in_seconds == 10
        if credential == "invalid-token":
            raise ValueError("Invalid token")
        if credential == "network-error":
            raise TransportError("Google is unavailable")
        if credential == "desktop-id-token":
            return {
                "iss": "https://accounts.google.com",
                "email": "tester@example.com",
                "email_verified": True,
                "nonce": "expected-nonce",
                "sub": "google-user-id",
            }
        return {
            "iss": "https://accounts.google.com",
            "email": "tester@example.com",
            "email_verified": credential != "unverified-token",
            "sub": "google-user-id",
        }

    def exchange_google_authorization_code(code, code_verifier, redirect_uri, client_id, client_secret):
        assert client_id == "418759424186-vhvn6f4g6ckvef5gvjdtqi4g6gvfmvpe.apps.googleusercontent.com"
        assert client_secret == "test-desktop-secret"
        if code == "invalid-code":
            raise ValueError("invalid_grant")
        return {"id_token": "desktop-id-token"}

    def upsert_user_record(
        username,
        email,
        full_name,
        is_active,
        is_dev,
        is_testing,
        is_config,
        is_fs_nxt,
        is_sap_fs_nxt,
    ):
        existing = next(
            (item for item in users if item["email"].lower() == email.lower()),
            None,
        )
        created = existing is None
        user_id = 2 if created else existing["id"]
        record = {
            "id": user_id,
            "username": username,
            "email": email,
            "full_name": full_name,
            "isActive": int(is_active),
            "isDev": int(is_dev),
            "isTesting": int(is_testing),
            "isConfig": int(is_config),
            "isFsNxt": int(is_fs_nxt),
            "isSapFsNxt": int(is_sap_fs_nxt),
        }
        upserted_users.append(record)
        return user_id, created

    def delete_user_record(email):
        existing = next(
            (item for item in users if item["email"].lower() == email.lower()),
            None,
        )
        if existing is None:
            return False
        users.remove(existing)
        deleted_users.append(email)
        return True

    def fetch_logs_for_username(username):
        user_record = next(
            (user for user in log_users if user["username"].lower() == username.lower()),
            None,
        )
        if user_record is None:
            return None
        if user_record["isAdmin"] == 1:
            return logs
        return [
            log for log in logs if log["username"].lower() == username.lower()
        ]

    return create_app(
        {
            "TESTING": True,
            "AUTH_SECRET": "test-signing-secret",
            "GOOGLE_TOKEN_VERIFIER": verify_google_token,
            "GOOGLE_DESKTOP_CLIENT_ID": "418759424186-vhvn6f4g6ckvef5gvjdtqi4g6gvfmvpe.apps.googleusercontent.com",
            "GOOGLE_DESKTOP_CLIENT_SECRET": "test-desktop-secret",
            "GOOGLE_TOKEN_EXCHANGER": exchange_google_authorization_code,
            "USER_LOOKUP_BY_USERNAME": (
                lambda username: user if username == user["username"] else None
            ),
            "USER_LOOKUP_BY_EMAIL": (
                lambda email: user if email == user["email"] else None
            ),
            "USERS_FETCHER": lambda: users,
            "USER_UPSERTER": upsert_user_record,
            "USER_DELETER": delete_user_record,
            "LOGS_FOR_USER_FETCHER": fetch_logs_for_username,
            "LOG_INSERTER": lambda username, client, tc, path, lane: inserted_logs.append(
                {
                    "username": username,
                    "client": client,
                    "TC": tc,
                    "path": path,
                    "lane": lane,
                }
            ),
            "INSERTED_LOGS": inserted_logs,
            "UPSERTED_USERS": upserted_users,
            "DELETED_USERS": deleted_users,
            "FETCHED_USERS": users,
            "FETCHED_LOGS": logs,
        }
    )


@pytest.fixture
def client(app):
    return app.test_client()


