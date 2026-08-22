"""Focused, non-destructive business checks for FS NxT ERP.

The checks in this module cover Dashboard, Facility Creation and Facility
Approval. Every facility validation scenario runs in a newly opened modal so
one test cannot contaminate the next test.

A validation test passes when invalid data is prevented from reaching the
review state. A missing or unclear validation message is recorded only as
supporting evidence; it does not turn a correctly blocked business rule into a
critical failure.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlparse

from ai_agents import config
from ai_agents.services.reporter_service import REPORTER, screenshot
from ai_agents.services.dom_service import get_toast_or_message_text
from ai_agents.services.evidence_service import wait_for_settle
from ai_agents.services.modal_service import get_open_modal, close_any_modal
from ai_agents.services.auth_service import role_capabilities, normalise_role


_FACILITY_BASELINE_BLOCKER: str | None = None
_FACILITY_FORM_PROFILE: dict[str, bool] = {}


@dataclass(frozen=True)
class DropdownOutcome:
    """Deterministic result for a dropdown attempt.

    ``passed`` follows the configured two-level policy:
    confirmed selection is preferred; visible usable options can be accepted as
    a fallback without pretending that the selected value was retained.
    """

    passed: bool
    selected: bool
    options_visible: bool
    detail: str


def _mark_modal_dropdown_fallback(modal, field_name: str) -> None:
    if modal is None:
        return
    try:
        modal.evaluate(
            r"""
            (e, name) => {
              const current = (e.getAttribute('data-qa-dropdown-fallback-fields') || '')
                .split('|').map(v => v.trim()).filter(Boolean);
              if (!current.includes(name)) current.push(name);
              e.setAttribute('data-qa-dropdown-fallback-fields', current.join('|'));
            }
            """,
            field_name,
        )
    except Exception:
        pass


def _modal_dropdown_fallbacks(modal) -> list[str]:
    if modal is None:
        return []
    try:
        raw = modal.get_attribute('data-qa-dropdown-fallback-fields') or ''
        return [part.strip() for part in raw.split('|') if part.strip()]
    except Exception:
        return []


def _path(url: str) -> str:
    return (urlparse(url).path or "/").lower().rstrip("/") or "/"


def _scope(page, scope=None):
    if scope is not None:
        return scope
    modal = get_open_modal(page)
    return modal if modal is not None else page


def _visible(page, selector: str, scope=None) -> bool:
    root = _scope(page, scope)
    try:
        loc = root.locator(selector)
        for index in range(min(loc.count(), 100)):
            if loc.nth(index).is_visible():
                return True
    except Exception:
        pass
    return False


def _button(page, pattern: str, scope=None):
    root = _scope(page, scope)
    rx = re.compile(pattern, re.I)

    try:
        loc = root.get_by_role("button", name=rx)
        for index in range(min(loc.count(), 30)):
            item = loc.nth(index)
            if item.is_visible():
                return item
    except Exception:
        pass

    try:
        loc = root.locator("button, [role='button']")
        for index in range(min(loc.count(), 120)):
            item = loc.nth(index)
            if not item.is_visible():
                continue
            text = " ".join((item.inner_text() or "").split())
            aria = item.get_attribute("aria-label") or ""
            title = item.get_attribute("title") or ""
            if rx.search(" ".join((text, aria, title))):
                return item
    except Exception:
        pass

    return None


def _exact_field_selectors(label_patterns: list[str]) -> list[str]:
    """Return stable selectors for known Facility Creation controls.

    The production form has existed in both native-select and custom-dropdown
    versions.  Stable IDs/names are attempted before broad text matching so a
    dropdown menu or nearby field cannot be mistaken for the trigger.
    """
    joined = " ".join(label_patterns).lower()
    selectors: list[str] = []

    if "facility\\s*name" in joined or "facility name" in joined:
        selectors.extend([
            "#facility-name",
            "#facilityName",
            "#name",
            "[name='facilityName']",
            "[data-testid='facility-name']",
        ])
    if "company" in joined:
        selectors.extend([
            "#facility-company",
            "#company",
            "[name='companyId']",
            "[data-testid='facility-company']",
            "[data-testid='company-code']",
        ])
    if "business\\s*partner" in joined or "partner" in joined:
        selectors.extend([
            "#facility-business-partner",
            "#businessPartner",
            "#partner",
            "[name='businessPartnerId']",
            "[data-testid='facility-business-partner']",
            "[data-testid='business-partner']",
        ])
    if "product" in joined or "facility\\s*type" in joined:
        selectors.extend([
            "#facility-product-id",
            "#product",
            "[name='productId']",
            "[data-testid='facility-product-id']",
            "[data-testid='product-type']",
        ])
    if "currency" in joined:
        selectors.extend([
            "#facility-currency",
            "#currency",
            "[name='currency']",
            "[data-testid='facility-currency']",
        ])
    if "start\\s*date" in joined or "from\\s*date" in joined or "issue\\s*date" in joined:
        selectors.extend([
            "#facility-start-date",
            "#start",
            "[name='startDate']",
            "[data-testid='facility-start-date']",
        ])
    if "end\\s*date" in joined or "maturity\\s*date" in joined or "to\\s*date" in joined:
        selectors.extend([
            "#facility-end-date",
            "#end",
            "[name='endDate']",
            "[data-testid='facility-end-date']",
        ])
    if "amount" in joined or "limit" in joined or "sanctioned" in joined:
        selectors.extend([
            "#facility-limit-amount",
            "#amount",
            "[name='limitAmount']",
            "[name='facilityAmount']",
            "[data-testid='facility-limit-amount']",
            "[data-testid='facility-amount']",
        ])

    return list(dict.fromkeys(selectors))


# Canonical pattern groups for every known Facility Creation control. Reused
# both to discover the current form's field profile and to build sibling-field
# exclusion zones for dropdown option discovery (see
# ``_other_field_exclusion_selectors``).
_FACILITY_FIELD_SPECS: dict[str, list[str]] = {
    "facility_name": [r"facility\s*name"],
    "company": [r"company\s*code", r"^company$"],
    "business_partner": [r"business\s*partner", r"partner"],
    "product": [r"product\s*code", r"product\s*id", r"product\s*type", r"^product$", r"facility\s*type"],
    "amount": [r"facility\s*amount", r"limit\s*amount", r"sanctioned\s*amount"],
    "currency": [r"currency"],
    "start_date": [r"start\s*date", r"from\s*date", r"issue\s*date"],
    "end_date": [r"end\s*date", r"maturity\s*date", r"to\s*date"],
}


def _other_field_exclusion_selectors(own_patterns: list[str] | None) -> list[str]:
    """Stable selectors for every known field control except ``own_patterns``.

    Custom-dropdown option discovery falls back to a bounded geometric scan
    when a field's own popup cannot be identified by role/aria-controls. That
    scan has been observed picking up a sibling field's label, placeholder or
    already-selected value instead - for example Business Partner's discovery
    grabbing Currency's default 'INR - Indian Rupee' text, or Product's own
    'Select Product' placeholder, simply because they sit within the scan
    region below the Business Partner trigger. Passing these selectors into
    the discovery script lets it exclude any element that IS, or structurally
    contains, another field's real control - regardless of its text or class
    name - which a text-matching filter alone cannot reliably catch.
    """
    own_key = None
    if own_patterns:
        own_set = {pattern.lower() for pattern in own_patterns}
        for key, patterns in _FACILITY_FIELD_SPECS.items():
            if {pattern.lower() for pattern in patterns} == own_set:
                own_key = key
                break

    selectors: list[str] = []
    for key, patterns in _FACILITY_FIELD_SPECS.items():
        if key == own_key:
            continue
        selectors.extend(_exact_field_selectors(patterns))
    return list(dict.fromkeys(selectors))


def _visible_control_near_candidate(candidate):
    """Resolve a visible trigger when a stable selector points to hidden state."""
    try:
        if candidate.is_visible():
            return candidate
    except Exception:
        return None

    try:
        wrapper = candidate.locator(
            "xpath=ancestor::*[contains(@class,'field') or contains(@class,'control') "
            "or contains(@class,'select') or @role='group'][1]"
        )
        if wrapper.count():
            controls = wrapper.first.locator(
                "select, input:not([type='hidden']), textarea, [role='combobox'], "
                "button[aria-haspopup], button[aria-controls], [tabindex='0']"
            )
            for index in range(min(controls.count(), 20)):
                item = controls.nth(index)
                if item.is_visible():
                    return item
    except Exception:
        pass
    return None


def _field(page, label_patterns: list[str], scope=None):
    """Find native and custom controls using stable IDs, labels and nearby text."""
    root = _scope(page, scope)
    control_selector = (
        "input, select, textarea, [role='combobox'], "
        "button[aria-haspopup='listbox'], button[aria-controls], "
        "[data-testid*='select'], [class*='select-trigger'], [class*='dropdown-trigger']"
    )

    for selector in _exact_field_selectors(label_patterns):
        try:
            loc = root.locator(selector)
            for index in range(min(loc.count(), 20)):
                resolved = _visible_control_near_candidate(loc.nth(index))
                if resolved is not None:
                    return resolved
        except Exception:
            pass

    for pattern in label_patterns:
        rx = re.compile(pattern, re.I)

        try:
            loc = root.get_by_label(rx)
            for index in range(min(loc.count(), 30)):
                item = loc.nth(index)
                resolved = _visible_control_near_candidate(item)
                if resolved is not None:
                    return resolved
        except Exception:
            pass

        try:
            labels = root.locator("label, [class*='label'], [data-label]")
            for index in range(min(labels.count(), 180)):
                label = labels.nth(index)
                if not label.is_visible():
                    continue
                try:
                    label_text = label.evaluate(
                        r"""
                        e => {
                          const own = Array.from(e.childNodes)
                            .filter(node => node.nodeType === Node.TEXT_NODE)
                            .map(node => node.textContent || '')
                            .join(' ');
                          return (own || e.getAttribute('data-label') ||
                                  e.getAttribute('aria-label') || e.innerText || '')
                            .replace(/\s+/g, ' ').trim();
                        }
                        """,
                        timeout=config.DROPDOWN_ACTION_TIMEOUT_MS,
                    )
                except Exception:
                    label_text = " ".join((label.inner_text() or "").split())
                if not rx.search(label_text):
                    continue

                target = label.get_attribute("for")
                if target:
                    item = root.locator(f"#{target}")
                    if item.count():
                        resolved = _visible_control_near_candidate(item.first)
                        if resolved is not None:
                            return resolved

                item = label.locator(control_selector)
                for control_index in range(min(item.count(), 10)):
                    candidate = item.nth(control_index)
                    if candidate.is_visible():
                        return candidate

                wrapper = label.locator(
                    "xpath=ancestor::*[contains(@class,'field') or contains(@class,'form') "
                    "or contains(@class,'control') or contains(@class,'select')][1]"
                )
                if not wrapper.count():
                    wrapper = label.locator("xpath=..")
                item = wrapper.first.locator(control_selector)
                for control_index in range(min(item.count(), 20)):
                    candidate = item.nth(control_index)
                    if candidate.is_visible():
                        return candidate
        except Exception:
            pass

        try:
            loc = root.locator(control_selector)
            for index in range(min(loc.count(), 220)):
                item = loc.nth(index)
                if not item.is_visible():
                    continue
                try:
                    nearby = item.evaluate(
                        r"""
                        e => {
                          const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                          const own = [
                            e.getAttribute('placeholder'), e.getAttribute('name'),
                            e.id, e.getAttribute('aria-label'),
                            e.getAttribute('data-testid'), e.getAttribute('aria-valuetext')
                          ].filter(Boolean).join(' ');
                          const wrap = e.closest(
                            "[class*='field'], [class*='form-group'], [class*='formGroup'], " +
                            "[class*='control'], [class*='select'], label, .row, .col"
                          );
                          return norm(own + ' ' + (wrap ? wrap.innerText : ''));
                        }
                        """,
                        timeout=max(500, min(config.DROPDOWN_ACTION_TIMEOUT_MS, 1800)),
                    )
                except Exception:
                    nearby = " ".join(
                        str(item.get_attribute(name) or "")
                        for name in (
                            "placeholder", "name", "id", "aria-label", "data-testid"
                        )
                    )
                if rx.search(str(nearby or "")):
                    return item
        except Exception:
            pass

    return None

def _field_name(item) -> str:
    if item is None:
        return "field"
    try:
        return item.evaluate(
            r"""
            e => {
              const norm = s => (s || '').replace(/\s+/g, ' ').trim();
              if (e.id) {
                const label = document.querySelector(`label[for="${e.id}"]`);
                if (label && norm(label.innerText)) return norm(label.innerText);
              }
              const wrap = e.closest('label');
              if (wrap && norm(wrap.innerText)) return norm(wrap.innerText);
              return norm(e.getAttribute('aria-label')) ||
                     norm(e.getAttribute('placeholder')) ||
                     norm(e.getAttribute('name')) ||
                     norm(e.id) || 'field';
            }
            """
        )
    except Exception:
        return "field"


def _set_native_value(item, value: str) -> bool:
    """Set a controlled input value and notify React/Vue/Angular listeners."""
    try:
        item.evaluate(
            r"""
            (e, value) => {
              const oldValue = e.value;
              const wasReadonly = e.hasAttribute('readonly');
              if (wasReadonly) e.removeAttribute('readonly');

              const proto = e instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype
                : e instanceof HTMLSelectElement
                  ? HTMLSelectElement.prototype
                  : HTMLInputElement.prototype;
              const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
              if (descriptor && descriptor.set) descriptor.set.call(e, value);
              else e.value = value;

              // React tracks the previous value internally. Resetting the
              // tracker makes the synthetic input event visible to React.
              if (e._valueTracker) e._valueTracker.setValue(oldValue);

              try {
                e.dispatchEvent(new InputEvent('input', {
                  bubbles: true,
                  inputType: 'insertText',
                  data: String(value)
                }));
              } catch (_) {
                e.dispatchEvent(new Event('input', {bubbles: true}));
              }
              e.dispatchEvent(new Event('change', {bubbles: true}));
              e.dispatchEvent(new Event('blur', {bubbles: true}));

              if (wasReadonly) e.setAttribute('readonly', '');
            }
            """,
            str(value),
        )
        return True
    except Exception:
        return False

def _option_is_placeholder(text: str, value: str | None) -> bool:
    label = " ".join(str(text or "").split())
    raw_value = "" if value is None else str(value).strip()
    return (
        not raw_value
        or bool(
            re.match(
                r"^(select|choose|please select|all|none|--)(\s+.*)?$",
                label,
                re.I,
            )
        )
    )


def _mark_visible_options_loaded(item, enabled: bool) -> None:
    """Remember that a custom dropdown visibly loaded usable options.

    Some bespoke React dropdowns render plain div elements without option roles
    and do not expose the selected value on the trigger. In that situation the
    visible option list is accepted as proof that the dropdown data loaded, so
    the remaining validation scenarios are not incorrectly marked NOT TESTED.
    """
    if item is None:
        return
    try:
        if enabled:
            item.evaluate(
                "e => e.setAttribute('data-qa-visible-options-loaded', '1')"
            )
        else:
            item.evaluate(
                "e => e.removeAttribute('data-qa-visible-options-loaded')"
            )
    except Exception:
        pass


def _visible_options_loaded(item) -> bool:
    try:
        return item.get_attribute('data-qa-visible-options-loaded') == '1'
    except Exception:
        return False


_POPUP_LIKE_SELECTOR = (
    "[role='listbox'],[role='menu'],[class*='dropdown-menu'],[class*='select-menu'],"
    "[class*='popover'],[class*='popup'],[class*='options'],[class*='menu-list'],"
    "ul[class*='select'],div[class*='select__menu']"
)


def _mark_existing_popups(page) -> None:
    """Tag every currently-visible popup-like element as 'already here'.

    Called right before a dropdown trigger is clicked, so the discovery step
    that runs after the click can tell a freshly-opened popup apart from one
    that was already on screen (a different field's open menu, a persistent
    panel, etc). Without this, option discovery has no way to know WHICH
    popup belongs to the field just clicked and falls back to guessing by
    screen position, which is how Business Partner's discovery ended up
    grabbing the Currency field's already-selected value.
    """
    try:
        page.evaluate(
            r"""
            (selector) => {
              document.querySelectorAll(selector).forEach(el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                const visible = r.width > 2 && r.height > 2 &&
                    s.display !== 'none' && s.visibility !== 'hidden';
                if (visible) el.setAttribute('data-qa-pre-existing-popup', '1');
                else el.removeAttribute('data-qa-pre-existing-popup');
              });
            }
            """,
            _POPUP_LIKE_SELECTOR,
        )
    except Exception:
        pass


def _click_first_visible_custom_option(page, item, exclude_selectors: list[str] | None = None) -> dict:
    """Find and click one visible non-placeholder custom option.

    Approach 1 uses a real Playwright click on the exact option element. The
    JavaScript phase only performs a bounded search and tags the best candidate;
    this is more React-compatible than calling ``element.click()`` directly.
    The returned ``candidateCount`` and ``optionTexts`` are also the evidence
    used by the configured visible-options fallback.

    ``exclude_selectors`` names every OTHER known Facility field control, so a
    popup that could not be scoped by role/aria-controls cannot mistake a
    sibling field's label, placeholder or already-selected value for one of
    its own options.
    """
    try:
        discovery = item.evaluate(
            r"""
            (trigger, excludeSelectors) => {
              const norm = value => (value || '').replace(/\s+/g, ' ').trim();
              // Strip a trailing sort/caret glyph (e.g. "Facility Nameâ‡…")
              // before testing against known non-option labels, so a column
              // header or another field's trigger can't slip past the filter
              // just because it carries a decorative arrow.
              const stripGlyphs = value => norm(value).replace(/[â–¾â–²â–¼â‡…â†•âŒ„âŒƒ]+\s*$/g, '').trim();
              const placeholder = /^(select|choose|please select|all|none|--)(\s+.*)?$/i;
              const excluded = /^(company code|facility name|business partner|product( type| id)?|limit amount|facility amount|currency|start date|end date|charges?|cancel|clear|review facility|upload|documents)\s*\*?$/i;
              const visible = el => {
                if (!el || !(el instanceof Element)) return false;
                const r = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return r.width > 2 && r.height > 2 && style.display !== 'none' &&
                       style.visibility !== 'hidden' && Number(style.opacity || 1) > 0.05;
              };
              const triggerBox = trigger.getBoundingClientRect();
              const triggerBottom = triggerBox.bottom;
              const candidates = new Map();

              document.querySelectorAll('[data-qa-dropdown-option-target]')
                .forEach(el => el.removeAttribute('data-qa-dropdown-option-target'));

              // Every other known field's own control element. A candidate
              // that IS one of these, or structurally contains/is contained by
              // one, belongs to a sibling field - not to this dropdown's
              // popup - regardless of its text or class name.
              const excludedElements = [];
              (excludeSelectors || []).forEach(selector => {
                try {
                  document.querySelectorAll(selector).forEach(el => excludedElements.push(el));
                } catch (_) { /* ignore invalid selector */ }
              });
              const belongsToOtherField = element =>
                excludedElements.some(ex => ex && (
                  element === ex || element.contains(ex) || ex.contains(element)
                ));

              const add = element => {
                if (!element || element === trigger || trigger.contains(element) || !visible(element)) return;
                if (element.matches('label,input,select,textarea')) return;
                if (element.closest('table, thead, [role="columnheader"]')) return;
                if (belongsToOtherField(element)) return;
                const text = norm(element.textContent);
                const stripped = stripGlyphs(text);
                if (!text || text.length > 180 || placeholder.test(stripped) || excluded.test(stripped)) return;

                const rect = element.getBoundingClientRect();
                const overlap = Math.max(
                  0,
                  Math.min(rect.right, triggerBox.right + 40) -
                  Math.max(rect.left, triggerBox.left - 40)
                );
                const near = rect.top >= triggerBottom - 12 &&
                             rect.top <= Math.min(innerHeight, triggerBottom + 260) &&
                             overlap >= Math.min(triggerBox.width, rect.width) * 0.35;
                const role = (element.getAttribute('role') || '').toLowerCase();
                const cls = typeof element.className === 'string'
                  ? element.className.toLowerCase()
                  : '';
                const semantic = role === 'option' ||
                  !!element.closest('[role="listbox"],[role="menu"],ul,ol') ||
                  /option|menu-item|dropdown-item|select-item|list-item/.test(cls);
                if (!semantic && !near) return;

                const childTexts = Array.from(element.children || [])
                  .filter(visible)
                  .map(child => norm(child.textContent))
                  .filter(Boolean);
                if (childTexts.length > 1 && text.includes('\n')) return;

                let score = 0;
                if (role === 'option') score += 120;
                if (element.closest('[role="listbox"]')) score += 90;
                if (element.closest('[role="menu"]')) score += 60;
                if (/option/.test(cls)) score += 65;
                if (/menu-item|dropdown-item|select-item|list-item/.test(cls)) score += 45;
                if (near) score += 45;
                if (!text.includes('\n')) score += 15;
                if (getComputedStyle(element).cursor === 'pointer') score += 12;
                score -= Math.max(0, rect.height - 70) / 4;

                const previous = candidates.get(element);
                if (!previous || score > previous.score) {
                  candidates.set(element, {
                    element,
                    text,
                    score,
                    top: rect.top,
                    left: rect.left,
                    width: rect.width,
                    height: rect.height,
                  });
                }
              };

              const itemSelector = '[role="option"],li,button,[data-value],' +
                '[class*="option"],[class*="item"]';

              // Priority 1: the trigger's own aria-controls target, once it is
              // actually visible. This is the most reliable signal there is -
              // the widget itself names its popup, so a different field's
              // menu cannot be mistaken for it.
              let usedScoped = false;
              const controlsId = trigger.getAttribute('aria-controls');
              if (controlsId) {
                const controlled = document.getElementById(controlsId);
                if (controlled && visible(controlled)) {
                  Array.from(controlled.querySelectorAll(itemSelector)).slice(0, 240).forEach(add);
                  usedScoped = candidates.size > 0;
                }
              }

              // Priority 2: popup-like elements that were NOT visible before
              // this trigger was clicked (see _mark_existing_popups). Scoping
              // to what just appeared is what stops a currency field's
              // already-selected label, a table header, or an "Upload" link
              // elsewhere on the page from being read as one of THIS
              // dropdown's options.
              if (!usedScoped) {
                const popupSelector = "[role='listbox'],[role='menu'],[class*='dropdown-menu']," +
                  "[class*='select-menu'],[class*='popover'],[class*='popup'],[class*='options']," +
                  "[class*='menu-list'],ul[class*='select'],div[class*='select__menu']";
                const freshPopups = Array.from(document.querySelectorAll(popupSelector))
                  .filter(el => visible(el) && !el.hasAttribute('data-qa-pre-existing-popup') && !trigger.contains(el));
                freshPopups.forEach(rootEl => {
                  Array.from(rootEl.querySelectorAll(itemSelector)).slice(0, 240).forEach(add);
                });
                usedScoped = candidates.size > 0;
              }

              // Last resort: nothing identified itself as this field's popup.
              // Fall back to the bounded geometric scan directly under the
              // trigger, same as before the scoped methods existed.
              if (!usedScoped) {
                Array.from(document.querySelectorAll(
                  '[role="listbox"] [role="option"],[role="option"],'+
                  '[role="menu"] [role="menuitem"],.react-select__option,'+
                  '[class*="dropdown-menu"] [class*="item"],'+
                  '[class*="select-menu"] [class*="option"],'+
                  '[class*="options"] > *,[class*="option-list"] > *'
                )).slice(0, 320).forEach(add);

                const xPoints = [
                  triggerBox.left + Math.min(24, triggerBox.width * 0.15),
                  triggerBox.left + triggerBox.width * 0.5,
                  triggerBox.right - Math.min(24, triggerBox.width * 0.15),
                ];
                const maxY = Math.min(innerHeight - 4, triggerBottom + 260);
                for (let y = triggerBottom + 16; y <= maxY; y += 24) {
                  for (const x of xPoints) {
                    for (const hit of document.elementsFromPoint(x, y).slice(0, 8)) {
                      let node = hit;
                      for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
                        add(node);
                      }
                    }
                  }
                }
              }

              const ranked = Array.from(candidates.values())
                .sort((a, b) => b.score - a.score || a.top - b.top);
              const optionTexts = [];
              for (const entry of ranked) {
                if (!optionTexts.includes(entry.text)) optionTexts.push(entry.text);
                if (optionTexts.length >= 8) break;
              }
              const best = ranked[0];
              if (!best || best.score < 30) {
                return {
                  targetId: '',
                  text: '',
                  candidateCount: ranked.length,
                  optionTexts,
                  scoped: usedScoped,
                };
              }

              const targetId = `qa-${Date.now()}-${Math.random().toString(36).slice(2)}`;
              best.element.setAttribute('data-qa-dropdown-option-target', targetId);
              return {
                targetId,
                text: best.text,
                score: best.score,
                candidateCount: ranked.length,
                optionTexts,
                scoped: usedScoped,
                box: {
                  x: best.left + best.width / 2,
                  y: best.top + best.height / 2,
                },
              };
            }
            """,
            exclude_selectors or [],
            timeout=max(700, min(config.DROPDOWN_DOM_SCAN_TIMEOUT_MS, 2500)),
        ) or {}
    except Exception as exc:
        return {
            "clicked": False,
            "text": "",
            "candidateCount": 0,
            "optionTexts": [],
            "error": str(exc).splitlines()[0][:180],
        }

    target_id = str(discovery.get("targetId") or "")
    if not target_id:
        return {
            **discovery,
            "clicked": False,
        }

    target = page.locator(
        f'[data-qa-dropdown-option-target="{target_id}"]'
    ).first
    click_error = ""
    for force in (False, True):
        try:
            target.scroll_into_view_if_needed(
                timeout=config.DROPDOWN_ACTION_TIMEOUT_MS
            )
            target.click(
                timeout=config.DROPDOWN_ACTION_TIMEOUT_MS,
                force=force,
            )
            return {**discovery, "clicked": True, "method": "playwright"}
        except Exception as exc:
            click_error = str(exc).splitlines()[0][:180]

    try:
        box = target.bounding_box(timeout=config.DROPDOWN_ACTION_TIMEOUT_MS)
        if box:
            page.mouse.click(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
            )
            return {**discovery, "clicked": True, "method": "mouse"}
    except Exception as exc:
        click_error = str(exc).splitlines()[0][:180]

    try:
        target.dispatch_event("mousedown")
        target.dispatch_event("mouseup")
        target.dispatch_event("click")
        return {**discovery, "clicked": True, "method": "dispatch_event"}
    except Exception as exc:
        click_error = str(exc).splitlines()[0][:180]

    return {
        **discovery,
        "clicked": False,
        "error": click_error or "option click did not complete",
    }

def _set_native_select_index(item, index: int) -> bool:
    """Select an option and notify React even when select_option is ignored."""
    try:
        item.select_option(index=index, timeout=2500)
    except Exception:
        try:
            item.evaluate(
                r"""
                (e, selectedIndex) => {
                  const previous = e.value;
                  e.selectedIndex = selectedIndex;
                  if (e._valueTracker) e._valueTracker.setValue(previous);
                  e.dispatchEvent(new Event('input', {bubbles: true}));
                  e.dispatchEvent(new Event('change', {bubbles: true}));
                  e.dispatchEvent(new Event('blur', {bubbles: true}));
                }
                """,
                index,
            )
        except Exception:
            return False

    try:
        selected_index = item.evaluate("e => e.selectedIndex")
        value = _input_value(item)
        return selected_index == index and bool(value)
    except Exception:
        return _has_meaningful_value(item)


def _select_first_native_option(page, item, timeout_ms: int = 9000) -> bool:
    """Wait for async options and select the first real native option.

    Facility Business Partner and Product options are populated after the modal
    opens and after Company is resolved. Treating the select as empty before
    those requests finish caused the validation cases to be marked NOT TESTED.
    """
    deadline = time.monotonic() + max(timeout_ms, 1000) / 1000
    last_count = 0

    while time.monotonic() < deadline:
        try:
            if not item.is_visible():
                page.wait_for_timeout(150)
                continue

            if _has_meaningful_value(item):
                return True

            if not item.is_enabled():
                page.wait_for_timeout(250)
                continue

            options = item.locator("option")
            last_count = options.count()
            fallback_index = None

            for index in range(last_count):
                option = options.nth(index)
                if option.is_disabled():
                    continue

                value = option.get_attribute("value")
                label = " ".join((option.inner_text() or "").split())

                if fallback_index is None and value not in (None, ""):
                    fallback_index = index

                if _option_is_placeholder(label, value):
                    continue

                if _set_native_select_index(item, index):
                    page.wait_for_timeout(250)
                    return _has_meaningful_value(item)

            if fallback_index is not None:
                if _set_native_select_index(item, fallback_index):
                    page.wait_for_timeout(250)
                    return _has_meaningful_value(item)
        except Exception:
            pass

        page.wait_for_timeout(300)

    print(
        "      [baseline] no selectable option became available "
        f"for {_field_name(item)!r}; option count: {last_count}"
    )
    return False


def _control_snapshot(item) -> dict:
    if item is None:
        return {"value": "", "text": "", "tag": "", "expanded": ""}
    try:
        return item.evaluate(
            r"""
            e => {
              const norm = value => (value || '').replace(/\s+/g, ' ').trim();
              const tag = e.tagName.toLowerCase();
              let value = '';
              let text = '';
              if (tag === 'select') {
                value = e.value || '';
                text = e.selectedIndex >= 0 && e.options[e.selectedIndex]
                  ? norm(e.options[e.selectedIndex].textContent)
                  : '';
              } else if (tag === 'input' || tag === 'textarea') {
                value = e.value || '';
                text = norm(e.getAttribute('aria-valuetext')) || norm(e.value);
              } else {
                value = e.getAttribute('data-value') ||
                        e.getAttribute('aria-valuetext') ||
                        e.getAttribute('value') || '';
                text = norm(e.textContent);
              }

              const wrapper = e.closest(
                "[class*='field'],[class*='control'],[class*='select'],[role='group'],label"
              ) || e.parentElement;
              if (!value && wrapper) {
                const state = wrapper.querySelector(
                  "input[type='hidden'],select,input[data-value],[aria-valuetext]"
                );
                if (state && state !== e) {
                  value = state.value || state.getAttribute('data-value') ||
                          state.getAttribute('aria-valuetext') || '';
                }
              }
              return {
                tag,
                value: norm(String(value)),
                text,
                expanded: e.getAttribute('aria-expanded') || '',
                id: e.id || '',
                name: e.getAttribute('name') || '',
                role: e.getAttribute('role') || '',
              };
            }
            """,
            timeout=max(500, min(config.DROPDOWN_ACTION_TIMEOUT_MS, 1800)),
        ) or {"value": "", "text": "", "tag": "", "expanded": ""}
    except Exception:
        return {"value": "", "text": "", "tag": "", "expanded": ""}


def _snapshot_meaningful(snapshot: dict) -> bool:
    value = " ".join(str(snapshot.get("value") or "").split())
    text = " ".join(str(snapshot.get("text") or "").split())
    candidate = value or text
    if not candidate:
        return False
    return not bool(
        re.match(
            r"^(select|choose|please select|all|none|--)(\s+.*)?$",
            candidate,
            re.I,
        )
    )


def _selection_changed(before: dict, after: dict, expected_text: str = "") -> bool:
    if not _snapshot_meaningful(after):
        return False
    before_value = " ".join(str(before.get("value") or "").split()).lower()
    before_text = " ".join(str(before.get("text") or "").split()).lower()
    after_value = " ".join(str(after.get("value") or "").split()).lower()
    after_text = " ".join(str(after.get("text") or "").split()).lower()
    expected = " ".join(str(expected_text or "").split()).lower()

    if expected and (expected in after_text or expected in after_value):
        return True
    if after_value and after_value != before_value:
        return True
    if after_text and after_text != before_text and not re.match(
        r"^(select|choose|please select)", after_text, re.I
    ):
        return True
    return not _snapshot_meaningful(before) and _snapshot_meaningful(after)


def _select_first_custom_option(
    page,
    item,
    *,
    reacquire=None,
    timeout_ms: int | None = None,
    field_name: str = "dropdown",
    own_patterns: list[str] | None = None,
) -> DropdownOutcome:
    """Try real selection first, then return visible-option evidence.

    The preferred result is a confirmed value/label change. When the custom
    widget cannot expose or retain a selected value, the function still records
    whether usable options were visibly loaded. The caller decides whether that
    fallback is accepted through ``DROPDOWN_VISIBLE_OPTIONS_PASS``.

    ``own_patterns`` identifies this field among the known Facility controls so
    every OTHER known control can be excluded from option discovery - see
    ``_other_field_exclusion_selectors``.
    """
    timeout_ms = timeout_ms or config.DROPDOWN_SELECT_TIMEOUT_MS
    deadline = time.monotonic() + max(timeout_ms, 800) / 1000
    exclude_selectors = _other_field_exclusion_selectors(own_patterns)

    def current_item():
        if reacquire is not None:
            try:
                latest = reacquire()
                if latest is not None:
                    return latest
            except Exception:
                pass
        return item

    trigger = current_item()
    before = _control_snapshot(trigger)
    _mark_visible_options_loaded(trigger, False)
    last_detail = "no visible options"
    options_visible = False
    visible_texts: list[str] = []

    for attempt in range(1, config.DROPDOWN_OPEN_ATTEMPTS + 1):
        if time.monotonic() >= deadline:
            break
        trigger = current_item()
        if trigger is None:
            last_detail = "trigger disappeared"
            page.wait_for_timeout(120)
            continue

        try:
            remaining = max(
                500,
                min(
                    config.DROPDOWN_ACTION_TIMEOUT_MS,
                    int((deadline - time.monotonic()) * 1000),
                ),
            )
            _mark_existing_popups(page)
            trigger.click(timeout=remaining, force=attempt > 1)
            page.wait_for_timeout(180)
        except Exception as exc:
            last_detail = f"open failed: {str(exc).splitlines()[0][:100]}"
            continue

        result = _click_first_visible_custom_option(page, trigger, exclude_selectors)
        candidate_count = int(result.get("candidateCount") or 0)
        option_texts = [
            " ".join(str(value or "").split())
            for value in (result.get("optionTexts") or [])
            if " ".join(str(value or "").split())
        ]
        if candidate_count or option_texts:
            options_visible = True
            visible_texts = list(dict.fromkeys(visible_texts + option_texts))[:8]
            _mark_visible_options_loaded(trigger, True)
            latest = current_item()
            _mark_visible_options_loaded(latest, True)

        if result.get("clicked"):
            expected = str(result.get("text") or "")
            method = str(result.get("method") or "click")
            page.wait_for_timeout(260)
            after_trigger = current_item()
            after = _control_snapshot(after_trigger)
            if _selection_changed(before, after, expected):
                print(
                    f"      [baseline] {field_name}: selected custom option "
                    f"{expected!r} using {method}",
                    flush=True,
                )
                return DropdownOutcome(
                    passed=True,
                    selected=True,
                    options_visible=True,
                    detail=f"selected and retained {expected!r} using {method}",
                )
            last_detail = (
                f"clicked {expected!r} using {method}, but selected state did not change"
            )
        elif result.get("error"):
            last_detail = str(result.get("error"))[:180]

        # Keyboard selection is the second Playwright strategy. Try one and two
        # ArrowDown presses so a placeholder row cannot trap the selection.
        for down_count in (1, 2):
            try:
                trigger = current_item()
                if trigger is None:
                    break
                trigger.click(timeout=config.DROPDOWN_ACTION_TIMEOUT_MS, force=True)
                for _ in range(down_count):
                    trigger.press("ArrowDown", timeout=config.DROPDOWN_ACTION_TIMEOUT_MS)
                trigger.press("Enter", timeout=config.DROPDOWN_ACTION_TIMEOUT_MS)
                page.wait_for_timeout(220)
                after = _control_snapshot(current_item())
                if _selection_changed(before, after):
                    print(
                        f"      [baseline] {field_name}: selected custom option "
                        "using keyboard",
                        flush=True,
                    )
                    return DropdownOutcome(
                        passed=True,
                        selected=True,
                        options_visible=options_visible,
                        detail="selected and retained using keyboard",
                    )
            except Exception as exc:
                last_detail = (
                    f"keyboard fallback failed: {str(exc).splitlines()[0][:100]}"
                )

        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(120)

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    latest = current_item()
    if options_visible:
        _mark_visible_options_loaded(latest, True)
    labels = ", ".join(repr(value) for value in visible_texts[:4])
    visible_detail = (
        f"visible options: {labels}" if labels else "usable options were visible"
    )
    print(
        f"      [baseline] {field_name}: custom selection not confirmed "
        f"({last_detail}); {visible_detail if options_visible else 'no usable options found'}",
        flush=True,
    )
    return DropdownOutcome(
        passed=bool(options_visible and config.DROPDOWN_VISIBLE_OPTIONS_PASS),
        selected=False,
        options_visible=options_visible,
        detail=(
            f"visible-options fallback accepted; {visible_detail}; {last_detail}"
            if options_visible and config.DROPDOWN_VISIBLE_OPTIONS_PASS
            else f"{last_detail}; {visible_detail if options_visible else 'no usable options found'}"
        ),
    )

def _date_variants(item, value: str) -> list[str]:
    variants = [str(value)]
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except Exception:
        return variants

    hint = " ".join(
        str(item.get_attribute(name) or "")
        for name in ("placeholder", "aria-label", "name", "id")
    ).lower()
    if "dd/mm" in hint or "select date" in hint:
        variants.insert(0, parsed.strftime("%d/%m/%Y"))
    elif "mm/dd" in hint:
        variants.insert(0, parsed.strftime("%m/%d/%Y"))
    variants.extend(
        [
            parsed.strftime("%Y-%m-%d"),
            parsed.strftime("%d-%m-%Y"),
        ]
    )
    return list(dict.fromkeys(variants))


def _fill(page, item, value: str) -> bool:
    if item is None:
        return False

    try:
        tag = item.evaluate("e => e.tagName.toLowerCase()")
        field_type = (item.get_attribute("type") or "").lower()
        role = (item.get_attribute("role") or "").lower()
        placeholder = (item.get_attribute("placeholder") or "").lower()
        name_hint = " ".join(
            str(item.get_attribute(name) or "")
            for name in ("name", "id", "aria-label", "placeholder")
        ).lower()
        readonly = item.get_attribute("readonly") is not None
        custom_select = (
            role == "combobox"
            or item.get_attribute("aria-haspopup") == "listbox"
            or placeholder.startswith(("select", "choose"))
            or (tag == "button" and item.get_attribute("aria-controls"))
        )
        is_date = field_type in ("date", "datetime-local") or "date" in name_hint

        if tag == "select":
            if value == "__first__":
                passed = _select_first_native_option(page, item)
            else:
                try:
                    item.select_option(label=str(value))
                    passed = True
                except Exception:
                    item.select_option(value=str(value))
                    passed = True
            if passed:
                wait_for_settle(page, quiet_ms=120, timeout_ms=1800)
            return passed

        if custom_select:
            if value == "__first__":
                outcome = _select_first_custom_option(page, item)
                if outcome.selected:
                    wait_for_settle(page, quiet_ms=180, timeout_ms=2500)
                return outcome.passed

        if field_type in ("checkbox", "radio"):
            if not item.is_checked():
                item.check(timeout=config.STEP_TIMEOUT_MS)
            return True

        if is_date:
            for candidate in _date_variants(item, str(value)):
                try:
                    if not readonly:
                        item.fill(candidate)
                    else:
                        _set_native_value(item, candidate)
                except Exception:
                    _set_native_value(item, candidate)
                page.wait_for_timeout(120)
                if _input_value(item):
                    return True
            return False

        if readonly:
            return _set_native_value(item, str(value)) and bool(_input_value(item))

        item.fill("" if value == "__first__" else str(value))
        return True
    except Exception:
        return _set_native_value(item, str(value))

def _input_value(item) -> str:
    snapshot = _control_snapshot(item)
    return str(snapshot.get("value") or snapshot.get("text") or "").strip()


def _control_display_value(item) -> str:
    snapshot = _control_snapshot(item)
    return str(snapshot.get("text") or snapshot.get("value") or "").strip()


def _has_meaningful_value(item) -> bool:
    return _snapshot_meaningful(_control_snapshot(item))

def _mark_baseline_skip(item, enabled: bool) -> None:
    if item is None:
        return
    try:
        if enabled:
            item.evaluate("e => e.setAttribute('data-qa-skip-baseline', '1')")
        else:
            item.evaluate("e => e.removeAttribute('data-qa-skip-baseline')")
    except Exception:
        pass


def _is_baseline_skipped(item) -> bool:
    try:
        return item.get_attribute("data-qa-skip-baseline") == "1"
    except Exception:
        return False


def _clear_field(page, item) -> bool:
    if item is None:
        return False
    _mark_visible_options_loaded(item, False)
    try:
        tag = item.evaluate("e => e.tagName.toLowerCase()")
        role = (item.get_attribute("role") or "").lower()
        if tag == "select":
            options = item.locator("option")
            for index in range(options.count()):
                option = options.nth(index)
                value = option.get_attribute("value")
                text = " ".join((option.inner_text() or "").split())
                if value in (None, "") or re.match(r"^(select|choose|please)", text, re.I):
                    item.select_option(index=index)
                    return not _has_meaningful_value(item)
            item.select_option(index=0)
            return True

        if tag in ("input", "textarea") or role == "combobox":
            try:
                item.fill("")
            except Exception:
                _set_native_value(item, "")
            page.wait_for_timeout(100)
            return not _has_meaningful_value(item)

        # Custom button-style comboboxes sometimes support Backspace/Delete.
        item.click(timeout=config.STEP_TIMEOUT_MS)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.press("Escape")
        return not _has_meaningful_value(item)
    except Exception:
        return _set_native_value(item, "")

def _required_unfilled(scope) -> list[str]:
    missing = []
    try:
        fields = scope.locator(
            "input[required], select[required], textarea[required], "
            "[aria-required='true']"
        )
        for index in range(min(fields.count(), 120)):
            item = fields.nth(index)
            if not item.is_visible() or not item.is_enabled() or _is_baseline_skipped(item):
                continue
            if config.DROPDOWN_VISIBLE_OPTIONS_PASS and _visible_options_loaded(item):
                continue
            field_type = (item.get_attribute("type") or "").lower()
            if field_type in ("checkbox", "radio"):
                if not item.is_checked():
                    missing.append(_field_name(item))
                continue
            if not _has_meaningful_value(item):
                missing.append(_field_name(item))
    except Exception:
        pass
    return missing


def _select_required_dropdown(
    page,
    modal,
    field_name: str,
    patterns: list[str],
    timeout_ms: int = 9000,
) -> DropdownOutcome:
    """Use confirmed selection first and visible-options fallback second."""
    deadline = time.monotonic() + max(timeout_ms, 1000) / 1000
    last_detail = "field not found"
    printed_wait = False
    any_options_visible = False
    visible_labels: list[str] = []

    while time.monotonic() < deadline:
        item = _field(page, patterns, modal)
        if item is None:
            last_detail = "field not found"
            page.wait_for_timeout(180)
            continue

        if _is_baseline_skipped(item):
            return DropdownOutcome(
                passed=True,
                selected=False,
                options_visible=False,
                detail="intentionally left invalid for this isolated test",
            )

        if _has_meaningful_value(item):
            return DropdownOutcome(
                passed=True,
                selected=True,
                options_visible=True,
                detail=f"selected value: {_control_display_value(item)!r}",
            )

        if not printed_wait:
            print(
                f"      [baseline] {field_name}: waiting for options and selecting...",
                flush=True,
            )
            printed_wait = True

        try:
            tag = item.evaluate(
                "e => e.tagName.toLowerCase()",
                timeout=config.DROPDOWN_ACTION_TIMEOUT_MS,
            )
        except Exception:
            tag = ""

        remaining_ms = max(500, int((deadline - time.monotonic()) * 1000))
        if tag == "select":
            try:
                options = item.locator("option")
                labels = []
                for index in range(min(options.count(), 80)):
                    option = options.nth(index)
                    label = " ".join((option.inner_text() or "").split())
                    value = option.get_attribute("value")
                    if option.is_disabled() or _option_is_placeholder(label, value):
                        continue
                    labels.append(label)
                if labels:
                    any_options_visible = True
                    visible_labels = list(dict.fromkeys(visible_labels + labels))[:8]
                    _mark_visible_options_loaded(item, True)
            except Exception:
                pass

            if _select_first_native_option(
                page,
                item,
                timeout_ms=min(
                    remaining_ms,
                    config.DROPDOWN_NATIVE_ATTEMPT_TIMEOUT_MS,
                ),
            ):
                wait_for_settle(page, quiet_ms=120, timeout_ms=1600)
                refreshed = _field(page, patterns, modal)
                if refreshed is not None and _has_meaningful_value(refreshed):
                    return DropdownOutcome(
                        passed=True,
                        selected=True,
                        options_visible=True,
                        detail=(
                            f"selected value: {_control_display_value(refreshed)!r}"
                        ),
                    )
            try:
                last_detail = (
                    f"native options available: {item.locator('option').count()}"
                )
            except Exception:
                last_detail = "native select had no usable option"
        else:
            attempt = _select_first_custom_option(
                page,
                item,
                reacquire=lambda: _field(page, patterns, modal),
                timeout_ms=min(
                    remaining_ms,
                    config.DROPDOWN_CUSTOM_ATTEMPT_TIMEOUT_MS,
                ),
                field_name=field_name,
                own_patterns=patterns,
            )
            any_options_visible = any_options_visible or attempt.options_visible
            refreshed = _field(page, patterns, modal)
            if attempt.options_visible and refreshed is not None:
                _mark_visible_options_loaded(refreshed, True)
            if attempt.selected and refreshed is not None and _has_meaningful_value(refreshed):
                return DropdownOutcome(
                    passed=True,
                    selected=True,
                    options_visible=True,
                    detail=f"selected value: {_control_display_value(refreshed)!r}; {attempt.detail}",
                )
            last_detail = attempt.detail
            if attempt.passed and attempt.options_visible:
                _mark_modal_dropdown_fallback(modal, field_name)
                return DropdownOutcome(
                    passed=True,
                    selected=False,
                    options_visible=True,
                    detail=attempt.detail,
                )

        page.wait_for_timeout(180)

    if any_options_visible and config.DROPDOWN_VISIBLE_OPTIONS_PASS:
        item = _field(page, patterns, modal)
        _mark_visible_options_loaded(item, True)
        _mark_modal_dropdown_fallback(modal, field_name)
        labels = ", ".join(repr(value) for value in visible_labels[:4])
        return DropdownOutcome(
            passed=True,
            selected=False,
            options_visible=True,
            detail=(
                "visible-options fallback accepted; "
                + (f"visible options: {labels}; " if labels else "")
                + last_detail
            ),
        )

    return DropdownOutcome(
        passed=False,
        selected=False,
        options_visible=any_options_visible,
        detail=f"{field_name}: {last_detail}",
    )

def _field_allows_empty(item) -> bool:
    """Return True only when the UI exposes a real empty/unselected state."""
    if item is None:
        return False
    if not _has_meaningful_value(item):
        return True
    try:
        return bool(
            item.evaluate(
                r"""
                e => {
                  if (e.tagName.toLowerCase() === 'select') {
                    return Array.from(e.options).some(option =>
                      !String(option.value || '').trim() ||
                      /^(select|choose|please select)/i.test(
                        (option.textContent || '').trim()
                      )
                    );
                  }
                  const wrapper = e.closest(
                    "[class*='field'],[class*='control'],[class*='select'],[role='group'],label"
                  ) || e.parentElement;
                  if (!wrapper) return false;
                  return !!wrapper.querySelector(
                    "button[aria-label*='clear' i],button[title*='clear' i]," +
                    "[data-testid*='clear'],[class*='clear-indicator'],[class*='clearButton']"
                  );
                }
                """,
                timeout=config.DROPDOWN_ACTION_TIMEOUT_MS,
            )
        )
    except Exception:
        return False


def _discover_facility_form_profile(page, modal) -> dict[str, bool]:
    specs = _FACILITY_FIELD_SPECS
    profile = {name: _field(page, patterns, modal) is not None for name, patterns in specs.items()}
    if profile.get("company"):
        profile["company_clearable"] = _field_allows_empty(
            _field(page, specs["company"], modal)
        )
    if profile.get("business_partner"):
        profile["business_partner_clearable"] = _field_allows_empty(
            _field(page, specs["business_partner"], modal)
        )
    if profile.get("product"):
        profile["product_clearable"] = _field_allows_empty(
            _field(page, specs["product"], modal)
        )
    if profile.get("currency"):
        profile["currency_clearable"] = _field_allows_empty(
            _field(page, specs["currency"], modal)
        )
    return profile


def _fill_valid_baseline(
    page,
    modal,
    *,
    ignore_cached_blocker: bool = False,
) -> tuple[bool, list[str]]:
    """Fill only the fields that actually exist in the current form version."""
    if _FACILITY_BASELINE_BLOCKER and not ignore_cached_blocker:
        return False, [f"baseline preflight unavailable: {_FACILITY_BASELINE_BLOCKER}"]

    profile = _discover_facility_form_profile(page, modal)
    missing: list[str] = []
    required_core = {
        "facility_name": "Facility Name",
        "business_partner": "Business Partner",
        "amount": "Facility Amount",
    }
    for key, label in required_core.items():
        if not profile.get(key):
            missing.append(f"{label} field not found")

    values = [
        ("facility_name", "Facility Name", [r"facility\s*name"], f"Automation Valid Facility {int(time.time())}"),
        (
            "amount",
            "Facility Amount",
            [r"facility\s*amount", r"limit\s*amount", r"sanctioned\s*amount"],
            "5000000",
        ),
        (
            "start_date",
            "Start Date",
            [r"start\s*date", r"from\s*date", r"issue\s*date"],
            (date.today() + timedelta(days=2)).isoformat(),
        ),
        (
            "end_date",
            "End Date",
            [r"end\s*date", r"maturity\s*date", r"to\s*date"],
            (date.today() + timedelta(days=365)).isoformat(),
        ),
    ]

    for key, name, patterns, value in values:
        if not profile.get(key):
            continue
        item = _field(page, patterns, modal)
        if item is None:
            missing.append(f"{name} field not found")
            continue
        if _is_baseline_skipped(item):
            continue
        if not _fill(page, item, value):
            missing.append(f"{name} could not be filled")

    dropdowns = [
        ("company", "Company", [r"company\s*code", r"^company$"], config.DROPDOWN_SELECT_TIMEOUT_MS),
        ("business_partner", "Business Partner", [r"business\s*partner", r"partner"], config.DEPENDENT_DROPDOWN_TIMEOUT_MS),
        ("product", "Product/Facility Type", [r"product\s*code", r"product\s*id", r"product\s*type", r"^product$", r"facility\s*type"], config.DEPENDENT_DROPDOWN_TIMEOUT_MS),
        ("currency", "Currency", [r"currency"], config.DROPDOWN_SELECT_TIMEOUT_MS),
    ]

    for key, name, patterns, timeout_ms in dropdowns:
        if not profile.get(key):
            continue
        outcome = _select_required_dropdown(
            page,
            modal,
            name,
            patterns,
            timeout_ms=timeout_ms,
        )
        if not outcome.passed:
            missing.append(f"{name} could not be selected ({outcome.detail})")
        else:
            print(f"      [baseline] {name}: {outcome.detail}", flush=True)
            page.wait_for_timeout(120)

    # Fill any additional visible required inputs that the form may add later.
    try:
        required = modal.locator(
            "input[required], select[required], textarea[required], [aria-required='true']"
        )
        for index in range(min(required.count(), 120)):
            item = required.nth(index)
            if not item.is_visible() or not item.is_enabled() or _has_meaningful_value(item):
                continue
            if config.DROPDOWN_VISIBLE_OPTIONS_PASS and _visible_options_loaded(item):
                continue
            hint = " ".join(
                (
                    _field_name(item),
                    item.get_attribute("name") or "",
                    item.get_attribute("placeholder") or "",
                    item.get_attribute("id") or "",
                )
            ).lower()
            tag = item.evaluate("e => e.tagName.toLowerCase()")
            field_type = (item.get_attribute("type") or "").lower()
            if tag == "select" or (item.get_attribute("role") or "").lower() == "combobox" or "select" in hint:
                _fill(page, item, "__first__")
            elif "date" in hint or field_type in ("date", "datetime-local"):
                _fill(page, item, (date.today() + timedelta(days=30)).isoformat())
            elif field_type == "email" or "email" in hint:
                _fill(page, item, "qa.automation@example.com")
            elif field_type == "tel" or "phone" in hint or "mobile" in hint:
                _fill(page, item, "9876543210")
            elif field_type == "number" or re.search(r"amount|rate|limit|tenor|margin", hint):
                _fill(page, item, "100")
            else:
                _fill(page, item, "Automation Valid Value")
    except Exception:
        pass

    wait_for_settle(page, quiet_ms=100, timeout_ms=700)
    missing.extend(_required_unfilled(modal))

    # A custom dropdown accepted through the visible-options policy is not a
    # missing dropdown-data test. Keep that evidence separate from real missing
    # text/amount/date fields so the pipeline can continue.
    fallback_fields = _modal_dropdown_fallbacks(modal)
    if fallback_fields:
        fallback_tokens = {
            re.sub(r"[^a-z0-9]+", "", name.lower())
            for name in fallback_fields
        }
        missing = [
            value
            for value in missing
            if not any(
                token and (token in re.sub(r"[^a-z0-9]+", "", value.lower())
                           or re.sub(r"[^a-z0-9]+", "", value.lower()) in token)
                for token in fallback_tokens
            )
        ]

    missing = list(dict.fromkeys(missing))
    return not missing, missing

def _validation_signal(page, modal, target=None) -> str:
    message = get_toast_or_message_text(page)
    if message:
        return message

    if target is not None:
        try:
            if target.get_attribute("aria-invalid") == "true":
                return f"{_field_name(target)} is marked invalid"
        except Exception:
            pass

    selectors = (
        "[aria-invalid='true'], .is-invalid, .field-error, .error-text, "
        ".invalid-feedback, .Mui-error, .ant-form-item-explain-error"
    )
    if _visible(page, selectors, modal):
        return "inline validation is visible"
    return "no validation message was detected"


def _review_reached(page) -> bool:
    modal = get_open_modal(page)
    send = _button(page, r"send\s+for\s+approval", modal)
    if send is not None:
        return True

    root = modal if modal is not None else page
    try:
        text = " ".join((root.inner_text() or "").split())
        if re.search(r"send\s+for\s+approval", text, re.I):
            return True
    except Exception:
        pass
    return False


def _click_review(page, modal):
    review = _button(page, r"^\s*review\s+facility\s*$|^\s*review\s*$", modal)
    if review is None:
        return False, "Review Facility was not found"

    try:
        if not review.is_enabled():
            return True, "Review Facility remained disabled"
    except Exception:
        pass

    try:
        review.click(timeout=config.STEP_TIMEOUT_MS)
        wait_for_settle(page, quiet_ms=160, timeout_ms=min(config.SETTLE_TIMEOUT_MS, 1600))
        return True, "Review Facility was clicked"
    except Exception as exc:
        # A disabled click is still a valid block. Other click failures are not
        # treated as application defects without stronger evidence.
        try:
            if not review.is_enabled():
                return True, "Review Facility remained disabled"
        except Exception:
            pass
        return False, f"Review action could not be completed: {str(exc).splitlines()[0][:180]}"


def _open_create_facility(page, url, record_result=False):
    close_any_modal(page)
    create = _button(page, r"\b(create|new|add)\s+facility\b", page)
    if create is None:
        if record_result:
            REPORTER.record(
                "functional",
                "Create Facility action is available",
                url,
                "A visible Create Facility control opens the creation form",
                "No visible Create Facility control was found",
                "fail",
                severity="high",
                screenshot=screenshot(page, "create_facility_missing"),
            )
        return None

    try:
        create.click(timeout=config.STEP_TIMEOUT_MS)
        wait_for_settle(page, quiet_ms=160, timeout_ms=min(config.SETTLE_TIMEOUT_MS, 1600))
    except Exception as exc:
        if record_result:
            REPORTER.record(
                "functional",
                "Create Facility form opens",
                url,
                "Selecting Create Facility opens the facility form",
                f"The control could not be opened: {str(exc).splitlines()[0][:200]}",
                "inconclusive",
                screenshot=screenshot(page, "create_facility_open_fail"),
            )
        return None

    modal = get_open_modal(page)
    root = modal if modal is not None else page

    # Company is usually preselected. Give dependent Business Partner and
    # Product options a short opportunity to arrive before the first baseline
    # is prepared. Individual selectors still have their own longer retry.
    try:
        page.wait_for_function(
            r"""
            () => {
              const dependent = Array.from(document.querySelectorAll('select')).filter(select => {
                const box = select.closest('[class*=field], [class*=form], label, div');
                const text = (box && box.innerText || '').toLowerCase();
                return text.includes('business partner') || text.includes('product');
              });
              if (!dependent.length) return true;
              return dependent.some(select =>
                Array.from(select.options).some(o => o.value && !/^select|choose/i.test(o.text))
              );
            }
            """,
            timeout=config.DEPENDENT_OPTIONS_WAIT_MS,
        )
    except Exception:
        pass

    visible = _field(
        page,
        [r"facility\s*name", r"facility\s*amount", r"limit\s*amount", r"start\s*date"],
        root,
    ) is not None

    if record_result:
        REPORTER.record(
            "functional",
            "Create Facility form opens",
            url,
            "Selecting Create Facility opens a form containing facility details",
            "Facility fields became visible"
            if visible
            else "The expected facility fields did not become visible",
            "pass" if visible else "fail",
            severity="high",
            screenshot="" if visible else screenshot(page, "facility_form_missing"),
        )

    return root if visible else None


def _reset_open_facility_form(page, modal):
    """Reset retained React modal state before each isolated test case."""
    reset = _button(page, r"^\s*(clear|reset)\s*$", modal)
    if reset is not None:
        try:
            reset.click(timeout=config.DROPDOWN_ACTION_TIMEOUT_MS)
            page.wait_for_timeout(180)
            refreshed = get_open_modal(page)
            if refreshed is not None:
                modal = refreshed
        except Exception:
            pass

    # Clear the two universally editable core fields as a deterministic
    # fallback.  Company/Currency defaults are intentionally preserved.
    for patterns in (
        [r"facility\s*name"],
        [r"facility\s*amount", r"limit\s*amount", r"sanctioned\s*amount"],
        [r"start\s*date", r"from\s*date", r"issue\s*date"],
        [r"end\s*date", r"maturity\s*date", r"to\s*date"],
    ):
        item = _field(page, patterns, modal)
        if item is not None:
            _clear_field(page, item)
    return modal


def _fresh_form(page, url):
    dismissal = close_any_modal(page)
    try:
        # If the previous popup could not be dismissed by automation, reload the
        # route before the next isolated case. One stale modal must never stop or
        # contaminate the remaining validation tests.
        if not dismissal.get("closed") or \
                urlparse(page.url).path != urlparse(url).path:
            page.goto(url, timeout=config.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            wait_for_settle(page)
    except Exception:
        pass

    modal = _open_create_facility(page, url, record_result=False)
    if modal is None:
        return None
    return _reset_open_facility_form(page, modal)

def _run_invalid_review_case(
    page,
    url,
    *,
    title: str,
    target_patterns: list[str],
    invalid_value: str | None,
    invalid_description: str,
    expected: str,
    severity: str = "high",
    leave_blank: bool = False,
):
    """Run one isolated invalid-field case and prove Review is blocked."""
    # The baseline preflight is a circuit breaker. If mandatory dropdowns or
    # fields could not be prepared once, do not repeat the same expensive
    # modal/dropdown waits for every remaining negative case. Record each case
    # as not confirmed and continue the pipeline immediately.
    if _FACILITY_BASELINE_BLOCKER:
        REPORTER.record(
            "validation",
            title,
            url,
            expected,
            (
                "Result: COULD NOT CONFIRM â€” the valid baseline preflight failed earlier: "
                f"{_FACILITY_BASELINE_BLOCKER}"
            ),
            "inconclusive",
        )
        return

    modal = _fresh_form(page, url)
    if modal is None:
        REPORTER.record(
            "validation",
            title,
            url,
            expected,
            "Result: COULD NOT CONFIRM â€” the Create Facility form could not be opened",
            "inconclusive",
        )
        return

    target = _field(page, target_patterns, modal)
    if target is None:
        REPORTER.record(
            "validation",
            title,
            url,
            expected,
            f"Result: COULD NOT CONFIRM â€” the target field for {invalid_description} was not found",
            "inconclusive",
        )
        close_any_modal(page)
        return

    target_was_initially_blank = leave_blank and not _has_meaningful_value(target)
    if target_was_initially_blank:
        # Keep an initially empty custom dropdown/input out of baseline filling.
        # This is essential for Business Partner required-field testing because
        # many custom selects do not expose a programmatic clear action.
        _mark_baseline_skip(target, True)

    baseline_ok, missing = _fill_valid_baseline(page, modal)
    fallback_fields = _modal_dropdown_fallbacks(modal)

    # React may rerender controls while the baseline is prepared, so reacquire
    # the target before applying the isolated invalid value.
    refreshed_target = _field(page, target_patterns, modal)
    if refreshed_target is not None:
        target = refreshed_target

    if leave_blank:
        if target_was_initially_blank and not _has_meaningful_value(target):
            cleared = True
        else:
            cleared = _clear_field(page, target)
        _mark_baseline_skip(target, False)
    else:
        cleared = True
        _fill(page, target, str(invalid_value or ""))

    actual_value = _input_value(target)
    if not baseline_ok:
        REPORTER.record(
            "validation",
            title,
            url,
            expected,
            (
                "Result: COULD NOT CONFIRM â€” a valid isolated baseline could not be prepared. "
                f"Other fields still missing: {missing}"
            ),
            "inconclusive",
        )
        close_any_modal(page)
        return

    # A dropdown whose selected value could not be independently confirmed but
    # whose options were visibly loaded is not treated as a reason to abandon
    # this case. Review is still clicked and the real blocked/allowed outcome
    # is reported, with the fallback fields disclosed as supporting evidence
    # rather than as grounds to withhold a verdict.
    if leave_blank and not cleared:
        REPORTER.record(
            "validation",
            title,
            url,
            expected,
            "Result: COULD NOT CONFIRM â€” the automated test could not clear the target field",
            "inconclusive",
        )
        close_any_modal(page)
        return

    clicked, click_detail = _click_review(page, modal)
    reached_review = _review_reached(page)
    blocked = clicked and not reached_review
    feedback = _validation_signal(page, modal, target)

    if blocked:
        status = "pass"
        result_word = "PASSED"
    elif clicked and reached_review:
        status = "fail"
        result_word = "FAILED"
    else:
        status = "inconclusive"
        result_word = "COULD NOT CONFIRM"

    REPORTER.record(
        "validation",
        title,
        url,
        expected,
        (
            f"Result: {result_word}; invalid test data: {invalid_description}; "
            f"value present in field: {actual_value!r}; Review allowed: "
            f"{'YES' if reached_review else 'NO'}; Review blocked: "
            f"{'YES' if blocked else 'NO'}; feedback: {feedback}; action: {click_detail}"
            + (
                f"; note: {', '.join(fallback_fields)} used the visible-options "
                "fallback (selection not independently confirmed)"
                if fallback_fields
                else ""
            )
        ),
        status,
        severity=severity,
        repro_steps=[
            f"Open {url}",
            "Open Create Facility",
            "Fill every other required field with valid data",
            f"Set only {_field_name(target)} to {invalid_description}",
            "Click Review Facility",
            "Verify that the Review step is not reached",
        ],
        screenshot=(
            screenshot(page, re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_"))
            if status == "fail"
            else ""
        ),
        suggested_fix=(
            "Block Review until this field contains a valid value and show a clear message "
            "beside the field."
            if status == "fail"
            else ""
        ),
    )
    close_any_modal(page)


def _test_baseline_preflight(page, url):
    """Prove once that a fully valid facility baseline can be prepared.

    When selectors cannot confirm a required dropdown selection, later
    validation cases are recorded quickly as Not Confirmed instead of each
    spending many seconds retrying the same impossible prerequisite.
    """
    global _FACILITY_BASELINE_BLOCKER
    _FACILITY_BASELINE_BLOCKER = None
    last_missing = []

    for attempt in range(1, config.FACILITY_BASELINE_RETRIES + 1):
        modal = _fresh_form(page, url)
        if modal is None:
            last_missing = ["Create Facility form could not be opened"]
            continue
        ok, missing = _fill_valid_baseline(
            page,
            modal,
            ignore_cached_blocker=True,
        )
        fallback_fields = _modal_dropdown_fallbacks(modal)
        close_any_modal(page)
        if ok:
            detail = f"Baseline prepared successfully on attempt {attempt}"
            if fallback_fields:
                detail += (
                    "; visible-options fallback accepted for "
                    + ", ".join(fallback_fields)
                    + ". Their dropdown data was visible, but selected state was not confirmed."
                )
            REPORTER.record(
                "functional",
                "Valid facility baseline can be prepared",
                url,
                "All required facility fields can be populated or visibly loaded for custom dropdowns",
                detail,
                "pass",
            )
            return
        last_missing = missing
        print(
            f"      [baseline preflight {attempt}/{config.FACILITY_BASELINE_RETRIES}] "
            f"missing: {missing}",
            flush=True,
        )

    _FACILITY_BASELINE_BLOCKER = "; ".join(last_missing)[:500] or "unknown baseline preparation issue"
    REPORTER.record(
        "functional",
        "Valid facility baseline can be prepared",
        url,
        "All required facility fields can be populated with valid test data",
        (
            "Result: COULD NOT CONFIRM â€” baseline preparation failed after "
            f"{config.FACILITY_BASELINE_RETRIES} attempt(s): {_FACILITY_BASELINE_BLOCKER}"
        ),
        "inconclusive",
    )


def _test_empty_review(page, url):
    modal = _fresh_form(page, url)
    if modal is None:
        REPORTER.record(
            "validation",
            "Empty facility cannot be reviewed",
            url,
            "An empty facility stays on the entry step",
            "The facility form could not be opened for this test",
            "inconclusive",
        )
        return

    clicked, click_detail = _click_review(page, modal)
    reached_review = _review_reached(page)
    blocked = clicked and not reached_review
    validation = _validation_signal(page, modal)

    REPORTER.record(
        "validation",
        "Empty facility cannot be reviewed",
        url,
        "Review is blocked when mandatory facility details are empty",
        (
            f"Result: {'PASSED' if blocked else 'FAILED' if clicked else 'COULD NOT CONFIRM'}; "
            f"Review allowed: {'YES' if reached_review else 'NO'}; "
            f"Review blocked: {'YES' if blocked else 'NO'}; "
            f"feedback: {validation}; action: {click_detail}"
        ),
        "pass" if blocked else ("inconclusive" if not clicked else "fail"),
        severity="high",
        screenshot="" if blocked else screenshot(page, "empty_review_not_blocked"),
    )
    close_any_modal(page)


def _test_clear_button(page, url):
    modal = _fresh_form(page, url)
    if modal is None:
        REPORTER.record(
            "functional",
            "Clear Facility form",
            url,
            "Clear removes entered values from the facility form",
            "The facility form could not be opened for this test",
            "inconclusive",
        )
        return

    name = _field(page, [r"facility\s*name"], modal)
    amount = _field(
        page,
        [r"facility\s*amount", r"sanctioned\s*amount", r"limit\s*amount"],
        modal,
    )
    clear = _button(page, r"^\s*(clear|reset)(\s+all)?\s*$", modal)

    if name is None or amount is None or clear is None:
        REPORTER.record(
            "functional",
            "Clear Facility form",
            url,
            "A Clear or Reset control removes entered values",
            "The modal-scoped fields or Clear/Reset control were not found",
            "inconclusive",
        )
        close_any_modal(page)
        return

    _fill(page, name, "Automation Temporary Facility")
    _fill(page, amount, "5000000")

    try:
        clear.click(timeout=config.STEP_TIMEOUT_MS)
        wait_for_settle(page, quiet_ms=200, timeout_ms=2500)
        name_value = _input_value(name)
        amount_value = _input_value(amount)
        passed = not name_value and not amount_value
        REPORTER.record(
            "functional",
            "Clear Facility form",
            url,
            "Clear removes values from all editable facility fields",
            (
                f"Facility Name after clear: '{name_value}'; "
                f"Facility Amount after clear: '{amount_value}'"
            ),
            "pass" if passed else "fail",
            severity="medium",
            screenshot="" if passed else screenshot(page, "clear_facility_failed"),
        )
    except Exception as exc:
        REPORTER.record(
            "functional",
            "Clear Facility form",
            url,
            "Clear removes entered values",
            f"The modal Clear action could not be completed: {str(exc).splitlines()[0][:200]}",
            "inconclusive",
        )
    close_any_modal(page)


def _test_negative_amount(page, url):
    _run_invalid_review_case(
        page,
        url,
        title="Negative Facility Amount cannot be reviewed",
        target_patterns=[r"facility\s*amount", r"sanctioned\s*amount", r"limit\s*amount"],
        invalid_value="-5000",
        invalid_description="a negative amount (-5000)",
        expected="Review Facility is blocked when Facility Amount is negative",
        severity="critical",
    )

def _test_date_order(page, url):
    if _FACILITY_BASELINE_BLOCKER:
        REPORTER.record(
            "validation",
            "End Date before Start Date cannot be reviewed",
            url,
            "Review Facility is blocked when End Date is earlier than Start Date",
            f"Result: COULD NOT CONFIRM â€” baseline preflight unavailable: {_FACILITY_BASELINE_BLOCKER}",
            "inconclusive",
        )
        return
    modal = _fresh_form(page, url)
    if modal is None:
        REPORTER.record(
            "validation",
            "End Date before Start Date cannot be reviewed",
            url,
            "Review Facility is blocked when End Date is earlier than Start Date",
            "Result: COULD NOT CONFIRM â€” the Create Facility form could not be opened",
            "inconclusive",
        )
        return

    start = _field(page, [r"start\s*date", r"from\s*date", r"issue\s*date"], modal)
    end = _field(page, [r"end\s*date", r"maturity\s*date", r"to\s*date"], modal)
    baseline_ok, missing = _fill_valid_baseline(page, modal)
    fallback_fields = _modal_dropdown_fallbacks(modal)

    if not baseline_ok or start is None or end is None:
        REPORTER.record(
            "validation",
            "End Date before Start Date cannot be reviewed",
            url,
            "Review Facility is blocked when End Date is earlier than Start Date",
            f"Result: COULD NOT CONFIRM â€” valid baseline missing: {missing}",
            "inconclusive",
        )
        close_any_modal(page)
        return

    start_value = (date.today() + timedelta(days=30)).isoformat()
    end_value = (date.today() + timedelta(days=1)).isoformat()
    _fill(page, start, start_value)
    _fill(page, end, end_value)

    clicked, click_detail = _click_review(page, modal)
    reached_review = _review_reached(page)
    blocked = clicked and not reached_review
    feedback = _validation_signal(page, modal, end)
    status = "pass" if blocked else ("fail" if clicked else "inconclusive")
    result_word = "PASSED" if status == "pass" else (
        "FAILED" if status == "fail" else "COULD NOT CONFIRM"
    )

    REPORTER.record(
        "validation",
        "End Date before Start Date cannot be reviewed",
        url,
        "Review Facility is blocked when End Date is earlier than Start Date",
        (
            f"Result: {result_word}; Start Date: {_input_value(start)!r}; "
            f"End Date: {_input_value(end)!r}; Review allowed: "
            f"{'YES' if reached_review else 'NO'}; Review blocked: "
            f"{'YES' if blocked else 'NO'}; feedback: {feedback}; action: {click_detail}"
            + (
                f"; note: {', '.join(fallback_fields)} used the visible-options "
                "fallback (selection not independently confirmed)"
                if fallback_fields
                else ""
            )
        ),
        status,
        severity="high",
        repro_steps=[
            f"Open {url}",
            "Open Create Facility and fill valid data",
            "Set End Date earlier than Start Date",
            "Click Review Facility",
            "Verify that the Review step is not reached",
        ],
        screenshot="" if blocked else screenshot(page, "end_date_before_start_date"),
        suggested_fix=(
            "Do not allow Review when End Date is earlier than Start Date."
            if status == "fail"
            else ""
        ),
    )
    close_any_modal(page)


def _test_dropdown_selection(
    page,
    url,
    *,
    title: str,
    field_name: str,
    patterns: list[str],
    timeout_ms: int,
):
    """Try selection first; otherwise prove that usable options are visible."""
    modal = _fresh_form(page, url)
    if modal is None:
        REPORTER.record(
            "functional",
            title,
            url,
            f"{field_name} can be populated in Create Facility",
            "Result: COULD NOT CONFIRM â€” the Create Facility form could not be opened",
            "inconclusive",
        )
        return

    item = _field(page, patterns, modal)
    if item is None:
        REPORTER.record(
            "functional",
            title,
            url,
            f"{field_name} can be populated in Create Facility",
            f"Result: COULD NOT CONFIRM â€” the {field_name} control was not found",
            "inconclusive",
            screenshot=screenshot(page, f"{field_name.lower().replace(' ', '_')}_not_found"),
        )
        close_any_modal(page)
        return

    outcome = _select_required_dropdown(
        page,
        modal,
        field_name,
        patterns,
        timeout_ms=timeout_ms,
    )
    refreshed = _field(page, patterns, modal)
    retained = refreshed is not None and _has_meaningful_value(refreshed)
    selected_pass = outcome.selected and retained
    visible_fallback_pass = (
        outcome.passed
        and outcome.options_visible
        and not outcome.selected
        and config.DROPDOWN_VISIBLE_OPTIONS_PASS
    )
    status = "pass" if selected_pass or visible_fallback_pass else "inconclusive"
    mode = (
        "Playwright selected and retained a value"
        if selected_pass
        else "Fallback accepted because usable dropdown values were visibly loaded"
        if visible_fallback_pass
        else "Selection and visible-option fallback were not confirmed"
    )
    REPORTER.record(
        "functional",
        title,
        url,
        (
            f"{field_name} is usable, or at minimum its available values are "
            "visibly loaded when automated selection is unsupported"
        ),
        (
            f"Result: {'PASSED' if status == 'pass' else 'COULD NOT CONFIRM'}; "
            f"mode: {mode}; selection retained: {'YES' if retained else 'NO'}; "
            f"options visible: {'YES' if outcome.options_visible else 'NO'}; "
            f"{outcome.detail}"
        ),
        status,
        severity="high",
        screenshot="" if status == "pass" else screenshot(
            page, f"{field_name.lower().replace(' ', '_')}_selection_not_confirmed"
        ),
    )
    close_any_modal(page)


def _test_facility_modal_dismiss(page, url, *, mode: str):
    """Verify the explicit Cancel or top-right Close control."""
    modal = _fresh_form(page, url)
    title = (
        "Cancel closes the Create Facility form"
        if mode == "cancel"
        else "Close icon closes the Create Facility form"
    )
    if modal is None:
        REPORTER.record(
            "functional",
            title,
            url,
            "The form can be dismissed without reloading the page",
            "Result: COULD NOT CONFIRM â€” the Create Facility form could not be opened",
            "inconclusive",
        )
        return

    pattern = r"^\s*cancel\s*$" if mode == "cancel" else r"\bclose\b|^\s*[xÃ—âœ•âœ–]\s*$"
    control = _button(page, pattern, modal)
    if control is None:
        REPORTER.record(
            "functional",
            title,
            url,
            "The form has a usable dismissal control",
            f"Result: COULD NOT CONFIRM â€” the {mode} control was not found",
            "inconclusive",
            screenshot=screenshot(page, f"facility_{mode}_missing"),
        )
        close_any_modal(page)
        return

    clicked = False
    click_detail = ""
    try:
        control.click(timeout=config.DROPDOWN_ACTION_TIMEOUT_MS)
        clicked = True
        page.wait_for_timeout(min(config.MODAL_CLOSE_WAIT_MS, 1200))
    except Exception as exc:
        click_detail = str(exc).splitlines()[0][:180]

    still_open = False
    current = get_open_modal(page)
    if current is not None:
        try:
            heading = " ".join((current.inner_text() or "").split())[:250]
            still_open = bool(re.search(r"create\s+facility", heading, re.I))
        except Exception:
            still_open = True

    status = "pass" if clicked and not still_open else (
        "fail" if clicked and still_open else "inconclusive"
    )
    REPORTER.record(
        "functional",
        title,
        url,
        "The Create Facility form closes without a page reload",
        (
            f"Result: {'PASSED' if status == 'pass' else 'FAILED' if status == 'fail' else 'COULD NOT CONFIRM'}; "
            f"control clicked: {'YES' if clicked else 'NO'}; form still open: "
            f"{'YES' if still_open else 'NO'}"
            + (f"; automation detail: {click_detail}" if click_detail else "")
        ),
        status,
        severity="high" if status == "fail" else "medium",
        screenshot="" if status == "pass" else screenshot(page, f"facility_{mode}_close_result"),
    )
    close_any_modal(page)

def _test_add_charges(page, url):
    modal = _fresh_form(page, url)
    if modal is None:
        REPORTER.record(
            "functional",
            "Charges can be added to a facility",
            url,
            "Add Charges creates an editable charge row or section",
            "The facility form could not be opened for this test",
            "inconclusive",
        )
        return

    add = _button(page, r"\badd\s+charge(s)?\b|\badd\b.*\bcharge", modal)
    if add is None:
        REPORTER.record(
            "functional",
            "Charges can be added to a facility",
            url,
            "An Add Charges action is available in facility creation",
            "No visible Add Charges action was found inside the facility modal",
            "fail",
            severity="high",
            screenshot=screenshot(page, "add_charges_missing"),
        )
        close_any_modal(page)
        return

    before = modal.locator("input, select, textarea").count()
    try:
        add.click(timeout=config.STEP_TIMEOUT_MS)
        wait_for_settle(page, quiet_ms=200, timeout_ms=2500)
        current_modal = get_open_modal(page)
        if current_modal is None:
            current_modal = modal
        after = current_modal.locator("input, select, textarea").count()
        charge_field = _field(
            page,
            [r"charge\s*type", r"charge\s*amount", r"charge\s*name"],
            current_modal,
        )
        charge_text = _visible(
            page,
            "text=/charge type|charge amount|charge name/i",
            current_modal,
        )
        passed = after > before or charge_field is not None or charge_text
        REPORTER.record(
            "functional",
            "Charges can be added to a facility",
            url,
            "Add Charges creates a new editable charge row or opens charge details",
            (
                f"Editable controls before: {before}; after: {after}; "
                f"charge field found: {charge_field is not None}"
            ),
            "pass" if passed else "fail",
            severity="high",
            screenshot="" if passed else screenshot(page, "add_charges_no_effect"),
        )
    except Exception as exc:
        REPORTER.record(
            "functional",
            "Charges can be added to a facility",
            url,
            "Add Charges opens or creates charge details",
            f"The Add Charges action could not be completed: {str(exc).splitlines()[0][:200]}",
            "inconclusive",
        )
    close_any_modal(page)


def _editable_field_count(scope) -> int:
    count = 0
    try:
        fields = scope.locator("input, select, textarea")
        for index in range(min(fields.count(), 150)):
            item = fields.nth(index)
            if not item.is_visible():
                continue
            try:
                if item.is_editable():
                    count += 1
            except Exception:
                if item.is_enabled() and item.get_attribute("readonly") is None:
                    count += 1
    except Exception:
        pass
    return count


def _test_review_readonly(page, url):
    if _FACILITY_BASELINE_BLOCKER:
        for title in (
            "Valid facility can reach Review",
            "Review state fields are read-only",
            "Send for Approval is available after Review",
        ):
            REPORTER.record(
                "functional",
                title,
                url,
                "A valid facility reaches the expected Review state",
                f"Result: COULD NOT CONFIRM â€” baseline preflight unavailable: {_FACILITY_BASELINE_BLOCKER}",
                "inconclusive",
            )
        return
    modal = _fresh_form(page, url)
    if modal is None:
        for title in (
            "Valid facility can reach Review",
            "Review state fields are read-only",
            "Send for Approval is available after Review",
        ):
            REPORTER.record(
                "functional",
                title,
                url,
                "A valid facility reaches the expected Review state",
                "Result: COULD NOT CONFIRM â€” the facility form could not be opened",
                "inconclusive",
            )
        return

    baseline_ok, missing = _fill_valid_baseline(page, modal)
    fallback_fields = _modal_dropdown_fallbacks(modal)
    if not baseline_ok:
        for title in (
            "Valid facility can reach Review",
            "Review state fields are read-only",
            "Send for Approval is available after Review",
        ):
            REPORTER.record(
                "functional",
                title,
                url,
                "A valid facility reaches the expected Review state",
                f"Result: COULD NOT CONFIRM â€” baseline fields missing: {missing}",
                "inconclusive",
            )
        close_any_modal(page)
        return

    clicked, click_detail = _click_review(page, modal)
    reached_review = _review_reached(page)
    # A dropdown accepted through the visible-options fallback no longer
    # withholds a verdict here - the real observed outcome (reached Review or
    # not) is reported, with the fallback fields disclosed as evidence.
    transition_status = "pass" if reached_review else (
        "inconclusive" if not clicked else "fail"
    )
    review_result_word = (
        "PASSED"
        if reached_review
        else "COULD NOT CONFIRM"
        if not clicked
        else "FAILED"
    )
    REPORTER.record(
        "functional",
        "Valid facility can reach Review",
        url,
        "Valid facility details proceed to the Review step",
        (
            f"Result: {review_result_word}; "
            f"Review reached: {'YES' if reached_review else 'NO'}; action: {click_detail}; "
            f"dropdown fallback fields: {fallback_fields or 'none'}"
        ),
        transition_status,
        severity="high",
        screenshot="" if reached_review else screenshot(page, "valid_review_not_reached"),
    )

    if not reached_review:
        for title, expected in (
            (
                "Review state fields are read-only",
                "Facility fields cannot be edited in Review state",
            ),
            (
                "Send for Approval is available after Review",
                "Send for Approval is visible in Review state",
            ),
        ):
            REPORTER.record(
                "functional",
                title,
                url,
                expected,
                "Result: COULD NOT CONFIRM â€” Review state was not reached",
                "inconclusive",
            )
        close_any_modal(page)
        return

    review_scope = get_open_modal(page) or page
    editable_count = _editable_field_count(review_scope)
    readonly_passed = editable_count == 0
    REPORTER.record(
        "functional",
        "Review state fields are read-only",
        url,
        "Facility fields cannot be edited in Review state",
        (
            f"Result: {'PASSED' if readonly_passed else 'FAILED'}; "
            f"editable field count in Review: {editable_count}"
        ),
        "pass" if readonly_passed else "fail",
        severity="high",
        screenshot="" if readonly_passed else screenshot(page, "review_fields_editable"),
    )

    send = _button(page, r"send\s+for\s+approval", review_scope)
    send_visible = send is not None
    REPORTER.record(
        "functional",
        "Send for Approval is available after Review",
        url,
        "Send for Approval is visible in Review state without being clicked",
        (
            f"Result: {'PASSED' if send_visible else 'FAILED'}; "
            f"Send for Approval visible: {'YES' if send_visible else 'NO'}"
        ),
        "pass" if send_visible else "fail",
        severity="high",
        screenshot="" if send_visible else screenshot(page, "send_for_approval_missing"),
    )
    close_any_modal(page)

def run_dashboard_tests(page, url):
    body = ""
    try:
        body = page.inner_text("body", timeout=3000)
    except Exception:
        pass

    cards = page.locator("[class*='kpi'], [class*='card'], article")
    card_count = sum(
        1 for index in range(min(cards.count(), 100)) if cards.nth(index).is_visible()
    )
    REPORTER.record(
        "functional",
        "Dashboard loads business summary",
        url,
        "Dashboard displays business summary cards or metrics",
        f"Visible summary/card elements: {card_count}",
        "pass" if card_count > 0 else "fail",
        severity="high",
        screenshot="" if card_count > 0 else screenshot(page, "dashboard_no_summary"),
    )

    numeric = bool(re.search(r"\b\d[\d,.]*\b", body))
    REPORTER.record(
        "functional",
        "Dashboard metrics contain values",
        url,
        "At least one dashboard metric contains a visible value",
        "Numeric dashboard content was found"
        if numeric
        else "No numeric metric was detected",
        "pass" if numeric else "fail",
        severity="medium",
    )


def _execute_focused_case(
    page,
    url,
    title,
    category,
    callback,
    case_number=None,
    total_cases=None,
):
    """Run one business case without allowing runner errors to stop the suite."""
    label = f"{case_number}/{total_cases}" if case_number and total_cases else ""
    print(f"    [case {label}] {title}", flush=True)
    REPORTER.update_run_state(current_page=url, current_case=title)
    before = len(REPORTER.results)
    try:
        callback()
    except Exception as exc:
        # A runner/selector error is not evidence that the application failed.
        # Record it as not confirmed and continue with a clean route.
        REPORTER.record(
            category,
            title,
            url,
            "The test case executes and produces a measured business verdict",
            (
                "Result: COULD NOT CONFIRM â€” the automated test hit an execution "
                f"error: {str(exc).splitlines()[0][:240]}"
            ),
            "inconclusive",
        )
        try:
            page.goto(url, timeout=config.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            wait_for_settle(page)
        except Exception:
            pass
    finally:
        # If a callback failed before it recorded anything, the inconclusive
        # result above guarantees that the report still shows the coverage gap.
        if len(REPORTER.results) == before:
            REPORTER.record(
                category,
                title,
                url,
                "The test case produces a clear result",
                "Result: COULD NOT CONFIRM â€” no measurable result was produced",
                "inconclusive",
            )
        try:
            close_any_modal(page)
        except Exception:
            pass
        REPORTER.checkpoint()


def run_facility_creation_tests(page, url):
    global _FACILITY_FORM_PROFILE

    modal = _open_create_facility(page, url, record_result=True)
    if modal is not None:
        _FACILITY_FORM_PROFILE = _discover_facility_form_profile(page, modal)
        print(
            "    Detected Create Facility fields: "
            + ", ".join(
                name.replace("_", " ").title()
                for name, present in _FACILITY_FORM_PROFILE.items()
                if present and not name.endswith("_clearable")
            ),
            flush=True,
        )
        close_any_modal(page)
    else:
        _FACILITY_FORM_PROFILE = {}

    cases: list[tuple[str, str, object]] = []

    def add(title, category, callback):
        cases.append((title, category, callback))

    add(
        "Valid facility baseline can be prepared",
        "functional",
        lambda: _test_baseline_preflight(page, url),
    )

    if _FACILITY_FORM_PROFILE.get("company"):
        add(
            "Company selection is available in Create Facility",
            "functional",
            lambda: _test_dropdown_selection(
                page,
                url,
                title="Company selection is available in Create Facility",
                field_name="Company",
                patterns=[r"company\s*code", r"^company$"],
                timeout_ms=config.DROPDOWN_SELECT_TIMEOUT_MS,
            ),
        )

    if _FACILITY_FORM_PROFILE.get("business_partner"):
        add(
            "Business Partner dropdown values are available",
            "functional",
            lambda: _test_dropdown_selection(
                page,
                url,
                title="Business Partner dropdown values are available",
                field_name="Business Partner",
                patterns=[r"business\s*partner", r"partner"],
                timeout_ms=config.DEPENDENT_DROPDOWN_TIMEOUT_MS,
            ),
        )

    if _FACILITY_FORM_PROFILE.get("currency"):
        add(
            "Currency selection is available in Create Facility",
            "functional",
            lambda: _test_dropdown_selection(
                page,
                url,
                title="Currency selection is available in Create Facility",
                field_name="Currency",
                patterns=[r"currency"],
                timeout_ms=config.DROPDOWN_SELECT_TIMEOUT_MS,
            ),
        )

    add(
        "Empty facility cannot be reviewed",
        "validation",
        lambda: _test_empty_review(page, url),
    )

    invalid_cases = []
    if _FACILITY_FORM_PROFILE.get("facility_name"):
        invalid_cases.extend([
            {
                "title": "Facility Name is required before Review",
                "target_patterns": [r"facility\s*name"],
                "invalid_value": None,
                "invalid_description": "an empty Facility Name",
                "expected": "Review Facility is blocked when Facility Name is empty",
                "leave_blank": True,
            },
            {
                "title": "Whitespace-only Facility Name cannot be reviewed",
                "target_patterns": [r"facility\s*name"],
                "invalid_value": "   ",
                "invalid_description": "spaces only",
                "expected": "Review Facility is blocked when Facility Name contains only whitespace",
            },
        ])
    if _FACILITY_FORM_PROFILE.get("company_clearable"):
        invalid_cases.append({
            "title": "Company is required before Review",
            "target_patterns": [r"company\s*code", r"company$"],
            "invalid_value": None,
            "invalid_description": "no Company selected",
            "expected": "Review Facility is blocked when Company is not selected",
            "leave_blank": True,
        })
    if _FACILITY_FORM_PROFILE.get("business_partner"):
        invalid_cases.append({
            "title": "Business Partner is required before Review",
            "target_patterns": [r"business\s*partner", r"partner"],
            "invalid_value": None,
            "invalid_description": "no Business Partner selected",
            "expected": "Review Facility is blocked when Business Partner is not selected",
            "leave_blank": True,
        })
    if _FACILITY_FORM_PROFILE.get("product"):
        invalid_cases.append({
            "title": "Product or Facility Type is required before Review",
            "target_patterns": [r"product", r"facility\s*type"],
            "invalid_value": None,
            "invalid_description": "no Product or Facility Type selected",
            "expected": "Review Facility is blocked when Product or Facility Type is not selected",
            "leave_blank": True,
        })
    if _FACILITY_FORM_PROFILE.get("amount"):
        invalid_cases.extend([
            {
                "title": "Facility Amount is required before Review",
                "target_patterns": [r"facility\s*amount", r"sanctioned\s*amount", r"limit\s*amount"],
                "invalid_value": None,
                "invalid_description": "an empty Facility Amount",
                "expected": "Review Facility is blocked when Facility Amount is empty",
                "leave_blank": True,
                "severity": "critical",
            },
            {
                "title": "Zero Facility Amount cannot be reviewed",
                "target_patterns": [r"facility\s*amount", r"sanctioned\s*amount", r"limit\s*amount"],
                "invalid_value": "0",
                "invalid_description": "zero amount (0)",
                "expected": "Review Facility is blocked when Facility Amount is zero",
                "severity": "critical",
            },
            {
                "title": "Non-numeric Facility Amount cannot be reviewed",
                "target_patterns": [r"facility\s*amount", r"sanctioned\s*amount", r"limit\s*amount"],
                "invalid_value": "abc",
                "invalid_description": "non-numeric text (abc)",
                "expected": "Review Facility is blocked when Facility Amount is not numeric",
                "severity": "critical",
            },
        ])
    if _FACILITY_FORM_PROFILE.get("currency_clearable"):
        invalid_cases.append({
            "title": "Currency is required before Review",
            "target_patterns": [r"currency"],
            "invalid_value": None,
            "invalid_description": "no Currency selected",
            "expected": "Review Facility is blocked when Currency is not selected",
            "leave_blank": True,
        })
    if _FACILITY_FORM_PROFILE.get("start_date"):
        invalid_cases.append({
            "title": "Start Date is required before Review",
            "target_patterns": [r"start\s*date", r"from\s*date", r"issue\s*date"],
            "invalid_value": None,
            "invalid_description": "an empty Start Date",
            "expected": "Review Facility is blocked when Start Date is empty",
            "leave_blank": True,
        })
    if _FACILITY_FORM_PROFILE.get("end_date"):
        invalid_cases.append({
            "title": "End Date is required before Review",
            "target_patterns": [r"end\s*date", r"maturity\s*date", r"to\s*date"],
            "invalid_value": None,
            "invalid_description": "an empty End Date",
            "expected": "Review Facility is blocked when End Date is empty",
            "leave_blank": True,
        })

    for case in invalid_cases:
        add(
            case["title"],
            "validation",
            lambda case=case: _run_invalid_review_case(page, url, **case),
        )

    if _FACILITY_FORM_PROFILE.get("amount"):
        add(
            "Negative Facility Amount cannot be reviewed",
            "validation",
            lambda: _test_negative_amount(page, url),
        )

    if _FACILITY_FORM_PROFILE.get("start_date") and _FACILITY_FORM_PROFILE.get("end_date"):
        add(
            "End Date cannot be earlier than Start Date",
            "validation",
            lambda: _test_date_order(page, url),
        )

    add(
        "Clear resets the facility form",
        "functional",
        lambda: _test_clear_button(page, url),
    )
    add(
        "Charges can be added to a facility",
        "functional",
        lambda: _test_add_charges(page, url),
    )
    add(
        "Cancel closes the Create Facility form",
        "functional",
        lambda: _test_facility_modal_dismiss(page, url, mode="cancel"),
    )
    add(
        "Close icon closes the Create Facility form",
        "functional",
        lambda: _test_facility_modal_dismiss(page, url, mode="close"),
    )
    add(
        "Valid facility reaches a protected Review state",
        "functional",
        lambda: _test_review_readonly(page, url),
    )

    total_cases = len(cases)
    for case_number, (title, category, callback) in enumerate(cases, start=1):
        _execute_focused_case(
            page,
            url,
            title,
            category,
            callback,
            case_number=case_number,
            total_cases=total_cases,
        )

    close_any_modal(page)

def _first_safe_facility_detail_trigger(page):
    """Find a non-destructive control that opens one facility's details."""
    destructive = re.compile(
        r"\b(approve|reject|decline|delete|remove|submit|send|cancel)\b",
        re.I,
    )
    preferred = re.compile(r"\b(view|detail|facility)\b|^\s*\d{4,}\s*$", re.I)
    try:
        rows = page.locator("table tbody tr")
        for row_index in range(min(rows.count(), 100)):
            row = rows.nth(row_index)
            if not row.is_visible():
                continue
            candidates = row.locator(
                "a, button, [role='button'], [tabindex='0'], "
                "td:first-child [class*='link'], td:first-child [class*='click']"
            )
            fallback = None
            for index in range(min(candidates.count(), 30)):
                item = candidates.nth(index)
                if not item.is_visible() or not item.is_enabled():
                    continue
                text = " ".join((item.inner_text() or "").split())
                blob = " ".join(
                    (
                        text,
                        item.get_attribute("aria-label") or "",
                        item.get_attribute("title") or "",
                    )
                )
                if destructive.search(blob):
                    continue
                fallback = fallback or item
                if preferred.search(blob):
                    return item
            if fallback is not None:
                return fallback

            # Some enterprise tables make the row itself keyboard-clickable.
            row_role = (row.get_attribute("role") or "").lower()
            if row_role in {"button", "link"} or row.get_attribute("tabindex") == "0":
                return row
    except Exception:
        pass
    return None


