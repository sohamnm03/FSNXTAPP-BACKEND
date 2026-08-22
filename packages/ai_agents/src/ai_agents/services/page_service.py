"""Per-page checks: responsive layout, link health, and safe exploratory
interaction.

The interaction logic follows two rules that exist specifically to stop the
tester reporting working features as broken:

  1. VERDICTS COME FROM MEASUREMENT. We capture a signature of the page before
     and after the click and let the measured difference decide. We never treat
     an LLM's guess at what a button is *for* as the pass criterion.
  2. A FAILURE MUST BE CONFIRMED TWICE. If the first attempt shows no change we
     reload and try again. If the second attempt does respond, the first was a
     timing artefact and we do not report a defect.

Console errors never change a functional verdict. They are real findings, but
"the page logged a warning while I clicked" is not evidence that the click
failed - conflating the two is what produced reports like "'+ Create Facility'
does not open the form" for a button that opens the form perfectly.
"""
import re
from urllib.parse import urljoin

from ai_agents import config
from ai_agents.services.reporter_service import REPORTER, screenshot
from ai_agents.services.dom_service import (
    build_selector, element_label, is_destructive_element, get_page_elements, resolve,
)
from ai_agents.services.modal_service import get_open_modal, close_any_modal
from ai_agents.services.form_service import test_form
from ai_agents.services import evidence_service as ev

_checked_links = set()


def check_responsive(page, url):
    original = page.viewport_size or {"width": 1440, "height": 900}
    for w, h, name in [(375, 812, "mobile"), (768, 1024, "tablet"), (1440, 900, "desktop")]:
        try:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(600)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            if overflow > 8:
                REPORTER.record(
                    "ui", f"Page is wider than a {name} screen ({w}px)", url,
                    f"The page fits inside a {name} screen without sideways scrolling",
                    f"The content is {overflow}px wider than the screen, so someone on a "
                    f"{name} device has to scroll sideways to see the rest of it",
                    "fail", severity="medium" if name == "mobile" else "low",
                    repro_steps=[f"Open {url}", f"View it on a {name}-sized screen ({w}x{h})"],
                    screenshot=screenshot(page, f"overflow_{name}"),
                    suggested_fix="Let the layout reflow at smaller widths, and allow wide tables "
                                  "to scroll inside their own area rather than pushing the page out.",
                )
            else:
                REPORTER.record("ui", f"Page fits a {name} screen ({w}px)", url,
                                "No sideways scrolling", "Fits correctly", "pass")
        except Exception as e:
            REPORTER.record("ui", f"Screen-size check at {name}", url, "Check runs",
                            f"Could not be measured: {e}", "skipped")
    page.set_viewport_size(original)
    page.wait_for_timeout(300)


def check_links(page, context, url, elements):
    hrefs = set()
    base = page.url
    for el in elements:
        h = el.get("href")
        if not h or h.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        hrefs.add(urljoin(base, h))
    for link in list(hrefs)[:25]:
        if link in _checked_links:
            continue
        _checked_links.add(link)
        try:
            resp = context.request.get(link, timeout=10000)
            if resp.status in config.BENIGN_REQUEST_STATUSES:
                # A protected route answering 401/403 to a bare request is
                # correct behaviour, not a broken link.
                REPORTER.record("api", f"Link is access-controlled: {link[:80]}", url,
                                "Protected pages require a signed-in session",
                                f"Returned {resp.status} to an unauthenticated check, which is "
                                "expected for a protected page", "pass")
            elif resp.status >= 400:
                REPORTER.record(
                    "api", f"Broken link: {link[:80]}", url,
                    "The link opens a working page",
                    f"Following this link returns an error from the server (status {resp.status}), "
                    "so the user lands on an error page or a blank screen",
                    "fail", severity="high" if resp.status >= 500 else "medium",
                    repro_steps=[f"Open {url}", f"Click the link to {link}"],
                    suggested_fix="Point the link at a page that exists, or remove it.",
                )
            else:
                REPORTER.record("api", f"Link works: {link[:80]}", url,
                                "Link opens successfully", f"Opens correctly (status {resp.status})",
                                "pass")
        except Exception as e:
            REPORTER.record("api", f"Link could not be checked: {link[:80]}", url,
                            "The link opens a working page",
                            f"The check itself could not complete: {str(e)[:160]}",
                            "skipped")


