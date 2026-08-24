"""Login, authentication/session tests, and role capability validation."""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from ai_agents import config
from ai_agents.services.reporter_service import REPORTER, screenshot
from ai_agents.services.dom_service import get_page_elements, get_toast_or_message_text
from ai_agents.services.listener_service import attach_listeners


ROLE_CAPABILITIES = {
    "FOURTHSIGNAL": {"create_facility": True, "approve_facility": False},
    "MAKER": {"create_facility": True, "approve_facility": False},
    "CHECKER": {"create_facility": False, "approve_facility": True},
    "CHECK": {"create_facility": False, "approve_facility": True},
    # Backward-compatible assumption from the previous project rules.
    "ADMIN": {"create_facility": True, "approve_facility": False},
}

_DENIED_RE = re.compile(
    r"access denied|not authorized|not authorised|permission denied|forbidden|"
    r"you do not have permission|unauthorized",
    re.I,
)
_CREATE_RE = re.compile(r"\bcreate\s+facility\b|\bnew\s+facility\b|\badd\s+facility\b", re.I)
_APPROVE_RE = re.compile(r"^\s*approve\s*$|\bapprove\s+facility\b", re.I)
_PENDING_RE = re.compile(r"\bpending\b", re.I)
_EMPTY_RE = re.compile(r"no\s+(pending\s+)?facilit|no\s+records|nothing\s+to\s+approve|empty", re.I)

_USERNAME_SELECTORS = (
    "input[autocomplete='username']",
    "input[autocomplete='email']",
    "input[type='email']",
    "input[name*='email' i]",
    "input[id*='email' i]",
    "input[name*='user' i]",
    "input[id*='user' i]",
    "input[name*='login' i]",
    "input[id*='login' i]",
    "input[type='text']",
    "input:not([type])",
)
_PASSWORD_SELECTORS = (
    "input[autocomplete='current-password']",
    "input[type='password']",
    "input[name*='password' i]",
    "input[id*='password' i]",
    "input[name*='pass' i]",
    "input[id*='pass' i]",
)
_SUBMIT_SELECTORS = (
    "button:has-text('Log in')",
    "button:has-text('Login')",
    "button:has-text('Sign in')",
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "[role='button']:has-text('Log in')",
    "[role='button']:has-text('Sign in')",
    "button[type='submit']",
    "input[type='submit']",
)
_ERROR_SELECTOR = (
    "[role='alert'], [aria-live='assertive'], .error, .alert-danger, "
    "[class*='error' i], [data-testid*='error' i]"
)
_MFA_SELECTORS = (
    "input[autocomplete='one-time-code']",
    "input[name*='otp' i]",
    "input[id*='otp' i]",
    "input[name*='verification' i]",
    "input[id*='verification' i]",
)


def _first_usable(page, selectors):
    """Return the first visible, enabled locator from an ordered selector list."""
    for selector in selectors:
        try:
            matches = page.locator(selector)
            for index in range(min(matches.count(), 20)):
                candidate = matches.nth(index)
                if candidate.is_visible() and candidate.is_enabled():
                    return candidate
        except Exception:
            continue
    return None


def _wait_for_usable(page, selectors, description):
    deadline = time.monotonic() + (config.PAGE_TIMEOUT_MS / 1000)
    while time.monotonic() < deadline:
        candidate = _first_usable(page, selectors)
        if candidate is not None:
            return candidate
        page.wait_for_timeout(100)
    raise RuntimeError(f"No visible {description} field was found")


def _click_login_action(page):
    action = _first_usable(page, _SUBMIT_SELECTORS)
    if action is None:
        raise RuntimeError("No visible login, sign-in, continue, or submit button was found")
    action.click(timeout=config.STEP_TIMEOUT_MS)


def _visible_login_error(page) -> str:
    try:
        messages = page.locator(_ERROR_SELECTOR)
        for index in range(min(messages.count(), 20)):
            item = messages.nth(index)
            if item.is_visible():
                text = " ".join((item.inner_text() or "").split())
                if text:
                    return text[:240]
    except Exception:
        pass
    return ""


