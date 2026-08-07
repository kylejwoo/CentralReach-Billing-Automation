from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from playwright.sync_api import BrowserContext, Frame, Locator, Page

from browser_utils import all_frames, newest_page, screenshot, visible_locator
from config import BULK_MERGE_CONFIG


@dataclass(frozen=True)
class ClaimIssue:
    client: str
    issue: str


def _click(locator: Locator, timeout: int = 7000) -> None:
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    try:
        locator.click(timeout=timeout)
    except Exception:
        locator.click(force=True, timeout=timeout)


def _visible_text(context: BrowserContext, pattern: re.Pattern[str]) -> Locator | None:
    for frame in all_frames(context):
        item = visible_locator(frame.get_by_text(pattern))
        if item is not None:
            return item
    return None


def wait_for_claims_page(context: BrowserContext) -> Page:
    page = newest_page(context)
    for _ in range(30):
        try:
            body = page.locator("body").inner_text(timeout=2000)
        except Exception:
            body = ""
        if "Separated Claims View" in body or "Start claims generation" in body:
            return page
        page.wait_for_timeout(500)
    screenshot(page, "claims_page_not_loaded", force=True)
    raise RuntimeError("Bulk Merge opened, but the Separated Claims View did not load.")


def collapse_all_if_available(context: BrowserContext) -> None:
    page = newest_page(context)
    button = _visible_text(context, re.compile(r"^\s*Collapse\s+All\s*$", re.I))
    if button is None:
        logging.info("Collapse All is not visible; claims appear already collapsed.")
        return
    _click(button)
    page.wait_for_timeout(250)
    logging.info("Clicked Collapse All.")


def expand_all_claims(context: BrowserContext) -> int:
    page = newest_page(context)
    expanded = 0
    for frame in all_frames(context):
        candidates = frame.get_by_text(
            re.compile(r"^\s*\d+\s+codes?\s+to\s+be\s+billed\s*$", re.I)
        )
        count = candidates.count()
        for index in range(count):
            item = candidates.nth(index)
            try:
                if not item.is_visible():
                    continue
                # After Collapse All, every codes link is collapsed. Click each one once.
                _click(item)
                page.wait_for_timeout(60)
                expanded += 1
            except Exception:
                continue
    logging.info("Expanded %d claim groups.", expanded)
    return expanded


def _claim_snapshot(frame: Frame) -> list[dict]:
    return frame.evaluate(
        r"""() => {
            function visible(el) {
                const s = getComputedStyle(el), r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            }
            function clean(v) { return String(v || '').replace(/\s+/g, ' ').trim(); }
            const idRe = /^(.+?)\s*\(ID:\s*(\d+)\)$/i;
            const dateRe = /\b\d{2}\/\d{2}\/\d{4}\b/;

            // CentralReach sometimes puts a DOB in a claim heading, for example
            // "CLIENT NAME (02/27/2020) (ID: ...)".  A date alone therefore does
            // not prove that a <tr> is a service row.  Build one unique heading
            // per claim and use the next heading as the lower boundary.
            const rawHeaders = Array.from(document.querySelectorAll('div,td,th,strong,b'))
                .filter(visible)
                .map(el => ({
                    el,
                    text: clean(el.textContent),
                    top: el.getBoundingClientRect().top,
                }))
                .filter(x => idRe.test(x.text))
                .sort((a, b) => a.top - b.top);

            const headers = [];
            const headerIds = new Set();
            for (const item of rawHeaders) {
                const match = item.text.match(idRe);
                if (!match || headerIds.has(match[2])) continue;
                headerIds.add(match[2]);
                headers.push({ ...item, match });
            }

            const output = [];
            for (let headerIndex = 0; headerIndex < headers.length; headerIndex++) {
                const h = headers[headerIndex];
                const m = h.match;
                const nextHeaderTop = headerIndex + 1 < headers.length
                    ? headers[headerIndex + 1].top
                    : Number.POSITIVE_INFINITY;

                let block = h.el;
                for (let i = 0; block && i < 10; i++, block = block.parentElement) {
                    const txt = clean(block.innerText);
                    if (/Auth:/i.test(txt) && /Diag Codes:/i.test(txt) && /codes? to be billed/i.test(txt)) break;
                }
                if (!block) continue;

                const text = clean(block.innerText);
                const authM = text.match(/Auth:\s*([^\s]+)?/i);
                const diagM = text.match(/Diag Codes:\s*(.*?)(?=\s+(?:Prov:|Payor:|Status:|Fac:|Bill:|Ref:|Ord:|Sup:|Pointer|Modifiers)|$)/i);
                const serviceRows = [];

                // Read only rows physically between this client heading and the
                // next client heading.  This prevents a broad claim container
                // from borrowing service rows from later clients.
                for (const row of Array.from(document.querySelectorAll('tr')).filter(visible)) {
                    const rowRect = row.getBoundingClientRect();
                    if (rowRect.top <= h.top || rowRect.top >= nextHeaderTop) continue;

                    const rowText = clean(row.innerText);
                    if (!dateRe.test(rowText)) continue;

                    const inputs = Array.from(row.querySelectorAll('input')).filter(visible);
                    const textInputs = inputs.filter(i => (i.type || 'text').toLowerCase() !== 'checkbox');

                    // Real CentralReach service rows contain the stacked pointer
                    // and modifier inputs.  Claim-heading rows can contain a DOB
                    // and a checkbox but do not contain this input cluster.
                    if (textInputs.length < 4) continue;

                    const pointerValues = textInputs.slice(0, 4).map(i => clean(i.value));
                    const modifierValues = textInputs.slice(4, 8).map(i => clean(i.value));
                    serviceRows.push({ rowText, pointerValues, modifierValues });
                }

                output.push({
                    client: clean(m[1]), id: m[2],
                    auth: authM ? clean(authM[1]) : '',
                    diag: diagM ? clean(diagM[1]) : '',
                    serviceRows
                });
            }
            return output;
        }"""
    )