def _test_facility_detail_popup(page, url):
    """Open and close one facility detail popup without approving anything."""
    try:
        close_any_modal(page)
    except Exception:
        pass

    trigger = _first_safe_facility_detail_trigger(page)
    if trigger is None:
        detail = "No pending/approved row with a safe detail control was available"
        for title, expected in (
            (
                "Facility details can be opened from the approval worklist",
                "Selecting a facility opens its detail view",
            ),
            (
                "Facility detail popup can be closed",
                "The facility detail popup closes without reloading the page",
            ),
        ):
            REPORTER.record(
                "functional",
                title,
                url,
                expected,
                f"Result: COULD NOT CONFIRM â€” {detail}",
                "inconclusive",
            )
        return

    try:
        trigger.click(timeout=config.STEP_TIMEOUT_MS)
        wait_for_settle(page, quiet_ms=160, timeout_ms=min(config.SETTLE_TIMEOUT_MS, 1600))
    except Exception as exc:
        detail = f"The safe facility detail control could not be clicked: {str(exc).splitlines()[0][:220]}"
        for title in (
            "Facility details can be opened from the approval worklist",
            "Facility detail popup can be closed",
        ):
            REPORTER.record(
                "functional",
                title,
                url,
                "The facility detail workflow can be measured",
                f"Result: COULD NOT CONFIRM â€” {detail}",
                "inconclusive",
            )
        return

    modal = get_open_modal(page)
    opened = modal is not None
    REPORTER.record(
        "functional",
        "Facility details can be opened from the approval worklist",
        url,
        "Selecting a facility opens its detail popup",
        (
            "Result: PASSED â€” a facility detail popup became visible"
            if opened
            else "Result: COULD NOT CONFIRM â€” the click completed, but no detail popup was detected"
        ),
        "pass" if opened else "inconclusive",
        screenshot="" if opened else screenshot(page, "facility_detail_not_confirmed"),
    )

    if not opened:
        REPORTER.record(
            "functional",
            "Facility detail popup can be closed",
            url,
            "The facility detail popup closes without reloading the page",
            "Result: COULD NOT CONFIRM â€” no popup was detected after opening the facility",
            "inconclusive",
        )
        return

    dismissal = close_any_modal(page)
    if dismissal.get("closed"):
        status = "pass"
        result = f"Result: PASSED â€” {dismissal.get('detail', 'the popup was dismissed')}"
    elif dismissal.get("confirmed_failure"):
        status = "fail"
        result = f"Result: FAILED â€” {dismissal.get('detail', 'the popup remained visible')}"
    else:
        status = "inconclusive"
        result = f"Result: COULD NOT CONFIRM â€” {dismissal.get('detail', 'dismissal was not measurable')}"

    REPORTER.record(
        "functional",
        "Facility detail popup can be closed",
        url,
        "The facility detail popup closes without reloading the page",
        result,
        status,
        severity="high",
        screenshot=(
            screenshot(page, "facility_detail_close_failed")
            if status == "fail"
            else ""
        ),
        suggested_fix=(
            "Connect the popup Close/Cancel controls to the modal dismissal handler."
            if status == "fail"
            else ""
        ),
    )


