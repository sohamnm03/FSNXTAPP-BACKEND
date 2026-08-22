"""Measures what actually changed on the page after an interaction.

This exists because the tester used to *assume* outcomes. It asked Claude "why is
clicking this useful?", got back a business promise ("sorts the list by name"),
and then reported that promise as failed without ever measuring whether the list
re-ordered. Combined with a stray console warning flipping the verdict, working
features were reported as broken.

Everything here is measurement. We capture a signature of the page before an
interaction and after it, diff them, and let the diff decide the verdict. When
the diff cannot distinguish working from broken, the answer is INCONCLUSIVE -
never "broken". A tester that says "I could not confirm this" is useful; one that
cries wolf is not.
"""
from ai_agents import config

# Verdicts. INCONCLUSIVE is deliberately first-class: it is the honest answer
# whenever the evidence does not separate a working feature from a broken one.
PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"

_CAPTURE_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const hash = s => { let h = 5381; for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0; return h; };
  const vis = el => {
    try {
      if (el.checkVisibility && !el.checkVisibility()) return false;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return false;
      const s = getComputedStyle(el);
      return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity) > 0.05;
    } catch (e) { return false; }
  };
  const cls = el => (typeof el.className === 'string' ? el.className : '');

  // ---- Result regions: tables, aria grids, and repeated card lists --------
  // rowKeys preserve ORDER, which is what makes a sort provable.
  const regions = [];
  const pushRows = (kind, key, rowEls) => {
    const rows = rowEls.filter(vis);
    if (!rows.length) return;
    regions.push({
      kind, key,
      rowCount: rows.length,
      rowKeys: rows.slice(0, 80).map(tr => {
        const cells = Array.from(tr.children || []).slice(0, 4).map(c => norm(c.innerText));
        return (cells.join(' | ') || norm(tr.innerText)).slice(0, 140);
      }),
      firstCell: rows.slice(0, 80).map(tr => {
        const c = (tr.children || [])[0];
        return norm(c ? c.innerText : tr.innerText).slice(0, 70);
      }),
    });
  };

  Array.from(document.querySelectorAll('table')).forEach((t, i) => {
    if (!vis(t)) return;
    let rows = Array.from(t.querySelectorAll('tbody tr'));
    if (!rows.length) rows = Array.from(t.querySelectorAll('tr')).slice(1);
    pushRows('table', 'table#' + i, rows);
  });

  Array.from(document.querySelectorAll('[role="grid"], [role="table"], [role="rowgroup"]'))
    .forEach((g, i) => {
      if (!vis(g)) return;
      pushRows('grid', 'grid#' + i, Array.from(g.querySelectorAll('[role="row"]')));
    });

  // Card/list layouts: a container whose children repeat the same shape.
  Array.from(document.querySelectorAll('ul, ol, [class*="list"], [class*="List"]'))
    .slice(0, 40).forEach((c, i) => {
      if (!vis(c)) return;
      const kids = Array.from(c.children).filter(vis);
      if (kids.length < 3) return;
      const tags = new Set(kids.map(k => k.tagName));
      if (tags.size > 2) return;                       // not a uniform list
      if (norm(c.innerText).length < 12) return;
      pushRows('list', 'list#' + i, kids);
    });

  // ---- Overlay / modal detection ------------------------------------------
  // Structural, not framework-specific: any positioned, sizeable, visible layer
  // holding real content. This catches Radix/Headless/Chakra/tailwind/custom
  // portals that a hard-coded class list misses.
  let candidates = [];
  Array.from(document.body.querySelectorAll('*')).slice(0, 4000).forEach(el => {
    let s;
    try { s = getComputedStyle(el); } catch (e) { return; }
    if (s.position !== 'fixed' && s.position !== 'absolute') return;
    if (!vis(el)) return;
    const r = el.getBoundingClientRect();
    if (r.width * r.height < 15000) return;            // too small for a dialog
    const text = norm(el.innerText);
    const focusables = el.querySelectorAll(
      'button, a, input, select, textarea, [role="button"], [tabindex]').length;
    // A bare backdrop covers everything and holds nothing - not a dialog.
    const coversAll = r.width >= innerWidth * 0.97 && r.height >= innerHeight * 0.97;
    if (coversAll && text.length < 10 && focusables === 0) return;
    const z = parseInt(s.zIndex, 10) || 0;
    const dialogish = el.getAttribute('role') === 'dialog' ||
      el.getAttribute('role') === 'alertdialog' ||
      el.getAttribute('aria-modal') === 'true' ||
      el.getAttribute('data-state') === 'open' ||
      /modal|dialog|drawer|sheet|popup|popover|offcanvas|lightbox/i.test(cls(el)) ||
      !!el.closest('[data-radix-portal], [data-headlessui-portal]');
    if (!dialogish && z < 10) return;
    if (focusables === 0 && text.length < 4) return;
    candidates.push({ el, z, area: r.width * r.height, text, focusables, dialogish });
  });
  // Keep the innermost panels (drop a wrapper that contains another candidate).
  const overlays = candidates
    .filter(c => !candidates.some(o => o !== c && c.el.contains(o.el)))
    .slice(0, 6)
    .map(c => ({
      z: c.z, area: Math.round(c.area), focusables: c.focusables,
      dialogish: c.dialogish,
      textHash: hash(c.text), textLen: c.text.length,
      label: c.text.slice(0, 80),
    }));

  // ---- Page-level signatures ---------------------------------------------
  const bodyText = norm(document.body.innerText);
  // Text outside any overlay, so opening a dialog doesn't look like the page changed.
  let baseText = bodyText;
  if (candidates.length) {
    const clone = document.body.cloneNode(true);
    // Can't map cloned nodes back; approximate by subtracting overlay text.
    candidates.forEach(c => { baseText = baseText.split(c.text).join(''); });
    baseText = norm(baseText);
  }
  const active = document.activeElement;

  return {
    url: location.href,
    title: document.title,
    regions,
    overlays,
    overlayCount: overlays.length,
    bodyTextHash: hash(bodyText), bodyTextLen: bodyText.length,
    baseTextHash: hash(baseText), baseTextLen: baseText.length,
    focused: active ? (active.tagName || '').toLowerCase() +
      (active.getAttribute && active.getAttribute('name') ? '[' + active.getAttribute('name') + ']' : '') : null,
    scrollY: Math.round(window.scrollY),
    formFieldCount: document.querySelectorAll('input, select, textarea').length,
  };
}
"""


def capture(page):
    """Signature of the page right now. Returns {} if it can't be taken."""
    try:
        return page.evaluate(_CAPTURE_JS)
    except Exception as e:
        print(f"    [evidence] capture failed: {e}")
        return {}


