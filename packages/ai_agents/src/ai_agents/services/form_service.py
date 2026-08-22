"""Form validation testing with per-field isolation.

The previous approach filled several fields with junk at once, submitted, and
judged the whole form from one message. That cannot attribute a failure to a
field: one field's error message masks another field's *missing* validation, so
real gaps were reported as passes and working forms were reported as broken.

This version does what a careful QA engineer does by hand:

  1. Establish a VALID baseline for every field (Claude proposes realistic,
     internally consistent values).
  2. For each field worth attacking, fill every OTHER field with its valid
     baseline and set only the target field to one invalid value.
  3. Submit. Any rejection is now attributable to that single field, and any
     acceptance proves that field has no validation.

Safety: the payload always contains at least one deliberately invalid field, so
the application is always expected to reject it. The all-valid baseline is never
submitted on its own, so no real record is created. Beyond the accept/reject
verdict each case also checks the things that actually generate support tickets:
is the message specific, is it attached to the right field, and does the form
throw away what the user already typed.
"""
import json
from datetime import date

from ai_agents import config
from ai_agents.prompts import load as load_prompt
from ai_agents.services.reporter_service import REPORTER, screenshot
from ai_agents.services.claude_service import safe_ask_json, judge
from ai_agents.services.dom_service import (
    build_selector, element_label, describe_field, fill_form_fields, resolve,
    get_toast_or_message_text,
)
from ai_agents.services.modal_service import get_open_modal, close_any_modal
from ai_agents.services import evidence_service as ev

BASELINE_PROMPT = load_prompt("form_baseline")
FIELD_CASES_PROMPT = load_prompt("form_field_cases")

# Messages that technically appear but tell the user nothing actionable.
_USELESS_MESSAGES = {
    "error", "invalid", "invalid input", "failed", "something went wrong",
    "bad request", "required", "validation error", "please try again", "oops",
    "error occurred", "invalid data", "invalid value",
}


def _fillable(elements):
    return [(i, el) for i, el in enumerate(elements)
            if el["tag"] in ("input", "select", "textarea")
            and (el.get("type") or "") not in ("submit", "button", "hidden")
            and not el.get("is_disabled")
            and not el.get("is_readonly")]


def _all_data_fields(elements):
    return [(i, el) for i, el in enumerate(elements)
            if el["tag"] in ("input", "select", "textarea")
            and (el.get("type") or "") not in ("submit", "button", "hidden")]


def find_submit_index(elements):
    """Locate the form's submit-style button from REAL elements.

    Deliberately conservative about wizard/row-level buttons: 'Next' or a
    row-level 'Add' is not the form's submit, and clicking it produces a verdict
    about the wrong thing.
    """
    import re
    strong, weak = None, None
    for i, el in enumerate(elements):
        if el.get("is_disabled") or el.get("is_covered"):
            continue
        blob = " ".join(str(el.get(k) or "") for k in ("text", "ariaLabel", "title")).lower()
        blob = " ".join(blob.split())
        is_btn = (el["tag"] == "button" or el.get("role") == "button"
                  or el.get("type") == "submit")
        if not is_btn or not blob:
            continue
        if re.search(r"\b(submit|save|create|confirm|proceed|review)\b", blob):
            if strong is None:
                strong = i
        elif weak is None and re.search(r"\b(apply|ok|send|done)\b", blob):
            weak = i
    return strong if strong is not None else weak


def _read_values(page, elements, indexes):
    """Snapshot what's currently in the fields.

    Uses input_value() where it works and falls back to the DOM value/text, so a
    custom dropdown or a contenteditable isn't misread as empty - misreading it
    is how a form gets wrongly accused of wiping the user's data.
    """
    values = {}
    for i in indexes:
        el = elements[i]
        got = None
        loc = resolve(page, el)
        if loc is not None:
            try:
                got = loc.input_value(timeout=1500)
            except Exception:
                try:
                    got = loc.evaluate(
                        "e => e.value !== undefined && e.value !== null && e.value !== '' "
                        "? String(e.value) : (e.innerText || e.textContent || '')",
                        timeout=1500)
                except Exception:
                    got = None
        values[i] = None if got is None else str(got).strip()
    return values


