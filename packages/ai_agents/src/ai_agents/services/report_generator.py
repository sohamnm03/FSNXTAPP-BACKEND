"""Rolls up captured console/API errors into results, then renders two reports:

  - JSON: the full, untouched technical data (for tooling / re-processing).
  - HTML: a business-readable report. Each issue is stated as what's broken,
    what a user experiences, why it matters, and what to do - with the raw
    technical evidence tucked behind a toggle for the engineers.
"""
import json
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ai_agents import config
from ai_agents.services.reporter_service import REPORTER
from ai_agents.services.report_humanizer import display_page, category_meta, severity_meta, SEVERITY_ORDER
from ai_agents.services.business_translator import translate_findings

SEV_BUCKETS = [
    ("critical", "Fix before release", "These allow wrong data into the system, expose "
     "something they shouldn't, or stop people working altogether."),
    ("high", "Fix soon", "These block or mislead someone trying to do their job."),
    ("medium", "Should fix", "These cause confusion, rework, or shut out some users."),
    ("low", "Polish", "Cosmetic and consistency items worth tidying up."),
]


def _normalise_request_url(raw_url):
    """Create a stable key for matching retries of the same API request."""
    try:
        parsed = urlparse(str(raw_url or ""))
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in {"_", "cachebuster", "cache_buster", "timestamp", "ts"}
        ]
        query.sort()
        return urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                "",
                urlencode(query, doseq=True),
                "",
            )
        )
    except Exception:
        return str(raw_url or "").strip().lower()


def _endpoint_label(raw_url):
    try:
        parsed = urlparse(str(raw_url or ""))
        label = parsed.path or raw_url
        if parsed.query:
            label += f"?{parsed.query}"
        return str(label)[:100]
    except Exception:
        return str(raw_url or "")[:100]


def _failure_priority(status):
    value = str(status or "")
    if value.startswith("5"):
        return 4
    if value.startswith("4"):
        return 3
    if "ERR_CONNECTION" in value or "ERR_NAME_NOT_RESOLVED" in value:
        return 2
    return 1