def collect_claims(context: BrowserContext) -> list[dict]:
    claims: list[dict] = []
    seen: set[str] = set()
    for frame in all_frames(context):
        try:
            for item in _claim_snapshot(frame):
                if item["id"] not in seen:
                    seen.add(item["id"])
                    claims.append(item)
        except Exception:
            continue
    return claims


def validate_claims(
    context: BrowserContext,
    location_mode: str,
    payer_mode: str = "kaiser",
) -> list[ClaimIssue]:
    issues: list[ClaimIssue] = []
    claims = collect_claims(context)
    if not claims:
        return [ClaimIssue("ALL CLAIMS", "No claim groups could be read.")]
    for claim in claims:
        client = f"{claim['client']} (ID: {claim['id']})"
        auth = str(claim.get("auth", "")).strip()
        diag = str(claim.get("diag", "")).strip()
        if not auth or auth.lower() in {"n/a", "na", "none", "-"}:
            issues.append(ClaimIssue(client, "Missing authorization number."))
        if not diag or diag.lower() in {"n/a", "na", "none", "-"}:
            issues.append(ClaimIssue(client, "Missing diagnosis code(s)."))
        rows = claim.get("serviceRows", [])
        if not rows:
            issues.append(ClaimIssue(client, "No expanded service-code rows were found."))
            continue
        for number, row in enumerate(rows, 1):
            pointers = [str(v).strip() for v in row.get("pointerValues", []) if str(v).strip()]
            modifiers = [str(v).strip() for v in row.get("modifierValues", []) if str(v).strip()]
            normalized_modifiers = [value.upper() for value in modifiers]
            if not pointers:
                issues.append(ClaimIssue(client, f"Service row {number} has no pointer."))
            if "MO" in normalized_modifiers:
                issues.append(
                    ClaimIssue(
                        client,
                        f"Service row {number} has modifier MO; remove it before continuing.",
                    )
                )
            mode = location_mode.strip().lower()
            payer = payer_mode.strip().lower()
            if payer == "essc":
                # ESSC does not use modifier 95 for either Office or Telehealth.
                # Modifier 52 is acceptable when already present and requires no action.
                if "95" in normalized_modifiers:
                    issues.append(
                        ClaimIssue(
                            client,
                            f"Service row {number} has modifier 95 on an ESSC claim; remove it.",
                        )
                    )
            else:
                if mode == "office" and "95" in normalized_modifiers:
                    issues.append(ClaimIssue(client, f"Service row {number} has modifier 95 on an Office claim."))
                elif mode == "telehealth" and "95" not in normalized_modifiers:
                    issues.append(ClaimIssue(client, f"Service row {number} is missing required modifier 95 for Telehealth."))
    return issues


def pause_until_valid(
    context: BrowserContext,
    location_mode: str,
    payer_mode: str = "kaiser",
) -> None:
    page = newest_page(context)
    expand_all_claims(context)
    while True:
        issues = validate_claims(context, location_mode, payer_mode)
        if not issues:
            print("\nAll claim checks passed: authorization, diagnosis codes, pointers, and modifiers.")
            return
        print("\nCLAIM VALIDATION NEEDS CORRECTION")
        print("=" * 72)
        for issue in issues:
            print(f"- {issue.client}: {issue.issue}")
        print("=" * 72)
        screenshot(page, "claim_validation_issues", force=True)
        answer = input(
            "Fix these items in CentralReach, then press ENTER to recheck. "
            "Type STOP to leave the page unchanged: "
        ).strip().upper()
        if answer == "STOP":
            raise RuntimeError("Stopped for manual claim corrections.")
        page.wait_for_timeout(150)


