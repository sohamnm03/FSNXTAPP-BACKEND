from __future__ import annotations

import json
import shutil
import sys
import types
import uuid
from pathlib import Path


class FakePage:
    url = "https://example.test/dashboard"

    def set_default_timeout(self, _value):
        pass

    def set_default_navigation_timeout(self, _value):
        pass


class FakeContext:
    def __init__(self):
        self.page = FakePage()

    def new_page(self):
        return self.page

    def close(self):
        pass


class FakeBrowser:
    def __init__(self):
        self.context = FakeContext()

    def on(self, _event, _callback):
        pass

    def new_context(self, **_kwargs):
        return self.context

    def close(self):
        pass


class FakePlaywright:
    class Chromium:
        def launch(self, **_kwargs):
            return FakeBrowser()

    chromium = Chromium()


class FakePlaywrightContext:
    def __enter__(self):
        return FakePlaywright()

    def __exit__(self, *_args):
        return False


def test_mocked_ai_agents_end_to_end(monkeypatch):
    """No browser, website, or Anthropic request is made by this package run."""
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: FakePlaywrightContext()
    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    from packages.ai_agents import runner

    source = str(runner.SOURCE_ROOT)
    if source not in sys.path:
        sys.path.insert(0, source)
    from ai_agents import config, main
    from ai_agents.services.reporter_service import REPORTER

    REPORTER.__init__()
    route = {"path": "/dashboard", "url": "https://example.test/dashboard", "name": "Dashboard", "source": "configured"}
    monkeypatch.setattr(main, "attach_listeners", lambda _page: None)
    monkeypatch.setattr(main, "login", lambda _page: True)
    monkeypatch.setattr(main, "discover_routes", lambda *_args: [route])
    monkeypatch.setattr(main, "configured_routes", lambda routes, _url: routes)
    monkeypatch.setattr(main, "select_routes", lambda routes: routes)
    monkeypatch.setattr(main, "run_auth_tests", lambda *_args: None)
    monkeypatch.setattr(main, "run_role_access_tests", lambda *_args: None)
    monkeypatch.setattr(main, "run_facility_role_capability_tests", lambda *_args: None)
    monkeypatch.setattr(
        main, "run_page_tests",
        lambda *_args: REPORTER.record("mock", "Mocked page check", route["url"], "works", "works", "pass"),
    )
    monkeypatch.setattr(main, "rollup_api_and_console", lambda: None)

    def fake_reports():
        Path(config.HTML_REPORT_PATH).write_text("<html>mock report</html>", encoding="utf-8")
        Path(config.JSON_REPORT_PATH).write_text(json.dumps({"mocked": True}), encoding="utf-8")

    monkeypatch.setattr(main, "generate_reports", fake_reports)
    run_id = str(uuid.uuid4())
    output_dir = runner.PACKAGE_ROOT / "runtime" / run_id
    try:
        result = runner.run_package(
            run_id,
            {"website_url": "https://example.test/login", "username": "test-user", "password": "test-only-secret", "routes": ["/dashboard"]},
            str(output_dir),
        )
        assert result["status"] == "completed"
        assert result["summary"]["pass"] == 2
        assert "site_test_report.html" in result["artifacts"]
        assert "site_test_report.json" in result["artifacts"]
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)
