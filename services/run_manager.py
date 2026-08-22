"""In-memory AI Agents job manager for the initial integration."""
from __future__ import annotations

import json, os, subprocess, sys, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from core.security import ensure_within, redact_text, safe_error, sanitize_data, secret_values


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunManager:
    def __init__(self, app):
        self.app = app
        self.package_root = Path(app.config["AI_AGENTS_PACKAGE_PATH"]).resolve()
        self.manifest = self._load_manifest()
        self._runs, self._processes, self._stop_requested = {}, {}, set()
        self._lock = threading.RLock()

    def _load_manifest(self):
        path = ensure_within(self.package_root, self.package_root / "manifest.json")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("id") != "ai_agents" or manifest.get("entrypoint") != "packages.ai_agents.runner:run_package":
            raise RuntimeError("The AI Agents manifest has an invalid identity or entrypoint.")
        if not manifest.get("enabled"):
            raise RuntimeError("The AI Agents package is disabled.")
        return manifest

    def _validate_inputs(self, inputs):
        if not isinstance(inputs, dict):
            raise ValueError("inputs must be a JSON object")
        required = {item["name"]: item for item in self.manifest["required_inputs"]}
        optional = {item["name"]: item for item in self.manifest["optional_inputs"]}
        unknown = set(inputs) - required.keys() - optional.keys()
        if unknown:
            raise ValueError(f"Unknown input(s): {', '.join(sorted(unknown))}")
        missing = [name for name in required if inputs.get(name) in (None, "", [])]
        if missing:
            raise ValueError(f"Missing required input(s): {', '.join(missing)}")
        validated = dict(inputs)
        for name, spec in {**required, **optional}.items():
            if name not in validated:
                continue
            value, expected = validated[name], spec.get("type", "string")
            if expected == "string" and not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            if expected == "boolean" and not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")
            if expected == "array" and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
                raise ValueError(f"{name} must be an array of strings")
            if spec.get("format") == "url" and isinstance(value, str):
                parsed = urlparse(value)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                    raise ValueError(f"{name} must be an HTTP(S) URL without embedded credentials")
        for route in validated.get("routes", []):
            if not route.startswith("/") or ".." in route or "\\" in route or "\x00" in route:
                raise ValueError("routes must be absolute URL paths without traversal")
        return validated

    def create(self, user_id, inputs):
        inputs = self._validate_inputs(inputs)
        run_id = str(uuid.uuid4())
        output_dir = ensure_within(self.package_root / "runtime", self.package_root / "runtime" / run_id)
        output_dir.mkdir(parents=True, exist_ok=False)
        with self._lock:
            self._runs[run_id] = {
                "id": run_id, "user_id": user_id, "status": "queued", "created_at": utcnow(),
                "started_at": None, "finished_at": None, "exit_status": None,
                "output_directory": str(output_dir), "log_path": str(output_dir / "run.log"),
                "result": None, "error": None,
            }
        threading.Thread(target=self._execute, args=(run_id, inputs), daemon=True, name=f"ai-agents-{run_id}").start()
        return run_id

    def get(self, run_id, user_id):
        with self._lock:
            run = self._runs.get(run_id)
            return dict(run) if run and run["user_id"] == user_id else None

    def _update(self, run_id, **values):
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id].update(values)

    @staticmethod
    def _worker_environment():
        allowed = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
        environment = {key: os.environ[key] for key in allowed if key in os.environ}
        environment.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        return environment

    def _execute(self, run_id, inputs):
        secrets = secret_values(inputs)
        output_dir, log_path = self.package_root / "runtime" / run_id, self.package_root / "runtime" / run_id / "run.log"
        command = [sys.executable, "-m", "core.worker", "--package-dir", str(self.package_root), "--run-id", run_id, "--output-dir", str(output_dir)]
        process = None
        try:
            process = subprocess.Popen(
                command, cwd=str(Path(__file__).resolve().parents[1]), env=self._worker_environment(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", shell=False, start_new_session=True,
            )
            with self._lock:
                self._processes[run_id] = process
            self._update(run_id, status="running", started_at=utcnow())
            process.stdin.write(json.dumps(inputs)); process.stdin.close()

            def capture():
                with log_path.open("w", encoding="utf-8") as log:
                    for line in process.stdout:
                        log.write(redact_text(line, secrets)); log.flush()

            reader = threading.Thread(target=capture, daemon=True); reader.start()
            deadline = time.monotonic() + min(int(self.manifest["timeout_seconds"]), int(self.app.config["RUN_TIMEOUT_SECONDS"]))
            terminal_status = None
            while process.poll() is None:
                with self._lock:
                    stopping = run_id in self._stop_requested
                if stopping or time.monotonic() >= deadline:
                    process.terminate(); terminal_status = "stopped" if stopping else "timed_out"; break
                time.sleep(0.05)
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); exit_code = process.wait(timeout=5)
            reader.join(timeout=5)
            if terminal_status:
                self._update(run_id, status=terminal_status, finished_at=utcnow(), exit_status=exit_code,
                             error="Run stopped by user." if terminal_status == "stopped" else "Run exceeded its configured timeout.")
                return
            result_path = output_dir / "result.json"
            if exit_code == 0 and result_path.is_file():
                result = sanitize_data(json.loads(result_path.read_text(encoding="utf-8")), secrets)
                self._update(run_id, status="completed", finished_at=utcnow(), exit_status=exit_code, result=result)
            else:
                message, error_path = "AI Agents worker exited unsuccessfully.", output_dir / "error.json"
                if error_path.is_file():
                    message = json.loads(error_path.read_text(encoding="utf-8")).get("error", message)
                self._update(run_id, status="failed", finished_at=utcnow(), exit_status=exit_code, error=safe_error(message, secrets))
        except BaseException as error:
            self._update(run_id, status="failed", finished_at=utcnow(), error=safe_error(error, secrets))
        finally:
            with self._lock:
                self._processes.pop(run_id, None); self._stop_requested.discard(run_id)

    def stop(self, run_id, user_id):
        with self._lock:
            run, process = self._runs.get(run_id), self._processes.get(run_id)
            if not run or run["user_id"] != user_id or not process or process.poll() is not None:
                return False
            self._stop_requested.add(run_id)
            return True