def _field_header(
    context: BrowserContext,
    label: str,
) -> tuple[Frame, Locator, dict[str, float]]:
    """Locate the exact first-row claim-grid column for ``label``.

    CentralReach repeats labels such as Billing outside the claims grid and its
    header markup is deeply nested.  Resolve the exact label text first, then
    choose the smallest ancestor that contains that label plus its own search
    and settings controls.  Ancestors containing another known column label are
    rejected so Facility, Billing, and Provider Supplier cannot fall back to the
    Provider cell.
    """
    candidates: list[tuple[float, Frame, Locator, dict[str, float]]] = []

    for frame in all_frames(context):
        try:
            results = frame.evaluate(
                """(expected) => {
                    const known = new Set([
                        'PROVIDER', 'PAYOR', 'FACILITY', 'BILLING', 'REFERRER',
                        'ORDERING', 'SUPERVISING', 'PROVIDER SUPPLIER',
                        'POINTER', 'MODIFIERS'
                    ]);
                    const norm = (value) => String(value || '')
                        .replace(/\s+/g, ' ').trim().toUpperCase();
                    const target = norm(expected);
                    const visible = (el) => {
                        if (!el) return false;
                        const style = getComputedStyle(el);
                        const r = el.getBoundingClientRect();
                        return style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            r.width > 0 && r.height > 0 &&
                            r.bottom > 0 && r.top < innerHeight;
                    };
                    const iconSemantic = (el) => (
                        String(el.getAttribute?.('class') || '') + ' ' +
                        String(el.getAttribute?.('aria-label') || '') + ' ' +
                        String(el.getAttribute?.('title') || '') + ' ' +
                        String(el.getAttribute?.('data-icon') || '') + ' ' +
                        String(el.outerHTML || '')
                    ).toLowerCase();
                    const exactKnownLabels = (root) => {
                        const labels = [];
                        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                        let n;
                        while ((n = walker.nextNode())) {
                            const value = norm(n.nodeValue);
                            if (known.has(value)) labels.push(value);
                        }
                        return [...new Set(labels)];
                    };

                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    const out = [];
                    let node;
                    while ((node = walker.nextNode())) {
                        if (norm(node.nodeValue) !== target) continue;
                        const textEl = node.parentElement;
                        if (!visible(textEl)) continue;
                        if (textEl.closest('nav, [role="navigation"], header')) continue;
                        const tr = textEl.getBoundingClientRect();
                        if (tr.top < 120) continue;

                        let cell = textEl;
                        for (let depth = 0; cell && depth < 9; depth += 1, cell = cell.parentElement) {
                            const r = cell.getBoundingClientRect();
                            if (r.width < 60 || r.width > 260 || r.height < 22 || r.height > 125) continue;
                            if (tr.left < r.left - 1 || tr.right > r.right + 1) continue;

                            const labels = exactKnownLabels(cell);
                            if (labels.length !== 1 || labels[0] !== target) continue;

                            const controls = Array.from(cell.querySelectorAll(
                                'button, [role="button"], a, i, svg, span'
                            )).filter((el) => {
                                if (!visible(el)) return false;
                                const cr = el.getBoundingClientRect();
                                const cx = cr.left + cr.width / 2;
                                const cy = cr.top + cr.height / 2;
                                return cx >= r.left + r.width * 0.45 &&
                                    cx <= r.right + 3 &&
                                    cy >= r.top - 2 && cy <= r.top + 46 &&
                                    cr.width <= 48 && cr.height <= 48;
                            });
                            const search = controls.some((el) => /search|magnif|lookup/.test(iconSemantic(el)));
                            const gear = controls.some((el) => /gear|cog|setting/.test(iconSemantic(el)));
                            if (!(search && gear) && controls.length < 2) continue;

                            const score = r.top * 100000 + r.width * 100 + depth;
                            out.push({x:r.left, y:r.top, width:r.width, height:r.height, score});
                        }
                    }
                    out.sort((a,b) => a.score - b.score);
                    return out;
                }""",
                label,
            )
            for result in results or []:
                box = {
                    "x": float(result["x"]),
                    "y": float(result["y"]),
                    "width": float(result["width"]),
                    "height": float(result["height"]),
                }
                candidates.append((float(result["score"]), frame, frame.locator("body"), box))
        except Exception:
            continue

    if not candidates:
        raise RuntimeError(
            f"Could not isolate the exact {label} claim-grid column."
        )

    candidates.sort(key=lambda value: value[0])
    _, frame, item, box = candidates[0]
    logging.info(
        "%s exact claim column located at x=%.1f y=%.1f width=%.1f",
        label, box["x"], box["y"], box["width"],
    )
    return frame, item, box

def _center(box: dict[str, float]) -> tuple[float, float]:
    return (
        box["x"] + box["width"] / 2,
        box["y"] + box["height"] / 2,
    )


