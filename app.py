from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
# CORS lets your React app (running on a different address/port) call this server —
# without this, the browser blocks the request for security reasons.
CORS(app)

# Fixed credentials — same as before, but now living only on the server,
# never sent to or visible from the frontend code.
VALID_USERNAME = "admin"
VALID_PASSWORD = "password123"

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    username = data.get("username", "")
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"success": False, "message": "Please fill in all fields."}), 400

    if username == VALID_USERNAME and password == VALID_PASSWORD:
        return jsonify({"success": True, "message": "Login successful."}), 200
    else:
        return jsonify({"success": False, "message": "Invalid username or password."}), 401

if __name__ == "__main__":
    # Runs the server on your own machine at http://localhost:5000
    app.run(port=5000, debug=True)