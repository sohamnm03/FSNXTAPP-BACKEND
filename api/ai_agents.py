"""Authenticated AI Agents execution APIs."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote
from flask import Blueprint, current_app, jsonify, send_file

from core.auth import authenticated_user
from core.security import ensure_within

ai_agents_api = Blueprint("ai_agents_api", __name__)


def _user_or_error():
    user_id = authenticated_user()
    return (user_id, None) if user_id else (None, (jsonify({"error": "A valid Bearer token is required."}), 401))


def _run_or_error(user_id: str, run_id: str):
    run = current_app.extensions["run_manager"].get(run_id, user_id)
    return (run, None) if run else (None, (jsonify({"error": "Run not found."}), 404))


@ai_agents_api.post("/api/ai-agents/runs")
def create_run():
    user_id, error = _user_or_error()
    if error:
        return error
    from flask import request
    try:
        run_id = current_app.extensions["run_manager"].create(user_id, (request.get_json(silent=True) or {}).get("inputs"))
    except ValueError as validation_error:
        return jsonify({"error": str(validation_error)}), 400
    return jsonify({"run_id": run_id, "status": "queued"}), 202


@ai_agents_api.get("/api/ai-agents/runs/<run_id>")
def get_run(run_id: str):
    user_id, error = _user_or_error()
    if error:
        return error
    run, error = _run_or_error(user_id, run_id)
    if error:
        return error
    return jsonify({key: run.get(key) for key in (
        "id", "status", "created_at", "started_at", "finished_at", "exit_status", "result", "error"
    )})


@ai_agents_api.get("/api/ai-agents/runs/<run_id>/logs")
def get_logs(run_id: str):
    user_id, error = _user_or_error()
    if error:
        return error
    run, error = _run_or_error(user_id, run_id)
    if error:
        return error
    path = ensure_within(run["output_directory"], run["log_path"])
    logs = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    return jsonify({"run_id": run_id, "logs": logs})


@ai_agents_api.post("/api/ai-agents/runs/<run_id>/stop")
def stop_run(run_id: str):
    user_id, error = _user_or_error()
    if error:
        return error
    run, error = _run_or_error(user_id, run_id)
    if error:
        return error
    if run["status"] not in {"queued", "running"}:
        return jsonify({"error": "Run is already in a terminal state."}), 409
    if not current_app.extensions["run_manager"].stop(run_id, user_id):
        return jsonify({"error": "Run worker is not available."}), 409
    return jsonify({"run_id": run_id, "status": "stopping"}), 202


@ai_agents_api.get("/api/ai-agents/runs/<run_id>/artifacts")
def list_artifacts(run_id: str):
    user_id, error = _user_or_error()
    if error:
        return error
    run, error = _run_or_error(user_id, run_id)
    if error:
        return error
    root = Path(run["output_directory"]).resolve()
    artifacts = []
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.name == "run.log":
                continue
            safe = ensure_within(root, path)
            relative = safe.relative_to(root).as_posix()
            artifacts.append({
                "path": relative,
                "size_bytes": safe.stat().st_size,
                "download_url": f"/api/ai-agents/runs/{run_id}/artifacts/{quote(relative)}",
            })
    return jsonify({"run_id": run_id, "artifacts": artifacts})


@ai_agents_api.get("/api/ai-agents/runs/<run_id>/artifacts/<path:artifact_path>")
def download_artifact(run_id: str, artifact_path: str):
    user_id, error = _user_or_error()
    if error:
        return error
    run, error = _run_or_error(user_id, run_id)
    if error:
        return error
    root = Path(run["output_directory"]).resolve()
    try:
        path = ensure_within(root, root / artifact_path)
    except ValueError:
        return jsonify({"error": "Invalid artifact path."}), 400
    if not path.is_file() or path.is_symlink() or path.name == "run.log":
        return jsonify({"error": "Artifact not found."}), 404
    return send_file(path, as_attachment=True, download_name=path.name)
