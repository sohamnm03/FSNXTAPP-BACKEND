"""Rewrites technical findings into business language for the HTML report.

Findings are sent to Claude in batches with the raw evidence, and come back as
{headline, what_happens, why_it_matters, fix, area, business_severity}. If the
call fails we fall back to the deterministic rule-based wording in
report_humanizer, so the report is always readable even with no API access.
"""
import json

from ai_agents import config
from ai_agents.prompts import load as load_prompt
from ai_agents.services.claude_service import safe_ask_json
from ai_agents.services.report_humanizer import humanize, category_meta

BUSINESS_REPORT_PROMPT = load_prompt("business_report")


_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _cap_business_severity(technical, proposed):
    """The wording layer may never make a measured finding more severe."""
    technical = technical if technical in _SEVERITY_RANK else "medium"
    proposed = proposed if proposed in _SEVERITY_RANK else technical
    if _SEVERITY_RANK[proposed] < _SEVERITY_RANK[technical]:
        return technical
    return proposed

_BATCH = 8


def _payload_for(entry):
    """Only the fields that help explain the finding - keep the prompt tight."""
    return {
        "test_id": entry["test_id"],
        "area_hint": category_meta(entry["category"])["label"],
        "technical_title": entry["title"],
        "page": entry["page_url"],
        "expected": entry["expected"],
        "observed": entry["actual"][:1200],
        "technical_severity": entry["severity"],
        "existing_fix_note": entry.get("suggested_fix", "")[:400],
        "steps": entry.get("repro_steps", [])[:6],
    }


def _fallback(entry):
    headline, message = humanize(entry)
    return {
        "test_id": entry["test_id"],
        "area": category_meta(entry["category"])["label"],
        "headline": headline,
        "what_happens": message,
        "why_it_matters": "",
        "fix": entry.get("suggested_fix", ""),
        "business_severity": entry["severity"],
        # These findings are measured facts, not inferences - don't tag them
        # "needs confirming" just because Claude didn't rewrite the wording.
        "confidence": "high",
    }


def translate_findings(entries):
    """Returns {test_id: business_dict} for every entry passed in."""
    if not entries:
        return {}
    if not config.BUSINESS_LANGUAGE:
        return {e["test_id"]: _fallback(e) for e in entries}

    out = {}
    total = (len(entries) + _BATCH - 1) // _BATCH
    for n, start in enumerate(range(0, len(entries), _BATCH), 1):
        batch = entries[start:start + _BATCH]
        print(f"  Writing plain-language summaries ({n}/{total})...")
        result = safe_ask_json(
            BUSINESS_REPORT_PROMPT,
            json.dumps([_payload_for(e) for e in batch], ensure_ascii=False),
            max_tokens=4000,
            default=None,
        )
        if isinstance(result, dict):
            result = result.get("findings") or result.get("results") or []
        by_id = {}
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict) and item.get("test_id"):
                    by_id[item["test_id"]] = item
        for i, e in enumerate(batch):
            got = by_id.get(e["test_id"])
            # Positional fallback: the model kept order but dropped/renamed the id.
            if not got and isinstance(result, list) and i < len(result) \
                    and isinstance(result[i], dict) and result[i].get("headline"):
                got = result[i]
            if got and got.get("headline"):
                merged = _fallback(e)
                merged.update({k: v for k, v in got.items() if v})
                merged["test_id"] = e["test_id"]
                merged["business_severity"] = _cap_business_severity(
                    e.get("severity"),
                    merged.get("business_severity"),
                )
                out[e["test_id"]] = merged
            else:
                out[e["test_id"]] = _fallback(e)
    return out
