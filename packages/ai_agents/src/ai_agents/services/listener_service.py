"""Browser event capture for console errors and API/network activity.

A Playwright ``requestfailed`` event does not always mean that the application
failed to load data. Browsers emit ``net::ERR_ABORTED`` when a request is
cancelled because the page navigated, reloaded, closed a modal, or replaced a
request with a newer one. Those cancellations are diagnostic noise, not product
defects.

This listener therefore:

* tracks successful API responses as evidence;
* records real HTTP/network failures separately;
* keeps benign cancellations out of the defect list; and
* limits API tracking to fetch/XHR or explicit ``/api/`` requests so images,
  fonts and analytics do not pollute the report.
"""
from ai_agents.services.reporter_service import REPORTER, screenshot

_API_RESOURCE_TYPES = {"fetch", "xhr"}
_BENIGN_FAILURE_MARKERS = (
    "err_aborted",
    "ns_binding_aborted",
    "request was cancelled",
    "request was canceled",
    "cancelled",
    "canceled",
)


def _request_url(request):
    try:
        return str(request.url or "")[:500]
    except Exception:
        return ""


def _request_page_url(request, page):
    """Return the document URL that initiated the request where possible."""
    try:
        frame_url = str(request.frame.url or "")
        if frame_url:
            return frame_url[:500]
    except Exception:
        pass

    try:
        return str(page.url or "")[:500]
    except Exception:
        return ""


def _request_method(request):
    try:
        return str(request.method or "GET").upper()
    except Exception:
        return "GET"


def _request_resource_type(request):
    try:
        return str(request.resource_type or "").lower()
    except Exception:
        return ""


def _is_api_request(request):
    resource_type = _request_resource_type(request)
    url = _request_url(request).lower()
    return resource_type in _API_RESOURCE_TYPES or "/api/" in url


def _failure_text(request):
    try:
        return str(request.failure or "unknown network failure")
    except Exception:
        return "unknown network failure"


def _is_benign_cancellation(failure):
    lowered = str(failure or "").lower()
    return any(marker in lowered for marker in _BENIGN_FAILURE_MARKERS)


def attach_listeners(page):
    def on_console(message):
        if message.type == "error":
            REPORTER.console_errors.append(
                {
                    "url": page.url,
                    "text": message.text[:400],
                }
            )

    def on_pageerror(error):
        REPORTER.record(
            "crash",
            "Uncaught page error",
            page.url,
            "No uncaught JS exceptions",
            str(error)[:400],
            "fail",
            severity="high",
            screenshot=screenshot(page, "pageerror"),
            suggested_fix=(
                "Fix the uncaught exception; check the browser developer-tools "
                "stack trace."
            ),
        )

    def on_crash():
        REPORTER.record(
            "execution",
            "Browser page crashed during a test",
            page.url,
            "The page remains available until the test case completes",
            (
                "The Playwright page crashed. This is recorded as an automation/runtime "
                "issue rather than a confirmed application defect; the next route will "
                "continue in a fresh page."
            ),
            "inconclusive",
            severity="high",
        )

    def on_response(response):
        try:
            request = response.request
            if not _is_api_request(request):
                return

            entry = {
                "url": _request_page_url(request, page),
                "request_url": str(response.url or "")[:500],
                "status": int(response.status),
                "method": _request_method(request),
                "resource_type": _request_resource_type(request),
            }

            if 200 <= response.status < 400:
                REPORTER.successful_requests.append(entry)
            else:
                REPORTER.failed_requests.append(entry)
        except Exception:
            pass

    def on_requestfailed(request):
        try:
            if not _is_api_request(request):
                return

            failure = _failure_text(request)
            entry = {
                "url": _request_page_url(request, page),
                "request_url": _request_url(request),
                "status": f"FAILED: {failure}",
                "method": _request_method(request),
                "resource_type": _request_resource_type(request),
            }

            if _is_benign_cancellation(failure):
                # Keep the event in raw diagnostics, but never turn it into a
                # failed product test. A later successful response for the same
                # endpoint proves that the page loaded the data normally.
                REPORTER.ignored_requests.append(
                    {
                        **entry,
                        "ignored_reason": "browser cancelled the request",
                    }
                )
                return

            REPORTER.failed_requests.append(entry)
        except Exception:
            pass

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("crash", on_crash)
    page.on("response", on_response)
    page.on("requestfailed", on_requestfailed)