def _editable_for_header(frame: Frame, header_box: dict[str, float]):
    """Return the actual editable element inside the opened white box.

    Playwright visibility checks can miss CentralReach's search input because
    the framework wraps it in a custom control. This searches by rendered
    geometry and also probes the exact white-box point with elementsFromPoint.
    """
    handle = frame.evaluate_handle(
        """({x, y, width, height}) => {
            const left = x - 8;
            const right = x + width + 8;
            const targetX = x + width / 2;
            const targetY = y + 52;
            const editable = (el) => el && (
                el.matches('input, textarea, [contenteditable=\"true\"], [contenteditable=\"plaintext-only\"], [role=\"textbox\"]')
            );
            const rectOkay = (el) => {
                const r = el.getBoundingClientRect();
                const cx = r.left + r.width / 2;
                const cy = r.top + r.height / 2;
                return r.width >= 20 && r.height >= 8 &&
                    cx >= left && cx <= right &&
                    cy >= y + 15 && cy <= y + 125;
            };

            const all = Array.from(document.querySelectorAll(
                'input, textarea, [contenteditable=\"true\"], [contenteditable=\"plaintext-only\"], [role=\"textbox\"]'
            )).filter(rectOkay);
            all.sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                const ad = Math.abs((ar.left + ar.width / 2) - targetX) * 5 +
                    Math.abs((ar.top + ar.height / 2) - targetY);
                const bd = Math.abs((br.left + br.width / 2) - targetX) * 5 +
                    Math.abs((br.top + br.height / 2) - targetY);
                return ad - bd;
            });
            if (all.length) return all[0];

            for (const el of document.elementsFromPoint(targetX, targetY)) {
                if (editable(el)) return el;
                const child = el.querySelector && el.querySelector(
                    'input, textarea, [contenteditable=\"true\"], [contenteditable=\"plaintext-only\"], [role=\"textbox\"]'
                );
                if (child) return child;
                let parent = el.parentElement;
                for (let i = 0; parent && i < 4; i += 1, parent = parent.parentElement) {
                    if (editable(parent)) return parent;
                    const nested = parent.querySelector && parent.querySelector(
                        'input, textarea, [contenteditable=\"true\"], [contenteditable=\"plaintext-only\"], [role=\"textbox\"]'
                    );
                    if (nested && rectOkay(nested)) return nested;
                }
            }
            return null;
        }""",
        header_box,
    )
    return handle.as_element()

def _icon_semantics(item: Locator) -> str:
    values = []
    for attribute in ("class", "aria-label", "title", "data-icon"):
        try:
            values.append(item.get_attribute(attribute) or "")
        except Exception:
            pass
    try:
        values.append(
            item.evaluate(
                "(element) => String(element.outerHTML || '').toLowerCase()"
            )
        )
    except Exception:
        pass
    return " ".join(values).lower()


def _click_header_icon(
    context: BrowserContext,
    label: str,
    frame: Frame,
    header_box: dict[str, float],
    icon_kind: str,
) -> None:
    """Click the search or gear icon inside one exact claim-grid header.

    This uses the header rectangle and inspects only compact controls inside it.
    It avoids coordinate guesses that can hit an adjacent column, dropdown arrow,
    or the top Billing navigation tab.
    """
    page = newest_page(context)
    result = frame.evaluate(
        """({box, kind}) => {
            const visible = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                    r.width > 0 && r.height > 0;
            };
            const sem = (el) => (
                String(el.getAttribute?.('class') || '') + ' ' +
                String(el.getAttribute?.('aria-label') || '') + ' ' +
                String(el.getAttribute?.('title') || '') + ' ' +
                String(el.getAttribute?.('data-icon') || '') + ' ' +
                String(el.outerHTML || '')
            ).toLowerCase();
            const inHeader = (r) => {
                const cx = r.left + r.width / 2;
                const cy = r.top + r.height / 2;
                return cx >= box.x + box.width * 0.42 &&
                    cx <= box.x + box.width + 4 &&
                    cy >= box.y - 3 && cy <= box.y + 43 &&
                    r.width <= 48 && r.height <= 48;
            };
            const actionable = (el) => {
                let cur = el;
                for (let i = 0; cur && i < 5; i += 1, cur = cur.parentElement) {
                    if (cur.matches?.('button, a, [role="button"], i, svg, span')) {
                        const r = cur.getBoundingClientRect();
                        if (visible(cur) && inHeader(r)) return cur;
                    }
                }
                return el;
            };

            const raw = Array.from(document.querySelectorAll(
                'button, a, [role="button"], i, svg, span'
            )).filter((el) => visible(el) && inHeader(el.getBoundingClientRect()));

            const unique = [];
            const seen = new Set();
            for (const el of raw) {
                const target = actionable(el);
                const r = target.getBoundingClientRect();
                const key = [Math.round(r.left), Math.round(r.top),
                    Math.round(r.width), Math.round(r.height)].join(':');
                if (seen.has(key)) continue;
                seen.add(key);
                unique.push({el: target, r, semantic: sem(target) + ' ' + sem(el)});
            }
            unique.sort((a, b) => a.r.left - b.r.left);

            let selected = null;
            if (kind === 'search') {
                selected = unique.find((x) => /search|magnif|lookup/.test(x.semantic));
            } else {
                selected = unique.find((x) => /gear|cog|setting/.test(x.semantic));
            }

            // CentralReach visually orders the controls as search, gear, arrow.
            // When icon libraries hide semantics, choose from distinct right-side
            // controls by position rather than using a guessed page coordinate.
            if (!selected) {
                const compact = unique.filter((x) => x.r.left >= box.x + box.width * 0.48);
                if (kind === 'search' && compact.length >= 1) selected = compact[0];
                if (kind === 'gear' && compact.length >= 2) selected = compact[1];
            }
            if (!selected) return null;

            selected.el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
            selected.el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
            selected.el.click();
            return {
                x: selected.r.left + selected.r.width / 2,
                y: selected.r.top + selected.r.height / 2,
                semantic: selected.semantic.slice(0, 180)
            };
        }""",
        {"box": header_box, "kind": icon_kind},
    )
    if not result:
        screenshot(page, f"{label}_{icon_kind}_icon_not_found", force=True)
        raise RuntimeError(f"Could not identify the {icon_kind} icon for {label}.")
    logging.info(
        "Clicked the %s %s icon at x=%.1f y=%.1f.",
        label, icon_kind, float(result["x"]), float(result["y"]),
    )
    page.wait_for_timeout(180 if icon_kind == "search" else 120)


