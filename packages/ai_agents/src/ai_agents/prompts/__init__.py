"""Loader for the Claude prompt templates (kept as .md files, not inline strings)."""
from pathlib import Path

_DIR = Path(__file__).parent


def load(name):
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()