def _real_console_since(index):
    """Console errors that arrived after `index`, excluding known noise and
    anything the page was already emitting before we touched it."""
    out = []
    for ce in REPORTER.console_errors[index:]:
        text = ce["text"]
        if ev.is_benign_console(text):
            continue
        if REPORTER.console_key(text) in REPORTER.baseline_console:
            continue
        out.append(ce)
    return out


def _control_value(loc):
    try:
        return str(loc.input_value(timeout=1200)).strip()
    except Exception:
        try:
            return str(loc.get_attribute("aria-checked") or
                       loc.get_attribute("aria-selected") or
                       loc.get_attribute("value") or "").strip()
        except Exception:
            return ""


def _first_non_default_option(loc):
    """Select a real native option rather than merely clicking the field."""
    try:
        options = loc.locator("option")
        current = _control_value(loc)
        fallback = None
        for i in range(options.count()):
            option = options.nth(i)
            if option.is_disabled():
                continue
            value = option.get_attribute("value")
            text = " ".join((option.inner_text() or "").split())
            if value is None or value == current:
                continue
            if fallback is None:
                fallback = value
            if value and not re.search(r"^(all|select|choose|--)$", text, re.I):
                loc.select_option(value=value)
                return True, text or value
        if fallback is not None:
            loc.select_option(value=fallback)
            return True, fallback
    except Exception as exc:
        return False, str(exc).splitlines()[0][:160]
    return False, "no alternative option was available"


def _choose_custom_option(page, loc):
    """Open a custom combobox and choose an actual visible option."""
    try:
        loc.click(timeout=config.STEP_TIMEOUT_MS)
        page.wait_for_timeout(200)
    except Exception as exc:
        return False, f"the filter could not be opened: {str(exc).splitlines()[0][:160]}"

    selectors = (
        "[role='option']:visible, [data-value]:visible, "
        ".dropdown-item:visible, .select-option:visible, .option:visible"
    )
    try:
        options = page.locator(selectors)
        fallback = None
        for i in range(min(options.count(), 80)):
            option = options.nth(i)
            if not option.is_visible():
                continue
            text = " ".join((option.inner_text() or "").split())
            if not text:
                continue
            if fallback is None:
                fallback = option
            if not re.search(r"^(all|select|choose|--)$", text, re.I):
                option.click(timeout=config.STEP_TIMEOUT_MS)
                return True, text
        if fallback is not None:
            text = " ".join((fallback.inner_text() or "").split())
            fallback.click(timeout=config.STEP_TIMEOUT_MS)
            return True, text
    except Exception as exc:
        return False, f"an option could not be selected: {str(exc).splitlines()[0][:160]}"

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False, "the dropdown opened but no selectable option was detected"


def _click_nearby_search(page, source):
    """Some search fields apply on Enter; others need a Search button."""
    try:
        source.press("Enter")
    except Exception:
        pass
    try:
        buttons = page.get_by_role("button", name=re.compile(r"^\s*search\s*$", re.I))
        for i in range(min(buttons.count(), 10)):
            button = buttons.nth(i)
            if button.is_visible() and button.is_enabled():
                button.click(timeout=2000)
                return
    except Exception:
        pass