def _click_header_search_icon(
    context: BrowserContext,
    label: str,
    frame: Frame,
    header_box: dict[str, float],
) -> None:
    _click_header_icon(context, label, frame, header_box, "search")


def _ensure_field_input(
    context: BrowserContext,
    label: str,
):
    """Open the selected column and focus only the search box beneath it.

    CentralReach sometimes renders the white search area through a wrapper that
    is not exposed as a normal input until it receives a real click. We first
    try normal DOM detection, then click several safe points inside the exact
    selected column and return the element that actually receives focus.
    """
    _prepare_claim_header_area(context)
    page = newest_page(context)
    frame, _, header_box = _field_header(context, label)

    logging.info("Opening the exact %s search box.", label)
    _click_header_search_icon(context, label, frame, header_box)

    target_x = header_box["x"] + header_box["width"] / 2
    header_bottom = header_box["y"] + header_box["height"]
    target_ys = [header_bottom + 5, header_bottom + 9, header_bottom + 13]

    script = r"""({box, x, ys}) => {
        const visible = (el) => {
            if (!el) return false;
            const style = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
                r.width >= 20 && r.height >= 8;
        };
        const inColumn = (el) => {
            if (!visible(el)) return false;
            const r = el.getBoundingClientRect();
            const cx = r.left + r.width / 2;
            const cy = r.top + r.height / 2;
            return cx >= box.x - 4 && cx <= box.x + box.width + 4 &&
                cy >= box.y + 8 && cy <= box.y + 155;
        };
        const editableSelector = [
            'input', 'textarea', '[contenteditable="true"]',
            '[contenteditable="plaintext-only"]', '[role="textbox"]'
        ].join(',');

        const candidates = Array.from(document.querySelectorAll(editableSelector))
            .filter(inColumn);
        candidates.sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return Math.abs((ar.left + ar.width / 2) - x) -
                Math.abs((br.left + br.width / 2) - x);
        });
        if (candidates[0]) {
            candidates[0].focus({preventScroll: true});
            return candidates[0];
        }

        // The visible white area may be a wrapper. Click only points that are
        // horizontally inside this exact column, then return what gained focus.
        for (const y of ys) {
            const hit = document.elementFromPoint(x, y);
            if (!hit) continue;
            const r = hit.getBoundingClientRect();
            if (x < box.x - 4 || x > box.x + box.width + 4) continue;
            hit.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX:x, clientY:y}));
            hit.click();
            hit.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX:x, clientY:y}));
            const active = document.activeElement;
            if (active && active !== document.body && active !== document.documentElement) {
                return active;
            }
            const nested = hit.querySelector && hit.querySelector(editableSelector);
            if (nested) { nested.focus({preventScroll:true}); return nested; }
        }
        return null;
    }"""

    for _ in range(8):
        handle = frame.evaluate_handle(
            script,
            {"box": header_box, "x": target_x, "ys": target_ys},
        )
        editable = handle.as_element()
        if editable is not None:
            try:
                editable.evaluate("el => el.focus({preventScroll: true})")
            except Exception:
                pass
            logging.info(
                "Focused the exact %s search box at x=%.1f using upper search-box points.",
                label, target_x,
            )
            return editable, header_box
        page.wait_for_timeout(80)

    screenshot(page, f"{label}_editable_not_found", force=True)
    raise RuntimeError(
        f"The {label} magnifying glass opened, but its exact search box could not be focused."
    )