def rollup_api_and_console():
    """Convert captured browser evidence into trustworthy report results.

    A request is reported as failed only when no successful response for the
    same endpoint was observed. Benign ``ERR_ABORTED`` cancellations are kept
    in raw diagnostics and never become defects.
    """
    successes = {}
    for response in REPORTER.successful_requests:
        key = (
            str(response.get("method") or "GET").upper(),
            _normalise_request_url(response.get("request_url")),
        )
        successes.setdefault(key, []).append(response)

    ignored = {}
    for request in REPORTER.ignored_requests:
        key = (
            str(request.get("method") or "GET").upper(),
            _normalise_request_url(request.get("request_url")),
        )
        ignored.setdefault(key, []).append(request)

    failures = {}
    for request in REPORTER.failed_requests:
        key = (
            str(request.get("method") or "GET").upper(),
            _normalise_request_url(request.get("request_url")),
        )
        failures.setdefault(key, []).append(request)

    all_keys = set(failures) | set(ignored)
    for key in sorted(all_keys):
        successful = successes.get(key) or []
        cancelled = ignored.get(key) or []
        failed = failures.get(key) or []

        # A later successful response is stronger evidence than an earlier
        # cancellation or failed retry. Report it as working, not broken.
        if successful:
            best = max(successful, key=lambda item: int(item.get("status") or 0))
            request_url = best.get("request_url") or key[1]
            previous_attempts = len(cancelled) + len(failed)
            REPORTER.record(
                "api",
                f"Data request succeeded: {_endpoint_label(request_url)}",
                best.get("url") or "",
                "The page successfully loads the data it needs",
                (
                    f"The API returned HTTP {best.get('status')}. "
                    f"The browser also cancelled or retried {previous_attempts} earlier "
                    "request(s), but a successful response was received, so the data-load "
                    "test passed."
                ),
                "pass",
                severity="info",
            )
            continue

        # Aborted-only activity is normal during navigation and modal closure.
        # It is retained in JSON diagnostics but omitted from the user-facing
        # test results because it proves neither success nor product failure.
        if cancelled and not failed:
            continue

        if not failed:
            continue

        failure = max(
            failed,
            key=lambda item: _failure_priority(item.get("status")),
        )
        status = str(failure.get("status") or "unknown failure")
        request_url = failure.get("request_url") or key[1]
        try:
            numeric_status = int(status)
        except (TypeError, ValueError):
            numeric_status = None
        if numeric_status in config.BENIGN_REQUEST_STATUSES:
            # Authentication probes and expected authorization denials are
            # validated by dedicated auth/role tests, not reported as generic
            # data-load defects.
            continue
        server_side = status.startswith("5") or any(
            marker in status.upper()
            for marker in (
                "ERR_CONNECTION_REFUSED",
                "ERR_CONNECTION_RESET",
                "ERR_CONNECTION_TIMED_OUT",
                "ERR_NAME_NOT_RESOLVED",
                "ERR_TIMED_OUT",
            )
        )

        REPORTER.record(
            "api",
            f"Data request failed: {_endpoint_label(request_url)}",
            failure.get("url") or "",
            "The page successfully loads the data it needs",
            (
                f"No successful response was observed for this API request. "
                f"The final result was {status}."
            ),
            "fail",
            severity="high" if server_side else "medium",
            repro_steps=[
                f"Open {failure.get('url') or 'the affected page'}",
                f"Wait for {_endpoint_label(request_url)} to finish",
            ],
            suggested_fix=(
                "Check the endpoint, authentication, CORS and server logs. Only real "
                "HTTP or network failures are reported here; browser cancellations are ignored."
            ),
        )

    seen = set()
    for console_error in REPORTER.console_errors:
        text = str(console_error.get("text") or "")
        key = text[:120]
        if key in seen or config.BENIGN_CONSOLE_RE.search(text):
            continue
        seen.add(key)
        REPORTER.record(
            "console",
            f"Hidden fault on {console_error['url'][:60]}",
            console_error["url"],
            "The page runs without internal faults",
            (
                "A fault occurred inside the page that the user never sees directly: "
                f"\"{console_error['text'][:240]}\". These commonly surface later as "
                "blank sections, missing rows, or buttons that stop responding"
            ),
            "fail",
            severity="medium",
            suggested_fix="Fix the underlying fault reported by the page.",
        )


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _write_json_report(summary):
    with open(config.JSON_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "summary": summary,
                "run_state": REPORTER.run_state,
                "results": REPORTER.results,
                "console_errors": REPORTER.console_errors,
                "failed_requests": REPORTER.failed_requests,
                "successful_requests": REPORTER.successful_requests,
                "ignored_requests": REPORTER.ignored_requests,
            },
            f,
            indent=2,
        )


def _verdict(fails):
    blockers = sum(1 for r in fails if r["_sev"] in ("critical", "high"))
    if any(r["_sev"] == "critical" for r in fails):
        return ("verdict-bad", "Not ready to release",
                f"{blockers} issue{'s' if blockers != 1 else ''} must be fixed first - "
                "wrong data can currently be saved or a core task is blocked.")
    if blockers:
        return ("verdict-bad", "Needs work before release",
                f"{blockers} issue{'s' if blockers != 1 else ''} stop someone from completing "
                "their work properly.")
    if fails:
        return ("verdict-warn", "Usable, with rough edges",
                f"{len(fails)} issue{'s' if len(fails) != 1 else ''} found. Nothing blocks a "
                "user, but they'll notice.")
    return ("verdict-good", "Looks good",
            "Every automated check passed on the pages tested.")