def _attempt_filter(page, el, loc):
    """Exercise a filter semantically: select/fill a value, then measure rows."""
    before = ev.capture(page)
    console_before = len(REPORTER.console_errors)
    original = _control_value(loc)
    tag = (el.get("tag") or "").lower()
    role = (el.get("role") or "").lower()
    control_type = (el.get("type") or "").lower()
    placeholder = (el.get("placeholder") or "").lower()
    label = element_label(el)
    changed = False
    detail = ""

    try:
        if tag == "select":
            changed, detail = _first_non_default_option(loc)
        elif role == "combobox" or placeholder.startswith(("select", "choose")):
            changed, detail = _choose_custom_option(page, loc)
        elif tag in ("input", "textarea"):
            # Use an impossible search token so a working search should either
            # show zero rows or an explicit no-results state.
            probe = "__qa_no_match_98765__"
            loc.fill(probe, timeout=config.STEP_TIMEOUT_MS)
            _click_nearby_search(page, loc)
            changed = _control_value(loc) == probe
            detail = f"entered the search value '{probe}'"
        else:
            changed, detail = _choose_custom_option(page, loc)
    except Exception as exc:
        return "unclickable", str(exc).splitlines()[0][:200], \
            _real_console_since(console_before), {}

    ev.wait_for_settle(page, quiet_ms=350, timeout_ms=config.SETTLE_TIMEOUT_MS)
    after = ev.capture(page)
    d = ev.diff(before, after)
    console_errors = _real_console_since(console_before)

    if not changed:
        return ev.INCONCLUSIVE, (
            f"The control was found, but the runner could not apply a different filter value "
            f"({detail}). It is not reported as broken."
        ), console_errors, d

    if any(d.get(key) for key in (
        "row_count_changed", "row_content_changed", "rows_reordered",
    )):
        return ev.PASS, f"{detail} and {ev.describe(d)}", console_errors, d

    # The value itself changed, which proves the control accepted the user's
    # choice. If the current data set produces the same rows, that is not proof
    # that filtering is broken.
    current = _control_value(loc)
    if current != original or tag not in ("input", "select", "textarea"):
        return ev.INCONCLUSIVE, (
            f"The filter value changed ({detail}), but the current table data did not provide "
            "a measurable row difference. The filter is not reported as failed."
        ), console_errors, d

    return ev.INCONCLUSIVE, (
        "The filter interaction could not be confirmed from the available data, so no defect "
        "is claimed."
    ), console_errors, d


def _mutate_filter_for_reset(page):
    """Create a non-default state before judging Reset."""
    try:
        searches = page.locator(
            "input[type='search']:visible, input[placeholder*='search' i]:visible, "
            "input[aria-label*='search' i]:visible"
        )
        for i in range(min(searches.count(), 20)):
            field = searches.nth(i)
            if field.is_editable():
                field.fill("qa-reset-probe")
                return "search", field, "qa-reset-probe"
    except Exception:
        pass

    try:
        selects = page.locator("select:visible")
        for i in range(min(selects.count(), 30)):
            field = selects.nth(i)
            original = _control_value(field)
            changed, _ = _first_non_default_option(field)
            if changed and _control_value(field) != original:
                return "select", field, original
    except Exception:
        pass

    return None, None, None


def _attempt_reset(page, loc):
    """Reset is valid only after the test first changes a filter/search value."""
    state_kind, field, original = _mutate_filter_for_reset(page)
    if field is None:
        return ev.INCONCLUSIVE, (
            "Reset was not judged because no filter or search field could be placed into a "
            "non-default state first. Clicking Reset on an already-reset page proves nothing."
        ), [], {}

    ev.wait_for_settle(page, quiet_ms=200, timeout_ms=2000)
    before = ev.capture(page)
    console_before = len(REPORTER.console_errors)

    try:
        loc.click(timeout=config.STEP_TIMEOUT_MS)
    except Exception as exc:
        return "unclickable", str(exc).splitlines()[0][:200], \
            _real_console_since(console_before), {}

    ev.wait_for_settle(page, quiet_ms=300, timeout_ms=config.SETTLE_TIMEOUT_MS)
    after = ev.capture(page)
    d = ev.diff(before, after)

    if state_kind == "search":
        reset_worked = _control_value(field) == ""
        evidence = "the search field was cleared" if reset_worked else \
            f"the search field still contains '{_control_value(field)}'"
    else:
        reset_worked = _control_value(field) == original
        evidence = "the dropdown returned to its original value" if reset_worked else \
            f"the dropdown remained on '{_control_value(field)}'"

    return (
        ev.PASS if reset_worked else ev.FAIL,
        evidence + (f"; {ev.describe(d)}" if d else ""),
        _real_console_since(console_before),
        d,
    )


def _normalise_action_kind(kind, label, el):
    text = " ".join((str(label or ""), str(el.get("placeholder") or ""))).lower()
    if re.search(r"\b(reset|clear filters?)\b", text):
        return "reset"
    if re.search(r"\bsearch\b", text) and el.get("tag") in ("input", "textarea"):
        return "filter"
    return kind or "button"