def _field_error_info(page):
    """Where did the validation feedback appear, and which field owns it?"""
    try:
        return page.evaluate(r"""
        () => {
          const norm = s => (s || '').replace(/\s+/g, ' ').trim();
          const vis = el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0 &&
                   getComputedStyle(el).visibility !== 'hidden';
          };
          const inlineSel = ".invalid-feedback, .Mui-error, .ant-form-item-explain-error," +
            " .field-error, .error-text, .helper-error, [class*='errorText']," +
            " [class*='error-message'], small.error, span.error, div.error, p.error," +
            " [role='alert']";
          const labelNear = el => {
            // The nearest field control to this message, plus its label text.
            let box = el.closest("[class*='form-item'], [class*='formItem'], " +
              "[class*='field'], [class*='control'], .MuiFormControl-root, div");
            for (let hop = 0; hop < 4 && box; hop++) {
              const f = box.querySelector('input, select, textarea');
              if (f) {
                let lab = '';
                if (f.id) {
                  const l = document.querySelector(`label[for="${f.id}"]`);
                  if (l) lab = norm(l.innerText);
                }
                if (!lab && f.closest('label')) lab = norm(f.closest('label').innerText);
                if (!lab) lab = f.getAttribute('aria-label') || f.getAttribute('placeholder') ||
                                f.getAttribute('name') || '';
                return norm(lab).slice(0, 60);
              }
              box = box.parentElement;
            }
            return '';
          };
          const inline = Array.from(document.querySelectorAll(inlineSel))
            .filter(vis)
            .map(e => ({ message: norm(e.innerText).slice(0, 160), field: labelNear(e) }))
            .filter(x => x.message);
          const marked = Array.from(document.querySelectorAll(
            "[aria-invalid='true'], .is-invalid, .Mui-error, .has-error"))
            .filter(vis)
            .map(e => {
              const f = e.matches('input,select,textarea') ? e :
                        e.querySelector('input,select,textarea');
              if (!f) return null;
              return norm(f.getAttribute('aria-label') || f.getAttribute('placeholder') ||
                          f.getAttribute('name') || '').slice(0, 60);
            }).filter(Boolean);
          return { inline, marked_fields: marked, marked_count: marked.length };
        }
        """)
    except Exception:
        return {"inline": [], "marked_fields": [], "marked_count": 0}


def _script_executed(page):
    try:
        return bool(page.evaluate(
            "() => !!(window.qaXss || document.querySelector('#qa-xss-probe'))"))
    except Exception:
        return False


def _clear_xss_probe(page):
    try:
        page.evaluate("() => { try { delete window.qaXss; } catch(e) { window.qaXss = undefined; } "
                      "document.querySelectorAll('#qa-xss-probe').forEach(n => n.remove()); }")
    except Exception:
        pass


def _mentions(text, label):
    """Does this message plausibly refer to that field?"""
    t = " ".join(str(text or "").split()).lower()
    lab = " ".join(str(label or "").split()).lower()
    if not t or not lab:
        return False
    if lab in t:
        return True
    words = [w for w in lab.replace("*", " ").split() if len(w) > 3]
    return bool(words) and any(w in t for w in words)


def test_form(page, url, elements, form_scope_desc):
    all_fields = _all_data_fields(elements)
    fields = _fillable(elements)
    if len(fields) < 1:
        if all_fields:
            names = [element_label(el) for _, el in all_fields[:6]]
            REPORTER.record(
                "functional",
                f"Detail fields are read-only ({form_scope_desc})",
                url,
                "A record-detail view displays data without allowing accidental edits",
                (
                    f"Found {len(all_fields)} visible data field(s), and none were editable"
                    + (f": {', '.join(names)}" if names else "")
                ),
                "pass",
            )
        return
    submit_idx = find_submit_index(elements)
    if submit_idx is None:
        REPORTER.record(
            "validation", f"Form could not be submit-tested ({form_scope_desc})", url,
            "The form has a save/submit button we can identify",
            "No enabled save/submit button was visible, so this form's validation rules were "
            "not exercised. This is a gap in the test run rather than a confirmed fault",
            "skipped",
            suggested_fix="If the submit control is an icon, give it a clear accessible name.")
        return

    described = [describe_field(el, i) for i, el in fields]
    try:
        page_context = page.inner_text("body", timeout=5000)
        page_context = " ".join(page_context.split())[:10000]
    except Exception:
        page_context = ""

    form_context = {
        "url": url,
        "form": form_scope_desc,
        "current_date": date.today().isoformat(),
        "page_context": page_context,
        "fields": described,
    }

    print(f"    Building a valid baseline for {len(described)} field(s)...")
    plan = safe_ask_json(
        BASELINE_PROMPT,
        json.dumps(form_context, ensure_ascii=False),
        max_tokens=3000,
        default={},
    ) or {}

    baseline = {}
    for item in (plan.get("baseline") or []):
        if isinstance(item, dict) and isinstance(item.get("index"), int):
            baseline[item["index"]] = str(item.get("value", ""))
    rules = plan.get("cross_field_rules") or []

    valid_indexes = [i for i, _ in fields]
    priority = [i for i in (plan.get("priority_fields") or []) if i in valid_indexes]
    for i in valid_indexes:
        if i not in priority:
            priority.append(i)
    targets = priority[:config.FORM_FIELDS_TO_TEST]

    print(f"    Testing {len(targets)} field(s) in isolation...")
    for target in targets:
        _test_one_field(
            page,
            url,
            elements,
            fields,
            target,
            baseline,
            rules,
            submit_idx,
            form_scope_desc,
            described,
            page_context,
        )


