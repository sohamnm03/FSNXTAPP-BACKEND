"""Entry point for the deterministic Playwright website test pipeline.

The pipeline is intentionally fault-isolated:
- every major stage is wrapped independently;
- every route runs in a fresh Playwright page;
- every focused business case records a result even when automation cannot
  complete it; and
- an atomic checkpoint is updated throughout the run.
"""
from __future__ import annotations

import atexit
import sys
import traceback
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from ai_agents import config
from ai_agents.services.reporter_service import REPORTER, screenshot
from ai_agents.services.listener_service import attach_listeners
from ai_agents.services.auth_service import (
    login,
    run_auth_tests,
    run_role_access_tests,
    run_facility_role_capability_tests,
)
from ai_agents.services.route_service import discover_routes, select_routes, configured_routes
from ai_agents.services.crawler_service import run_page_tests
from ai_agents.services.report_generator import rollup_api_and_console, generate_reports


def _harden_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _configure_page(page):
    page.set_default_timeout(config.STEP_TIMEOUT_MS)
    page.set_default_navigation_timeout(config.PAGE_TIMEOUT_MS)
    attach_listeners(page)


def _run_stage(name, callback, *, fatal=False):
    """Run one pipeline stage and continue after isolated infrastructure errors."""
    print(f"\n=== {name} ===", flush=True)
    REPORTER.update_run_state(status="running", current_stage=name, current_case="")
    try:
        value = callback()
        REPORTER.complete_stage(name)
        return value
    except KeyboardInterrupt:
        REPORTER.update_run_state(status="interrupted")
        raise
    except Exception as exc:
        traceback.print_exc()
        REPORTER.add_pipeline_error(name, exc)
        # Fatal stages (currently only the primary login) are reported by the
        # caller with the correct domain-specific result. Avoid adding a second,
        # duplicate generic execution result for the same failure.
        if fatal:
            raise
        REPORTER.record(
            "execution",
            f"Pipeline stage could not complete: {name}",
            config.URL,
            "The stage completes without stopping later stages",
            (
                "The test runner encountered an execution error, not a confirmed "
                f"application defect: {str(exc).splitlines()[0][:300]}"
            ),
            "inconclusive",
        )
        return None


def _safe_generate_reports():
    try:
        rollup_api_and_console()
    except Exception as exc:
        REPORTER.add_pipeline_error("evidence rollup", exc)
    try:
        generate_reports()
    except Exception as exc:
        REPORTER.add_pipeline_error("report generation", exc)
        print(
            "Report generation failed, but pipeline_checkpoint.json contains all "
            f"results collected so far: {str(exc).splitlines()[0][:240]}",
            flush=True,
        )
    finally:
        REPORTER.checkpoint()


def _launch_browser(playwright):
    launch_args = ["--disable-dev-shm-usage"]
    launch_kwargs = {
        "headless": config.HEADLESS,
        "timeout": config.BROWSER_LAUNCH_TIMEOUT_MS,
        "args": launch_args,
    }
    if config.BROWSER_EXECUTABLE_PATH:
        launch_kwargs["executable_path"] = config.BROWSER_EXECUTABLE_PATH
    return playwright.chromium.launch(**launch_kwargs)