def _stat_cards(summary, fails):
    crit = sum(1 for r in fails if r["_sev"] == "critical")
    high = sum(1 for r in fails if r["_sev"] == "high")
    unsure = summary.get("inconclusive", 0)
    unsure_card = (f'<div class="card"><div class="n" style="color:#5b6472">{unsure}</div>'
                   f'<div class="l">Needs a look</div></div>') if unsure else ""
    return f"""<div class="cards">
<div class="card"><div class="n">{summary['total_pages_tested']}</div><div class="l">Pages tested</div></div>
<div class="card"><div class="n">{summary['total_test_cases']}</div><div class="l">Checks run</div></div>
<div class="card"><div class="n ok">{summary['passed']}</div><div class="l">Passed</div></div>
<div class="card"><div class="n bad">{crit + high}</div><div class="l">Must fix</div></div>
<div class="card"><div class="n warn">{summary['failed'] - crit - high}</div><div class="l">Lower priority</div></div>
{unsure_card}
</div>"""


def _area_summary(fails):
    """A director's first question is 'which part of the product is worst?'"""
    if not fails:
        return ""
    areas = {}
    for r in fails:
        area = r["_biz"].get("area") or category_meta(r["category"])["label"]
        a = areas.setdefault(area, {"critical": 0, "high": 0, "medium": 0, "low": 0})
        a[r["_sev"]] = a.get(r["_sev"], 0) + 1
    rows = ""
    for area, c in sorted(areas.items(),
                          key=lambda kv: (-kv[1]["critical"], -kv[1]["high"], -kv[1]["medium"])):
        total = sum(c.values())
        rows += f"""<tr><td class="area-name">{esc(area)}</td>
        <td>{'<span class="dot crit">' + str(c['critical']) + '</span>' if c['critical'] else ''}
            {'<span class="dot high">' + str(c['high']) + '</span>' if c['high'] else ''}
            {'<span class="dot med">' + str(c['medium']) + '</span>' if c['medium'] else ''}
            {'<span class="dot low">' + str(c['low']) + '</span>' if c['low'] else ''}</td>
        <td class="area-total">{total}</td></tr>"""
    return f"""<h2>Where the problems are</h2>
    <table class="area-table"><tr><th>Part of the product</th><th>Issues by urgency</th>
    <th>Total</th></tr>{rows}</table>"""


def _issue_card(r, origin):
    biz = r["_biz"]
    sev = severity_meta(r["_sev"])
    cat = category_meta(r["category"])
    page = display_page(r["page_url"], origin)

    shot = ""
    if r["screenshot"]:
        shot = (f'<img class="thumb" src="{esc(r["screenshot"])}" alt="What this looks like" '
                f'loading="lazy" onclick="zoom(this.src)">')

    why = (f'<div class="why"><span class="lbl">Why it matters</span>{esc(biz["why_it_matters"])}</div>'
           if biz.get("why_it_matters") else "")
    fix = (f'<div class="fix"><span class="lbl">What to do</span>{esc(biz["fix"])}</div>'
           if biz.get("fix") else "")
    conf = ("" if biz.get("confidence") in (None, "high")
            else f'<span class="chip chip-conf">needs confirming</span>')

    steps = "".join(f"<li>{esc(s)}</li>" for s in r["repro_steps"])
    steps_html = f'<div class="steps"><span class="lbl">How to see it</span><ol>{steps}</ol></div>' \
        if steps else ""

    return f"""<div class="issue" style="--sev:{sev['color']}">
  <div class="issue-top">
    <span class="status-chip status-fail">âœ• FAILED</span>
    <span class="chip" style="background:{sev['bg']};color:{sev['color']}">{sev['label']}</span>
    <span class="chip chip-cat">{cat['emoji']} {esc(biz.get('area') or cat['label'])}</span>
    {conf}
    <span class="page-tag">{esc(page)}</span>
  </div>
  <h3>{esc(biz['headline'])}</h3>
  <p class="what">{esc(biz.get('what_happens') or '')}</p>
  {why}
  {shot}
  {fix}
  {steps_html}
  <details class="tech"><summary>Technical detail for developers ({r['test_id']})</summary>
    <table>
      <tr><th>Check</th><td>{esc(r['title'])}</td></tr>
      <tr><th>Page</th><td>{esc(r['page_url'])}</td></tr>
      <tr><th>Expected</th><td>{esc(r['expected'])}</td></tr>
      <tr><th>Observed</th><td>{esc(r['actual'])}</td></tr>
      <tr><th>Original rating</th><td>{r['severity']} ({r['category']})</td></tr>
    </table>
  </details>
</div>"""