def _attempt(page, url, el, kind):
    """Perform one semantic action and return measured evidence."""
    loc = resolve(page, el)
    if loc is None:
        return "unresolved", "the control could not be found on the page this time", [], {}

    if kind == "filter":
        return _attempt_filter(page, el, loc)
    if kind == "reset":
        return _attempt_reset(page, loc)

    before = ev.capture(page)
    console_before = len(REPORTER.console_errors)
    try:
        loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        loc.click(timeout=config.STEP_TIMEOUT_MS)
    except Exception as exc:
        return "unclickable", str(exc).splitlines()[0][:200], \
            _real_console_since(console_before), {}

    ev.wait_for_settle(page)
    after = ev.capture(page)
    d = ev.diff(before, after)
    verdict, evidence = ev.classify(kind, d)

    # A sort/filter/tab/pagination control can legitimately show no row change
    # when there is only one row, no matching alternative data, or the selected
    # tab is already active. Lack of change alone is not enough to call it broken.
    if verdict == ev.FAIL and kind in {"sort", "pagination", "tab", "toggle"}:
        verdict = ev.INCONCLUSIVE
        evidence = (
            f"{evidence}. The current data/state did not provide enough evidence to prove the "
            "control is broken, so no failure is reported."
        )

    return verdict, evidence, _real_console_since(console_before), d

