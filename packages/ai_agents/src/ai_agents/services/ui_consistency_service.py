"""Visual consistency & usability checks.

Everything here is measured from the real rendered page (computed styles, real
geometry, real hit-testing) rather than guessed, so these findings are facts,
not opinions:

  - text styling that is inconsistent across the page (too many fonts, sizes,
    weights; near-duplicate sizes like 13px vs 13.5px that look like mistakes)
  - colour palette inconsistency (near-duplicate greys/brand colours)
  - buttons that look alike but are styled differently, and buttons that look
    different but do the same thing
  - text that is too faint to read comfortably (WCAG contrast)
  - controls that LOOK clickable but aren't, and controls that are covered
  - text that is visually cut off / truncated
  - tap targets too small to hit reliably
"""
import json

from ai_agents import config
from ai_agents.prompts import load as load_prompt
from ai_agents.services.reporter_service import REPORTER, screenshot
from ai_agents.services.claude_service import safe_ask_json

UI_CONSISTENCY_PROMPT = load_prompt("ui_consistency")

_STYLE_AUDIT_JS = r"""
() => {
  const parseColor = (c) => {
    if (!c) return null;
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x.trim()));
    return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1};
  };
  const relLum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const contrast = (a, b) => {
    const L1 = Math.max(relLum(a), relLum(b)), L2 = Math.min(relLum(a), relLum(b));
    return (L1 + 0.05) / (L2 + 0.05);
  };
  const effectiveBg = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = parseColor(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.1) return c;
      n = n.parentElement;
    }
    return {r: 255, g: 255, b: 255, a: 1};
  };
  const blend = (fg, bg) => fg.a >= 1 ? fg : {
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1
  };
  const rgbStr = (c) => `rgb(${Math.round(c.r)}, ${Math.round(c.g)}, ${Math.round(c.b)})`;
  const visible = (el) => {
    if (el.checkVisibility && !el.checkVisibility()) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity) > 0.05;
  };
  // Only elements that render their own text (not wrapper containers).
  const ownText = (el) => Array.from(el.childNodes)
      .filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();

  const fonts = {}, sizes = {}, weights = {}, textColors = {}, bgColors = {}, radii = {};
  const lowContrast = [], truncated = [], tinyTargets = [], fakeDisabled = [],
        covered = [], buttons = [], headings = [];

  const all = Array.from(document.body.querySelectorAll('*')).filter(visible).slice(0, 2500);

  for (const el of all) {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const text = ownText(el);
    const tag = el.tagName.toLowerCase();

    if (text) {
      const fam = (s.fontFamily || '').split(',')[0].replace(/["']/g, '').trim();
      if (fam) fonts[fam] = (fonts[fam] || 0) + 1;
      sizes[s.fontSize] = (sizes[s.fontSize] || 0) + 1;
      weights[s.fontWeight] = (weights[s.fontWeight] || 0) + 1;

      const fg0 = parseColor(s.color);
      if (fg0) {
        const bg = effectiveBg(el);
        const fg = blend(fg0, bg);
        textColors[rgbStr(fg)] = (textColors[rgbStr(fg)] || 0) + 1;
        const px = parseFloat(s.fontSize);
        const bold = parseFloat(s.fontWeight) >= 700;
        const large = px >= 24 || (px >= 18.66 && bold);
        const ratio = contrast(fg, bg);
        const needed = large ? 3.0 : 4.5;
        // A switched-off control is SUPPOSED to look faint - exempt it, or every
        // greyed-out button gets reported as unreadable text.
        const inDisabled = !!(el.closest('[disabled], [aria-disabled="true"]') ||
                              el.matches('[disabled], [aria-disabled="true"]'));
        if (ratio < needed && text.length > 1 && !inDisabled) {
          lowContrast.push({
            text: text.slice(0, 60), ratio: Math.round(ratio * 100) / 100,
            needed, fontSize: s.fontSize, color: rgbStr(fg), background: rgbStr(bg)
          });
        }
      }
      // Visually clipped text
      if (el.scrollWidth > el.clientWidth + 2 && s.overflow !== 'visible' &&
          el.clientWidth > 0 && /hidden|clip/.test(s.overflowX + s.overflow)) {
        truncated.push({
          text: text.slice(0, 60), visiblePx: el.clientWidth, neededPx: el.scrollWidth,
          hasEllipsis: s.textOverflow === 'ellipsis'
        });
      }
    }

    const bgc = parseColor(s.backgroundColor);
    if (bgc && bgc.a > 0.1 && r.width * r.height > 400) {
      bgColors[rgbStr(bgc)] = (bgColors[rgbStr(bgc)] || 0) + 1;
    }
    if (/^h[1-6]$/.test(tag) && text) headings.push({level: +tag[1], text: text.slice(0, 60)});

    const isCtl = tag === 'button' || tag === 'a' || el.getAttribute('role') === 'button' ||
                  (tag === 'input' && /^(button|submit|reset)$/.test(el.type || ''));
    if (isCtl) {
      const label = (el.innerText || el.value || el.getAttribute('aria-label') ||
                     el.getAttribute('title') || '').trim().slice(0, 40);
      const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true';
      if (s.borderRadius && s.borderRadius !== '0px') {
        radii[s.borderRadius] = (radii[s.borderRadius] || 0) + 1;
      }
      if (tag === 'button' || el.getAttribute('role') === 'button') {
        buttons.push({
          label, disabled,
          background: s.backgroundColor, color: s.color, fontSize: s.fontSize,
          fontWeight: s.fontWeight, fontFamily: (s.fontFamily || '').split(',')[0].replace(/["']/g, ''),
          borderRadius: s.borderRadius, border: s.border, padding: s.padding,
          height: Math.round(r.height), width: Math.round(r.width), cursor: s.cursor
        });
      }
      // Disabled but styled to look fully enabled - users click and nothing happens.
      if (disabled && parseFloat(s.opacity) >= 0.9 && s.cursor !== 'not-allowed' && label) {
        fakeDisabled.push({label, opacity: s.opacity, cursor: s.cursor});
      }
      // Something is painted on top of this control.
      if (!disabled) {
        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        if (cx > 0 && cy > 0 && cx < innerWidth && cy < innerHeight) {
          const top = document.elementFromPoint(cx, cy);
          if (top && top !== el && !el.contains(top) && !top.contains(el)) {
            covered.push({label: label || tag, coveredBy: top.tagName.toLowerCase()});
          }
        }
      }
      // Too small to hit reliably (WCAG 2.2 target size minimum is 24x24).
      if (!disabled && (r.width < 24 || r.height < 24) && label) {
        tinyTargets.push({label, width: Math.round(r.width), height: Math.round(r.height)});
      }
    }
  }
  const topN = (obj, n) => Object.entries(obj).sort((a, b) => b[1] - a[1]).slice(0, n)
      .map(([k, v]) => ({value: k, count: v}));
  return {
    fontFamilies: topN(fonts, 12), fontSizes: topN(sizes, 20), fontWeights: topN(weights, 10),
    textColors: topN(textColors, 20), backgroundColors: topN(bgColors, 16),
    borderRadii: topN(radii, 10),
    buttons: buttons.slice(0, 40), headings: headings.slice(0, 25),
    lowContrast: lowContrast.slice(0, 12), truncated: truncated.slice(0, 12),
    tinyTargets: tinyTargets.slice(0, 12), fakeDisabled: fakeDisabled.slice(0, 12),
    covered: covered.slice(0, 12),
    viewport: {width: innerWidth, height: innerHeight}
  };
}
"""