def _issues_section(fails, origin):
    if not fails:
        return ('<div class="all-good"><b>No issues found.</b> Every automated check passed on '
                'the pages that were tested.</div>')
    out = []
    for key, heading, blurb in SEV_BUCKETS:
        items = [r for r in fails if r["_sev"] == key]
        if not items:
            continue
        meta = severity_meta(key)
        cards = "".join(_issue_card(r, origin) for r in items)
        out.append(
            f'<h2 class="bucket" style="border-left-color:{meta["color"]}">{heading}'
            f'<span class="count">{len(items)}</span></h2>'
            f'<p class="bucket-blurb">{blurb}</p>'
            f'<div class="grid">{cards}</div>')
    return "".join(out)


def _validation_results_section(results, origin):
    # Show every form-validation oracle with an explicit status badge.
    workflow_titles = {
        "Create Facility form opens",
        "Clear Facility form",
        "Charges can be added to a facility",
        "Valid facility can reach Review",
        "Review state fields are read-only",
        "Send for Approval is available after Review",
    }
    items = [
        result
        for result in results
        if result.get("category") == "validation"
        or result.get("title") in workflow_titles
    ]
    if not items:
        return ""

    status_meta = {
        "pass": ("âœ“ PASSED", "status-pass"),
        "fail": ("âœ• FAILED", "status-fail"),
        "inconclusive": ("? NOT CONFIRMED", "status-unsure"),
        "skipped": ("â€“ NOT TESTED", "status-skipped"),
    }
    cards = []
    for result in items:
        label, css_class = status_meta.get(
            result.get("status"),
            (str(result.get("status") or "UNKNOWN").upper(), "status-unsure"),
        )
        page = display_page(result.get("page_url") or "", origin)
        shot = ""
        if result.get("screenshot"):
            shot = (
                f'<a class="validation-shot" href="{esc(result["screenshot"])}" '
                'target="_blank">View evidence</a>'
            )
        cards.append(
            f'''<article class="validation-card validation-{esc(result.get("status") or "unknown")}">
  <div class="validation-card-top">
    <span class="status-chip {css_class}">{label}</span>
    <span class="test-id">{esc(result.get("test_id"))}</span>
    <span class="page-tag">{esc(page)}</span>
  </div>
  <h3>{esc(result.get("title"))}</h3>
  <div class="validation-line"><span>Expected</span>{esc(result.get("expected"))}</div>
  <div class="validation-line"><span>Observed</span>{esc(result.get("actual"))}</div>
  {shot}
</article>'''
        )

    passed = sum(1 for item in items if item.get("status") == "pass")
    failed = sum(1 for item in items if item.get("status") == "fail")
    other = len(items) - passed - failed
    return f'''<section class="validation-results">
<h2>Facility form validation and Review workflow results</h2>
<p class="validation-summary"><b>{passed} passed</b> Â· <b>{failed} failed</b> Â· <b>{other} not confirmed/not tested</b></p>
<div class="validation-grid">{"".join(cards)}</div>
</section>'''


