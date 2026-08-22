"""Claude API access - structured JSON completions used by the other services."""
import json
import os
import re
import time

from ai_agents import config
from ai_agents.prompts import load as load_prompt

_client = None


def get_client():
    global _client
    if _client is None:
        api_key = config.ANTHROPIC_API_KEY
        if not api_key:
            raise RuntimeError("Set ANTHROPIC_API_KEY as an environment variable first.")
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "Install the anthropic package before enabling Claude-backed checks."
            ) from exc
        _client = anthropic.Anthropic(
            api_key=api_key,
            timeout=config.CLAUDE_TIMEOUT_SECONDS,
            max_retries=0,
        )
    return _client


def ask_json(system_prompt, user_prompt, max_tokens=1500):
    client = get_client()
    strict_system = system_prompt + "\n\nRespond with ONLY valid JSON. No preamble, no markdown fences."
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=strict_system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = "\n".join(b.text for b in response.content if b.type == "text")
    cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude did not return valid JSON: {e}\nRaw:\n{raw}")


def safe_ask_json(system_prompt, user_prompt, max_tokens=1500, default=None):
    """Retry transient Claude/API failures without stopping the test run."""
    last_error = None

    for attempt in range(1, config.CLAUDE_MAX_RETRIES + 1):
        try:
            return ask_json(system_prompt, user_prompt, max_tokens=max_tokens)
        except Exception as exc:
            last_error = exc

        if attempt < config.CLAUDE_MAX_RETRIES:
            delay = config.CLAUDE_RETRY_DELAY_SECONDS * attempt
            print(
                f"    [claude] attempt {attempt}/{config.CLAUDE_MAX_RETRIES} "
                f"failed; retrying in {delay:.1f}s"
            )
            time.sleep(delay)

    print(
        "    [claude] call failed after retries: "
        f"{str(last_error).splitlines()[0][:240]}"
    )
    return default


JUDGE_SYSTEM_PROMPT = load_prompt("judge_system")


def judge(expected, evidence):
    """Grade a validation case from deterministic evidence first.

    Whether invalid data progressed is a boolean business outcome and should not
    depend on an LLM interpretation. Claude is used only when the measured state
    is genuinely ambiguous. This is the main guard against a blocked form being
    rewritten as a failure merely because its validation message was generic.
    """
    evidence_obj = evidence
    if isinstance(evidence, str):
        try:
            evidence_obj = json.loads(evidence)
        except Exception:
            evidence_obj = None

    if isinstance(evidence_obj, dict):
        wiped = bool(evidence_obj.get("entered_data_cleared"))
        blocked = evidence_obj.get("submit_was_blocked")
        if blocked is None:
            blocked = evidence_obj.get("form_still_open")
        url_changed = bool(evidence_obj.get("url_changed"))
        progressed = bool(
            evidence_obj.get("review_reached")
            or evidence_obj.get("success_state")
            or evidence_obj.get("record_created")
        )

        if wiped:
            return {
                "status": "fail",
                "severity": "high",
                "reasoning": "The invalid submission was rejected, but other valid values were erased.",
                "user_impact": "The user must re-enter work after correcting one field.",
                "suggested_fix": "Preserve all entered values when validation blocks submission.",
            }
        if blocked is True and not url_changed and not progressed:
            return {
                "status": "pass",
                "severity": "info",
                "reasoning": "The invalid value was prevented from progressing to the next state.",
                "user_impact": "",
                "suggested_fix": "",
            }
        if progressed or (url_changed and blocked is not True):
            return {
                "status": "fail",
                "severity": "high",
                "reasoning": "The measured workflow progressed despite the invalid value.",
                "user_impact": "Invalid data can move into the next business state.",
                "suggested_fix": "Block progression until the invalid field is corrected.",
            }

    # In deterministic focused mode, ambiguity must remain explicit. Do not
    # turn an uncertain browser state into an LLM-authored pass/fail verdict or
    # pause the pipeline waiting for an external API.
    if not config.ENABLE_CLAUDE_PAGE_ANALYSIS:
        return {
            "status": "inconclusive",
            "severity": "info",
            "reasoning": "The measured browser evidence did not prove either progression or validation blocking.",
            "user_impact": "",
            "suggested_fix": "",
        }

    serialised = evidence if isinstance(evidence, str) else json.dumps(
        evidence, ensure_ascii=False
    )
    return safe_ask_json(
        JUDGE_SYSTEM_PROMPT,
        f"Expected outcome: {expected}\n\nActual evidence observed: {serialised}",
        max_tokens=250,
        default={"status": "inconclusive", "severity": "info",
                 "reasoning": "The automated judge could not confirm the outcome",
                 "suggested_fix": ""},
    )