def _px(value):
    try:
        return float(str(value).replace("px", ""))
    except (TypeError, ValueError):
        return None


def _authored_sizes(font_sizes):
    """Drops the browser's own default control size (13.333px), which the
    developer never chose - counting it produces a false 'inconsistent' every
    time a page has a plain button or input."""
    out = []
    for f in font_sizes:
        p = _px(f["value"])
        if p is None or abs(p - 13.3333) < 0.01:
            continue
        out.append({"px": p, "count": f["count"], "value": f["value"]})
    return out


def _near_duplicate_sizes(font_sizes):
    """13px next to 13.5px is almost always an accident, not a design choice.
    Both sides must be used more than once, so a single stray value doesn't
    get reported as a systemic inconsistency."""
    used = sorted((f for f in _authored_sizes(font_sizes) if f["count"] >= 2),
                  key=lambda f: f["px"])
    pairs = []
    for a, b in zip(used, used[1:]):
        if 0 < b["px"] - a["px"] <= 1.0:
            pairs.append(f"{a['px']:g}px vs {b['px']:g}px")
    return pairs


def _rgb_tuple(value):
    try:
        nums = value.replace("rgb(", "").replace(")", "").split(",")
        return tuple(int(float(n)) for n in nums[:3])
    except Exception:
        return None