def wait_for_settle(page, quiet_ms=450, timeout_ms=6000):
    """Wait until the DOM stops changing, instead of sleeping a fixed amount.

    A fixed sleep is what made animated modals and slow fetches look like
    "nothing happened". This returns as soon as the page is genuinely stable.
    """
    try:
        page.wait_for_function(
            """
            (quiet) => {
              const w = window;
              if (!w.__qaSettle) {
                w.__qaSettle = {last: Date.now(), n: 0};
                const obs = new MutationObserver(() => {
                  w.__qaSettle.last = Date.now();
                  w.__qaSettle.n++;
                });
                obs.observe(document.documentElement,
                  {childList: true, subtree: true, attributes: true, characterData: true});
                w.__qaSettleObs = obs;
                return false;
              }
              return (Date.now() - w.__qaSettle.last) >= quiet;
            }
            """,
            arg=quiet_ms, timeout=timeout_ms,
        )
    except Exception:
        pass  # settled or timed out - either way we measure what's there now
    finally:
        try:
            page.evaluate("""() => {
              if (window.__qaSettleObs) { window.__qaSettleObs.disconnect();
                delete window.__qaSettleObs; delete window.__qaSettle; }
            }""")
        except Exception:
            pass


def _region_map(sig):
    return {r["key"]: r for r in (sig.get("regions") or [])}


def diff(before, after):
    """What measurably changed. Every key is a fact, not an inference."""
    d = {
        "captured": bool(before) and bool(after),
        "url_changed": False, "new_url": None,
        "overlay_opened": False, "overlay_closed": False, "overlay_label": None,
        "rows_reordered": False, "row_count_changed": False,
        "rows_before": None, "rows_after": None,
        "row_content_changed": False,
        "reorder_example": None,
        "page_text_changed": False, "base_text_changed": False,
        "field_count_changed": False,
        "any_change": False,
    }
    if not d["captured"]:
        return d

    if before.get("url") != after.get("url"):
        d["url_changed"] = True
        d["new_url"] = after.get("url")

    b_ov, a_ov = before.get("overlayCount", 0), after.get("overlayCount", 0)
    if a_ov > b_ov:
        d["overlay_opened"] = True
        tops = sorted(after.get("overlays") or [], key=lambda o: -o.get("area", 0))
        if tops:
            d["overlay_label"] = tops[0].get("label")
    elif a_ov < b_ov:
        d["overlay_closed"] = True

    bm, am = _region_map(before), _region_map(after)
    for key, br in bm.items():
        ar = am.get(key)
        if not ar:
            continue
        bk, ak = br.get("rowKeys") or [], ar.get("rowKeys") or []
        if br.get("rowCount") != ar.get("rowCount"):
            d["row_count_changed"] = True
            d["rows_before"], d["rows_after"] = br.get("rowCount"), ar.get("rowCount")
        elif bk and ak and bk != ak:
            # Same rows, different order == a sort demonstrably happened.
            if sorted(bk) == sorted(ak):
                d["rows_reordered"] = True
                bf = br.get("firstCell") or []
                af = ar.get("firstCell") or []
                if bf and af and bf[0] != af[0]:
                    d["reorder_example"] = (bf[0], af[0])
            else:
                d["row_content_changed"] = True

    if before.get("bodyTextHash") != after.get("bodyTextHash") or \
            before.get("bodyTextLen") != after.get("bodyTextLen"):
        d["page_text_changed"] = True
    if before.get("baseTextHash") != after.get("baseTextHash") or \
            before.get("baseTextLen") != after.get("baseTextLen"):
        d["base_text_changed"] = True
    if before.get("formFieldCount") != after.get("formFieldCount"):
        d["field_count_changed"] = True

    d["any_change"] = any(d[k] for k in (
        "url_changed", "overlay_opened", "overlay_closed", "rows_reordered",
        "row_count_changed", "row_content_changed", "page_text_changed",
        "base_text_changed", "field_count_changed"))
    return d


