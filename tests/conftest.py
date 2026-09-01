from __future__ import annotations

import pytest
from google.auth.exceptions import TransportError
from app import create_app


@pytest.fixture
def app():
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
        return {
            "iss": "https://accounts.google.com",
            "email": "tester@example.com",
            "email_verified": credential != "unverified-token",
            "sub": "google-user-id",
        }

    return create_app(
        {
            "TESTING": True,
            "AUTH_SECRET": "test-signing-secret",
            "GOOGLE_TOKEN_VERIFIER": verify_google_token,
            "USER_LOOKUP_BY_USERNAME": (
                lambda username: user if username == user["username"] else None
            ),
            "USER_LOOKUP_BY_EMAIL": (
                lambda email: user if email == user["email"] else None
            ),
        }
    )


@pytest.fixture
def client(app):
    return app.test_client()


