"""Reliable modal/popup detection and dismissal.

The tester must never report a working popup as "stuck" merely because its
close control is an icon or because a broad CSS selector matched a nested
open dropdown. This module therefore:

* ranks visible modal candidates instead of returning the first selector hit;
* recognises labelled close/cancel controls and icon-only X controls;
* verifies that the *same* popup disappeared after the action; and
* exposes whether a failed dismissal was actually confirmed or only could not
  be automated.

Existing callers may continue to ignore the dictionary returned by
``close_any_modal``. Callers that produce a verdict should inspect
``closed`` and ``confirmed_failure``.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import re
import uuid

from ai_agents import config


_CLOSE_WORD_RE = re.compile(r"\b(close|dismiss|cancel|back|done)\b", re.I)
_CLOSE_CLASS_RE = re.compile(r"close|dismiss|cancel|modal[-_]?x|dialog[-_]?x", re.I)
_CLOSE_GLYPHS = {"x", "Ã—", "âœ•", "âœ–", "â•³", "â¨¯"}


def _metadata(locator):
    try:
        return locator.evaluate(
            r"""
            e => {
              const r = e.getBoundingClientRect();
              const s = getComputedStyle(e);
              const norm = value => (value || '').replace(/\s+/g, ' ').trim();
              const heading = e.querySelector(
                'h1,h2,h3,h4,[role="heading"],.modal-title,[class*="modalTitle"],'+
                '[class*="modal-title"],[class*="dialogTitle"],[class*="dialog-title"]'
              );
              return {
                tag: e.tagName.toLowerCase(),
                role: e.getAttribute('role') || '',
                ariaModal: e.getAttribute('aria-modal') || '',
                dataState: e.getAttribute('data-state') || '',
                cls: typeof e.className === 'string' ? e.className : '',
                id: e.id || '',
                text: norm(e.innerText || e.textContent).slice(0, 700),
                heading: norm(heading && (heading.innerText || heading.textContent)).slice(0, 180),
                visible: r.width > 0 && r.height > 0 && s.display !== 'none' &&
                         s.visibility !== 'hidden' && Number(s.opacity || 1) > 0.02,
                x: r.x, y: r.y, width: r.width, height: r.height,
                area: r.width * r.height,
                viewportArea: Math.max(1, innerWidth * innerHeight),
                position: s.position,
                zIndex: Number.parseInt(s.zIndex, 10) || 0,
                hasDialogAncestor: !!e.parentElement && !!e.parentElement.closest(
                  '[role="dialog"],[role="alertdialog"],[aria-modal="true"],dialog[open],'+
                  '.modal.show,.MuiDialog-root,.ant-modal-wrap,.p-dialog,.ReactModal__Content'
                )
              };
            }
            """
        )
    except Exception:
        return None


def _modal_score(meta):
    if not meta or not meta.get("visible"):
        return -1

    role = str(meta.get("role") or "").lower()
    cls = str(meta.get("cls") or "").lower()
    tag = str(meta.get("tag") or "").lower()
    state = str(meta.get("dataState") or "").lower()
    area_ratio = float(meta.get("area") or 0) / max(float(meta.get("viewportArea") or 1), 1)

    score = 0
    if role in {"dialog", "alertdialog"}:
        score += 130
    if str(meta.get("ariaModal") or "").lower() == "true":
        score += 125
    if tag == "dialog":
        score += 110
    if re.search(r"(^|\s)(modal|dialog|popup|drawer|sheet|offcanvas)(\s|$|[-_])", cls):
        score += 85
    if any(token in cls for token in (
        "muidialog", "ant-modal", "p-dialog", "reactmodal", "chakra-modal",
        "headlessui", "radix", "drawer", "offcanvas",
    )):
        score += 70
    if state == "open":
        # data-state=open is also used by dropdown triggers and accordion items.
        # It only counts strongly when the element is large enough to be a popup.
        score += 35 if area_ratio >= 0.025 else 5
    if meta.get("position") in {"fixed", "absolute"} and area_ratio >= 0.025:
        score += 25
    if area_ratio >= 0.08:
        score += 20
    if meta.get("hasDialogAncestor") and role not in {"dialog", "alertdialog"} \
            and str(meta.get("ariaModal") or "").lower() != "true":
        score -= 45
    if area_ratio < 0.005 and role not in {"dialog", "alertdialog"}:
        score -= 60
    score += min(max(int(meta.get("zIndex") or 0), 0), 10000) / 10000
    return score


def get_open_modal(page):
    """Return the highest-confidence visible popup, not the first selector hit."""
    try:
        loc = page.locator(config.MODAL_SELECTOR)
        best = None
        best_score = 0
        for index in range(min(loc.count(), 60)):
            candidate = loc.nth(index)
            meta = _metadata(candidate)
            score = _modal_score(meta)
            if score > best_score:
                best = candidate
                best_score = score
        return best
    except Exception:
        return None


def _snapshot_modal(modal):
    token = f"qa-modal-{uuid.uuid4().hex}"
    try:
        modal.evaluate("(e, token) => e.setAttribute('data-qa-modal-token', token)", token)
    except Exception:
        token = ""
    meta = _metadata(modal) or {}
    meta["token"] = token
    return meta


def _normalise_text(value):
    return " ".join(str(value or "").split()).lower()


def _looks_like_same_modal(before, after):
    if not after:
        return False
    before_heading = _normalise_text(before.get("heading"))
    after_heading = _normalise_text(after.get("heading"))
    if before_heading and after_heading and before_heading == after_heading:
        return True

    before_text = _normalise_text(before.get("text"))[:500]
    after_text = _normalise_text(after.get("text"))[:500]
    if before_text and after_text:
        similarity = SequenceMatcher(None, before_text, after_text).ratio()
        if similarity >= 0.72:
            return True

    bw, bh = float(before.get("width") or 0), float(before.get("height") or 0)
    aw, ah = float(after.get("width") or 0), float(after.get("height") or 0)
    if bw and bh and aw and ah:
        same_size = abs(bw - aw) <= max(12, bw * 0.08) and \
            abs(bh - ah) <= max(12, bh * 0.08)
        if same_size and before_text[:80] and before_text[:80] in after_text:
            return True
    return False


def _same_modal_is_open(page, before):
    token = before.get("token")
    if token:
        try:
            tagged = page.locator(f'[data-qa-modal-token="{token}"]')
            for index in range(min(tagged.count(), 3)):
                if tagged.nth(index).is_visible():
                    return True
        except Exception:
            pass

    current = get_open_modal(page)
    if current is None:
        return False
    return _looks_like_same_modal(before, _metadata(current) or {})


def _wait_for_dismissal(page, before):
    try:
        page.wait_for_function(
            r"""
            token => {
              if (!token) return true;
              const el = document.querySelector(`[data-qa-modal-token="${token}"]`);
              if (!el) return true;
              const r = el.getBoundingClientRect();
              const s = getComputedStyle(el);
              return r.width < 1 || r.height < 1 || s.display === 'none' ||
                     s.visibility === 'hidden' || Number(s.opacity || 1) <= 0.02;
            }
            """,
            before.get("token") or "",
            timeout=max(config.MODAL_CLOSE_WAIT_MS, 300),
        )
    except Exception:
        try:
            page.wait_for_timeout(min(max(config.MODAL_CLOSE_WAIT_MS, 300), 1800))
        except Exception:
            pass
    return not _same_modal_is_open(page, before)


def _control_metadata(control, modal_meta):
    try:
        return control.evaluate(
            r"""
            (e, modalBox) => {
              const r = e.getBoundingClientRect();
              const norm = value => (value || '').replace(/\s+/g, ' ').trim();
              return {
                text: norm(e.innerText || e.value || e.textContent),
                aria: norm(e.getAttribute('aria-label')),
                title: norm(e.getAttribute('title')),
                testId: norm(e.getAttribute('data-testid')),
                cls: typeof e.className === 'string' ? e.className : '',
                disabled: !!e.disabled || e.getAttribute('aria-disabled') === 'true',
                visible: r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden',
                x: r.x, y: r.y, width: r.width, height: r.height,
                hasSvg: !!e.querySelector('svg'),
                modalRight: modalBox.x + modalBox.width,
                modalTop: modalBox.y,
                modalWidth: modalBox.width,
                modalHeight: modalBox.height
              };
            }
            """,
            {
                "x": float(modal_meta.get("x") or 0),
                "y": float(modal_meta.get("y") or 0),
                "width": float(modal_meta.get("width") or 0),
                "height": float(modal_meta.get("height") or 0),
            },
        )
    except Exception:
        return None


def _dismiss_candidates(modal, modal_meta):
    candidates = []
    try:
        controls = modal.locator(
            "button, [role='button'], input[type='button'], input[type='reset']"
        )
        for index in range(min(controls.count(), 80)):
            control = controls.nth(index)
            meta = _control_metadata(control, modal_meta)
            if not meta or not meta.get("visible") or meta.get("disabled"):
                continue

            blob = " ".join(
                str(meta.get(key) or "")
                for key in ("text", "aria", "title", "testId", "cls")
            ).strip()
            text = _normalise_text(meta.get("text"))
            accessible = " ".join(
                str(meta.get(key) or "") for key in ("aria", "title", "testId")
            )
            confidence = ""
            reason = ""
            rank = 0

            if _CLOSE_WORD_RE.search(blob) or _CLOSE_CLASS_RE.search(blob):
                confidence, reason, rank = "high", "labelled close/cancel control", 120
            elif text in _CLOSE_GLYPHS:
                confidence, reason, rank = "high", "X close icon", 115
            elif _normalise_text(accessible) in _CLOSE_GLYPHS:
                confidence, reason, rank = "high", "accessible X close icon", 112
            else:
                # Last-resort recognition for icon-only buttons in the modal's
                # top-right header. A failure through this heuristic is never a
                # confirmed application defect; it remains inconclusive.
                right_gap = float(meta.get("modalRight") or 0) - \
                    (float(meta.get("x") or 0) + float(meta.get("width") or 0))
                top_gap = float(meta.get("y") or 0) - float(meta.get("modalTop") or 0)
                small = float(meta.get("width") or 0) <= 64 and \
                    float(meta.get("height") or 0) <= 64
                near_right = right_gap <= max(30, float(meta.get("modalWidth") or 0) * 0.08)
                near_top = top_gap <= max(45, float(meta.get("modalHeight") or 0) * 0.16)
                icon_like = meta.get("hasSvg") or len(text) <= 2
                if small and near_right and near_top and icon_like:
                    confidence, reason, rank = "medium", "top-right icon button", 55

            if rank:
                # Rightmost controls are preferred when ranks are otherwise equal.
                rank += int(float(meta.get("x") or 0) / 10000)
                candidates.append((rank, confidence, reason, control))
    except Exception:
        pass

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates


def _click_control(control, force=False):
    try:
        control.click(timeout=config.STEP_TIMEOUT_MS, force=force)
        return True, ""
    except Exception as exc:
        return False, str(exc).splitlines()[0][:180]


def close_any_modal(page):
    """Dismiss the current popup and return evidence about the attempt.

    Return keys:
      ``modal_found``          a popup was visible before the attempt
      ``closed``               the same popup disappeared
      ``method``               the successful/last attempted method
      ``confirmed_failure``    a high-confidence close control was clicked but
                               the same popup remained visible
      ``detail``               concise diagnostic text
    """
    modal = get_open_modal(page)
    if modal is None:
        return {
            "modal_found": False,
            "closed": True,
            "method": "none",
            "confirmed_failure": False,
            "detail": "No visible popup was open",
        }

    before = _snapshot_modal(modal)
    candidates = _dismiss_candidates(modal, before)
    high_confidence_clicked = False
    attempted = []

    for _, confidence, reason, control in candidates:
        # A rerender can detach remaining candidate locators, so always stop once
        # the original modal has disappeared.
        if not _same_modal_is_open(page, before):
            return {
                "modal_found": True,
                "closed": True,
                "method": attempted[-1] if attempted else "close control",
                "confirmed_failure": False,
                "detail": "The popup was dismissed",
            }

        clicked, error = _click_control(control, force=False)
        method = reason
        if not clicked:
            clicked, error = _click_control(control, force=True)
            method += " (forced click)"
        attempted.append(method)
        if clicked and confidence == "high":
            high_confidence_clicked = True
        if clicked and _wait_for_dismissal(page, before):
            return {
                "modal_found": True,
                "closed": True,
                "method": method,
                "confirmed_failure": False,
                "detail": f"Closed using {method}",
            }
        if error:
            attempted[-1] += f"; click error: {error}"

    # Escape is a useful fallback, but failure of Escape alone is not proof that
    # the popup is broken because many valid applications intentionally disable
    # keyboard dismissal.
    for _ in range(max(config.MODAL_CLOSE_RETRIES, 1)):
        try:
            page.keyboard.press("Escape")
            attempted.append("Escape key")
        except Exception as exc:
            attempted.append(f"Escape key error: {str(exc).splitlines()[0][:120]}")
        if _wait_for_dismissal(page, before):
            return {
                "modal_found": True,
                "closed": True,
                "method": "Escape key",
                "confirmed_failure": False,
                "detail": "Closed using the Escape key",
            }

    still_open = _same_modal_is_open(page, before)
    return {
        "modal_found": True,
        "closed": not still_open,
        "method": attempted[-1] if attempted else "no close control identified",
        "confirmed_failure": bool(still_open and high_confidence_clicked),
        "detail": (
            "The same popup remained visible after: " + "; ".join(attempted[:8])
            if still_open
            else "The original popup is no longer visible"
        ),
    }