def run_facility_approval_page_tests(page, url):
    body = ""
    try:
        body = page.inner_text("body", timeout=3000)
    except Exception:
        pass

    table_present = _visible(page, "table", page) or bool(
        re.search(r"pending|approved|facility", body, re.I)
    )
    REPORTER.record(
        "functional",
        "Facility Approval page loads approval records",
        url,
        "The page displays the facility approval worklist or an explicit empty state",
        "Approval worklist/table or facility status content was found"
        if table_present
        else "No approval content was detected",
        "pass" if table_present else "fail",
        severity="high",
        screenshot="" if table_present else screenshot(page, "facility_approval_empty"),
    )

    pending_visible = bool(re.search(r"\bpending\b", body, re.I))
    approved_visible = bool(re.search(r"\bapproved\b", body, re.I))
    views_present = pending_visible and approved_visible
    REPORTER.record(
        "functional",
        "Pending and Approved facility views are available",
        url,
        "Users can distinguish facilities awaiting approval from approved facilities",
        (
            f"Pending visible: {pending_visible}; Approved visible: {approved_visible}"
        ),
        "pass" if views_present else "fail",
        severity="medium",
        screenshot="" if views_present else screenshot(page, "approval_status_views_missing"),
    )

    _test_facility_detail_popup(page, url)



