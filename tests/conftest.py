from __future__ import annotations

from datetime import datetime

import pytest
from google.auth.exceptions import TransportError
from app import create_app


@pytest.fixture
def app():
    inserted_logs = []
    inserted_users = []
    active_status_updates = []
    users = [
        {
            "username": "tester",
            "email": "tester@example.com",
            "full_name": "Tester User",
            "isActive": 1,
            "created_at": datetime(2026, 9, 9, 10, 30, 0),
            "updated_at": datetime(2026, 9, 9, 11, 45, 0),
        }
    ]
    user = {
        "id": 1,
        "username": "tester",
        "email": "tester@example.com",
        "password": "test-password",
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

    def update_user_active_status(email, is_active):
        if email == "missing@example.com":
            return False
        active_status_updates.append({"email": email, "isActive": is_active})
        return True

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
            "USER_INSERTER": lambda username, email, full_name: inserted_users.append(
                {
                    "id": 2,
                    "username": username,
                    "email": email,
                    "full_name": full_name,
                }
            )
            or 2,
            "USER_ACTIVE_STATUS_UPDATER": update_user_active_status,
            "LOG_INSERTER": lambda username, client, tc, path: inserted_logs.append(
                {
                    "username": username,
                    "client": client,
                    "TC": tc,
                    "path": path,
                }
            ),
            "INSERTED_LOGS": inserted_logs,
            "INSERTED_USERS": inserted_users,
            "FETCHED_USERS": users,
            "ACTIVE_STATUS_UPDATES": active_status_updates,
        }
    )


@pytest.fixture
def client(app):
    return app.test_client()