def _near_duplicate_colors(colors):
    """Colours a user can't tell apart but a designer never intended to differ."""
    vals = [(_rgb_tuple(c["value"]), c["value"]) for c in colors]
    vals = [(t, s) for t, s in vals if t]
    pairs = []
    for i, (t1, s1) in enumerate(vals):
        for t2, s2 in vals[i + 1:]:
            dist = sum(abs(a - b) for a, b in zip(t1, t2))
            if 0 < dist <= 24:
                pairs.append(f"{s1} vs {s2}")
    return pairs[:6]


def _button_style_groups(buttons):
    """Group buttons by their full visual signature. Many groups for what is
    conceptually one kind of button = an inconsistent-looking UI."""
    groups = {}
    for b in buttons:
        if b.get("disabled"):
            continue
        key = (b.get("background"), b.get("color"), b.get("fontSize"),
               b.get("fontWeight"), b.get("borderRadius"), b.get("border"))
        groups.setdefault(key, []).append(b.get("label") or "(no label)")
    return groups


def run_ui_consistency_checks(page, url):
    """Records the objective findings, then asks Claude to judge the
    subjective 'does this look like one consistent product' question."""
    try:
        data = page.evaluate(_STYLE_AUDIT_JS)
    except Exception as e:
        REPORTER.record("consistency", "Visual consistency audit", url,
                        "Page styling can be measured", f"Audit could not run: {e}", "skipped")
        return

    # ---- Text styling consistency -------------------------------------------
    families = [f["value"] for f in data["fontFamilies"]]
    if len(families) > 3:
        REPORTER.record(
            "consistency", "Too many different fonts on one page", url,
            "A page uses 1-2 typefaces so it reads as one consistent product",
            f"{len(families)} different fonts are in use: {', '.join(families[:6])}",
            "fail", severity="medium",
            repro_steps=[f"Open {url}", "Compare the lettering in the menu, headings, table and buttons"],
            screenshot=screenshot(page, "fonts_inconsistent"),
            suggested_fix="Standardise on one primary typeface (plus at most one accent) "
                          "and apply it through shared styles instead of per-component fonts.",
        )
    else:
        REPORTER.record("consistency", "Font usage is consistent", url,
                        "1-2 typefaces in use", f"Fonts in use: {', '.join(families) or 'none detected'}",
                        "pass")

    sizes = data["fontSizes"]
    authored = _authored_sizes(sizes)
    if len(authored) > 10:
        listed = ", ".join(s["value"] for s in authored[:10])
        REPORTER.record(
            "consistency", "Text sizes are all over the place", url,
            "Text uses a small, deliberate set of sizes (roughly 5-8)",
            f"{len(authored)} different text sizes are in use: {listed}...",
            "fail", severity="low",
            repro_steps=[f"Open {url}", "Compare text size between similar labels and table cells"],
            suggested_fix="Define a type scale (e.g. 12/14/16/20/24px) and use only those sizes.",
        )
    dupes = _near_duplicate_sizes(sizes)
    if dupes:
        REPORTER.record(
            "consistency", "Nearly-identical text sizes used side by side", url,
            "Text sizes differ meaningfully or not at all",
            "These sizes differ by a hair, which looks like a mistake rather than a "
            f"design decision: {'; '.join(dupes[:5])}",
            "fail", severity="low",
            suggested_fix="Round these to a single value from your type scale.",
        )

    # ---- Colour consistency --------------------------------------------------
    color_dupes = _near_duplicate_colors(data["textColors"]) + \
        _near_duplicate_colors(data["backgroundColors"])
    if color_dupes:
        REPORTER.record(
            "consistency", "Nearly-identical colours used in different places", url,
            "One colour per purpose, reused everywhere it applies",
            "These colour pairs are visually indistinguishable but are different values, "
            f"which means the palette has drifted: {'; '.join(color_dupes[:5])}",
            "fail", severity="low",
            repro_steps=[f"Open {url}", "Compare the shade of body text and secondary text"],
            suggested_fix="Replace one-off colour values with shared design tokens/variables.",
        )
    if len(data["textColors"]) > 12:
        REPORTER.record(
            "consistency", "Too many text colours on one page", url,
            "A small palette (roughly 3-5 text colours)",
            f"{len(data['textColors'])} distinct text colours are in use",
            "fail", severity="low",
            suggested_fix="Reduce to a primary, secondary and muted text colour, plus status colours.",
        )

    # ---- Buttons -------------------------------------------------------------
    groups = _button_style_groups(data["buttons"])
    if len(groups) > 4:
        examples = "; ".join(
            f"[{', '.join(v[:3])}]" for v in list(groups.values())[:5]
        )
        REPORTER.record(
            "consistency", "Buttons don't look like they belong to the same app", url,
            "Buttons of the same importance share one look",
            f"{len(groups)} visually different button styles found among "
            f"{sum(len(v) for v in groups.values())} buttons: {examples}",
            "fail", severity="medium",
            repro_steps=[f"Open {url}", "Compare the buttons in the toolbar, the table rows and any popup"],
            screenshot=screenshot(page, "buttons_inconsistent"),
            suggested_fix="Introduce shared button variants (primary / secondary / subtle) and "
                          "use them everywhere instead of ad-hoc styling.",
        )
    else:
        REPORTER.record("consistency", "Button styling is consistent", url,
                        "Buttons share a small set of looks",
                        f"{len(groups)} button style(s) across {len(data['buttons'])} buttons", "pass")

    for b in data["fakeDisabled"]:
        REPORTER.record(
            "usability", f"'{b['label']}' looks clickable but does nothing", url,
            "A control that can't be used looks visibly unavailable (greyed out)",
            f"This control is switched off, yet it is drawn at full strength "
            f"(opacity {b['opacity']}, cursor '{b['cursor']}'), so people will click it and "
            "assume the app is broken when nothing happens",
            "fail", severity="medium",
            repro_steps=[f"Open {url}", f"Click '{b['label']}'", "Notice nothing happens"],
            screenshot=screenshot(page, f"fake_disabled_{b['label']}"),
            suggested_fix="Grey the control out (reduced opacity + 'not-allowed' cursor) while it is "
                          "unavailable, or explain why it can't be used right now.",
        )

    # One invisible full-page layer can cover every control at once. Report that
    # as a single page-level problem rather than one finding per button.
    if data["covered"]:
        by_blocker = {}
        for c in data["covered"]:
            by_blocker.setdefault(c["coveredBy"], []).append(c["label"])
        for blocker, labels in by_blocker.items():
            if len(labels) >= 3:
                REPORTER.record(
                    "usability", "An invisible layer is blocking clicks on this page", url,
                    "Every visible control can actually be clicked",
                    f"{len(labels)} controls cannot be clicked because something is painted over "
                    f"the whole area (for example: {', '.join(labels[:4])}). To a user the page "
                    "looks normal but nothing responds",
                    "fail", severity="critical",
                    repro_steps=[f"Open {url}", f"Try to click '{labels[0]}'", "Nothing happens"],
                    screenshot=screenshot(page, "click_blocked_overlay"),
                    suggested_fix="Find the layer covering the page (often a leftover popup "
                                  "backdrop or a full-size wrapper) and remove it or let clicks "
                                  "pass through it.",
                )
            else:
                for label in labels:
                    REPORTER.record(
                        "usability", f"'{label}' is hidden behind something else", url,
                        "Every visible control can actually be clicked",
                        f"Another element sits on top of this control, so a click lands on the "
                        "wrong thing and the control appears not to work",
                        "fail", severity="high",
                        repro_steps=[f"Open {url}", f"Try to click '{label}'"],
                        screenshot=screenshot(page, f"covered_{label}"),
                        suggested_fix="Fix the overlap so the control sits on top and is clickable.",
                    )

    for t in data["tinyTargets"]:
        REPORTER.record(
            "usability", f"'{t['label']}' is too small to tap reliably", url,
            "Clickable things are at least 24x24 pixels",
            f"This control is only {t['width']}x{t['height']} pixels, which is fiddly with a "
            "mouse and very hard on a touchscreen",
            "fail", severity="low",
            repro_steps=[f"Open {url}", f"Try to tap '{t['label']}' on a phone"],
            suggested_fix="Increase the clickable area (padding counts) to at least 24x24, ideally 44x44.",
        )

    # ---- Readability ---------------------------------------------------------
    for lc in data["lowContrast"]:
        REPORTER.record(
            "usability", f"Text is too faint to read comfortably: \"{lc['text']}\"", url,
            f"Text stands out from its background by at least {lc['needed']}:1 (accessibility standard)",
            f"This text only reaches {lc['ratio']}:1 ({lc['color']} on {lc['background']} at "
            f"{lc['fontSize']}), so it will be hard to read on a laptop screen in daylight "
            "and for anyone with reduced vision",
            "fail", severity="medium",
            repro_steps=[f"Open {url}", f"Look at the text \"{lc['text']}\""],
            screenshot=screenshot(page, "low_contrast"),
            suggested_fix="Darken the text (or lighten the background) until it meets "
                          f"{lc['needed']}:1 contrast.",
        )

    for tr in data["truncated"]:
        REPORTER.record(
            "usability", f"Text is cut off on screen: \"{tr['text']}\"", url,
            "Content is either shown in full or clearly marked as shortened",
            f"This text needs {tr['neededPx']}px but only has {tr['visiblePx']}px, so part of it is "
            f"invisible{'' if tr['hasEllipsis'] else ' with no â€œ...â€ to show it was shortened'}"
            f"{' - users cannot see the full value' if not tr['hasEllipsis'] else ''}",
            "fail", severity="low" if tr["hasEllipsis"] else "medium",
            repro_steps=[f"Open {url}", f"Find the text \"{tr['text']}\""],
            suggested_fix="Give the column/label more room, allow wrapping, or show the full value "
                          "in a tooltip on hover.",
        )

    # ---- Heading structure ---------------------------------------------------
    levels = [h["level"] for h in data["headings"]]
    if levels:
        if levels.count(1) == 0:
            REPORTER.record(
                "consistency", "Page has no main heading", url,
                "Every page states its own title as a top-level heading",
                "No main (H1) heading was found, so the page doesn't announce what it is - "
                "this hurts screen readers and search indexing",
                "fail", severity="low",
                suggested_fix="Add a single main heading naming the page (e.g. 'Dashboard').",
            )
        elif levels.count(1) > 1:
            REPORTER.record(
                "consistency", "Page has more than one main heading", url,
                "Exactly one top-level heading per page",
                f"{levels.count(1)} main (H1) headings were found, which makes the page's "
                "structure ambiguous",
                "fail", severity="low",
                suggested_fix="Keep one main heading and demote the rest to sub-headings.",
            )

    # ---- Claude's judgement on the aggregate look ---------------------------
    payload = {
        "url": url,
        "fontFamilies": data["fontFamilies"], "fontSizes": data["fontSizes"],
        "fontWeights": data["fontWeights"], "textColors": data["textColors"],
        "backgroundColors": data["backgroundColors"], "borderRadii": data["borderRadii"],
        "buttons": data["buttons"], "headings": data["headings"],
    }
    verdict = safe_ask_json(UI_CONSISTENCY_PROMPT, json.dumps(payload)[:50000],
                            max_tokens=2000, default={}) or {}
    for issue in (verdict.get("issues") or [])[:6]:
        REPORTER.record(
            "consistency", issue.get("title", "Visual inconsistency"), url,
            issue.get("expected", "The page looks visually consistent"),
            issue.get("evidence", ""), "fail",
            severity=issue.get("severity", "low"),
            repro_steps=[f"Open {url}"] + (issue.get("where") and [issue["where"]] or []),
            suggested_fix=issue.get("suggested_fix", ""),
        )


