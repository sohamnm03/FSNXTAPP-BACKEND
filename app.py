"""Minimal Flask backend exposing the login API only."""
from __future__ import annotations

import os
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

from core.auth import create_access_token

load_dotenv()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        AUTH_SECRET=os.environ.get("AUTH_SECRET", "development-only-change-me"),
        AUTH_TOKEN_MAX_AGE=int(os.environ.get("AUTH_TOKEN_MAX_AGE", "3600")),
        AUTH_USERNAME=os.environ.get("AUTH_USERNAME", "admin"),
        AUTH_PASSWORD=os.environ.get("AUTH_PASSWORD", "password123"),
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)
    CORS(app)
    @app.post("/api/login")
    def login():
        data = request.get_json(silent=True) or {}
        username, password = data.get("username", ""), data.get("password", "")
        if not username or not password:
            return jsonify({"success": False, "message": "Please fill in all fields."}), 400
        if username != app.config["AUTH_USERNAME"] or password != app.config["AUTH_PASSWORD"]:
            return jsonify({"success": False, "message": "Invalid username or password."}), 401
        return jsonify({
            "success": True,
            "message": "Login successful.",
            "access_token": create_access_token(username),
            "token_type": "Bearer",
            "expires_in": app.config["AUTH_TOKEN_MAX_AGE"],
        })

    return app


app = create_app()

if __name__ == "__main__":
    app.run(port=5000, debug=True, use_reloader=False)