def describe(d):
    """Plain-language statement of the measured change, for the report."""
    if not d.get("captured"):
        return "the page could not be measured before and after this action"
    bits = []
    if d["url_changed"]:
        bits.append(f"moved to a new page ({d['new_url']})")
    if d["overlay_opened"]:
        label = (d.get("overlay_label") or "").strip()
        bits.append(f"opened a popup{' titled ' + repr(label[:40]) if label else ''}")
    if d["overlay_closed"]:
        bits.append("closed the popup")
    if d["rows_reordered"]:
        ex = d.get("reorder_example")
        bits.append("re-ordered the list" + (
            f" (first entry changed from '{ex[0][:32]}' to '{ex[1][:32]}')" if ex else ""))
    if d["row_count_changed"]:
        bits.append(f"changed the number of rows shown from {d['rows_before']} to {d['rows_after']}")
    if d["row_content_changed"]:
        bits.append("replaced the rows with different data")
    if not bits and d["base_text_changed"]:
        bits.append("updated the content on the page")
    elif not bits and d["page_text_changed"]:
        bits.append("updated something on screen")
    if not bits:
        bits.append("produced no measurable change at all "
                    "(same address, same popups, same rows, same page text)")
    return ", ".join(bits)


# What each kind of control is supposed to demonstrably do. Used to derive the
# expected outcome from the CONTROL, never from an unverified LLM promise.
_EXPECTATION = {
    "sort":       ("re-order the list", ("rows_reordered", "row_content_changed", "row_count_changed")),
    "filter":     ("change which rows are listed", ("row_count_changed", "row_content_changed", "rows_reordered")),
    "pagination": ("show a different page of results", ("row_content_changed", "row_count_changed", "url_changed")),
    "open_modal": ("open a form or popup", ("overlay_opened",)),
    "nav":        ("open another page or section", ("url_changed", "base_text_changed", "row_content_changed")),
    "tab":        ("switch to a different section of the page", ("base_text_changed", "row_content_changed", "row_count_changed", "url_changed")),
    "toggle":     ("change what is shown", ("base_text_changed", "row_count_changed", "row_content_changed", "overlay_opened")),
    "button":     ("do something visible", None),
}


def expectation_for(kind):
    label, _ = _EXPECTATION.get(kind or "button", _EXPECTATION["button"])
    return f"Clicking it should {label}"


def classify(kind, d):
    """Turn a measured diff into a verdict.

    Returns (verdict, evidence_sentence). The rules are deliberately
    conservative: we only say FAIL when nothing whatsoever changed, and we say
    INCONCLUSIVE when something changed but not the specific thing this kind of
    control implies - because "not what I predicted" is not the same as "broken".
    """
    evidence = describe(d)
    if not d.get("captured"):
        return INCONCLUSIVE, evidence

    _, keys = _EXPECTATION.get(kind or "button", _EXPECTATION["button"])

    if not d["any_change"]:
        return FAIL, evidence
    if keys is None:
        return PASS, evidence          # any visible change satisfies a plain button
    if any(d.get(k) for k in keys):
        return PASS, evidence
    return INCONCLUSIVE, (
        f"{evidence} - this is a visible response, but not the "
        f"{_EXPECTATION.get(kind or 'button', _EXPECTATION['button'])[0]} that this "
        "control implies, so it could not be confirmed either way")


def is_benign_console(text):
    """Noise that says nothing about whether the feature under test works.

    Attributing these to a click is how a working button gets reported broken.
    """
    return bool(config.BENIGN_CONSOLE_RE.search(text or ""))

