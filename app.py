"""Minimal Flask backend exposing the login API only."""
from __future__ import annotations

import os
from hmac import compare_digest

import mysql.connector
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer

load_dotenv()


def find_user_by_username(username: str) -> dict | None:
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
                SELECT id, username, email, password
                FROM users
                WHERE username = %s
                LIMIT 1
                """,
                (username,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()
    finally:
        connection.close()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        AUTH_SECRET=os.environ.get("AUTH_SECRET", "development-only-change-me"),
        AUTH_TOKEN_MAX_AGE=int(os.environ.get("AUTH_TOKEN_MAX_AGE", "3600")),
        USER_LOOKUP=find_user_by_username,
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
            user = app.config["USER_LOOKUP"](username)
        except (mysql.connector.Error, KeyError, ValueError):
            app.logger.exception("Database lookup failed during login")
            return jsonify({"success": False, "message": "Login service is unavailable."}), 503

        stored_password = user.get("password") if user else None
        password_matches = isinstance(stored_password, str) and compare_digest(
            stored_password, password
        )

        if not password_matches:
            return jsonify({"success": False, "message": "Invalid username or password."}), 401

        serializer = URLSafeTimedSerializer(app.config["AUTH_SECRET"], salt="backend-auth")
        return jsonify(
            {
                "success": True,
                "message": "Login successful.",
                "access_token": serializer.dumps(
                    {"sub": str(user["id"]), "username": user["username"]}
                ),
                "token_type": "Bearer",
                "expires_in": app.config["AUTH_TOKEN_MAX_AGE"],
            }
        )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(port=5000, debug=True, use_reloader=False)