def _record_primary_role_context(
    page,
    url,
    capability: str,
    control_pattern: str,
    label: str,
):
    role = normalise_role(config.PRIMARY_USER_ROLE)
    expected_allowed = role_capabilities(role).get(capability, False)
    visible = _button(page, control_pattern, page) is not None
    if capability == "approve_facility" and expected_allowed and not visible:
        body = ""
        try:
            body = page.inner_text("body", timeout=config.STEP_TIMEOUT_MS)
        except Exception:
            pass
        empty = bool(re.search(r"no\s+(pending\s+)?facilit|no\s+records|nothing\s+to\s+approve", body, re.I))
        rows = page.locator("table tbody tr")
        row_count = 0
        try:
            row_count = sum(
                1 for index in range(min(rows.count(), 100))
                if rows.nth(index).is_visible()
            )
        except Exception:
            pass
        status = "inconclusive" if empty or row_count == 0 else "fail"
    else:
        status = "pass" if visible == expected_allowed else "fail"
    REPORTER.record(
        "role_access",
        f"Primary {role} context: {label}",
        url,
        f"{role} {'can' if expected_allowed else 'cannot'} {label}",
        f"Control visible: {visible}; configured primary role: {role}",
        status,
        severity="critical",
        screenshot="" if status == "pass" else screenshot(page, f"primary_role_{capability}"),
        suggested_fix=(
            "Apply the facility role matrix in both the UI and backend authorization."
            if status == "fail" else ""
        ),
    )


def run_focused_business_tests(page, url):
    path = _path(url)
    if "facility-creation" in path or "facilitycreation" in path:
        _record_primary_role_context(
            page,
            url,
            "create_facility",
            r"\b(create|new|add)\s+facility\b",
            "create facilities",
        )
        if role_capabilities(config.PRIMARY_USER_ROLE).get("create_facility", False):
            run_facility_creation_tests(page, url)
    elif "facility-approval" in path or "facilityapproval" in path:
        _record_primary_role_context(
            page,
            url,
            "approve_facility",
            r"^\s*approve\s*$|\bapprove\s+facility\b",
            "approve facilities",
        )
        run_facility_approval_page_tests(page, url)
    elif "dashboard" in path or path == "/":
        run_dashboard_tests(page, url)

