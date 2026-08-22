"""Path containment, error sanitization, and secret redaction."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SECRET_KEY_RE = re.compile(r"password|passwd|secret|token|api[_-]?key|credential|role_test_users|extra_role_users", re.I)
INLINE_SECRET_RE = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)(\s*[:=]\s*)([^\s,;]+)")
QUOTED_PATH_RE = re.compile(r"(?<=[\"'])(?:[A-Za-z]:\\[^\"'\r\n]+|/(?:home|Users|var|tmp|opt|srv|app)/[^\"'\r\n]+)")


def ensure_within(base: str | Path, candidate: str | Path) -> Path:
    root, resolved = Path(base).resolve(), Path(candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Path escapes its allowed directory")
    return resolved


def secret_values(payload: Any, key: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for child_key, value in payload.items():
            found.update(secret_values(value, str(child_key)))
    elif isinstance(payload, list):
        for value in payload:
            found.update(secret_values(value, key))
    elif SECRET_KEY_RE.search(key) and payload not in (None, "") and len(str(payload)) >= 3:
        value = str(payload)
        found.add(value)
        if "role" in key.lower():
            for record in value.split(","):
                parts = record.split(":", 2)
                if len(parts) >= 2 and len(parts[-1]) >= 3:
                    found.add(parts[-1])
    return found


def redact_text(text: str, secrets: set[str] | None = None) -> str:
    safe = str(text)
    for value in sorted(secrets or (), key=len, reverse=True):
        safe = safe.replace(value, "[REDACTED]")
    safe = INLINE_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", safe)
    return QUOTED_PATH_RE.sub("[PATH]", safe)


def sanitize_data(value: Any, secrets: set[str] | None = None) -> Any:
    if isinstance(value, dict):
        return {str(key): ("[REDACTED]" if SECRET_KEY_RE.search(str(key)) else sanitize_data(child, secrets)) for key, child in value.items()}
    if isinstance(value, list):
        return [sanitize_data(item, secrets) for item in value]
    return redact_text(value, secrets) if isinstance(value, str) else value


def safe_error(error: BaseException | str, secrets: set[str] | None = None) -> str:
    return redact_text(str(error).splitlines()[0][:500], secrets)
