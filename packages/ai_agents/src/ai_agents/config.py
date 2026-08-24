"""Environment-driven configuration shared by every service.

All values come from environment variables (loaded from a local .env file
if present). Focused business tests are deterministic by default; Claude can
still be enabled for wording or exploratory analysis, but it never decides a
measured pass/fail result.
"""
import os
import re

# The API worker never loads a .env file. The optional CLI explicitly opts in.


def _bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


URL = os.environ.get(
    "WEBSITE_URL",
    "http://localhost:3000/login",
).strip()
LOGIN_USER_ID = os.environ.get("TEST_USERNAME", "").strip()
LOGIN_PASSWORD = os.environ.get("TEST_PASSWORD", "")
PRIMARY_USER_ROLE = os.environ.get("PRIMARY_USER_ROLE", "FOURTHSIGNAL").strip().upper()

HEADLESS = _bool("HEADLESS", "0")
BROWSER_EXECUTABLE_PATH = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip()
BROWSER_LAUNCH_TIMEOUT_MS = _int("BROWSER_LAUNCH_TIMEOUT_MS", 30000)
IGNORE_HTTPS_ERRORS = _bool("IGNORE_HTTPS_ERRORS", "0")
MAX_PAGES = _int("MAX_PAGES", 15)
MAX_ACTIONS_PER_PAGE = _int("MAX_ACTIONS_PER_PAGE", 6)
ALLOW_DESTRUCTIVE = _bool("ALLOW_DESTRUCTIVE", "0")
TEST_PROFILE = os.environ.get("TEST_PROFILE", "functional").strip().lower()

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6").strip()
ANTHROPIC_API_KEY = ""
CLAUDE_MAX_RETRIES = _int("CLAUDE_MAX_RETRIES", 2)
CLAUDE_RETRY_DELAY_SECONDS = _float("CLAUDE_RETRY_DELAY_SECONDS", 1.5)
CLAUDE_TIMEOUT_SECONDS = _float("CLAUDE_TIMEOUT_SECONDS", 25.0)
ENABLE_CLAUDE_PAGE_ANALYSIS = _bool("ENABLE_CLAUDE_PAGE_ANALYSIS", "0")
BUSINESS_LANGUAGE = _bool("BUSINESS_LANGUAGE", "0")

# Role credentials. Preferred format:
# FOURTHSIGNAL:user:password,MAKER:user:password,CHECKER:user:password
# CHECK is accepted as an alias for CHECKER. ADMIN remains supported for older
# environments and is treated as create-only unless the application says otherwise.
ROLE_TEST_USERS = os.environ.get("ROLE_TEST_USERS", "").strip()
EXTRA_ROLE_USERS = os.environ.get("EXTRA_ROLE_USERS", "").strip()
FACILITY_CREATION_PATH = os.environ.get(
    "FACILITY_CREATION_PATH", "/facility-creation"
).strip()
FACILITY_APPROVAL_PATH = os.environ.get(
    "FACILITY_APPROVAL_PATH", "/facility-approval"
).strip()

# Form validation depth. Fields are tested one at a time with every unrelated
# required field holding valid data.
FORM_FIELDS_TO_TEST = _int("FORM_FIELDS_TO_TEST", 6)
FORM_CASES_PER_FIELD = _int("FORM_CASES_PER_FIELD", 3)

# Comma-separated paths to test without being prompted.
TEST_ROUTES = os.environ.get("TEST_ROUTES", "").strip()
LOGIN_ONLY = False
# Automation runs must never pause at an input prompt. Enable this only when a
# human intentionally wants to choose routes from the terminal.
INTERACTIVE_ROUTE_SELECTION = _bool("INTERACTIVE_ROUTE_SELECTION", "0")
FOCUS_ONLY = _bool("FOCUS_ONLY", "1")
FOCUS_ROUTES = os.environ.get(
    "FOCUS_ROUTES",
    "/dashboard,/facility-creation,/facility-approval",
).strip()
RUN_TEN_UI_CHECKS = _bool("RUN_TEN_UI_CHECKS", "1")
RUN_FOCUSED_BUSINESS_TESTS = _bool("RUN_FOCUSED_BUSINESS_TESTS", "1")
ENABLE_GENERIC_SAFE_ACTIONS = _bool("ENABLE_GENERIC_SAFE_ACTIONS", "0")