def _passed_section(passes):
    if not passes:
        return ""
    by_area = {}
    for r in passes:
        by_area.setdefault(category_meta(r["category"])["label"], []).append(r)
    rows = "".join(
        f'<li><b>{esc(area)}</b> â€” {len(items)} check{"s" if len(items) != 1 else ""} passed</li>'
        for area, items in sorted(by_area.items(), key=lambda kv: -len(kv[1])))
    return f"""<details class="block"><summary>What was verified as working ({len(passes)} checks)</summary>
    <ul class="clean">{rows}</ul></details>"""


def _skipped_section(skipped):
    if not skipped:
        return ""
    items = "".join(f'<li><b>{esc(category_meta(r["category"])["label"])}:</b> '
                    f'{esc(r["title"])} â€” {esc(r["actual"])}</li>' for r in skipped)
    return f"""<details class="block"><summary>Not tested ({len(skipped)}) â€” gaps in this run,
    not defects</summary><ul class="clean">{items}</ul></details>"""


def _inconclusive_section(items):
    """Things worth a human look that the tool could NOT prove either way.

    Kept rigorously out of the defect list. Reporting an unproven suspicion as a
    confirmed bug is what makes a whole report untrustworthy.
    """
    if not items:
        return ""
    rows = "".join(
        f'<li><b>{esc(category_meta(r["category"])["label"])}</b> â€” {esc(r["title"])}'
        f'<div class="unsure-detail">{esc(r["actual"][:400])}</div></li>' for r in items)
    return f"""<details class="block unsure"><summary>Worth a manual look ({len(items)}) â€”
    could not be confirmed automatically, NOT counted as defects</summary>
    <p class="bucket-blurb">The tester interacted with these but the evidence did not prove
    whether they work or not. They are listed so nothing is silently dropped.</p>
    <ul class="clean">{rows}</ul></details>"""


def _technical_appendix(by_cat):
    def rows(items):
        out = []
        for r in items:
            repro = "<br>".join(f"{i+1}. {esc(s)}" for i, s in enumerate(r["repro_steps"]))
            shot = (f"<a href='{esc(r['screenshot'])}' target='_blank'>view</a>"
                    if r["screenshot"] else "")
            out.append(f"""<tr>
              <td>{r['test_id']}</td><td>{esc(r['title'])}</td>
              <td><span class="status-chip status-{'pass' if r['status'] == 'pass' else 'fail' if r['status'] == 'fail' else 'skipped' if r['status'] == 'skipped' else 'unsure'}">{'âœ“ PASSED' if r['status'] == 'pass' else 'âœ• FAILED' if r['status'] == 'fail' else 'â€“ NOT TESTED' if r['status'] == 'skipped' else '? NOT CONFIRMED'}</span></td>
              <td><span class="pill" style="background:{severity_meta(r['severity'])['color']}">{r['severity']}</span></td>
              <td class="small">{esc(r['page_url'])}</td>
              <td>{esc(r['expected'])}</td><td>{esc(r['actual'])}</td>
              <td class="small">{repro}</td><td>{shot}</td><td>{esc(r['suggested_fix'])}</td></tr>""")
        return "".join(out)

    order = ["auth", "security", "role_access", "crash", "functional", "validation",
             "usability", "consistency", "ui", "api", "console", "navigation"]
    body = ""
    for cat in order + [c for c in by_cat if c not in order]:
        items = by_cat.get(cat, [])
        if not items:
            continue
        fails = sum(1 for i in items if i["status"] == "fail")
        body += f"""<details><summary>{category_meta(cat)['label']}
        ({len(items)} checks, {fails} issues)</summary>
        <table><tr><th>ID</th><th>Check</th><th>Status</th><th>Severity</th><th>Page</th>
        <th>Expected</th><th>Observed</th><th>Steps</th><th>Shot</th><th>Fix</th></tr>
        {rows(items)}</table></details>"""
    return f"""<details class="appendix"><summary>Full technical test log (QA / engineering)</summary>
    {body}</details>"""