def _fallback_field_cases(target_desc):
    """Build cases only from measured field type and native constraints."""
    if not isinstance(target_desc, dict):
        return []

    label = target_desc.get("label") or target_desc.get("name") or "Field"
    kind = str(target_desc.get("kind") or "").lower()
    cases = []

    def add(angle, title, value, expected, severity):
        cases.append({
            "test_id": f"F{len(cases) + 1}",
            "angle": angle,
            "title": title,
            "value": value,
            "expected": expected,
            "severity_if_missing": severity,
        })

    if target_desc.get("required"):
        add(
            "required_empty",
            f"{label} is required",
            "",
            (
                f"Submit is blocked and validation identifies {label} as required. "
                "The form remains open and preserves all other entered values."
            ),
            "high",
        )

    if kind in {"number", "money", "amount", "decimal"}:
        add(
            "out_of_range",
            f"Negative {label} is rejected",
            "-1",
            (
                f"Submit is blocked because {label} cannot be negative. "
                "The form remains open and preserves all entered values."
            ),
            "critical",
        )
        add(
            "wrong_format",
            f"Non-numeric {label} is rejected",
            "12abc",
            (
                f"The {label} field does not accept a non-numeric value. "
                "Submit remains blocked and other entered values are retained."
            ),
            "high",
        )
        add(
            "precision",
            f"Unsupported precision in {label} is handled",
            "100.999",
            (
                f"The {label} field rejects unsupported decimal precision or applies "
                "the documented rounding rule without silently storing an unexpected value."
            ),
            "high",
        )
    elif kind == "email":
        add(
            "wrong_format",
            f"Malformed {label} is rejected",
            "not-an-email",
            (
                f"Submit is blocked and validation identifies {label} as an invalid "
                "email address while preserving the other values."
            ),
            "high",
        )
    elif kind in {"date", "datetime", "datetime-local"}:
        add(
            "wrong_format",
            f"Invalid {label} date is rejected",
            "31/02/2027",
            (
                f"Submit is blocked because {label} is not a valid date. "
                "The form stays open and preserves the remaining values."
            ),
            "high",
        )
    elif kind in {"tel", "phone"}:
        add(
            "wrong_format",
            f"Malformed {label} is rejected",
            "ABC123",
            (
                f"Submit is blocked and validation identifies {label} as invalid. "
                "Other form values remain unchanged."
            ),
            "medium",
        )

    maxlength = target_desc.get("maxlength")
    try:
        maxlength = int(maxlength) if maxlength is not None else 0
    except (TypeError, ValueError):
        maxlength = 0
    if maxlength > 0:
        add(
            "over_maxlength",
            f"{label} maximum length is enforced",
            "X" * (maxlength + 1),
            (
                f"The {label} field prevents or rejects values longer than "
                f"{maxlength} characters and retains all other values."
            ),
            "medium",
        )

    if target_desc.get("pattern") and kind not in {"number", "date", "email"}:
        add(
            "wrong_format",
            f"{label} format rule is enforced",
            "INVALID_FORMAT",
            (
                f"Submit is blocked and validation explains the required format for "
                f"{label}. Other entered values remain intact."
            ),
            "high",
        )

    return cases[:4]