STRICT_UI_AUDIT = _bool(
    "STRICT_UI_AUDIT",
    "1" if TEST_PROFILE in {"ui", "visual", "accessibility", "full"} else "0",
)

MIN_TEST_CASES_PER_PAGE = _int("MIN_TEST_CASES_PER_PAGE", 15)
TARGET_TEST_CASES_PER_PAGE = _int("TARGET_TEST_CASES_PER_PAGE", 20)
PAGE_TIMEOUT_MS = _int("PAGE_TIMEOUT_MS", 20000)
STEP_TIMEOUT_MS = _int("STEP_TIMEOUT_MS", 5000)
SETTLE_TIMEOUT_MS = _int("SETTLE_TIMEOUT_MS", 4000)
CONTINUE_AFTER_PAGE_ERROR = _bool("CONTINUE_AFTER_PAGE_ERROR", "1")
CASE_RETRY_COUNT = max(0, _int("CASE_RETRY_COUNT", 1))
FACILITY_BASELINE_RETRIES = max(1, _int("FACILITY_BASELINE_RETRIES", 2))
DROPDOWN_SELECT_TIMEOUT_MS = _int("DROPDOWN_SELECT_TIMEOUT_MS", 4000)
DEPENDENT_DROPDOWN_TIMEOUT_MS = _int("DEPENDENT_DROPDOWN_TIMEOUT_MS", 6000)
DEPENDENT_OPTIONS_WAIT_MS = _int("DEPENDENT_OPTIONS_WAIT_MS", 1000)
DROPDOWN_OPEN_ATTEMPTS = max(1, _int("DROPDOWN_OPEN_ATTEMPTS", 2))
DROPDOWN_ACTION_TIMEOUT_MS = _int("DROPDOWN_ACTION_TIMEOUT_MS", 1400)
DROPDOWN_DOM_SCAN_TIMEOUT_MS = _int("DROPDOWN_DOM_SCAN_TIMEOUT_MS", 1800)
DROPDOWN_NATIVE_ATTEMPT_TIMEOUT_MS = _int("DROPDOWN_NATIVE_ATTEMPT_TIMEOUT_MS", 2200)
DROPDOWN_CUSTOM_ATTEMPT_TIMEOUT_MS = _int("DROPDOWN_CUSTOM_ATTEMPT_TIMEOUT_MS", 2600)
DROPDOWN_VISIBLE_OPTIONS_PASS = _bool("DROPDOWN_VISIBLE_OPTIONS_PASS", "1")
MODAL_CLOSE_WAIT_MS = _int("MODAL_CLOSE_WAIT_MS", 1200)
MODAL_CLOSE_RETRIES = _int("MODAL_CLOSE_RETRIES", 2)
ENABLE_UI_AUDIT = _bool("ENABLE_UI_AUDIT", "0")
ENABLE_RESPONSIVE_AUDIT = _bool("ENABLE_RESPONSIVE_AUDIT", "0")
ENABLE_GENERIC_USABILITY_FINDINGS = _bool("ENABLE_GENERIC_USABILITY_FINDINGS", "0")
ENABLE_LINK_CHECKS = _bool("ENABLE_LINK_CHECKS", "0")
REPORT_SECONDARY_VALIDATION_ISSUES = _bool("REPORT_SECONDARY_VALIDATION_ISSUES", "0")

HTML_REPORT_PATH = os.environ.get("HTML_REPORT_PATH", "site_test_report.html").strip()
JSON_REPORT_PATH = os.environ.get("JSON_REPORT_PATH", "site_test_report.json").strip()
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "test_screenshots").strip()
CHECKPOINT_PATH = os.environ.get("CHECKPOINT_PATH", "pipeline_checkpoint.json").strip()
AUTO_CHECKPOINT = _bool("AUTO_CHECKPOINT", "1")

