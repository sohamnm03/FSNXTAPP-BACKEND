"""Runs the full check suite over the pages the operator selected.

This deliberately does NOT crawl outward. Testing exactly the pages that were
chosen means the whole budget goes into depth per page - more validation cases,
the visual-consistency audit, clickability - which is where real defects show up.
"""
import json
import re

from ai_agents import config
from ai_agents.prompts import load as load_prompt
from ai_agents.services.reporter_service import REPORTER, screenshot
from ai_agents.services.dom_service import get_page_elements
from ai_agents.services.claude_service import safe_ask_json
from ai_agents.services.page_service import check_links, check_responsive, perform_safe_action
from ai_agents.services.form_service import test_form
from ai_agents.services.ui_consistency_service import (
    run_ui_consistency_checks,
    run_ten_ui_checks,
)
from ai_agents.services.focused_business_service import run_focused_business_tests
from ai_agents.services.evidence_service import wait_for_settle
from ai_agents.services.listener_service import attach_listeners

PAGE_ANALYSIS_PROMPT = load_prompt("page_analysis")


def _load_page(page, url):
    """Returns True when the page is loaded and worth testing."""
    try:
        if page.url.split("#")[0] != url:
            page.goto(
                url,
                timeout=config.PAGE_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(400)
    except Exception as e:
        REPORTER.record(
            "execution",
            "Page navigation could not be confirmed",
            url,
            "The page opens normally",
            (
                "The browser could not complete navigation. This may be a network, browser, "
                f"or test-environment problem rather than an application defect: "
                f"{str(e).splitlines()[0][:200]}"
            ),
            "inconclusive",
            severity="critical",
            screenshot=screenshot(page, "load_unconfirmed"),
        )
        return False

    try:
        body_text = page.inner_text("body", timeout=5000).strip()
        if len(body_text) < 5:
            REPORTER.record("crash", "Page opens completely blank", url,
                            "The page shows its content",
                            "The page loaded but nothing was rendered - the user sees an empty "
                            "white screen",
                            "fail", severity="critical",
                            screenshot=screenshot(page, "blank_page"),
                            suggested_fix="A fault is stopping this page from rendering; check the "
                                          "page's own errors on load.")
            return False
        if re.search(r"application error|something went wrong|unexpected error|uncaught",
                     body_text, re.I):
            REPORTER.record("crash", "Page shows an error message instead of content", url,
                            "The page shows its content",
                            f"The page displays an error to the user: \"{body_text[:160]}\"",
                            "fail", severity="critical",
                            screenshot=screenshot(page, "error_page"),
                            suggested_fix="Fix the fault behind this error screen.")
            return False
    except Exception:
        pass
    return True


def _norm(s):
    return " ".join(str(s or "").split()).lower()


def _find_claimed_element(name, elements):
    """Match the control Claude named back to a real measured element."""
    want = _norm(name)
    if not want:
        return None
    for el in elements:
        for key in ("text", "label", "ariaLabel", "title", "placeholder", "name", "id"):
            got = _norm(el.get(key))
            if got and (got == want or (len(want) > 3 and want in got) or
                        (len(got) > 3 and got in want)):
                return el
    return None


def _has_name(el):
    return any(str(el.get(k) or "").strip()
               for k in ("text", "label", "ariaLabel", "title"))


# Each claim type maps to a check against MEASURED element data. A claim we can
# prove is recorded as a defect; a claim we cannot prove is recorded as
# inconclusive; a claim the data actively contradicts is discarded as a
# hallucination. Previously every claim went straight in as a confirmed failure.
def _verify_claim(claim_type, el, elements):
    if el is None:
        return None, "the control named in this observation could not be matched to anything " \
                     "measured on the page"
    if claim_type == "no_accessible_name":
        return (not _has_name(el)), \
            ("this control has no readable name at all - no text, no label, no description"
             if not _has_name(el) else
             f"the control does have a name ('{(el.get('text') or el.get('label') or el.get('ariaLabel') or el.get('title'))}')")
    if claim_type == "label_is_only_a_code":
        text = str(el.get("text") or "").strip()
        only_code = bool(text) and not any(c.isalpha() for c in text)
        return only_code, (f"this control is labelled only '{text}', which gives the user no idea "
                           "what pressing it does" if only_code
                           else f"the control is labelled '{text}', which is descriptive")
    if claim_type == "input_without_label":
        if el.get("tag") not in ("input", "select", "textarea"):
            return False, "this is not an input field"
        missing = not str(el.get("label") or "").strip() and \
            not str(el.get("ariaLabel") or "").strip()
        ph = el.get("placeholder")
        if not missing:
            return False, f"the field is labelled '{el.get('label') or el.get('ariaLabel')}'"
        detail = "this field has no permanent label"
        if ph:
            detail += (f", only the hint text '{ph}' which disappears as soon as the user "
                       "starts typing, so they lose track of what the field was for")
        return True, detail
    if claim_type == "disabled_but_looks_enabled":
        bad = bool(el.get("is_disabled")) and bool(el.get("looks_enabled"))
        return bad, ("this control is switched off but drawn to look usable"
                     if bad else "the control's appearance matches whether it can be used")
    if claim_type == "tiny_click_target":
        w, h = el.get("width") or 0, el.get("height") or 0
        bad = 0 < min(w, h) < 24
        return bad, (f"this control is only {w}x{h} pixels, which is fiddly to hit"
                     if bad else f"the control is {w}x{h} pixels, large enough to click reliably")
    if claim_type == "duplicate_label":
        name = _norm(el.get("text") or el.get("label") or el.get("ariaLabel"))
        n = sum(1 for e in elements
                if _norm(e.get("text") or e.get("label") or e.get("ariaLabel")) == name)
        return n > 1, (f"{n} different controls share the name '{name}'" if n > 1
                       else f"the name '{name}' is used only once on this page")
    return None, ""     # not machine-verifiable


_CLAIM_SEVERITY = {
    "no_accessible_name": "medium",
    "label_is_only_a_code": "medium",
    "input_without_label": "medium",
    "disabled_but_looks_enabled": "medium",
    "tiny_click_target": "low",
    "duplicate_label": "low",
}


def _record_verified_defect(d, url, elements):
    claim_type = d.get("claim_type") or "other"
    where = d.get("element")
    el = _find_claimed_element(where, elements)
    proven, proof = _verify_claim(claim_type, el, elements)

    if proven is False:
        # The measured data contradicts the claim - drop it entirely rather than
        # publishing a defect that isn't there.
        print(f"    [dropped unproven claim] {d.get('title', '')[:70]} - {proof}")
        return
    if proven is True:
        REPORTER.record(
            "usability", d.get("title", "Usability problem"), url,
            "The page is clear and usable for everyone",
            (f"On '{where}': " if where else "") + proof,
            "fail", severity=d.get("severity") or _CLAIM_SEVERITY.get(claim_type, "low"),
            repro_steps=[f"Open {url}"] + ([f"Look at '{where}'"] if where else []),
            suggested_fix=d.get("suggested_fix", ""))
        return
    # Not machine-verifiable: report as an observation to check, never as a
    # confirmed defect.
    REPORTER.record(
        "usability", d.get("title", "Usability observation"), url,
        "The page is clear and usable for everyone",
        (f"On '{where}': " if where else "") + (d.get("detail") or "") +
        " (this was spotted by review of the page's structure and has not been confirmed by "
        "interacting with the page)",
        "inconclusive",
        repro_steps=[f"Open {url}"] + ([f"Look at '{where}'"] if where else []),
        suggested_fix=d.get("suggested_fix", ""))


def _run_single_page(
    page,
    context,
    route,
    route_number,
    total_routes,
    visited_actions,
    tested,
):
    """Run one page deeply while keeping failures isolated to that page."""
    url = route["url"]
    tests_before = len(REPORTER.results)

    tested.add(url)
    REPORTER.pages_tested.add(url)
    print(
        f"\n=== Page {route_number}/{total_routes}: "
        f"{route['name']}  ({route['path']}) ==="
    )

    if not _load_page(page, url):
        return

    REPORTER.snapshot_console_baseline()
    elements = get_page_elements(page)

    if config.ENABLE_LINK_CHECKS:
        print("  Checking links...")
        check_links(page, context, url, elements)

    if config.ENABLE_RESPONSIVE_AUDIT:
        print("  Checking screen sizes...")
        check_responsive(page, url)

    if config.RUN_TEN_UI_CHECKS:
        print("  Running 10 measured UI checks...")
        run_ten_ui_checks(page, url)

    if config.ENABLE_UI_AUDIT:
        print("  Running the extended visual audit...")
        run_ui_consistency_checks(page, url)

    if config.RUN_FOCUSED_BUSINESS_TESTS:
        print("  Running focused dashboard/facility business checks...")
        run_focused_business_tests(page, url)
        # The focused service may open and close a modal. Refresh the measured
        # element list before generic exploratory actions are bound to indexes.
        elements = get_page_elements(page)

    analysis = {}
    if config.ENABLE_CLAUDE_PAGE_ANALYSIS:
        print("  Analysing the page with Claude...")
        summary = {
            "url": url,
            "page_name": route["name"],
            "title": page.title(),
            "elements": [
                {
                    "index": index,
                    **{
                        key: value
                        for key, value in element.items()
                        if key != "xpath"
                    },
                }
                for index, element in enumerate(elements)
            ],
        }
        analysis = safe_ask_json(
            PAGE_ANALYSIS_PROMPT,
            json.dumps(summary, ensure_ascii=False)[:60000],
            max_tokens=3500,
            default={},
        ) or {}
    else:
        print("  Claude page analysis disabled; using deterministic checks only.")

    page_type = analysis.get("page_type")
    if page_type:
        print(f"    -> {page_type}")

    if config.ENABLE_GENERIC_USABILITY_FINDINGS:
        for defect in (analysis.get("possible_defects") or [])[:8]:
            _record_verified_defect(defect, url, elements)

    actions = []
    if config.ENABLE_GENERIC_SAFE_ACTIONS:
        actions = (analysis.get("safe_actions") or [])[:config.MAX_ACTIONS_PER_PAGE]
    if actions:
        print(f"  Trying {len(actions)} safe interaction(s)...")

    bound = []
    for action in actions:
        index = action.get("index")
        if isinstance(index, int) and 0 <= index < len(elements):
            bound.append((dict(elements[index]), action))

    for element_snapshot, action in bound:
        try:
            perform_safe_action(
                page,
                url,
                [element_snapshot],
                {**action, "index": 0},
                visited_actions,
            )
        except Exception as exc:
            print(
                "    [action skipped] "
                f"{action.get('reason', 'Unnamed action')}: "
                f"{str(exc).splitlines()[0][:180]}"
            )
            continue

        # Restore the page after every exploratory action. Filters, tabs and
        # open modals must not contaminate the next action or the focused tests.
        try:
            page.goto(
                url,
                timeout=config.PAGE_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
            wait_for_settle(page, quiet_ms=250, timeout_ms=config.SETTLE_TIMEOUT_MS)
        except Exception:
            break

    if config.RUN_FOCUSED_BUSINESS_TESTS:
        print("  Focused business validation already covered this page; "
              "skipping generic form guessing.")
    else:
        print("  Testing generic form validation on the page...")
        refreshed_elements = get_page_elements(page)
        non_modal = [
            element
            for element in refreshed_elements
            if not element.get("in_modal")
        ]
        test_form(page, url, non_modal, f"the {route['name']} page")

    generated_count = len(REPORTER.results) - tests_before
    print(f"  Generated {generated_count} test result(s) for {route['name']}.")

    if generated_count < config.MIN_TEST_CASES_PER_PAGE:
        REPORTER.record(
            "coverage",
            f"Page coverage below target: {route['name']}",
            url,
            f"At least {config.MIN_TEST_CASES_PER_PAGE} valid tests are generated",
            (
                f"Only {generated_count} defensible tests were generated. "
                "Unsupported or duplicate cases were not invented to reach the target."
            ),
            "inconclusive",
            severity="low",
            suggested_fix=(
                "Add page-specific business rules, role scenarios, test data, "
                "or additional safe workflows for this page."
            ),
        )


def run_page_tests(page, context, routes):
    """Run every selected route in a fresh page and continue after page failures.

    A broken/stale modal or crashed renderer on Facility Creation can no longer
    poison the Dashboard or Facility Approval tests because each route gets an
    isolated Playwright page while retaining the authenticated browser context.
    """
    visited_actions = set()
    tested = set()

    for route_number, route in enumerate(routes, 1):
        url = route["url"]
        if url in tested:
            continue

        REPORTER.update_run_state(
            status="running",
            current_stage="Selected page test suite",
            current_page=url,
            current_case="",
        )
        route_page = None
        try:
            route_page = context.new_page()
            route_page.set_default_timeout(config.STEP_TIMEOUT_MS)
            route_page.set_default_navigation_timeout(config.PAGE_TIMEOUT_MS)
            attach_listeners(route_page)
            _run_single_page(
                page=route_page,
                context=context,
                route=route,
                route_number=route_number,
                total_routes=len(routes),
                visited_actions=visited_actions,
                tested=tested,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            REPORTER.record(
                "execution",
                f"Page test execution could not complete: {route.get('name', url)}",
                url,
                "An error on one page does not stop the remaining pages",
                (
                    "The page runner encountered an automation/infrastructure error: "
                    f"{str(exc).splitlines()[0][:300]}. Later routes will still run."
                ),
                "inconclusive",
                screenshot=(
                    screenshot(route_page, "page_execution_error")
                    if route_page is not None else ""
                ),
            )
            print(
                f"  [page error] {route.get('name', url)}: "
                f"{str(exc).splitlines()[0][:200]}",
                flush=True,
            )
            tested.add(url)
            if not config.CONTINUE_AFTER_PAGE_ERROR:
                raise
        finally:
            REPORTER.checkpoint()
            try:
                if route_page is not None:
                    route_page.close()
            except Exception:
                pass

    REPORTER.update_run_state(current_page="", current_case="")
    return tested