def _wait_for_login_result(page) -> None:
    deadline = time.monotonic() + (config.PAGE_TIMEOUT_MS / 1000)
    password_absent_since = None
    while time.monotonic() < deadline:
        password_field = _first_usable(page, _PASSWORD_SELECTORS)
        error = _visible_login_error(page)
        current_url = page.url
        if error:
            raise RuntimeError(f"The website rejected the login: {error}")
        if _first_usable(page, _MFA_SELECTORS) is not None:
            raise RuntimeError(
                "The credentials were accepted, but multi-factor verification is required; login cannot be confirmed automatically"
            )
        if password_field is None:
            password_absent_since = password_absent_since or time.monotonic()
            if time.monotonic() - password_absent_since >= 0.75:
                print(f"Login successful. Redirected to: {current_url}", flush=True)
                return
        else:
            password_absent_since = None
        page.wait_for_timeout(250)
    raise RuntimeError(
        "Login could not be confirmed: the website kept the credential form visible"
    )


def normalise_role(role: str) -> str:
    value = str(role or "").strip().upper().replace(" ", "")
    if value == "CHECK":
        return "CHECKER"
    if value in {"FOURTHSIGNALUSER", "FOURTHSIGNAL"}:
        return "FOURTHSIGNAL"
    return value


def role_capabilities(role: str) -> dict[str, bool]:
    canonical = normalise_role(role)
    return dict(ROLE_CAPABILITIES.get(canonical, {
        "create_facility": False,
        "approve_facility": False,
    }))


def parse_role_test_users(raw: str | None = None) -> list[tuple[str, str, str]]:
    """Parse ROLE:user:password entries without exposing credentials in output."""
    raw = config.ROLE_TEST_USERS if raw is None else raw
    parsed: list[tuple[str, str, str]] = []
    for item in str(raw or "").split(","):
        parts = item.strip().split(":", 2)
        if len(parts) != 3:
            continue
        role, user, password = (part.strip() for part in parts)
        role = normalise_role(role)
        if role and user and password:
            parsed.append((role, user, password))
    return parsed