def _test_one_field(page, url, elements, fields, target, baseline, rules,
                    submit_idx, form_scope_desc, described, page_context):
    target_el = elements[target]
    target_label = element_label(target_el)
    target_desc = next((d for d in described if d["index"] == target), None)

    cases = safe_ask_json(
        FIELD_CASES_PROMPT,
        json.dumps(
            {
                "url": url,
                "form": form_scope_desc,
                "current_date": date.today().isoformat(),
                "page_context": page_context,
                "target_field": target_desc,
                "all_fields": described,
                "cross_field_rules": rules,
            },
            ensure_ascii=False,
        ),
        max_tokens=2500,
        default=[],
    )
    if isinstance(cases, dict):
        cases = cases.get("cases") or cases.get("test_cases") or []
    if not isinstance(cases, list):
        cases = []

    existing_angles = {
        str(case.get("angle") or "")
        for case in cases
        if isinstance(case, dict)
    }
    for fallback_case in _fallback_field_cases(target_desc):
        if fallback_case["angle"] not in existing_angles:
            cases.append(fallback_case)
            existing_angles.add(fallback_case["angle"])

    if not cases:
        print(f"      No defensible cases found for '{target_label}'.")
        return

    for tc in cases[:config.FORM_CASES_PER_FIELD]:
        _run_case(page, url, elements, fields, target, target_label, baseline,
                  submit_idx, form_scope_desc, tc)
        close_any_modal(page)


