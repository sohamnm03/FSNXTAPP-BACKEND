"""Fixed AI Agents subprocess entrypoint."""
from __future__ import annotations

import argparse, importlib.util, json, sys
from pathlib import Path
from core.security import ensure_within, redact_text, safe_error, sanitize_data, secret_values


def _redact_artifacts(output_dir, secrets):
    for path in output_dir.rglob("*"):
        if path.is_file() and path.name != "run.log" and not path.is_symlink() and path.suffix.lower() in {".json", ".html", ".txt", ".log", ".md", ".csv"}:
            safe = ensure_within(output_dir, path)
            safe.write_text(redact_text(safe.read_text(encoding="utf-8", errors="replace"), secrets), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    package_root = Path(args.package_dir).resolve()
    manifest = json.loads(ensure_within(package_root, package_root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("id") != "ai_agents" or manifest.get("entrypoint") != "packages.ai_agents.runner:run_package" or not manifest.get("enabled"):
        print("AI Agents package is not registered or enabled", file=sys.stderr); return 2
    output_dir = ensure_within(package_root / "runtime", args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    inputs = json.load(sys.stdin); secrets = secret_values(inputs)
    try:
        runner_path = ensure_within(package_root, package_root / "runner.py")
        spec = importlib.util.spec_from_file_location("registered_ai_agents_runner", runner_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("AI Agents runner could not be loaded")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        result = sanitize_data(module.run_package(args.run_id, inputs, str(output_dir)), secrets)
        _redact_artifacts(output_dir, secrets)
        (output_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0
    except BaseException as error:
        message = safe_error(error, secrets); _redact_artifacts(output_dir, secrets)
        print(f"AI Agents execution failed: {message}", file=sys.stderr, flush=True)
        (output_dir / "error.json").write_text(json.dumps({"status": "failed", "error": message}, indent=2), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
