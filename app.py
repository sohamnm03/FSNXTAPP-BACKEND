"""Minimal Flask backend exposing the login API only."""
from __future__ import annotations

import base64
import binascii
import json
import os
import time
from hmac import compare_digest

import mysql.connector
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token as google_id_token
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer

load_dotenv()

DEFAULT_GOOGLE_CLIENT_ID = (
    "418759424186-1unbscgfsrscmpopcfip8vrd62isu5rd.apps.googleusercontent.com"
)
DEFAULT_GOOGLE_CLOCK_SKEW_SECONDS = 10


def _find_user(column: str, value: str) -> dict | None:
    if column not in {"username", "email"}:
        raise ValueError("Unsupported user lookup column")

    connection = mysql.connector.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        port=int(os.environ.get("DB_PORT", "3306")),
        connection_timeout=10,
    )
    try:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT id, username, email, password
                FROM users
                WHERE LOWER({column}) = LOWER(%s)
                LIMIT 1
                """,
                (value,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()
    finally:
        connection.close()


def find_user_by_username(username: str) -> dict | None:
    return _find_user("username", username)


def find_user_by_email(email: str) -> dict | None:
    return _find_user("email", email)


def verify_google_credential(
    credential: str,
    audience: str,
    clock_skew_in_seconds: int = DEFAULT_GOOGLE_CLOCK_SKEW_SECONDS,
) -> dict:
    return google_id_token.verify_oauth2_token(
        credential,
        GoogleRequest(),
        audience,
        clock_skew_in_seconds=clock_skew_in_seconds,
    )


def diagnose_google_credential(credential: str, expected_audience: str) -> str:
    """Return a safe reason without logging or returning the credential itself."""
    try:
        parts = credential.split(".")
        if len(parts) != 3:
            return "Google returned a malformed ID token."
        encoded_payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded_payload).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return "Google returned a malformed ID token."

    audience = claims.get("aud")
    if audience != expected_audience:
        return (
            "Google token client ID mismatch. "
            f"Expected {expected_audience}, received {audience or 'no audience'}."
        )
    if claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        return "Google token issuer is invalid."
    try:
        if float(claims.get("exp", 0)) <= time.time():
            return "Google ID token has expired. Please sign in again."
    except (TypeError, ValueError):
        return "Google ID token has an invalid expiry time."
    return "Google ID token signature validation failed."


def create_auth_response(app: Flask, user: dict, message: str) -> dict:
    serializer = URLSafeTimedSerializer(app.config["AUTH_SECRET"], salt="backend-auth")
    return {
        "success": True,
        "message": message,
        "access_token": serializer.dumps(
            {"sub": str(user["id"]), "username": user["username"]}
        ),
        "token_type": "Bearer",
        "expires_in": app.config["AUTH_TOKEN_MAX_AGE"],
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
        },
    }


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    google_client_id = (
        os.environ.get("GOOGLE_SSO_CLIENT_ID")
        or os.environ.get("GOOGLE_CLIENT_ID")
        or DEFAULT_GOOGLE_CLIENT_ID
    )
    app.config.from_mapping(
        AUTH_SECRET=os.environ.get("AUTH_SECRET", "development-only-change-me"),
        AUTH_TOKEN_MAX_AGE=int(os.environ.get("AUTH_TOKEN_MAX_AGE", "3600")),
        GOOGLE_CLIENT_ID=google_client_id,
        GOOGLE_CLOCK_SKEW_SECONDS=int(
            os.environ.get(
                "GOOGLE_CLOCK_SKEW_SECONDS",
                str(DEFAULT_GOOGLE_CLOCK_SKEW_SECONDS),
            )
        ),
        GOOGLE_TOKEN_VERIFIER=verify_google_credential,
        USER_LOOKUP_BY_USERNAME=find_user_by_username,
        USER_LOOKUP_BY_EMAIL=find_user_by_email,
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)
    CORS(app)

    @app.post("/api/login")
    def login():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "Invalid request body."}), 400

        username, password = data.get("username", ""), data.get("password", "")
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or not username
            or not password
        ):
            return jsonify({"success": False, "message": "Please fill in all fields."}), 400

        try:
            user = app.config["USER_LOOKUP_BY_USERNAME"](username)
        except (mysql.connector.Error, KeyError, ValueError):
            app.logger.exception("Database lookup failed during login")
            return jsonify({"success": False, "message": "Login service is unavailable."}), 503

        stored_password = user.get("password") if user else None
        password_matches = isinstance(stored_password, str) and compare_digest(
            stored_password, password
        )

        if not password_matches:
            return jsonify({"success": False, "message": "Invalid username or password."}), 401

        return jsonify(create_auth_response(app, user, "Login successful."))

    @app.post("/api/auth/google")
    def google_login():
        data = request.get_json(silent=True) or {}
        credential = data.get("credential", "") if isinstance(data, dict) else ""
        if not isinstance(credential, str) or not credential:
            return jsonify({"success": False, "message": "Google credential is required."}), 400

        try:
            claims = app.config["GOOGLE_TOKEN_VERIFIER"](
                credential,
                app.config["GOOGLE_CLIENT_ID"],
                app.config["GOOGLE_CLOCK_SKEW_SECONDS"],
            )
        except TransportError:
            app.logger.exception("Google signing keys could not be reached")
            return jsonify(
                {
                    "success": False,
                    "message": "Google verification service is unavailable. Please try again.",
                }
            ), 503
        except (GoogleAuthError, ValueError) as exc:
            app.logger.exception("Google credential verification failed: %s", exc)
            return jsonify(
                {
                    "success": False,
                    "message": diagnose_google_credential(
                        credential,
                        app.config["GOOGLE_CLIENT_ID"],
                    ),
                }
            ), 401

        issuer = claims.get("iss")
        email = claims.get("email")
        if (
            issuer not in {"accounts.google.com", "https://accounts.google.com"}
            or claims.get("email_verified") is not True
            or not isinstance(email, str)
            or not email
        ):
            return jsonify({"success": False, "message": "Google email could not be verified."}), 401

        try:
            user = app.config["USER_LOOKUP_BY_EMAIL"](email)
        except (mysql.connector.Error, KeyError, ValueError):
            app.logger.exception("Database lookup failed during Google login")
            return jsonify({"success": False, "message": "Login service is unavailable."}), 503

        if not user:
            return jsonify(
                {
                    "success": False,
                    "message": "No account is registered for this Google email.",
                }
            ), 403

        return jsonify(create_auth_response(app, user, "Google login successful."))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(port=5000, debug=True, use_reloader=False)