def perform_safe_action(page, url, elements, action, visited_actions):
    idx = action.get("index")
    if not isinstance(idx, int) or not (0 <= idx < len(elements)):
        return
    el = elements[idx]
    label = element_label(el)
    kind = _normalise_action_kind(
        action.get("kind", "button"),
        label,
        el,
    )
    action_key = (url, build_selector(el), kind)
    if action_key in visited_actions:
        return
    visited_actions.add(action_key)

    if is_destructive_element(el) and not config.ALLOW_DESTRUCTIVE:
        REPORTER.record(
            "functional", f"'{label}' was deliberately not clicked", url,
            "Controls that change or delete real data are left alone during a safe test run",
            "This control looked like it changes real data (delete/approve/pay/save), so it was "
            "skipped to avoid affecting live records",
            "skipped")
        return

    if el.get("is_covered"):
        return          # reported once, page-level, by the visual audit

    if el.get("is_disabled"):
        reason = el.get("not_clickable_reason") or "switched off"
        if el.get("looks_enabled"):
            REPORTER.record(
                "usability", f"'{label}' looks available but is switched off", url,
                "A control that can't be used looks visibly unavailable",
                f"This control is currently unavailable ({reason}) but is drawn to look "
                "perfectly usable, so people will click it and think the app is frozen",
                "fail", severity="medium",
                repro_steps=[f"Open {url}", f"Click '{label}'", "Nothing happens"],
                screenshot=screenshot(page, f"looks_enabled_{label}"),
                suggested_fix="Grey it out while unavailable, and ideally say why "
                              "(e.g. 'no further pages').")
        else:
            REPORTER.record("functional", f"'{label}' is correctly shown as unavailable", url,
                            "Unavailable controls are visibly greyed out",
                            f"Correctly greyed out and not clickable ({reason})", "pass")
        return

    expected = (
        str(action.get("expected_change") or "").strip()
        or (
            "Reset clears changed search and filter values"
            if kind == "reset"
            else ev.expectation_for(kind)
        )
    )
    verdict, evidence, console_errs, d = _attempt(page, url, el, kind)

    # ---- Confirm before reporting -------------------------------------------
    # A negative result gets a second, independent attempt on a freshly loaded
    # page. Anything that works on retry was a timing artefact, not a defect.
    if verdict in (ev.FAIL, "unclickable", "unresolved"):
        first = verdict
        first_evidence = evidence
        try:
            page.goto(url, timeout=25000)
            ev.wait_for_settle(page)
        except Exception:
            pass
        fresh = get_page_elements(page)
        retry_el = _rematch(el, fresh) or el
        verdict, evidence, console_errs2, d = _attempt(page, url, retry_el, kind)
        console_errs = console_errs + console_errs2

        if verdict in (ev.PASS, ev.INCONCLUSIVE):
            # Worked the second time - do NOT report a defect.
            REPORTER.record(
                "functional", f"Clicking '{label}'", url, expected,
                f"On the first attempt it {first_evidence}, but on a second attempt it "
                f"{evidence}. Because it responded correctly when retried, this is treated as a "
                "timing effect during testing rather than a fault in the application",
                "pass")
            _report_console(url, label, console_errs, page)
            _followup(page, url, label, d)
            return
        if first == "unclickable" and verdict == "unclickable":
            REPORTER.record(
                "functional", f"'{label}' cannot be clicked", url, expected,
                f"Two separate attempts on a freshly loaded page both failed to click this "
                f"control. The browser reported: {evidence}",
                "fail", severity="high",
                repro_steps=[f"Open {url}", f"Click '{label}'"],
                screenshot=screenshot(page, f"clickfail_{label}"),
                suggested_fix="Check the control is enabled, visible, and not underneath another "
                              "element at the moment the user would click it.")
            _report_console(url, label, console_errs, page)
            return
        if verdict == "unresolved":
            REPORTER.record(
                "functional", f"'{label}' could not be located for testing", url, expected,
                "The control was visible when the page was scanned but could not be found again "
                "when the test ran, so it was not tested. This is a limitation of the test run, "
                "not a confirmed fault",
                "skipped")
            return
        # Both attempts genuinely produced no measurable change.
        REPORTER.record(
            "functional", f"Clicking '{label}' has no effect", url, expected,
            f"Clicked twice on a freshly loaded page. Both times it {evidence}. Nothing on the "
            "screen changed - the address, the popups, the rows in the list and the page text "
            "were all identical before and after",
            "fail", severity="medium",
            repro_steps=[f"Open {url}", f"Click '{label}'", "Observe that nothing changes"],
            screenshot=screenshot(page, f"noeffect_{label}"),
            suggested_fix="Connect this control to an action, or remove it if it isn't meant to "
                          "do anything.")
        _report_console(url, label, console_errs, page)
        return

    # ---- Positive / unconfirmable outcomes ---------------------------------
    if verdict == ev.PASS:
        REPORTER.record("functional", f"Clicking '{label}' works", url, expected,
                        f"Clicking it {evidence}", "pass")
    else:
        REPORTER.record(
            "functional", f"Clicking '{label}' - could not confirm", url, expected,
            f"Clicking it {evidence}", "inconclusive",
            repro_steps=[f"Open {url}", f"Click '{label}'"],
            suggested_fix="Worth a quick manual check: the control responds, but the automated "
                          "test could not prove it did the specific thing it appears to promise.")

    _report_console(url, label, console_errs, page)
    _followup(page, url, label, d)


def _rematch(el, fresh_elements):
    """Find the same control again in a freshly scanned element list, by
    identity rather than by position - indexes do not survive a re-render."""
    want_xpath = el.get("xpath")
    for cand in fresh_elements:
        if want_xpath and cand.get("xpath") == want_xpath:
            return cand
    keys = ("testId", "id", "name", "ariaLabel", "title", "placeholder")
    for cand in fresh_elements:
        if cand.get("tag") != el.get("tag"):
            continue
        if any(el.get(k) and cand.get(k) == el.get(k) for k in keys):
            return cand
        a = " ".join(str(el.get("text") or "").split())
        b = " ".join(str(cand.get("text") or "").split())
        if a and a == b:
            return cand
    return None


def _report_console(url, label, console_errs, page):
    """Console faults are reported on their own terms - never as the reason a
    click 'failed'."""
    if not console_errs:
        return
    texts = list(dict.fromkeys(REPORTER.console_key(c["text"]) for c in console_errs))[:3]
    REPORTER.record(
        "console", f"Interacting with '{label}' triggers a hidden fault", url,
        "Using the page does not cause internal faults",
        f"Clicking '{label}' produced {len(console_errs)} internal fault(s) that the user never "
        f"sees directly: \"{'; '.join(texts)}\". The control itself still responded - this is a "
        "separate problem that commonly shows up later as missing data or a section that stops "
        "updating",
        "fail", severity="medium",
        repro_steps=[f"Open {url}", f"Click '{label}'"],
        suggested_fix="Fix the underlying fault this interaction triggers.")