def _settings_icon_for_header(
    context: BrowserContext,
    label: str,
) -> Locator:
    _prepare_claim_header_area(context)
    frame, _, header_box = _field_header(context, label)
    header_right = header_box["x"] + header_box["width"]
    header_center_y = header_box["y"] + header_box["height"] / 2

    controls = frame.locator(
        "button, [role='button'], a, i, svg, span"
    )
    ranked: list[tuple[float, Locator]] = []
    seen: set[tuple[int, int]] = set()

    for index in range(controls.count()):
        item = controls.nth(index)
        try:
            if not item.is_visible():
                continue
            box = item.bounding_box()
            if box is None:
                continue

            center_x, center_y = _center(box)
            key = (round(center_x), round(center_y))
            if key in seen:
                continue
            seen.add(key)

            dx = center_x - header_right
            dy = abs(center_y - header_center_y)
            viewport = frame.evaluate(
                "() => ({width: window.innerWidth, height: window.innerHeight})"
            )
            if (
                box["y"] + box["height"] <= 0
                or box["y"] >= float(viewport["height"])
                or box["x"] + box["width"] <= 0
                or box["x"] >= float(viewport["width"])
            ):
                continue

            if -30 <= dx <= 260 and dy <= 90:
                attrs = " ".join([
                    item.get_attribute("class") or "",
                    item.get_attribute("aria-label") or "",
                    item.get_attribute("title") or "",
                    item.get_attribute("data-icon") or "",
                ]).lower()

                is_gear = any(
                    token in attrs
                    for token in ["gear", "cog", "setting"]
                )

                # SVG/icon libraries sometimes put the semantic class on a
                # child node. Check the rendered HTML as a fallback.
                if not is_gear:
                    try:
                        html = item.evaluate(
                            "(element) => String(element.outerHTML || '').toLowerCase()"
                        )
                        is_gear = any(
                            token in html
                            for token in ["gear", "cog", "setting"]
                        )
                    except Exception:
                        is_gear = False

                if not is_gear:
                    continue

                # The gear normally sits after the search icon.
                target_dx = 55
                score = abs(dx - target_dx) * 10 + dy * 5
                ranked.append((score, item))
        except Exception:
            continue

    if not ranked:
        raise RuntimeError(
            f"Could not find the settings icon for {label}."
        )

    ranked.sort(key=lambda value: value[0])
    return ranked[0][1]

def _select_option(context: BrowserContext, pattern: re.Pattern[str]) -> None:
    """Select the visible autocomplete row using an in-frame DOM click."""
    page = newest_page(context)
    source = pattern.pattern
    flags = pattern.flags

    # CentralReach often renders the blue autocomplete row as a nested DIV.
    # Click inside the frame that owns it so iframe offsets cannot misplace the click.
    for _ in range(20):
        for frame in all_frames(context):
            try:
                result = frame.evaluate(
                    r"""({source, flags}) => {
                        const re = new RegExp(source, flags & 2 ? 'i' : '');
                        const norm = v => String(v || '').replace(/\s+/g, ' ').trim();
                        const visible = el => {
                            if (!el) return false;
                            const st = getComputedStyle(el);
                            const r = el.getBoundingClientRect();
                            return st.display !== 'none' && st.visibility !== 'hidden' &&
                                st.opacity !== '0' && r.width > 20 && r.height > 16 &&
                                r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
                        };
                        const selectors = [
                            '[role="option"]', 'li', '.ui-menu-item', '.ui-autocomplete li',
                            '[class*="autocomplete"]', '[class*="suggestion"]', '[class*="result"]',
                            'a', 'div'
                        ];
                        const found = [];
                        for (const sel of selectors) {
                            for (const el of document.querySelectorAll(sel)) {
                                if (!visible(el)) continue;
                                const text = norm(el.innerText || el.textContent);
                                if (!text || !re.test(text)) continue;
                                const r = el.getBoundingClientRect();
                                // Prefer a compact dropdown row, not a large ancestor container.
                                if (r.height > 140 || r.width > 700) continue;
                                found.push({el, area:r.width*r.height, len:text.length, top:r.top});
                            }
                        }
                        found.sort((a,b) => a.area-b.area || a.len-b.len || a.top-b.top);
                        const target = found[0]?.el;
                        if (!target) return null;
                        target.scrollIntoView({block:'nearest', inline:'nearest'});
                        const r = target.getBoundingClientRect();
                        const x = r.left + Math.min(r.width/2, 80);
                        const y = r.top + r.height/2;
                        for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) {
                            target.dispatchEvent(new MouseEvent(type, {
                                bubbles:true, cancelable:true, view:window, clientX:x, clientY:y
                            }));
                        }
                        if (typeof target.click === 'function') target.click();
                        return {text:norm(target.innerText || target.textContent), x, y};
                    }""",
                    {"source": source, "flags": flags},
                )
                if result:
                    logging.info(
                        "Clicked autocomplete option '%s' at x=%.1f y=%.1f.",
                        result.get("text", ""), float(result.get("x", 0)), float(result.get("y", 0)),
                    )
                    page.wait_for_timeout(250)
                    return
            except Exception:
                continue
        page.wait_for_timeout(100)

    screenshot(page, "autocomplete_option_not_found", force=True)
    raise RuntimeError("The expected autocomplete option was not found or could not be clicked.")

def _visible_input_in_cell(cell: Locator) -> Locator | None:
    inputs = cell.locator("input")
    for index in range(inputs.count()):
        candidate = inputs.nth(index)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                return candidate
        except Exception:
            continue
    return None