DESTRUCTIVE_RE = re.compile(
    r"\b(delete|remove|destroy|approve|reject|decline|pay|payment|checkout|purchase|"
    r"submit|save|confirm|apply|deactivate|disable|terminate|revoke|cancel|"
    r"logout|log\s*out|sign\s*out|reset\s*password|change\s*password)\b",
    re.I,
)

MODAL_SELECTOR = (
    "[role='dialog'], [role='alertdialog'], [aria-modal='true'], .modal.show, .modal[open], "
    "dialog[open], .MuiDialog-root, .MuiDrawer-root, .ant-modal-wrap, .ant-drawer-open, "
    ".p-dialog, .popup, .ReactModal__Content, .chakra-modal__content, "
    "[data-state='open'], [data-radix-portal], [data-headlessui-portal], "
    ".offcanvas.show, .drawer.open, .sheet[data-open]"
)

BENIGN_CONSOLE_RE = re.compile(
    r"("
    r"ResizeObserver loop|"
    r"Download the React DevTools|"
    r"react-devtools|"
    r"Warning:.*(deprecat|componentWill|legacy|StrictMode|defaultProps|findDOMNode)|"
    r"was preloaded using link preload|"
    r"Failed to load resource.*\.(png|jpe?g|gif|svg|ico|woff2?|ttf|map)|"
    r"favicon|source ?map|chrome-extension://|"
    r"Non-Error promise rejection captured|"
    r"AbortError|The user aborted a request|signal is aborted|"
    r"net::ERR_(ABORTED|BLOCKED_BY_CLIENT)|"
    r"Added non-passive event listener|"
    r"\[HMR\]|\[webpack|hot-update|"
    r"google-?analytics|googletagmanager|gtag|segment\.(io|com)|hotjar|"
    r"clarity\.ms|doubleclick|facebook\.net|sentry"
    r")",
    re.I,
)

BENIGN_REQUEST_STATUSES = {401, 403, 499}


def configure(inputs: dict, output_dir=None, backend_mode=False) -> None:
    """Apply run-scoped inputs inside the already isolated worker process."""
    global URL, LOGIN_USER_ID, LOGIN_PASSWORD, PRIMARY_USER_ROLE, TEST_ROUTES, LOGIN_ONLY
    global HEADLESS, ALLOW_DESTRUCTIVE, INTERACTIVE_ROUTE_SELECTION
    global HTML_REPORT_PATH, JSON_REPORT_PATH, SCREENSHOT_DIR, CHECKPOINT_PATH
    global ANTHROPIC_API_KEY, ROLE_TEST_USERS, EXTRA_ROLE_USERS

    URL = str(inputs.get("website_url", URL)).strip()
    LOGIN_USER_ID = str(inputs.get("username", LOGIN_USER_ID)).strip()
    LOGIN_PASSWORD = str(inputs.get("password", LOGIN_PASSWORD))
    PRIMARY_USER_ROLE = str(inputs.get("primary_user_role", PRIMARY_USER_ROLE)).strip().upper()
    routes = inputs.get("routes", [])
    TEST_ROUTES = ",".join(routes) if isinstance(routes, list) else str(routes)
    LOGIN_ONLY = isinstance(routes, list) and not routes
    ANTHROPIC_API_KEY = str(inputs.get("anthropic_api_key", ""))
    ROLE_TEST_USERS = str(inputs.get("role_test_users", ""))
    EXTRA_ROLE_USERS = str(inputs.get("extra_role_users", ""))

    if backend_mode:
        HEADLESS = bool(inputs.get("headless", True))
        ALLOW_DESTRUCTIVE = False
        INTERACTIVE_ROUTE_SELECTION = False
    if output_dir:
        root = os.path.abspath(output_dir)
        HTML_REPORT_PATH = os.path.join(root, "site_test_report.html")
        JSON_REPORT_PATH = os.path.join(root, "site_test_report.json")
        SCREENSHOT_DIR = os.path.join(root, "screenshots")
        CHECKPOINT_PATH = os.path.join(root, "pipeline_checkpoint.json")