def login(page, user=None, password=None):
    user = user or config.LOGIN_USER_ID
    password = password or config.LOGIN_PASSWORD
    print(f"Navigating to {config.URL}", flush=True)
    page.goto(config.URL, timeout=config.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    username_field = _wait_for_usable(page, _USERNAME_SELECTORS, "username or email")
    username_field.fill(user)

    password_field = _first_usable(page, _PASSWORD_SELECTORS)
    if password_field is None:
        # Support common two-step forms that ask for the user ID before showing
        # the password field.
        _click_login_action(page)
        password_field = _wait_for_usable(page, _PASSWORD_SELECTORS, "password")

    password_field.fill(password)
    _click_login_action(page)
    _wait_for_login_result(page)


def _new_test_page(browser):
    context = browser.new_context(ignore_https_errors=config.IGNORE_HTTPS_ERRORS)
    page = context.new_page()
    page.set_default_timeout(config.STEP_TIMEOUT_MS)
    page.set_default_navigation_timeout(config.PAGE_TIMEOUT_MS)
    attach_listeners(page)
    return context, page


def _origin() -> str:
    parsed = urlparse(config.URL)
    return f"{parsed.scheme}://{parsed.netloc}"


def _route_url(path: str) -> str:
    return _origin() + "/" + str(path or "").lstrip("/")


def _body_text(page) -> str:
    try:
        return page.inner_text("body", timeout=config.STEP_TIMEOUT_MS)
    except Exception:
        return ""


def _visible_button(page, pattern: re.Pattern) -> bool:
    try:
        buttons = page.locator("button, [role='button']")
        for index in range(min(buttons.count(), 120)):
            item = buttons.nth(index)
            if not item.is_visible():
                continue
            text = " ".join((item.inner_text() or "").split())
            aria = item.get_attribute("aria-label") or ""
            title = item.get_attribute("title") or ""
            if pattern.search(" ".join((text, aria, title))):
                return True
    except Exception:
        pass
    return False


def _page_access(page, expected_path: str) -> tuple[bool, str]:
    body = _body_text(page)
    current_path = (urlparse(page.url).path or "/").rstrip("/") or "/"
    wanted = ("/" + expected_path.lstrip("/")).rstrip("/") or "/"
    on_login = "/login" in current_path.lower()
    denied = bool(_DENIED_RE.search(body))
    accessible = not on_login and not denied and current_path == wanted
    detail = (
        f"current path: {current_path}; expected path: {wanted}; "
        f"login page: {on_login}; access-denied message: {denied}"
    )
    return accessible, detail


def _approval_data_state(page) -> tuple[bool, bool, str]:
    """Return pending evidence, explicit empty-state evidence and a description."""
    body = _body_text(page)
    pending = bool(_PENDING_RE.search(body))
    empty = bool(_EMPTY_RE.search(body))
    try:
        rows = page.locator("table tbody tr")
        visible_rows = sum(
            1 for index in range(min(rows.count(), 100)) if rows.nth(index).is_visible()
        )
    except Exception:
        visible_rows = 0
    # A Pending tab/heading is not evidence that a pending record exists.
    # Only visible worklist rows count as actionable data.
    has_records = visible_rows > 0
    return has_records, empty, f"visible table rows: {visible_rows}; pending text: {pending}; empty state: {empty}"


def run_auth_tests(browser, protected_url):
    print("\n=== Authentication & session tests ===", flush=True)

    context, page = _new_test_page(browser)
    try:
        page.goto(protected_url, timeout=config.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        redirected = "/login" in page.url or page.url.rstrip("/") != protected_url.rstrip("/")
        REPORTER.record(
            "auth",
            "Protected page requires login",
            protected_url,
            "An unauthenticated visit is redirected to login or denied",
            f"Landed on {page.url}",
            "pass" if redirected else "fail",
            severity="high",
            repro_steps=["Open a fresh browser with no session", f"Go directly to {protected_url}"],
            screenshot="" if redirected else screenshot(page, "auth_no_redirect"),
            suggested_fix=(
                "Add a router and backend authorization guard for protected routes."
                if not redirected else ""
            ),
        )
    except Exception as exc:
        REPORTER.record(
            "auth",
            "Protected page requires login",
            protected_url,
            "The unauthenticated access check completes",
            f"Automation could not confirm the outcome: {str(exc).splitlines()[0][:220]}",
            "inconclusive",
        )
    finally:
        context.close()

    context, page = _new_test_page(browser)
    try:
        page.goto(config.URL, timeout=config.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_selector("input[placeholder='Enter user ID']", timeout=config.PAGE_TIMEOUT_MS)
        page.fill("input[placeholder='Enter user ID']", config.LOGIN_USER_ID)
        page.fill("input[placeholder='Enter password']", "definitely-wrong-password-123!")
        page.click("button.login-btn:has-text('Login')", timeout=config.STEP_TIMEOUT_MS)
        page.wait_for_timeout(700)
        still_on_login = "/login" in page.url
        message = get_toast_or_message_text(page)
        REPORTER.record(
            "auth",
            "Wrong password is rejected",
            config.URL,
            "Login fails and the user remains unauthenticated",
            f"Still on login: {still_on_login}; visible message: {message or 'none detected'}",
            "pass" if still_on_login else "fail",
            severity="critical",
            screenshot="" if still_on_login else screenshot(page, "auth_wrong_password"),
        )
        if still_on_login:
            REPORTER.record(
                "auth",
                "Failed login gives user feedback",
                config.URL,
                "A failed login displays a clear error message",
                f"Visible message: {message or 'none detected'}",
                "pass" if bool(message) else "fail",
                severity="low",
                suggested_fix="Show a clear invalid-credentials message." if not message else "",
            )
    except Exception as exc:
        REPORTER.record(
            "auth",
            "Wrong password is rejected",
            config.URL,
            "The invalid-login test completes",
            f"Automation could not confirm the outcome: {str(exc).splitlines()[0][:220]}",
            "inconclusive",
        )
    finally:
        context.close()


def collect_nav_links_for(browser, user, password):
    context, page = _new_test_page(browser)
    links = set()
    error = None
    try:
        login(page, user, password)
        page.wait_for_timeout(500)
        for element in get_page_elements(page):
            if element.get("href"):
                links.add(element["href"])
            elif element.get("text"):
                links.add(element["text"])
    except Exception as exc:
        error = str(exc)
    finally:
        context.close()
    return links, error


def run_role_access_tests(browser):
    """Optional generic navigation comparison retained for backward compatibility."""
    if not config.EXTRA_ROLE_USERS:
        REPORTER.record(
            "role_access",
            "Generic role navigation comparison",
            config.URL,
            "Additional navigation roles are configured when this comparison is needed",
            "Not configured; facility-specific role capability checks run separately",
            "skipped",
        )
        return

    base_links, base_error = collect_nav_links_for(
        browser, config.LOGIN_USER_ID, config.LOGIN_PASSWORD
    )
    if base_error:
        REPORTER.record(
            "role_access",
            "Generic role navigation comparison",
            config.URL,
            "The primary role navigation can be measured",
            f"Primary role navigation could not be collected: {base_error[:220]}",
            "inconclusive",
        )
        return

    for pair in config.EXTRA_ROLE_USERS.split(","):
        if ":" not in pair:
            continue
        user, password = pair.split(":", 1)
        links, error = collect_nav_links_for(browser, user.strip(), password.strip())
        if error:
            REPORTER.record(
                "role_access",
                f"Navigation comparison for '{user.strip()}'",
                config.URL,
                "The configured role account signs in",
                f"The role account could not be measured: {error[:220]}",
                "inconclusive",
            )
            continue
        REPORTER.record(
            "role_access",
            f"Navigation comparison for '{user.strip()}'",
            config.URL,
            "Role navigation can be compared with the primary user",
            (
                f"Only primary: {sorted(base_links - links)[:15] or 'none'}; "
                f"only role user: {sorted(links - base_links)[:15] or 'none'}"
            ),
            "pass",
        )


def _record_creation_capability(page, role: str, creation_url: str):
    capabilities = role_capabilities(role)
    accessible, access_detail = _page_access(page, config.FACILITY_CREATION_PATH)
    create_visible = _visible_button(page, _CREATE_RE)
    expected_allowed = capabilities["create_facility"]

    if expected_allowed:
        passed = accessible and create_visible
        actual = (
            f"Create Facility visible: {create_visible}; route accessible: {accessible}; "
            f"{access_detail}"
        )
    else:
        passed = (not create_visible) or (not accessible)
        actual = (
            f"Create Facility visible: {create_visible}; route accessible: {accessible}; "
            f"{access_detail}"
        )

    REPORTER.record(
        "role_access",
        f"{role} facility creation permission",
        creation_url,
        (
            f"{role} can create a facility"
            if expected_allowed
            else f"{role} cannot create a facility"
        ),
        actual,
        "pass" if passed else "fail",
        severity="critical",
        screenshot="" if passed else screenshot(page, f"role_create_{role.lower()}"),
        suggested_fix=(
            "Align both the frontend control and backend authorization with the role matrix: "
            "FourthSignal and Maker can create; Checker cannot create."
            if not passed else ""
        ),
    )


def _record_approval_capability(page, role: str, approval_url: str):
    capabilities = role_capabilities(role)
    accessible, access_detail = _page_access(page, config.FACILITY_APPROVAL_PATH)
    approve_visible = _visible_button(page, _APPROVE_RE)
    expected_allowed = capabilities["approve_facility"]

    if not expected_allowed:
        passed = not approve_visible
        REPORTER.record(
            "role_access",
            f"{role} cannot approve facilities",
            approval_url,
            f"Approve controls are not available to {role}",
            (
                f"Approve visible: {approve_visible}; route accessible: {accessible}; "
                f"{access_detail}"
            ),
            "pass" if passed else "fail",
            severity="critical",
            screenshot="" if passed else screenshot(page, f"role_approve_{role.lower()}"),
            suggested_fix=(
                "Hide approval controls for FourthSignal/Maker and enforce the same restriction "
                "in the approval API."
                if not passed else ""
            ),
        )
        return

    REPORTER.record(
        "role_access",
        f"{role} can access Facility Approval",
        approval_url,
        "Checker can open the Facility Approval worklist",
        access_detail,
        "pass" if accessible else "fail",
        severity="critical",
        screenshot="" if accessible else screenshot(page, "checker_approval_access"),
    )

    has_records, explicit_empty, data_detail = _approval_data_state(page)
    if approve_visible:
        status = "pass"
        actual = f"Approve visible: True; {data_detail}"
    elif has_records and not explicit_empty:
        status = "fail"
        actual = f"Pending/record evidence exists but Approve was not visible; {data_detail}"
    else:
        status = "inconclusive"
        actual = (
            "Approve could not be verified because no pending facility requiring an action "
            f"was detected; {data_detail}"
        )

    REPORTER.record(
        "role_access",
        "Checker can approve a pending facility",
        approval_url,
        "Approve is visible for Checker when a pending facility is available",
        actual,
        status,
        severity="critical",
        screenshot="" if status != "fail" else screenshot(page, "checker_approve_missing"),
        suggested_fix=(
            "Expose Approve to Checker for pending rows and enforce Checker authorization in the API."
            if status == "fail" else ""
        ),
    )


def run_facility_role_capability_tests(browser):
    """Validate the exact FS NxT ERP role matrix without clicking destructive actions.

    Expected matrix:
      - FourthSignal: create facility, cannot approve.
      - Maker: create facility, cannot approve.
      - Checker: cannot create facility, can approve pending facilities.
    """
    users = parse_role_test_users()
    required = {"FOURTHSIGNAL", "MAKER", "CHECKER"}
    configured = {role for role, _, _ in users}

    if not users:
        REPORTER.record(
            "role_access",
            "Facility role matrix is configured",
            config.URL,
            "FourthSignal, Maker and Checker credentials are available for role validation",
            (
                "ROLE_TEST_USERS is empty. Configure: "
                "FOURTHSIGNAL:user:password,MAKER:user:password,CHECKER:user:password"
            ),
            "inconclusive",
        )
        return

    missing = sorted(required - configured)
    if missing:
        REPORTER.record(
            "role_access",
            "Facility role matrix has all required users",
            config.URL,
            "FourthSignal, Maker and Checker are all configured",
            f"Missing role credentials: {', '.join(missing)}",
            "inconclusive",
        )
    else:
        REPORTER.record(
            "role_access",
            "Facility role matrix has all required users",
            config.URL,
            "FourthSignal, Maker and Checker are all configured",
            "All required role accounts are configured",
            "pass",
        )

    creation_url = _route_url(config.FACILITY_CREATION_PATH)
    approval_url = _route_url(config.FACILITY_APPROVAL_PATH)

    for role, user, password in users:
        if role not in ROLE_CAPABILITIES and role != "CHECKER":
            REPORTER.record(
                "role_access",
                f"Unknown role mapping: {role}",
                config.URL,
                "The role has a defined capability contract",
                "No built-in facility capability rule exists for this role",
                "inconclusive",
            )
            continue

        context, page = _new_test_page(browser)
        try:
            login(page, user, password)
        except Exception as exc:
            REPORTER.record(
                "role_access",
                f"{role} role account can sign in",
                config.URL,
                f"The configured {role} account signs in",
                f"Could not sign in with the configured account: {str(exc).splitlines()[0][:220]}",
                "inconclusive",
            )
            context.close()
            continue

        REPORTER.record(
            "role_access",
            f"{role} role account can sign in",
            config.URL,
            f"The configured {role} account signs in",
            f"Signed in and landed on {page.url}",
            "pass",
        )

        try:
            page.goto(creation_url, timeout=config.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(400)
            _record_creation_capability(page, role, creation_url)
        except Exception as exc:
            REPORTER.record(
                "role_access",
                f"{role} facility creation permission",
                creation_url,
                "The creation capability can be verified",
                f"Automation could not confirm the route: {str(exc).splitlines()[0][:220]}",
                "inconclusive",
            )

        try:
            page.goto(approval_url, timeout=config.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(400)
            _record_approval_capability(page, role, approval_url)
        except Exception as exc:
            REPORTER.record(
                "role_access",
                f"{role} facility approval permission",
                approval_url,
                "The approval capability can be verified",
                f"Automation could not confirm the route: {str(exc).splitlines()[0][:220]}",
                "inconclusive",
            )
        finally:
            context.close()


# Backward-compatible name used by older main.py files.
def run_facility_approval_role_tests(browser):
    run_facility_role_capability_tests(browser)