def _run_case(page, url, elements, fields, target, target_label, baseline,
              submit_idx, form_scope_desc, tc):
    case_id = f"{tc.get('test_id', '?')}"
    title = tc.get("title", "(untitled)")
    expected = tc.get("expected", "")
    invalid_value = str(tc.get("value", ""))
    angle = tc.get("angle", "")

    try:
        # Every OTHER field gets its valid baseline; only the target is invalid.
        # This is what makes the verdict attributable to one field.
        fill_plan = []
        for i, _ in fields:
            if i == target:
                fill_plan.append({"index": i, "value": invalid_value})
            elif i in baseline:
                v = baseline[i]
                fill_plan.append({"index": i, "value": "" if v == "__first__" else v})
        fill_log = fill_form_fields(page, elements, fill_plan)

        typed_before = _read_values(page, elements, [i for i, _ in fields])
        url_before = page.url
        sig_before = ev.capture(page)
        _clear_xss_probe(page)

        sub_loc = resolve(page, elements[submit_idx])
        if sub_loc is None:
            return
        # Safe: this payload always contains at least one deliberately invalid
        # field, so the application is expected to reject it.
        sub_loc.click(timeout=config.STEP_TIMEOUT_MS)

        # Wait for the app to actually respond rather than sleeping a fixed
        # amount - a slow server round-trip used to look like silent rejection.
        ev.wait_for_settle(page, quiet_ms=300, timeout_ms=config.SETTLE_TIMEOUT_MS)

        toast = get_toast_or_message_text(page)
        errs = _field_error_info(page)
        typed_after = _read_values(page, elements, [i for i, _ in fields])
        sig_after = ev.capture(page)
        d = ev.diff(sig_before, sig_after)

        # An unchanged SPA URL is not proof that validation blocked the action.
        # Review steps commonly replace modal content without navigation. Treat
        # progression and validation as separate, directly measured signals.
        form_still_open = get_open_modal(page) is not None and not d.get("url_changed")
        progressed = bool(
            d.get("url_changed")
            or d.get("overlay_opened")
            or d.get("overlay_closed")
            or d.get("row_count_changed")
            or d.get("row_content_changed")
        )
        inline_msgs = [x["message"] for x in errs["inline"]]
        attributed = any(_mentions(x["field"], target_label) or
                         _mentions(x["message"], target_label)
                         for x in errs["inline"]) or \
            any(_mentions(f, target_label) for f in errs["marked_fields"]) or \
            _mentions(toast, target_label)

        wiped = [elements[i] for i, before in typed_before.items()
                 if before and not (typed_after.get(i) or "")]

        validation_visible = bool(toast or inline_msgs or errs["marked_count"])
        if progressed:
            blocked_outcome = False
        elif validation_visible:
            blocked_outcome = True
        else:
            # The click completed, but neither progression nor validation was
            # positively observed. This is automation uncertainty, not a fail.
            blocked_outcome = None

        evidence_obj = {
            "field_under_test": target_label,
            "invalid_value_used": invalid_value,
            "other_fields_were_valid": True,
            "submit_was_blocked": blocked_outcome,
            "form_still_visible": form_still_open,
            "review_reached": progressed,
            "url_changed": bool(d.get("url_changed")),
            "message_shown": toast or None,
            "messages_beside_fields": inline_msgs or None,
            "error_points_at_this_field": attributed,
            "fields_highlighted": errs["marked_count"],
            "entered_data_cleared": [element_label(e) for e in wiped] or None,
            "page_changed": ev.describe(d),
        }
        evidence = json.dumps(evidence_obj, ensure_ascii=False)

        verdict = judge(expected, evidence_obj)
        status = verdict.get("status", "inconclusive")
        REPORTER.record(
            "validation", f"{target_label} - {title}", url,
            expected, evidence, status,
            severity=verdict.get("severity") or tc.get("severity_if_missing", "medium"),
            repro_steps=[f"Open {url} ({form_scope_desc})",
                         "Fill every field with valid data"] +
                        [f"Set '{target_label}' to: {invalid_value or '(empty)'}"] +
                        ["Click the save/submit button"],
            screenshot=screenshot(page, f"form_{case_id}") if status == "fail" else "",
            suggested_fix=verdict.get("suggested_fix", ""))

        # ---- Secondary observations, only where they're provable ------------
        if (
            config.REPORT_SECONDARY_VALIDATION_ISSUES
            and form_still_open
            and toast
            and not inline_msgs
        ):
            REPORTER.record(
                "validation", f"'{target_label}' error is not shown beside the field", url,
                "The field that is wrong shows its own message next to it",
                f"The form was correctly rejected, but the only feedback was a general message "
                f"(\"{toast[:110]}\"). On a form this size the user has to work out for "
                "themselves which field it refers to",
                "fail", severity="low",
                repro_steps=[f"Open {url} ({form_scope_desc})",
                             f"Set '{target_label}' to: {invalid_value or '(empty)'}",
                             "Click save"],
                suggested_fix="Show the message under the specific field and highlight that field.")

        if (
            config.REPORT_SECONDARY_VALIDATION_ISSUES
            and form_still_open
            and (toast or inline_msgs)
            and not attributed
        ):
            shown = toast or (inline_msgs[0] if inline_msgs else "")
            REPORTER.record(
                "validation", f"Error message does not identify '{target_label}'", url,
                f"The message names the field that is wrong ('{target_label}')",
                f"Only '{target_label}' was invalid - every other field held valid data - but the "
                f"message shown was \"{shown[:110]}\", which doesn't point at that field. The user "
                "has to guess which of the fields the app is objecting to",
                "fail", severity="medium",
                repro_steps=[f"Open {url} ({form_scope_desc})",
                             "Fill every field validly",
                             f"Set only '{target_label}' to: {invalid_value or '(empty)'}",
                             "Click save"],
                suggested_fix=f"Name the field and the rule, e.g. "
                              f"'{target_label} must be ...'.")

        if config.REPORT_SECONDARY_VALIDATION_ISSUES:
            _check_message_quality(
                toast or (inline_msgs[0] if inline_msgs else ""),
                url,
                form_scope_desc,
                target_label,
                invalid_value,
            )

        if wiped:
            names = ", ".join(element_label(e) for e in wiped[:4])
            REPORTER.record(
                "validation", "The form clears what the user typed when it is rejected", url,
                "A rejected submit keeps everything the user already entered",
                f"After the submit was refused, these fields were emptied: {names}. Anyone "
                "filling in a long form would have to type all of it again",
                "fail", severity="high",
                repro_steps=[f"Open {url} ({form_scope_desc})", "Fill it in",
                             "Submit with one invalid field",
                             "Notice the other fields are now empty"],
                screenshot=screenshot(page, f"data_loss_{case_id}"),
                suggested_fix="Keep the entered values when validation fails; only clear the form "
                              "after a successful save.")

        if angle == "dangerous_input" and _script_executed(page):
            REPORTER.record(
                "security", "Text typed into the form is executed as code", url,
                "Anything a user types is displayed as text, never run as code",
                f"A value containing markup was entered into '{target_label}' and the page ran it "
                "instead of displaying it. This is the pattern behind account-takeover attacks, "
                "where an attacker gets their own code running inside another user's session",
                "fail", severity="critical",
                repro_steps=[f"Open {url} ({form_scope_desc})",
                             f"Enter a script tag into '{target_label}'", "Click save"],
                screenshot=screenshot(page, "xss_echo"),
                suggested_fix="Escape all user-supplied values before displaying them, and never "
                              "insert them as raw HTML.")
            _clear_xss_probe(page)

    except Exception as e:
        REPORTER.record(
            "validation", f"{target_label} - {title} could not be completed", url, expected,
            f"The check itself hit an error, so this field's rule was not verified: {e}",
            "skipped", screenshot=screenshot(page, "form_error"))

