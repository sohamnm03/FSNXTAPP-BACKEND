from __future__ import annotations

import pytest
from app import create_app


@pytest.fixture
def app():
    user = {
        "id": 1,
        "username": "tester",
        "email": "tester@example.com",
        "password": "test-password",
    }
    return create_app(
        {
            "TESTING": True,
            "AUTH_SECRET": "test-signing-secret",
            "USER_LOOKUP": lambda username: user if username == user["username"] else None,
        }
    )


@pytest.fixture
def client(app):
    return app.test_client()


