"""Standard package adapter for the AI Agents project."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from core.security import ensure_within, sanitize_data, secret_values

PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT / "src"


def run_package(run_id: str, inputs: dict, output_dir: str) -> dict:
    """Execute one isolated run and return structured, relative artifact metadata."""
    if str(uuid.UUID(run_id)) != run_id:
        raise ValueError("run_id must be a canonical UUID")
    expected = ensure_within(PACKAGE_ROOT / "runtime", PACKAGE_ROOT / "runtime" / run_id)
    actual = ensure_within(PACKAGE_ROOT / "runtime", output_dir)
    if actual != expected:
        raise ValueError("output_dir does not match the registered run directory")
    actual.mkdir(parents=True, exist_ok=True)
    source = str(SOURCE_ROOT)
    if source not in sys.path:
        sys.path.insert(0, source)

    from ai_agents import main as package_main
    from ai_agents.services.reporter_service import REPORTER

    package_main.main(inputs, output_dir=str(actual), backend_mode=True)
    if REPORTER.run_state.get("status") == "error":
        errors = REPORTER.run_state.get("pipeline_errors", [])
        raise RuntimeError(errors[-1].get("error", "AI Agents pipeline failed") if errors else "AI Agents pipeline failed")
    counts = REPORTER.counts()
    artifacts = [
        path.relative_to(actual).as_posix()
        for path in sorted(actual.rglob("*"))
        if path.is_file() and path.name != "run.log" and not path.is_symlink()
    ]
    return sanitize_data(
        {
            "status": "completed",
            "summary": {
                "pages_tested": len(REPORTER.pages_tested),
                "checks": len(REPORTER.results),
                **counts,
                "pipeline_status": REPORTER.run_state.get("status"),
            },
            "artifacts": artifacts,
        },
        secret_values(inputs),
    )
