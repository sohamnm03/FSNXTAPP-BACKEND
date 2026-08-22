"""Signed bearer-token helpers for the single login API."""
from __future__ import annotations

from flask import current_app, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["AUTH_SECRET"], salt="backend-auth")


def create_access_token(user_id: str) -> str:
    return _serializer().dumps({"sub": user_id})


def authenticated_user() -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    try:
        payload = _serializer().loads(token, max_age=current_app.config["AUTH_TOKEN_MAX_AGE"])
    except (BadSignature, SignatureExpired):
        return None
    user_id = payload.get("sub") if isinstance(payload, dict) else None
    return user_id if isinstance(user_id, str) and user_id else None