def generate_reports():
    counts = REPORTER.counts()
    by_cat = {}
    for r in REPORTER.results:
        by_cat.setdefault(r["category"], []).append(r)

    summary = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "website": config.URL,
        "total_pages_tested": len(REPORTER.pages_tested),
        "total_test_cases": len(REPORTER.results),
        "passed": counts.get("pass", 0),
        "failed": counts.get("fail", 0),
        "inconclusive": counts.get("inconclusive", 0),
        "skipped": counts.get("skipped", 0),
        "pages": sorted(REPORTER.pages_tested),
        "pipeline_status": REPORTER.run_state.get("status", "unknown"),
        "current_stage": REPORTER.run_state.get("current_stage", ""),
        "pipeline_errors": len(REPORTER.run_state.get("pipeline_errors", [])),
    }
    _write_json_report(summary)

    fails = [r for r in REPORTER.results if r["status"] == "fail"]
    passes = [r for r in REPORTER.results if r["status"] == "pass"]
    skipped = [r for r in REPORTER.results if r["status"] == "skipped"]
    unsure = [r for r in REPORTER.results if r["status"] == "inconclusive"]

    if fails:
        print(f"\nPreparing the report for {len(fails)} finding(s)...")
    business = translate_findings(fails)
    for r in fails:
        r["_biz"] = business.get(r["test_id"]) or {"headline": r["title"],
                                                   "what_happens": r["actual"]}
        technical_severity = (
            r["severity"] if r["severity"] in SEVERITY_ORDER else "medium"
        )
        proposed_severity = (
            r["_biz"].get("business_severity") or technical_severity
        )
        if proposed_severity not in SEVERITY_ORDER:
            proposed_severity = technical_severity

        # The plain-language report may explain or downgrade a finding, but it
        # must never escalate a measured medium issue into Critical/High.
        if SEVERITY_ORDER[proposed_severity] < SEVERITY_ORDER[technical_severity]:
            proposed_severity = technical_severity
        r["_sev"] = proposed_severity
    fails.sort(key=lambda r: (SEVERITY_ORDER.get(r["_sev"], 9),
                              r["_biz"].get("area") or ""))

    origin = urlparse(config.URL).scheme + "://" + urlparse(config.URL).netloc
    vclass, vtitle, vtext = _verdict(fails)
    if summary["pipeline_status"] != "completed":
        vclass = "verdict-warn"
        vtitle = "Test run did not fully complete"
        vtext = (
            f"Pipeline status: {summary['pipeline_status']}. Results collected before the "
            "interruption are preserved, but unexecuted checks must not be treated as passed."
        )
    pages_list = "".join(f"<li>{esc(display_page(p, origin))}</li>" for p in summary["pages"])

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Website Test Report</title><style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;margin:0;padding:24px;
  background:#eef1f5;color:#1a1d21;line-height:1.5}}
.wrap{{max-width:1100px;margin:auto;background:#fff;padding:32px;border-radius:12px;
  box-shadow:0 1px 3px rgba(0,0,0,.08)}}