def _click_search_icon(context: BrowserContext, label: str, cell: Locator) -> None:
    page = newest_page(context)
    controls = cell.locator(
        "button, [role='button'], a, i, svg, span, "
        "[class*='search'], [class*='magnif']"
    )
    visible_controls: list[Locator] = []

    for index in range(controls.count()):
        item = controls.nth(index)
        try:
            if item.is_visible():
                visible_controls.append(item)
        except Exception:
            continue

    search_control = None
    for item in visible_controls:
        try:
            joined = " ".join([
                item.get_attribute("class") or "",
                item.get_attribute("aria-label") or "",
                item.get_attribute("title") or "",
                item.get_attribute("data-icon") or "",
            ]).lower()
            if any(token in joined for token in ["search", "magnif", "lookup"]):
                search_control = item
                break
        except Exception:
            continue

    # In the CentralReach header, the magnifying glass is the first icon and
    # the settings gear is the second. Use the first visible control as a safe
    # fallback when its icon has no accessible label.
    if search_control is None and visible_controls:
        search_control = visible_controls[0]

    if search_control is None:
        screenshot(page, f"{label}_search_icon_not_found", force=True)
        raise RuntimeError(
            f"No text box or magnifying glass was found under {label}."
        )

    _click(search_control)
    page.wait_for_timeout(600)


def _fill_field(
    context: BrowserContext,
    label: str,
    query: str,
    option: re.Pattern[str],
) -> None:
    page = newest_page(context)
    box, _ = _ensure_field_input(context, label)

    existing = ""
    try:
        existing = box.input_value().strip()
    except Exception:
        try:
            existing = str(box.evaluate("el => el.value || el.textContent || ''")).strip()
        except Exception:
            existing = ""

    if existing:
        logging.info("%s is already populated in the claim header.", label)
        return

    # Never send Select All to the page body. Fill only after an actual
    # input/contenteditable element has been found and focused.
    try:
        box.fill(query, timeout=4000)
    except Exception:
        box.evaluate(
            """(el, value) => {
                el.focus();
                if ('value' in el) {
                    const setter = Object.getOwnPropertyDescriptor(
                        Object.getPrototypeOf(el), 'value'
                    )?.set;
                    if (setter) setter.call(el, value);
                    else el.value = value;
                } else {
                    el.textContent = value;
                }
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'a'}));
            }""",
            query,
        )
    logging.info("Filled '%s' into the actual %s input.", query, label)
    page.wait_for_timeout(180)

    # Provider Supplier's autocomplete is rendered correctly, but CentralReach
    # does not always accept a synthetic mouse click on its blue result row.
    # Since the matching row is already visible and is the first result, commit
    # it through the focused input with ArrowDown + Enter. This uses the same
    # keyboard behavior as a person selecting the highlighted suggestion.
    if label == "Provider Supplier":
        try:
            box.press("ArrowDown", timeout=1500)
            page.wait_for_timeout(80)
            box.press("Enter", timeout=1500)
            page.wait_for_timeout(250)
            logging.info("Committed Provider Supplier autocomplete with ArrowDown + Enter.")
        except Exception as exc:
            logging.warning("Keyboard commit for Provider Supplier failed: %s", exc)
            _select_option(context, option)
    else:
        _select_option(context, option)

    logging.info("Filled %s.", label)

def _sync_all(context: BrowserContext, label: str) -> None:
    """Open the exact column gear and click its visible To all claims row in-frame."""
    _prepare_claim_header_area(context)
    page = newest_page(context)
    frame, _, header_box = _field_header(context, label)
    _click_header_icon(context, label, frame, header_box, "gear")
    page.wait_for_timeout(120)

    clicked = None
    # Use DOM events in the owning frame. This avoids page-coordinate errors
    # and works even when the menu is inside an iframe or transformed container.
    for _ in range(12):
        for current in all_frames(context):
            try:
                result = current.evaluate(
                    r"""({gearX, headerY}) => {
                        const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
                        const visible = el => {
                            if (!el) return false;
                            const st = getComputedStyle(el);
                            const r = el.getBoundingClientRect();
                            return st.display !== 'none' && st.visibility !== 'hidden' &&
                                st.opacity !== '0' && r.width > 30 && r.height > 18 &&
                                r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
                        };
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                        const rows = [];
                        let node;
                        while ((node = walker.nextNode())) {
                            if (norm(node.nodeValue) !== 'to all claims') continue;
                            let el = node.parentElement;
                            while (el && el !== document.body) {
                                const r = el.getBoundingClientRect();
                                const txt = norm(el.innerText || el.textContent);
                                if (visible(el) && txt === 'to all claims' &&
                                    r.height >= 22 && r.height <= 80 && r.width >= 80 && r.width <= 450) {
                                    const cx = r.left + r.width/2;
                                    const cy = r.top + r.height/2;
                                    rows.push({el, score:Math.abs(cx-gearX)+Math.abs(r.top-headerY)*0.25, cx, cy});
                                    break;
                                }
                                el = el.parentElement;
                            }
                        }
                        rows.sort((a,b) => a.score-b.score);
                        const target = rows[0]?.el;
                        if (!target) return null;
                        target.scrollIntoView({block:'nearest', inline:'nearest'});
                        const r = target.getBoundingClientRect();
                        const x = r.left + r.width/2;
                        const y = r.top + r.height/2;
                        for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) {
                            target.dispatchEvent(new MouseEvent(type, {
                                bubbles:true, cancelable:true, view:window, clientX:x, clientY:y
                            }));
                        }
                        if (typeof target.click === 'function') target.click();
                        return {x, y};
                    }""",
                    {
                        "gearX": header_box["x"] + header_box["width"] - 14,
                        "headerY": header_box["y"],
                    },
                )
                if result:
                    clicked = result
                    break
            except Exception:
                continue
        if clicked:
            break
        page.wait_for_timeout(80)

    if not clicked:
        screenshot(page, f"{label}_to_all_claims_not_found", force=True)
        raise RuntimeError(f"Could not click the visible To all claims menu row for {label}.")

    logging.info(
        "Clicked %s To all claims at x=%.1f y=%.1f.",
        label, float(clicked.get("x", 0)), float(clicked.get("y", 0)),
    )
    page.wait_for_timeout(300)

    # Verify the menu row is gone. Check only visible exact rows.
    still_open = False
    for current in all_frames(context):
        try:
            still_open = bool(current.evaluate(
                r"""() => {
                    const norm = v => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    for (const el of document.querySelectorAll('li, a, div, span, [role="menuitem"]')) {
                        if (norm(el.innerText || el.textContent) !== 'to all claims') continue;
                        const st = getComputedStyle(el); const r = el.getBoundingClientRect();
                        if (st.display !== 'none' && st.visibility !== 'hidden' &&
                            r.width > 20 && r.height > 12 && r.bottom > 0 && r.top < innerHeight) return true;
                    }
                    return false;
                }"""
            ))
            if still_open:
                break
        except Exception:
            continue
    if still_open:
        screenshot(page, f"{label}_to_all_claims_click_failed", force=True)
        raise RuntimeError(f"Clicked To all claims for {label}, but the menu stayed open.")

    logging.info("Synced %s to all claims.", label)



