"""Test-result recording and atomic progress checkpoints."""
import json
import os
import re
import time
from datetime import datetime

from ai_agents import config


class Reporter:
    def __init__(self):
        self.results = []
        self.pages_tested = set()
        self.console_errors = []
        self.failed_requests = []
        self.successful_requests = []
        self.ignored_requests = []
        self.baseline_console = set()
        self._counter = 0
        self.run_state = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "starting",
            "current_stage": "startup",
            "current_page": "",
            "current_case": "",
            "completed_stages": [],
            "pipeline_errors": [],
        }

    def next_id(self):
        self._counter += 1
        return f"T{self._counter:04d}"

    def snapshot_console_baseline(self):
        for entry in self.console_errors:
            self.baseline_console.add(self.console_key(entry.get("text")))

    @staticmethod
    def console_key(text):
        return " ".join(str(text or "").split())[:120]

    def update_run_state(self, **changes):
        self.run_state.update(changes)
        self.run_state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.checkpoint()

    def complete_stage(self, stage: str):
        stages = self.run_state.setdefault("completed_stages", [])
        if stage not in stages:
            stages.append(stage)
        self.update_run_state(current_stage=stage, current_case="")

    def add_pipeline_error(self, stage: str, error: Exception | str):
        self.run_state.setdefault("pipeline_errors", []).append(
            {
                "stage": stage,
                "error": str(error)[:500],
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.checkpoint()

    def checkpoint(self):
        if not config.AUTO_CHECKPOINT:
            return
        payload = {
            "run_state": self.run_state,
            "summary": self.counts(),
            "pages_tested": sorted(self.pages_tested),
            "results": self.results,
            "console_errors": self.console_errors,
            "failed_requests": self.failed_requests,
            "successful_requests": self.successful_requests,
            "ignored_requests": self.ignored_requests,
        }
        path = config.CHECKPOINT_PATH
        temp = f"{path}.tmp"
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(temp, path)
        except Exception:
            try:
                if os.path.exists(temp):
                    os.remove(temp)
            except Exception:
                pass

    def record(
        self,
        category,
        title,
        page_url,
        expected,
        actual,
        status,
        severity="info",
        repro_steps=None,
        screenshot=None,
        suggested_fix="",
    ):
        if status not in {"pass", "fail", "inconclusive", "skipped"}:
            status = "inconclusive"
        entry = {
            "test_id": self.next_id(),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "category": category,
            "title": title,
            "page_url": page_url,
            "expected": expected,
            "actual": actual,
            "status": status,
            "severity": severity if status == "fail" else "info",
            "repro_steps": repro_steps or [],
            "screenshot": screenshot or "",
            "suggested_fix": suggested_fix,
        }
        self.results.append(entry)
        mark = {
            "pass": "PASS",
            "fail": "FAIL",
            "inconclusive": "UNSURE",
            "skipped": "SKIP",
        }.get(status, status)
        print(f"  [{mark}] ({category}) {title}", flush=True)
        self.checkpoint()
        return entry

    def counts(self):
        counts = {"pass": 0, "fail": 0, "inconclusive": 0, "skipped": 0}
        for result in self.results:
            status = result.get("status", "inconclusive")
            counts[status] = counts.get(status, 0) + 1
        return counts


REPORTER = Reporter()


def screenshot(page, name):
    os.makedirs(config.SCREENSHOT_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name)[:80]
    path = f"{config.SCREENSHOT_DIR}/{safe}_{int(time.time())}.png"
    try:
        page.screenshot(path=path, full_page=True, timeout=config.PAGE_TIMEOUT_MS)
        return path
    except Exception:
        return ""