def _clustered_colour_count(values, distance=36):
    """Count visually distinct RGB colours rather than exact CSS strings."""
    clusters = []
    for value in values:
        rgb = _rgb_tuple(value)
        if not rgb:
            continue
        for cluster in clusters:
            if sum(abs(a - b) for a, b in zip(rgb, cluster)) <= distance:
                break
        else:
            clusters.append(rgb)
    return len(clusters)


def _heading_hierarchy_ok(headings):
    """Compare visual size by semantic heading level, not DOM order.

    The previous check compared each heading to the one before it in the page.
    A later H2 after a small H4 therefore failed even though the hierarchy was
    correct. This version compares average sizes for H1, H2, H3, etc.
    """
    by_level = {}
    for item in headings:
        try:
            level = int(item.get("level"))
            size = float(item.get("size"))
        except (TypeError, ValueError):
            continue
        by_level.setdefault(level, []).append(size)
    ordered = sorted((level, sum(vals) / len(vals)) for level, vals in by_level.items())
    for (_, larger), (_, smaller) in zip(ordered, ordered[1:]):
        if smaller > larger + 2.0:
            return False
    return True


def run_ten_ui_checks(page, url):
    """Record ten measured UI checks without turning style preferences into bugs.

    In the default functional profile, a visual check fails only when there is a
    direct usability impact: severely unreadable text, genuinely tiny interactive
    labels, misalignment, or clipped content. Exact design-system thresholds are
    enforced only when STRICT_UI_AUDIT=1.
    """
    try:
        data = page.evaluate(r"""
        () => {
          const visible = el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' &&
                   s.visibility !== 'hidden' && Number(s.opacity || 1) > 0.05;
          };
          const ownText = el => Array.from(el.childNodes)
            .filter(n => n.nodeType === Node.TEXT_NODE)
            .map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim();
          const parse = c => {
            const m = String(c || '').match(/rgba?\(([^)]+)\)/);
            if (!m) return null;
            const p = m[1].split(',').map(x => Number(x.trim()));
            return {r:p[0], g:p[1], b:p[2], a:p.length > 3 ? p[3] : 1};
          };
          const blend = (fg, bg) => fg.a >= .999 ? fg : ({
            r:fg.r*fg.a+bg.r*(1-fg.a), g:fg.g*fg.a+bg.g*(1-fg.a),
            b:fg.b*fg.a+bg.b*(1-fg.a), a:1
          });
          const lum = c => {
            const f = v => { v /= 255; return v <= .03928 ? v/12.92 : Math.pow((v+.055)/1.055,2.4); };
            return .2126*f(c.r)+.7152*f(c.g)+.0722*f(c.b);
          };
          const ratio = (a,b) => {
            const x = Math.max(lum(a),lum(b)), y = Math.min(lum(a),lum(b));
            return (x+.05)/(y+.05);
          };
          const effectiveBg = el => {
            let n=el;
            while(n && n !== document.documentElement){
              const s=getComputedStyle(n);
              // A flat computed background colour is not representative of a
              // gradient/image. Skip rather than manufacture a false contrast fail.
              if(s.backgroundImage && s.backgroundImage !== 'none') return null;
              const c=parse(s.backgroundColor);
              if(c && c.a > .05) return c;
              n=n.parentElement;
            }
            return {r:255,g:255,b:255,a:1};
          };
          const normColour = c => {
            const p=parse(c); if(!p) return '';
            return `rgb(${Math.round(p.r)}, ${Math.round(p.g)}, ${Math.round(p.b)})`;
          };
          const elements = Array.from(document.body.querySelectorAll('*'))
            .filter(visible).slice(0,3000);
          const textEls = elements.filter(el => ownText(el));
          const unique = values => [...new Set(values.filter(Boolean))];

          const fonts = unique(textEls.map(e => getComputedStyle(e).fontFamily
            .split(',')[0].replace(/["']/g,'').trim())
            .filter(f => !/icon|fontawesome|material symbols/i.test(f));
          const sizeBuckets = unique(textEls.map(e =>
            String(Math.round(Number.parseFloat(getComputedStyle(e).fontSize))) + 'px'));
          const colors = unique(textEls.map(e => normColour(getComputedStyle(e).color)));

          const criticalTiny = [];
          let advisorySmall = 0;
          const lowContrastStrict = [];
          const lowContrastSevere = [];
          for (const el of textEls.slice(0,1200)) {
            const text=ownText(el);
            const s=getComputedStyle(el);
            const px=Number.parseFloat(s.fontSize);
            const interactive=!!el.closest('button,a,[role="button"],[role="tab"]');
            const disabled=!!el.closest('[disabled],[aria-disabled="true"]');
            if (!disabled && (px < 8 || (interactive && px < 9))) {
              criticalTiny.push({text:text.slice(0,60),size:px});
            } else if (px < 11) {
              advisorySmall++;
            }

            const fg0=parse(s.color), bg=effectiveBg(el);
            if(!fg0 || !bg || fg0.a < .5 || disabled || text.length < 2) continue;
            const fg=blend(fg0,bg);
            const cr=ratio(fg,bg);
            const bold=Number.parseInt(s.fontWeight)>=700;
            const needed=(px>=24 || (px>=18.66 && bold)) ? 3 : 4.5;
            if(cr + .05 < needed) lowContrastStrict.push({text:text.slice(0,60),ratio:cr});
            if(cr + .05 < 3.0) lowContrastSevere.push({text:text.slice(0,60),ratio:cr});
          }

          const buttons=elements.filter(e =>
            e.matches('button,[role="button"],input[type="button"],input[type="submit"]'));
          const buttonStyles=unique(buttons.filter(e => !e.disabled && e.getAttribute('aria-disabled') !== 'true')
            .map(e => {
              const s=getComputedStyle(e), r=e.getBoundingClientRect();
              const label=(e.innerText || e.value || e.getAttribute('aria-label') || '').trim();
              const kind=(label.length <= 2 || (!label && e.querySelector('svg'))) ? 'icon' : 'text';
              const round=(n,step) => Math.round(n/step)*step;
              return [kind,Math.round(Number.parseFloat(s.fontSize)),s.fontWeight,
                round(r.height,4),round(Number.parseFloat(s.borderRadius)||0,2),
                normColour(s.backgroundColor),normColour(s.color),s.borderStyle,s.borderWidth].join('|');
            }));

          const headings=elements.filter(e => /^H[1-6]$/.test(e.tagName)).map(e => ({
            level:Number(e.tagName.slice(1)), size:Number.parseFloat(getComputedStyle(e).fontSize)
          }));

          const misaligned=[];
          for (const row of elements.filter(e => e.matches('tr,.form-row,[class*="formRow"]')).slice(0,140)) {
            const kids=Array.from(row.children).filter(visible);
            if(kids.length < 2) continue;
            const centers=kids.map(k => {const r=k.getBoundingClientRect(); return r.top+r.height/2;});
            if(Math.max(...centers)-Math.min(...centers) > 12) misaligned.push(row);
          }

          const tables=elements.filter(e => e.matches('table'));
          const uncontainedTableOverflow=tables.filter(t => {
            if(t.scrollWidth <= t.clientWidth + 2) return false;
            let n=t.parentElement;
            while(n && n !== document.body){
              const ox=getComputedStyle(n).overflowX;
              if(ox === 'auto' || ox === 'scroll') return false;
              n=n.parentElement;
            }
            return true;
          }).length;

          return {
            fonts, sizeBuckets, colors,
            criticalTiny:criticalTiny.slice(0,12), advisorySmall,
            lowContrastStrict:lowContrastStrict.slice(0,20),
            lowContrastSevere:lowContrastSevere.slice(0,20),
            buttonCount:buttons.length, buttonStyles,
            headings, headingCount:headings.length,
            misalignedCount:misaligned.length,
            tableCount:tables.length, uncontainedTableOverflow,
            pageOverflow:document.documentElement.scrollWidth > window.innerWidth + 8,
            bodyVisible:document.body.innerText.trim().length > 0
          };
        }
        """)
    except Exception as exc:
        REPORTER.record("ui", "UI audit could not run", url,
                        "The ten UI checks execute successfully", f"UI measurement failed: {exc}",
                        "skipped")
        return

    strict = config.STRICT_UI_AUDIT
    colour_count = _clustered_colour_count(data["colors"])
    hierarchy_ok = _heading_hierarchy_ok(data["headings"])
    contrast_issues = data["lowContrastStrict"] if strict else data["lowContrastSevere"]

    # The final boolean marks checks that can prove an actual usability blocker
    # in the functional profile. Typography scales, colour-counts, button
    # variants and heading aesthetics remain visible measurements, but they are
    # advisory unless the user explicitly enables STRICT_UI_AUDIT.
    checks = [
        ("UI 1 - Page renders visible content", data["bodyVisible"],
         "The page renders visible content", "Visible page content was detected", True),
        ("UI 2 - Font family usage is consistent", len(data["fonts"]) <= 3,
         "The page uses no more than three intentional font families",
         f"Font families found ({len(data['fonts'])}): {', '.join(data['fonts'][:8])}", False),
        ("UI 3 - Font-size scale is controlled",
         len(data["sizeBuckets"]) <= (10 if strict else 14),
         "The page uses a controlled, rounded font-size scale",
         f"Rounded font-size buckets ({len(data['sizeBuckets'])}): {', '.join(data['sizeBuckets'][:16])}", False),
        ("UI 4 - Text is not unreasonably small", len(data["criticalTiny"]) == 0,
         "No visible business text is below 8px and interactive labels are at least 9px",
         f"Critical tiny text: {len(data['criticalTiny'])}; smaller supporting text (advisory only): {data['advisorySmall']}", True),
        ("UI 5 - Text colours have valid contrast", len(contrast_issues) == 0,
         ("Normal text meets WCAG contrast thresholds" if strict else
          "No text has severely unreadable contrast below 3:1"),
         f"Confirmed contrast issues: {len(contrast_issues)}; examples: {contrast_issues[:3]}", True),
        ("UI 6 - Text colour palette is consistent",
         colour_count <= (12 if strict else 24),
         "The page uses a controlled set of visually distinct text colours",
         f"Visually clustered text colours: {colour_count} (raw computed values: {len(data['colors'])})", False),
        ("UI 7 - Button styling is consistent",
         len(data["buttonStyles"]) <= (6 if strict else 12),
         "Buttons use a manageable set of intentional text/icon variants",
         f"Visible buttons: {data['buttonCount']}; normalised style variants: {len(data['buttonStyles'])}", False),
        ("UI 8 - Heading hierarchy is consistent", hierarchy_ok,
         "Higher-level headings are not visually smaller than lower-level headings",
         f"Visible headings: {data['headingCount']}; hierarchy valid: {hierarchy_ok}", False),
        ("UI 9 - Rows and controls are aligned", data["misalignedCount"] == 0,
         "Controls and cells in the same row align within twelve pixels",
         f"Potentially misaligned rows: {data['misalignedCount']}", False),
        ("UI 10 - Page and tables avoid horizontal clipping",
         not data["pageOverflow"] and data["uncontainedTableOverflow"] == 0,
         "The page fits the viewport and wide tables have their own horizontal scroll container",
         f"Page overflow: {data['pageOverflow']}; uncontained overflowing tables: "
         f"{data['uncontainedTableOverflow']} of {data['tableCount']}", True),
    ]

    for title, measured_pass, expected, actual, functional_blocker in checks:
        passed = measured_pass or (not strict and not functional_blocker)
        if not strict and not functional_blocker and not measured_pass:
            actual += (
                "; measured as a design-system advisory only in the functional "
                "profile, so it is not reported as broken functionality"
            )
        REPORTER.record(
            "ui", title, url, expected, actual,
            "pass" if passed else "fail",
            severity="medium" if not passed else "info",
            screenshot="" if passed else screenshot(page, title.lower().replace(' ', '_')),
            suggested_fix=(
                "Use shared design tokens and responsive layout rules for this measured UI issue."
                if not passed else ""
            ),
        )

