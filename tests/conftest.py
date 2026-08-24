from __future__ import annotations

import pytest
from app import create_app


@pytest.fixture
def app():
    return create_app({
        "TESTING": True, "AUTH_SECRET": "test-signing-secret", "AUTH_USERNAME": "tester",
        "AUTH_PASSWORD": "test-password",
    })


@pytest.fixture
def client(app): return app.test_client()


