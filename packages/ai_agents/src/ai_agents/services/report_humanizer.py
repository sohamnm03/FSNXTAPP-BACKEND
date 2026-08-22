"""Translates raw test-result entries (written for QA/engineering) into
plain-language summaries a non-technical reader - a director, a product
owner, a client - can understand at a glance.

Nothing here changes what gets tested or how; it only reshapes how a
finding is *described* in the HTML report. The JSON report keeps the raw
technical fields untouched, for tooling and for engineers who want them.
"""
import re

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

SEVERITY_META = {
    "critical": {"label": "Critical", "color": "#7b1113", "bg": "#fdeceb", "emoji": "\U0001F6A8"},
    "high":     {"label": "High",     "color": "#b3261e", "bg": "#fdeceb", "emoji": "\U0001F534"},
    "medium":   {"label": "Medium",   "color": "#9a5b00", "bg": "#fdf3e3", "emoji": "\U0001F7E0"},
    "low":      {"label": "Low",      "color": "#8a6d00", "bg": "#fdf8e3", "emoji": "\U0001F7E1"},
    "info":     {"label": "Info",     "color": "#3b6e3b", "bg": "#eaf7ea", "emoji": "â„¹ï¸"},
}

CATEGORY_META = {
    "auth":        {"label": "Login & Security",       "emoji": "\U0001F512"},
    "security":    {"label": "Security",                "emoji": "\U0001F6E1ï¸"},
    "role_access": {"label": "User Permissions",        "emoji": "\U0001F465"},
    "navigation":  {"label": "Navigation",              "emoji": "\U0001F9ED"},
    "functional":  {"label": "Buttons & Interactions",  "emoji": "\U0001F5B1ï¸"},
    "validation":  {"label": "Form Validation",         "emoji": "\U0001F4DD"},
    "usability":   {"label": "Ease of Use",             "emoji": "\U0001F9E9"},
    "consistency": {"label": "Design Consistency",      "emoji": "\U0001F3A8"},
    "ui":          {"label": "Look & Feel",             "emoji": "\U0001F58Cï¸"},
    "api":         {"label": "Links & Data Loading",    "emoji": "\U0001F517"},
    "console":     {"label": "Background Errors",       "emoji": "âš™ï¸"},
    "crash":       {"label": "Page Crashes",            "emoji": "\U0001F4A5"},
}


def category_meta(category):
    return CATEGORY_META.get(category, {"label": category.replace("_", " ").title(), "emoji": "â€¢"})


def severity_meta(severity):
    return SEVERITY_META.get(severity, SEVERITY_META["info"])


def _short(text, limit=200):
    """First line only, capped to `limit` chars - full multi-line technical
    dumps (stack traces, Playwright call logs) belong in Technical details,
    not in a plain-language summary."""
    text = (text or "").strip()
    if not text:
        return text
    line = text.splitlines()[0].strip()
    truncated = len(line) < len(text)
    if len(line) > limit:
        line = line[:limit].rstrip() + "â€¦"
        truncated = True
    return line + (" (full details below)" if truncated else "")


# Each rule: (category, compiled regex, handler(match, entry) -> (title, message))
# First matching rule wins; unmatched titles fall back to the raw title/actual text.
_RULES = []


def _rule(category, pattern):
    def register(fn):
        _RULES.append((category, re.compile(pattern), fn))
        return fn
    return register


@_rule("auth", r"^Primary login$")
def _r_primary_login(m, e):
    return ("Could not log in with the test account",
            f"The automated login failed: {_short(e['actual'])}")


@_rule("auth", r"^Protected page requires login$")
def _r_protected_page(m, e):
    return ("A private page can be viewed without logging in",
            f"Visiting this page directly, with no session, should send a visitor to the login "
            f"screen. Instead: {_short(e['actual'])}")


@_rule("auth", r"^Wrong password is rejected$")
def _r_wrong_password(m, e):
    return ("A wrong password is being accepted",
            f"Logging in with an incorrect password should fail. Instead: {_short(e['actual'])}")


@_rule("auth", r"^Failed login shows no error message$")
def _r_no_login_error(m, e):
    return ("No error message after a failed login",
            "When a login attempt fails, the user isn't shown any message explaining why - "
            "this reads as the app silently doing nothing.")


@_rule("auth", r"^(Protected page without login|Wrong password test)$")
def _r_auth_test_error(m, e):
    return ("A login security check could not be completed",
            f"The test itself hit an error, so this could not be verified: {_short(e['actual'])}")


@_rule("role_access", r"^Login as role user '(.*)'$")
def _r_role_login(m, e):
    return (f"Could not sign in as the '{m.group(1)}' test user",
            f"So visible permissions for this role could not be compared: {_short(e['actual'])}")