def main(inputs=None, output_dir=None, backend_mode=False):
    config.configure(inputs or {}, output_dir=output_dir, backend_mode=backend_mode)
    _harden_console()
    atexit.register(REPORTER.checkpoint)
    print(
        "Safe mode: "
        + ("OFF - destructive actions ALLOWED" if config.ALLOW_DESTRUCTIVE else "ON - destructive actions blocked"),
        flush=True,
    )
    print(
        f"Primary role context: {config.PRIMARY_USER_ROLE}; "
        f"checkpoint: {config.CHECKPOINT_PATH}",
        flush=True,
    )

    browser = None
    context = None
    primary_page = None
    selected = []

    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)

            def on_browser_disconnected():
                # Do not overwrite a successful/known terminal state when the
                # runner intentionally closes Chromium during normal cleanup.
                if REPORTER.run_state.get("status") in {
                    "starting",
                    "running",
                }:
                    REPORTER.update_run_state(status="browser_disconnected")

            browser.on("disconnected", on_browser_disconnected)
            context = browser.new_context(ignore_https_errors=config.IGNORE_HTTPS_ERRORS)
            primary_page = context.new_page()
            _configure_page(primary_page)

            try:
                _run_stage(
                    "Primary login",
                    lambda: login(primary_page),
                    fatal=True,
                )
            except Exception as exc:
                REPORTER.record(
                    "auth",
                    "Primary login",
                    config.URL,
                    "The configured test account signs in",
                    (
                        "Sign-in could not be completed, so authenticated business tests "
                        f"could not run: {str(exc).splitlines()[0][:240]}"
                    ),
                    "fail",
                    severity="critical",
                    screenshot=screenshot(primary_page, "login_fail"),
                )
                REPORTER.update_run_state(status="login_failed")
                return

            REPORTER.record(
                "auth",
                "Primary login",
                config.URL,
                "Valid credentials sign in and land on the application",
                f"Signed in successfully and landed on {primary_page.url}",
                "pass",
                repro_steps=[
                    f"Go to {config.URL}",
                    f"Sign in as '{config.LOGIN_USER_ID}'",
                ],
            )
            start_url = primary_page.url

            if config.LOGIN_ONLY:
                print("Login-only check completed; no application routes were supplied.", flush=True)
                REPORTER.update_run_state(
                    status="completed",
                    current_stage="complete",
                    current_case="",
                )
                return

            def route_stage():
                discovered = discover_routes(primary_page, context, start_url)
                routes = configured_routes(discovered, start_url)
                return select_routes(routes)

            selected = _run_stage("Route discovery and selection", route_stage) or []
            if not selected:
                REPORTER.record(
                    "coverage",
                    "No routes selected",
                    start_url,
                    "At least one configured route is available for testing",
                    "No routes were selected or discovered",
                    "inconclusive",
                )
                REPORTER.update_run_state(status="no_routes")
                return
            if config.MAX_PAGES > 0 and len(selected) > config.MAX_PAGES:
                selected = selected[: config.MAX_PAGES]

            _run_stage(
                "Authentication and session checks",
                lambda: run_auth_tests(browser, start_url),
            )
            _run_stage(
                "Legacy role navigation comparison",
                lambda: run_role_access_tests(browser),
            )
            _run_stage(
                "Facility role capability checks",
                lambda: run_facility_role_capability_tests(browser),
            )
            _run_stage(
                "Selected page test suite",
                lambda: run_page_tests(primary_page, context, selected),
            )

            REPORTER.update_run_state(status="completed", current_stage="complete", current_case="")
    except KeyboardInterrupt:
        print("\nRun interrupted by the operator. Saving partial results...", flush=True)
        REPORTER.update_run_state(status="interrupted")
    except Exception as exc:
        traceback.print_exc()
        REPORTER.add_pipeline_error("top-level", exc)
        REPORTER.record(
            "execution",
            "Test pipeline encountered an unexpected top-level error",
            primary_page.url if primary_page is not None else config.URL,
            "The pipeline reaches report generation",
            (
                "The runner stopped because of an infrastructure or automation error: "
                f"{str(exc).splitlines()[0][:300]}"
            ),
            "inconclusive",
            screenshot=screenshot(primary_page, "pipeline_error") if primary_page is not None else "",
        )
        REPORTER.update_run_state(status="error")
    finally:
        _safe_generate_reports()
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass

    counts = REPORTER.counts()
    print(
        f"\nSummary: {len(REPORTER.pages_tested)} page(s), "
        f"{len(REPORTER.results)} checks - {counts.get('pass', 0)} passed / "
        f"{counts.get('fail', 0)} failed / {counts.get('inconclusive', 0)} "
        f"not confirmed / {counts.get('skipped', 0)} not tested.",
        flush=True,
    )


def cli():
    """Optional local entry point; API workers never load .env or prompt."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run the AI Agents website test package")
    parser.add_argument("--input-json", help="Path to a JSON object containing run inputs")
    parser.add_argument("--output-dir", default="runtime/cli")
    parser.add_argument("--load-dotenv", action="store_true", help="Explicitly load a local .env for CLI debugging")
    args = parser.parse_args()
    if args.load_dotenv:
        import importlib
        from dotenv import load_dotenv
        load_dotenv()
        importlib.reload(config)
    inputs = {}
    if args.input_json:
        with open(args.input_json, encoding="utf-8") as handle:
            inputs = json.load(handle)
    main(inputs, output_dir=args.output_dir, backend_mode=False)


if __name__ == "__main__":
    cli()



