"""Flask backend exposing authentication and log APIs."""
from __future__ import annotations

import base64
import binascii
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from hmac import compare_digest
from urllib.parse import urlparse

import mysql.connector
import requests
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token as google_id_token
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer
from werkzeug.security import check_password_hash
load_dotenv()

DEFAULT_GOOGLE_CLIENT_ID = (
    "418759424186-1unbscgfsrscmpopcfip8vrd62isu5rd.apps.googleusercontent.com"
)
DEFAULT_GOOGLE_DESKTOP_CLIENT_ID = (
    "418759424186-vhvn6f4g6ckvef5gvjdtqi4g6gvfmvpe.apps.googleusercontent.com"
)
DEFAULT_GOOGLE_CLOCK_SKEW_SECONDS = 10
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
LOOPBACK_REDIRECT_HOSTS = {"127.0.0.1", "localhost", "::1"}

IST = ZoneInfo("Asia/Kolkata")


def get_current_ist_time() -> datetime:
    """
    Return the current date and time in India Standard Time (IST).
    This is independent of the server's local timezone.
    """
    return datetime.now(IST).replace(tzinfo=None)

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
                SELECT id, username, email
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


def fetch_users() -> list[dict]:
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
                """
                SELECT username, email, isActive, created_at, updated_at, full_name
                FROM users
                ORDER BY created_at DESC, id DESC
                """
            )
            return cursor.fetchall()
        finally:
            cursor.close()
    finally:
        connection.close()


def insert_user(username: str, email: str, full_name: str) -> int:
    connection = mysql.connector.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        port=int(os.environ.get("DB_PORT", "3306")),
        connection_timeout=10,
    )
    try:
        cursor = connection.cursor()
        try:
            current_time = get_current_ist_time()
            cursor.execute(
                """
                INSERT INTO users (username, email, full_name, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (username, email, full_name, current_time, current_time),
            )
            connection.commit()
            return cursor.lastrowid
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
    finally:
        connection.close()


def update_user_active_status(email: str, is_active: bool) -> bool:
    connection = mysql.connector.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        port=int(os.environ.get("DB_PORT", "3306")),
        connection_timeout=10,
    )
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE users
                SET isActive = %s, updated_at = %s
                WHERE LOWER(email) = LOWER(%s)
                """,
                (1 if is_active else 0, get_current_ist_time(), email),
            )
            connection.commit()
            return cursor.rowcount > 0
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
    finally:
        connection.close()


def fetch_logs() -> list[dict]:
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
                """
                SELECT *
                FROM table_logs
                ORDER BY created_at DESC
                """
            )
            return cursor.fetchall()
        finally:
            cursor.close()
    finally:
        connection.close()


def fetch_logs_for_username(username: str) -> list[dict] | None:
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
                """
                SELECT COUNT(*) AS user_count, MAX(COALESCE(isAdmin, 0)) AS isAdmin
                FROM users
                WHERE LOWER(username) = LOWER(%s)
                """,
                (username,),
            )
            user = cursor.fetchone()
            if user is None or user.get("user_count") == 0:
                return None

            is_admin = int(user.get("isAdmin") or 0) == 1
            if is_admin:
                cursor.execute(
                    """
                    SELECT *
                    FROM table_logs
                    ORDER BY created_at DESC
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT *
                    FROM table_logs
                    WHERE LOWER(username) = LOWER(%s)
                    ORDER BY created_at DESC
                    """,
                    (username,),
                )
            return cursor.fetchall()
        finally:
            cursor.close()
    finally:
        connection.close()