def _prepare_claim_header_area(context: BrowserContext) -> None:
    """
    Put the first expanded claim header inside the visible viewport.

    The claims page can remain scrolled far down after validation. When that
    happens, the first Provider header has a negative Y coordinate and a
    geometry search can accidentally select controls in the fixed top
    navigation bar.
    """
    page = newest_page(context)

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    for frame in all_frames(context):
        try:
            frame.evaluate(
                """() => {
                    window.scrollTo(0, 0);
                    document.documentElement.scrollTop = 0;
                    document.body.scrollTop = 0;

                    const scrollables = Array.from(
                        document.querySelectorAll('*')
                    ).filter((element) => {
                        const style = getComputedStyle(element);
                        return (
                            /(auto|scroll)/.test(style.overflowY) &&
                            element.scrollHeight > element.clientHeight + 20
                        );
                    });

                    for (const element of scrollables) {
                        element.scrollTop = 0;
                    }
                }"""
            )
        except Exception:
            continue

    page.wait_for_timeout(700)


def _dismiss_open_menus(context: BrowserContext) -> None:
    """Press Escape to close any gear menu left open from a previous retry."""
    page = newest_page(context)
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    except Exception:
        pass


def fill_and_sync_claim_fields(
    context: BrowserContext,
    billed_person: str,
    site: str,
) -> None:
    _prepare_claim_header_area(context)
    _dismiss_open_menus(context)
    person_option = re.compile(
        rf"{re.escape(billed_person)}\s*\(ID:\s*\d+\)",
        re.I,
    )

    normalized_site = site.upper()
    if normalized_site not in {"AHC", "SAC"}:
        raise ValueError(f"Unsupported billing site: {site}")

    if not BULK_MERGE_CONFIG.exists():
        raise RuntimeError(
            "Missing bulk_merge_config.json. Copy bulk_merge_config.example.json "
            "to bulk_merge_config.json and replace the example organization values "
            "with your local CentralReach bulk-merge values."
        )

    try:
        private_config = json.loads(BULK_MERGE_CONFIG.read_text(encoding="utf-8"))
        site_config = private_config[normalized_site]
        org_query = str(site_config["organization_query"]).strip()
        org_pattern = str(site_config["organization_option_regex"]).strip()
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Invalid bulk_merge_config.json configuration for {normalized_site}. "
            "Use bulk_merge_config.example.json as the template."
        ) from exc

    if not org_query or not org_pattern:
        raise RuntimeError(
            f"Bulk-merge configuration for {normalized_site} is incomplete."
        )

    org_option = re.compile(org_pattern, re.I)

    for label, query, option in [
        ("Provider", billed_person, person_option),
        ("Facility", org_query, org_option),
        ("Billing", org_query, org_option),
        ("Provider Supplier", billed_person, person_option),
    ]:
        _fill_field(context, label, query, option)
        _sync_all(context, label)

def scroll_to_start_generation(context: BrowserContext) -> None:
    page = newest_page(context)
    button = _visible_text(context, re.compile(r"^\s*Start\s+claims\s+generation\s*$", re.I))
    if button is None:
        screenshot(page, "start_claims_generation_not_found", force=True)
        raise RuntimeError("Could not find Start claims generation at the bottom of the page.")
    button.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    print("\nClaim setup is complete. Start claims generation has NOT been clicked.")
    input("Review every claim in the browser. Press ENTER here when you are finished reviewing: ")