h1{{margin:0 0 4px;font-size:26px}}
.sub{{color:#667;font-size:14px;margin-bottom:22px}}
.verdict{{padding:18px 22px;border-radius:10px;margin-bottom:22px}}
.verdict h2{{margin:0 0 4px;font-size:20px}} .verdict p{{margin:0;font-size:14px}}
.verdict-bad{{background:#fdecea;color:#7b1113}}
.verdict-warn{{background:#fdf4e3;color:#8a5300}}
.verdict-good{{background:#eaf7ec;color:#1b6b30}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:26px}}
.card{{flex:1;min-width:120px;background:#f7f9fb;border:1px solid #e5e9ef;border-radius:10px;
  padding:14px;text-align:center}}
.card .n{{font-size:28px;font-weight:700}} .card .l{{font-size:11px;text-transform:uppercase;
  letter-spacing:.04em;color:#7a8494}}
.n.ok{{color:#1b6b30}} .n.bad{{color:#b3261e}} .n.warn{{color:#8a5300}}
h2{{font-size:19px;margin:30px 0 10px}}
.area-table{{width:100%;border-collapse:collapse;margin-bottom:8px}}
.area-table th{{text-align:left;font-size:10px;text-transform:uppercase;color:#7a8494;
  padding:6px 8px;border-bottom:1px solid #e5e9ef}}
.area-table td{{padding:9px 8px;border-bottom:1px solid #f0f2f5;font-size:14px}}
.area-name{{font-weight:600}} .area-total{{color:#7a8494;width:60px}}
.dot{{display:inline-block;min-width:22px;padding:1px 7px;border-radius:11px;color:#fff;
  font-size:11px;font-weight:700;margin-right:4px}}
.dot.crit{{background:#7b1113}} .dot.high{{background:#b3261e}}
.dot.med{{background:#9a5b00}} .dot.low{{background:#8a6d00}}
h2.bucket{{border-left:4px solid #999;padding-left:12px;display:flex;align-items:center;gap:10px}}
.count{{background:#eef1f5;color:#444;border-radius:12px;padding:1px 11px;font-size:13px;
  font-weight:600}}
.bucket-blurb{{margin:0 0 14px 16px;color:#667;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}}
.issue{{border:1px solid #e5e9ef;border-left:5px solid var(--sev);border-radius:10px;padding:16px 18px}}
.issue-top{{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:10px}}
.chip{{font-size:11px;font-weight:600;padding:3px 9px;border-radius:12px}}
.chip-cat{{background:#eef1f5;color:#445}}
.chip-conf{{background:#fff4e5;color:#8a5300}}
.status-chip{{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:999px;
  font-size:11px;font-weight:800;letter-spacing:.02em;white-space:nowrap}}
.status-pass{{background:#e4f6e8;color:#176b2c;border:1px solid #9dd5a8}}
.status-fail{{background:#fdeceb;color:#9b1c1c;border:1px solid #f2aaa5}}
.status-unsure{{background:#fff5df;color:#845400;border:1px solid #edcf8a}}
.status-skipped{{background:#eef1f5;color:#667085;border:1px solid #d6dbe3}}
.validation-results{{margin-top:28px;padding-top:4px}}
.validation-summary{{margin:0 0 14px;color:#667;font-size:14px}}
.validation-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}}
.validation-card{{border:1px solid #e5e9ef;border-left:5px solid #98a2b3;border-radius:10px;
  padding:15px;background:#fff}}
.validation-card.validation-pass{{border-left-color:#2e8b45;background:#fbfffc}}
.validation-card.validation-fail{{border-left-color:#b3261e;background:#fffdfd}}
.validation-card.validation-inconclusive{{border-left-color:#b7791f}}
.validation-card-top{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:9px}}
.validation-card .page-tag{{margin-left:auto}}
.validation-card h3{{font-size:15px;margin:0 0 10px;line-height:1.35}}
.validation-line{{font-size:12px;color:#445;margin-top:7px;padding-top:7px;border-top:1px solid #f0f2f5}}
.validation-line span{{display:block;color:#7a8494;font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.04em;margin-bottom:2px}}
.test-id{{font:11px ui-monospace,monospace;color:#7a8494}}
.validation-shot{{display:inline-block;margin-top:10px;font-size:12px;font-weight:600;color:#175cd3}}
.page-tag{{margin-left:auto;font-size:11px;color:#8a94a4;font-family:ui-monospace,monospace}}
.issue h3{{margin:0 0 8px;font-size:16px;line-height:1.35}}
.what{{margin:0 0 10px;font-size:14px}}
.lbl{{display:block;font-size:10px;text-transform:uppercase;letter-spacing:.05em;
  color:#7a8494;font-weight:700;margin-bottom:3px}}
.why{{background:#f7f9fb;border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:10px}}
.fix{{background:#eef5ff;border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:10px}}
.steps{{font-size:13px;margin-bottom:8px}}
.steps ol{{margin:4px 0 0;padding-left:20px;color:#445}}
.thumb{{width:100%;max-height:180px;object-fit:cover;object-position:top;border-radius:8px;
  border:1px solid #e5e9ef;cursor:zoom-in;display:block;margin-bottom:10px}}
.tech summary{{cursor:pointer;font-size:12px;color:#8a94a4}}
.tech table{{width:100%;font-size:12px;margin-top:8px;border-collapse:collapse}}
.tech th{{text-align:left;color:#7a8494;padding:4px 10px 4px 0;vertical-align:top;
  white-space:nowrap;font-weight:600}}
.tech td{{padding:4px 0;word-break:break-word;white-space:pre-wrap}}
.all-good{{background:#eaf7ec;color:#1b6b30;padding:20px;border-radius:10px}}
details.block{{margin-top:22px;background:#f7f9fb;border:1px solid #e5e9ef;border-radius:10px;
  padding:12px 18px}}
details.block>summary{{cursor:pointer;font-weight:600;color:#445}}
details.unsure{{background:#f6f7f9;border-left:4px solid #98a2b3}}
.unsure-detail{{font-size:12px;color:#667;margin:3px 0 10px}}
ul.clean{{font-size:13px;color:#445}}
ul.clean li{{margin-bottom:6px}}
details.appendix{{margin-top:30px;border-top:2px solid #e5e9ef;padding-top:14px}}
details.appendix>summary{{cursor:pointer;font-weight:700;font-size:15px;color:#445}}
details.appendix details{{margin-top:14px}}
details.appendix summary{{cursor:pointer;font-weight:600;color:#556}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{padding:8px;border-bottom:1px solid #f0f2f5;text-align:left;font-size:12px;
  vertical-align:top}}
th{{background:#f7f9fb;font-size:10px;text-transform:uppercase;color:#7a8494}}
.pass{{color:#1b6b30;font-weight:700}} .fail{{color:#b3261e;font-weight:700}}
.skipped{{color:#8a94a4;font-weight:700}}
.small{{font-size:11px;color:#667;word-break:break-all}}
.pill{{color:#fff;padding:2px 8px;border-radius:10px;font-size:10px}}
#lb{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:99;
  align-items:center;justify-content:center;cursor:zoom-out;padding:20px}}
#lb img{{max-width:100%;max-height:100%;border-radius:8px}}
@media(max-width:640px){{body{{padding:10px}}.wrap{{padding:18px}}.grid,.validation-grid{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<h1>Website Test Report</h1>
<div class="sub">{esc(config.URL)} &middot; {summary['generated'].replace('T', ' at ')}
 &middot; Pipeline: {esc(summary['pipeline_status'])}</div>
<div class="verdict {vclass}"><h2>{vtitle}</h2><p>{vtext}</p></div>
{_stat_cards(summary, fails)}
{_area_summary(fails)}
{_validation_results_section(REPORTER.results, origin)}
{_issues_section(fails, origin)}
{_passed_section(passes)}
{_inconclusive_section(unsure)}
{_skipped_section(skipped)}
<details class="block"><summary>Pages covered by this run ({len(summary['pages'])})</summary>
<ul class="clean">{pages_list}</ul></details>
{_technical_appendix(by_cat)}
</div>
<div id="lb" onclick="this.style.display='none'"><img id="lbi" alt=""></div>
<script>
function zoom(s){{document.getElementById('lbi').src=s;document.getElementById('lb').style.display='flex';}}
addEventListener('keydown',e=>{{if(e.key==='Escape')document.getElementById('lb').style.display='none';}});
</script>
</body></html>"""
    with open(config.HTML_REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nHTML report: {config.HTML_REPORT_PATH}\nJSON report: {config.JSON_REPORT_PATH}")