def insert_log(username: str, client: str, tc: str, path: str, lane: str) -> None:
    connection = mysql.connector.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        port=int(os.environ.get("DB_PORT", "3306")),
        connection_timeout=10,
    )
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO table_logs (username, client, TC, path, lane, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (username, client, tc, path, lane, get_current_ist_time()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
    finally:
        connection.close()


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


def exchange_google_authorization_code(
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> dict:
    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    payload = response.json() if response.content else {}
    if not response.ok:
        raise ValueError(
            payload.get("error_description")
            or payload.get("error")
            or "Google rejected the authorization code."
        )
    return payload


def verified_google_email(claims: dict) -> str | None:
    issuer = claims.get("iss")
    email = claims.get("email")
    if (
        issuer not in {"accounts.google.com", "https://accounts.google.com"}
        or claims.get("email_verified") is not True
        or not isinstance(email, str)
        or not email
    ):
        return None
    return email


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


def password_matches(user: dict | None, password: str) -> bool:
    if not user:
        return False

    stored_hash = user.get("password_hash")
    if isinstance(stored_hash, str) and stored_hash:
        try:
            return check_password_hash(stored_hash, password)
        except ValueError:
            return False

    stored_password = user.get("password")
    return isinstance(stored_password, str) and compare_digest(stored_password, password)


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    google_client_id = (
        os.environ.get("GOOGLE_SSO_CLIENT_ID")
        or os.environ.get("GOOGLE_CLIENT_ID")
        or DEFAULT_GOOGLE_CLIENT_ID
    )
    google_desktop_client_id = (
        os.environ.get("GOOGLE_DESKTOP_CLIENT_ID")
        or DEFAULT_GOOGLE_DESKTOP_CLIENT_ID
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
        GOOGLE_DESKTOP_CLIENT_ID=google_desktop_client_id,
        GOOGLE_DESKTOP_CLIENT_SECRET=os.environ.get("GOOGLE_DESKTOP_CLIENT_SECRET", ""),
        GOOGLE_TOKEN_EXCHANGER=exchange_google_authorization_code,
        USER_LOOKUP_BY_USERNAME=find_user_by_username,
        USER_LOOKUP_BY_EMAIL=find_user_by_email,
        USERS_FETCHER=fetch_users,
        USER_INSERTER=insert_user,
        USER_ACTIVE_STATUS_UPDATER=update_user_active_status,
        LOGS_FETCHER=fetch_logs,
        LOGS_FOR_USER_FETCHER=fetch_logs_for_username,
        LOG_INSERTER=insert_log,
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)
    CORS(app)

    @app.get("/api/users")
    def get_users():
        try:
            users = app.config["USERS_FETCHER"]()
        except (mysql.connector.Error, KeyError, ValueError):
            app.logger.exception("Database lookup failed while fetching users")
            return jsonify({"success": False, "message": "User service is unavailable."}), 503

        return jsonify(
            {
                "success": True,
                "users": [
                    {
                        "username": user.get("username"),
                        "email": user.get("email"),
                        "full_name": user.get("full_name"),
                        "isActive": user.get("isActive"),
                        "created_at": (
                            user["created_at"].isoformat()
                            if hasattr(user.get("created_at"), "isoformat")
                            else user.get("created_at")
                        ),
                        "updated_at": (
                            user["updated_at"].isoformat()
                            if hasattr(user.get("updated_at"), "isoformat")
                            else user.get("updated_at")
                        ),
                    }
                    for user in users
                ],
            }
        )

    @app.post("/api/users")
    def create_user():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "Invalid request body."}), 400

        username = data.get("username", "")
        email = data.get("email", "")
        full_name = data.get("full_name", data.get("fullName", ""))
        if not all(isinstance(value, str) and value.strip() for value in (username, email, full_name)):
            return jsonify(
                {"success": False, "message": "username, email, and full_name are required."}
            ), 400

        username = username.strip()
        email = email.strip()
        full_name = full_name.strip()
        if len(username) > 100:
            return jsonify({"success": False, "message": "username exceeds maximum length."}), 400
        if len(email) > 255:
            return jsonify({"success": False, "message": "email exceeds maximum length."}), 400
        if len(full_name) > 255:
            return jsonify({"success": False, "message": "full_name exceeds maximum length."}), 400

        try:
            user_id = app.config["USER_INSERTER"](
                username,
                email,
                full_name,
            )
        except mysql.connector.IntegrityError:
            return jsonify({"success": False, "message": "Username or email already exists."}), 409
        except (mysql.connector.Error, KeyError, ValueError):
            app.logger.exception("Database insert failed while creating user")
            return jsonify({"success": False, "message": "User service is unavailable."}), 503

        return jsonify(
            {
                "success": True,
                "message": "User created successfully.",
                "user": {
                    "id": user_id,
                    "username": username,
                    "email": email,
                    "full_name": full_name,
                },
            }
        ), 201

    @app.patch("/api/users/active")
    def set_user_active_status():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "Invalid request body."}), 400

        email = data.get("email", "")
        is_active = data.get("isActive")
        if not isinstance(email, str) or not email.strip():
            return jsonify({"success": False, "message": "email is required."}), 400
        if not isinstance(is_active, bool):
            return jsonify({"success": False, "message": "isActive must be true or false."}), 400

        email = email.strip()
        if len(email) > 255:
            return jsonify({"success": False, "message": "email exceeds maximum length."}), 400

        try:
            updated = app.config["USER_ACTIVE_STATUS_UPDATER"](email, is_active)
        except (mysql.connector.Error, KeyError, ValueError):
            app.logger.exception("Database update failed while changing user active status")
            return jsonify({"success": False, "message": "User service is unavailable."}), 503

        if not updated:
            return jsonify({"success": False, "message": "User not found."}), 404

        return jsonify(
            {
                "success": True,
                "message": "User active status updated successfully.",
                "user": {"email": email, "isActive": is_active},
            }
        )

    @app.get("/api/logs")
    def get_logs():
        username = request.args.get("username", "")
        if not isinstance(username, str) or not username.strip():
            return jsonify({"success": False, "message": "username is required."}), 400

        username = username.strip()
        if len(username) > 100:
            return jsonify({"success": False, "message": "username exceeds maximum length."}), 400

        try:
            logs = app.config["LOGS_FOR_USER_FETCHER"](username)
        except (mysql.connector.Error, KeyError, ValueError):
            app.logger.exception("Database lookup failed while fetching logs")
            return jsonify({"success": False, "message": "Log service is unavailable."}), 503

        if logs is None:
            return jsonify({"success": False, "message": "User not found."}), 404

        return jsonify(
            {
                "success": True,
                "logs": [
                    {
                        key: (
                            value.isoformat()
                            if hasattr(value, "isoformat")
                            else value
                        )
                        for key, value in log.items()
                    }
                    for log in logs
                ],
            }
        )

    @app.post("/api/logs")
    def create_log():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "Invalid request body."}), 400

        field_limits = {"username": 100, "client": 100, "TC": 100, "path": 500, "lane": 100}
        values = {field: data.get(field) for field in field_limits}

        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            return jsonify(
                {"success": False, "message": "username, client, TC, path, and lane are required."}
            ), 400

        too_long = [
            field
            for field, limit in field_limits.items()
            if len(values[field]) > limit
        ]
        if too_long:
            return jsonify(
                {
                    "success": False,
                    "message": f"Field exceeds maximum length: {', '.join(too_long)}.",
                }
            ), 400

        try:
            app.config["LOG_INSERTER"](
                values["username"],
                values["client"],
                values["TC"],
                values["path"],
                values["lane"],
            )
        except (mysql.connector.Error, KeyError, ValueError):
            app.logger.exception("Database insert failed while creating log")
            return jsonify({"success": False, "message": "Log service is unavailable."}), 503

        return jsonify({"success": True, "message": "Log created successfully."}), 201

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

        if not password_matches(user, password):
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

        email = verified_google_email(claims)
        if not email:
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

    @app.post("/api/auth/google/desktop")
    def google_desktop_login():
        if not app.config["GOOGLE_DESKTOP_CLIENT_SECRET"]:
            app.logger.error("GOOGLE_DESKTOP_CLIENT_SECRET is not configured")
            return jsonify(
                {"success": False, "message": "Google desktop sign-in is not configured on the server."}
            ), 500

        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "Invalid request body."}), 400

        code = data.get("code", "")
        code_verifier = data.get("codeVerifier", "")
        redirect_uri = data.get("redirectUri", "")
        expected_nonce = data.get("nonce", "")
        if not all(
            isinstance(value, str) and value
            for value in (code, code_verifier, redirect_uri, expected_nonce)
        ):
            return jsonify({"success": False, "message": "Google authorization details are missing."}), 400

        parsed_redirect = urlparse(redirect_uri)
        if parsed_redirect.scheme != "http" or parsed_redirect.hostname not in LOOPBACK_REDIRECT_HOSTS:
            return jsonify({"success": False, "message": "Invalid redirect URI."}), 400

        try:
            tokens = app.config["GOOGLE_TOKEN_EXCHANGER"](
                code,
                code_verifier,
                redirect_uri,
                app.config["GOOGLE_DESKTOP_CLIENT_ID"],
                app.config["GOOGLE_DESKTOP_CLIENT_SECRET"],
            )
        except requests.RequestException:
            app.logger.exception("Google token exchange request failed")
            return jsonify({"success": False, "message": "Could not reach Google to complete sign-in."}), 503
        except ValueError as exc:
            app.logger.warning("Google authorization code exchange rejected: %s", exc)
            return jsonify({"success": False, "message": "Google sign-in could not be completed. Please try again."}), 401

        id_token_value = tokens.get("id_token")
        if not isinstance(id_token_value, str) or not id_token_value:
            return jsonify({"success": False, "message": "Google did not return an identity token."}), 401

        try:
            claims = app.config["GOOGLE_TOKEN_VERIFIER"](
                id_token_value,
                app.config["GOOGLE_DESKTOP_CLIENT_ID"],
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
        except (GoogleAuthError, ValueError):
            app.logger.exception("Google desktop credential verification failed")
            return jsonify({"success": False, "message": "Google sign-in could not be verified."}), 401

        if claims.get("nonce") != expected_nonce:
            return jsonify({"success": False, "message": "Google sign-in could not be verified."}), 401

        email = verified_google_email(claims)
        if not email:
            return jsonify({"success": False, "message": "Google email could not be verified."}), 401

        try:
            user = app.config["USER_LOOKUP_BY_EMAIL"](email)
        except (mysql.connector.Error, KeyError, ValueError):
            app.logger.exception("Database lookup failed during Google desktop login")
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