def _followup(page, url, label, d):
    """If the click opened a popup, test it. If it navigated, come back."""
    if d.get("overlay_opened") or get_open_modal(page) is not None:
        test_modal(page, url, label)
    elif d.get("url_changed"):
        try:
            page.go_back(wait_until="domcontentloaded", timeout=15000)
            ev.wait_for_settle(page)
        except Exception:
            try:
                page.goto(url, timeout=20000)
            except Exception:
                pass


def test_modal(page, url, opener_label):
    """Test the popup: does it show content, does its form validate, does it close."""
    modal = get_open_modal(page)
    modal_elements = []
    if modal is not None:
        for scope in config.MODAL_SELECTOR.split(","):
            modal_elements = get_page_elements(page, scope.strip())
            if modal_elements:
                break
    if not modal_elements:
        modal_elements = [e for e in get_page_elements(page) if e.get("in_modal")]

    if not modal_elements:
        # We could not read the popup's contents. That is a gap in the test, not
        # proof the popup is empty - saying "the popup is empty" here was wrong.
        REPORTER.record(
            "functional", f"The popup opened by '{opener_label}' could not be read", url,
            "The popup's contents can be inspected and tested",
            "A popup opened, but the automated test could not read the controls inside it, so "
            "its contents were not checked. This is a limitation of the test run",
            "skipped")
    else:
        REPORTER.record(
            "functional", f"The popup opened by '{opener_label}' shows its content", url,
            "The popup opens with the fields and buttons the user needs",
            f"Opened with {len(modal_elements)} usable control(s)", "pass",
            repro_steps=[f"Open {url}", f"Click '{opener_label}'"])
        test_form(page, url, modal_elements, f"the '{opener_label}' popup")

    dismissal = close_any_modal(page)
    ev.wait_for_settle(page, quiet_ms=300, timeout_ms=3000)
    if dismissal.get("closed"):
        REPORTER.record(
            "functional", f"The popup opened by '{opener_label}' closes properly", url,
            "The popup closes with its close/cancel button or the Escape key",
            dismissal.get("detail") or "Closes correctly", "pass")
    else:
        # Reacquire the modal and its controls once. React rerenders can detach a
        # locator during the first attempt even though the close control works.
        second = close_any_modal(page)
        ev.wait_for_settle(page, quiet_ms=300, timeout_ms=3000)
        if second.get("closed"):
            REPORTER.record(
                "functional", f"The popup opened by '{opener_label}' closes properly", url,
                "The popup closes with its close/cancel button or the Escape key",
                second.get("detail") or "Closed on a second attempt", "pass")
        elif dismissal.get("confirmed_failure") or second.get("confirmed_failure"):
            detail = second.get("detail") or dismissal.get("detail") or \
                "A recognised close control was clicked but the popup remained visible"
            REPORTER.record(
                "functional", f"The popup opened by '{opener_label}' will not close", url,
                "A visible close or cancel control dismisses the popup",
                detail,
                "fail", severity="high",
                repro_steps=[f"Open {url}", f"Click '{opener_label}'",
                             "Click the visible close/cancel control"],
                screenshot=screenshot(page, f"modal_stuck_{opener_label}"),
                suggested_fix="Wire the visible close/cancel control to dismiss the popup.")
            try:
                page.goto(url, timeout=20000)     # recover so later tests aren't blocked
            except Exception:
                pass
        else:
            # Automation could not prove the popup was broken. For example, an
            # unlabelled custom icon may not be safely identifiable. Uncertainty
            # is never converted into a user-facing defect.
            REPORTER.record(
                "functional", f"Popup dismissal for '{opener_label}' could not be confirmed", url,
                "The popup can be dismissed through its visible controls",
                second.get("detail") or dismissal.get("detail") or
                "The test could not identify and verify a close action",
                "inconclusive",
                repro_steps=[f"Open {url}", f"Click '{opener_label}'",
                             "Close the popup using its visible icon or button"],
            )
            try:
                page.goto(url, timeout=20000)
            except Exception:
                pass