@_rule("crash", r"^Page failed to load$")
def _r_page_failed(m, e):
    return ("A page did not load", _short(e["actual"]))


@_rule("crash", r"^Page appears crashed or blank$")
def _r_page_blank(m, e):
    return ("A page loaded blank or showed a crash/error message", _short(e["actual"]))


@_rule("crash", r"^Uncaught page error$")
def _r_uncaught(m, e):
    return ("The page crashed due to a coding error (uncaught exception)", _short(e["actual"]))


@_rule("crash", r"^Test run aborted unexpectedly$")
def _r_run_aborted(m, e):
    return ("The automated test run stopped early due to an unexpected error", _short(e["actual"]))


@_rule("ui", r"^Horizontal overflow at (\w+) width \((\d+)px\)$")
def _r_overflow(m, e):
    device, width = m.group(1), m.group(2)
    return (f"Page doesn't fit {device} screens ({width}px wide)",
            f"{_short(e['actual'])}. Visitors on a {device}-sized screen will have to scroll "
            "sideways to see everything.")


@_rule("ui", r"^Claude-flagged: (.*)$")
def _r_claude_flag(m, e):
    return (m.group(1), _short(e["actual"], limit=320))


@_rule("api", r"^Broken link: (.*)$")
def _r_broken_link(m, e):
    return ("A link on the page is broken",
            f"Following this link fails ({_short(e['actual'])}). Link: {m.group(1)}")


@_rule("api", r"^Link unreachable: (.*)$")
def _r_link_unreachable(m, e):
    return ("A link could not be reached", f"{_short(e['actual'])}. Link: {m.group(1)}")


@_rule("api", r"^Failed request: (.*)$")
def _r_failed_request(m, e):
    return ("Part of the page failed to load its data",
            f"A background request this page depends on failed ({_short(e['actual'])}), so some "
            "information on the page may be missing, wrong, or stuck loading.")


@_rule("console", r"^Console error on ")
def _r_console_error(m, e):
    return ("A hidden technical error happened on this page",
            f"This may not be visible to users, but it's worth a developer look: "
            f"\"{_short(e['actual'], limit=160)}\"")


@_rule("validation", r"^Form found but no submit button identified")
def _r_form_no_submit(m, e):
    return ("A form could not be tested automatically",
            "No obvious submit/save button was found for this form, so it was skipped.")


@_rule("validation", r"^F\d*\?? execution error$")
def _r_form_exec_error(m, e):
    return ("A form validation check could not be completed", f"Test error: {_short(e['actual'])}")


@_rule("validation", r"^F\d+: (.*)$")
def _r_form_case(m, e):
    return (m.group(1),
            f"Expected: {_short(e['expected'], limit=250)}. "
            f"What actually happened: {_short(e['actual'], limit=250)}")


@_rule("functional", r"^Skipped destructive control '(.*)'$")
def _r_skipped_destructive(m, e):
    return (f"'{m.group(1)}' was intentionally not clicked",
            "This looked like it deletes, approves, pays, or otherwise changes real data, so it "
            "was left alone for safety.")


@_rule("functional", r"^Click '(.*)' \((\w+)\)$")
def _r_click(m, e):
    label = m.group(1)
    if e["actual"].startswith("Click failed"):
        return (f"The '{label}' button/link did not respond when clicked", _short(e["actual"]))
    return (f"Clicking '{label}' caused a hidden error",
            f"{_short(e['actual'])} - the click itself worked, but it triggered an error "
            "behind the scenes.")


@_rule("functional", r"^Modal opened by '(.*)'$")
def _r_modal_opened(m, e):
    return (f"The popup opened by '{m.group(1)}' appears to be empty", _short(e["actual"]))


@_rule("functional", r"^Modal closes cleanly \('(.*)'\)$")
def _r_modal_closes(m, e):
    return (f"The popup opened by '{m.group(1)}' won't close",
            "Neither the close/cancel button nor the Escape key dismissed it - a visitor could "
            "get stuck with it open.")


def humanize(entry):
    """Returns (friendly_title, plain_message) for a result entry."""
    for category, pattern, handler in _RULES:
        if entry["category"] != category:
            continue
        m = pattern.match(entry["title"])
        if m:
            try:
                return handler(m, entry)
            except Exception:
                break
    # Fallback: no rule matched. The observed text is already written in
    # business language at the point it was recorded, so use it directly rather
    # than padding it with "Expected:/Actual:" scaffolding a reader can't use.
    return (entry["title"], _short(entry["actual"], 400))


def display_page(url, site_origin):
    """Shortens a page URL for display: shows the path only when it's on the
    site being tested, falls back to the full URL otherwise."""
    if site_origin and url.startswith(site_origin):
        path = url[len(site_origin):]
        return path or "/"
    return url

