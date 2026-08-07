from __future__ import annotations

import logging
import re

from playwright.sync_api import BrowserContext, Frame, Locator, Page

from browser_utils import all_frames, newest_page, screenshot, visible_locator
from providers import normalize_name


def _safe_click(locator: Locator, timeout: int = 5_000) -> None:
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    try:
        locator.click(timeout=timeout)
    except Exception:
        locator.click(force=True, timeout=timeout)


def _open_providers_filter(context: BrowserContext) -> tuple[Page, Frame]:
    page = newest_page(context)

    for frame in all_frames(context):
        headers = frame.get_by_text(
            re.compile(r"^\s*Providers?\s*$", re.IGNORECASE)
        )

        for index in range(headers.count()):
            header = headers.nth(index)
            try:
                if not header.is_visible():
                    continue

                # Prefer the table-header Providers control. It normally has a
                # nearby menu/filter button. If no separate button is found,
                # clicking the header itself opens the provider panel.
                parent = header.locator("xpath=..")
                button = visible_locator(
                    parent.locator(
                        "button, [role='button'], "
                        ".fa-filter, .icon-filter, "
                        "[class*='filter'], [class*='menu']"
                    )
                )

                _safe_click(button if button is not None else header)
                page.wait_for_timeout(700)

                if _find_provider_panel_frame(context) is not None:
                    return page, frame
            except Exception:
                continue

    screenshot(page, "providers_filter_not_found", force=True)
    raise RuntimeError("Could not open the Providers filter.")


def _find_provider_panel_frame(
    context: BrowserContext,
) -> Frame | None:
    for frame in all_frames(context):
        try:
            found = frame.evaluate(
                """() => {
                    function visible(element) {
                        const style = getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    }

                    const titles = Array.from(
                        document.querySelectorAll('div, span, header, h1, h2, h3')
                    ).filter((element) => (
                        visible(element) &&
                        /^\\s*Providers?\\s*$/i.test(
                            String(element.textContent || '')
                        )
                    ));

                    return titles.some((title) => {
                        let current = title;
                        for (let depth = 0; current && depth < 8; depth += 1) {
                            const text = String(current.innerText || '');
                            const rect = current.getBoundingClientRect();
                            const lines = text
                                .split(/\\n+/)
                                .map((line) => line.trim())
                                .filter(Boolean);

                            if (
                                rect.width >= 120 &&
                                rect.width <= 700 &&
                                rect.height >= 70 &&
                                rect.height <= 1000 &&
                                lines.length >= 2
                            ) {
                                return true;
                            }
                            current = current.parentElement;
                        }
                        return false;
                    });
                }"""
            )
            if found:
                return frame
        except Exception:
            continue
    return None


def _read_panel_lines(frame: Frame) -> list[str]:
    result = frame.evaluate(
        """() => {
            function visible(element) {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return (
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    rect.width > 0 &&
                    rect.height > 0
                );
            }

            const titles = Array.from(
                document.querySelectorAll('div, span, header, h1, h2, h3')
            ).filter((element) => (
                visible(element) &&
                /^\\s*Providers?\\s*$/i.test(
                    String(element.textContent || '')
                )
            ));

            let best = null;

            for (const title of titles) {
                let current = title;

                for (let depth = 0; current && depth < 8; depth += 1) {
                    const rect = current.getBoundingClientRect();
                    const lines = String(current.innerText || '')
                        .split(/\\n+/)
                        .map((line) => line.trim())
                        .filter(Boolean);

                    const candidate = (
                        rect.width >= 120 &&
                        rect.width <= 700 &&
                        rect.height >= 70 &&
                        rect.height <= 1000 &&
                        lines.length >= 2
                    );

                    if (candidate) {
                        if (!best || lines.length < best.lines.length) {
                            best = {
                                lines,
                                width: rect.width,
                                height: rect.height
                            };
                        }
                        break;
                    }

                    current = current.parentElement;
                }
            }

            return best ? best.lines : [];
        }"""
    )

    return [str(line).strip() for line in result if str(line).strip()]


def _looks_like_person_name(value: str) -> bool:
    normalized = normalize_name(value)

    if normalized in {
        "PROVIDER",
        "PROVIDERS",
        "CLEAR",
        "SEARCH",
        "FILTER",
        "FILTERS",
    }:
        return False

    parts = normalized.split()
    if len(parts) < 2 or len(parts) > 6:
        return False

    return all(
        re.fullmatch(r"[A-Z][A-Z'\-]*", part) is not None
        for part in parts
    )


def read_full_provider_names(
    context: BrowserContext,
) -> list[str]:
    page, _ = _open_providers_filter(context)
    panel_frame = _find_provider_panel_frame(context)

    if panel_frame is None:
        screenshot(page, "providers_panel_not_found", force=True)
        raise RuntimeError(
            "The Providers filter opened, but its panel could not be read."
        )

    lines = _read_panel_lines(panel_frame)
    names: list[str] = []
    seen: set[str] = set()

    for line in lines:
        # Remove a selection marker if CentralReach adds one to the line.
        cleaned = re.sub(r"^[✓✔\-\s]+", "", line).strip()
        if not _looks_like_person_name(cleaned):
            continue

        normalized = normalize_name(cleaned)
        if normalized not in seen:
            seen.add(normalized)
            names.append(normalized)

    if not names:
        screenshot(page, "providers_panel_empty", force=True)
        raise RuntimeError(
            "No full provider names could be read from the Providers filter."
        )

    logging.info(
        "Full provider names shown by CentralReach: %s",
        names,
    )

    # Deliberately leave the Providers panel open while appointment rows are
    # reviewed and selected. The user uses that list as a visual verification
    # reference. CentralReach will naturally leave this billing page when Bulk
    # Merge Claims is opened.
    return names


def close_providers_filter(context: BrowserContext) -> None:
    page = newest_page(context)

    # First try the X control within the compact Providers panel.
    for frame in all_frames(context):
        try:
            closed = frame.evaluate(
                """() => {
                    function visible(element) {
                        const style = getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    }

                    const titles = Array.from(
                        document.querySelectorAll('div, span, header, h1, h2, h3')
                    ).filter((element) => (
                        visible(element) &&
                        /^\\s*Providers?\\s*$/i.test(
                            String(element.textContent || '')
                        )
                    ));

                    for (const title of titles) {
                        let panel = title;

                        for (let depth = 0; panel && depth < 8; depth += 1) {
                            const rect = panel.getBoundingClientRect();
                            const lines = String(panel.innerText || '')
                                .split(/\\n+/)
                                .map((line) => line.trim())
                                .filter(Boolean);

                            if (
                                rect.width >= 120 &&
                                rect.width <= 700 &&
                                rect.height >= 70 &&
                                rect.height <= 1000 &&
                                lines.length >= 2
                            ) {
                                const controls = Array.from(
                                    panel.querySelectorAll(
                                        'button, [role="button"], a, span'
                                    )
                                ).filter(visible);

                                const close = controls.find((element) => {
                                    const text = String(
                                        element.textContent || ''
                                    ).trim();
                                    const label = String(
                                        element.getAttribute('aria-label') || ''
                                    );
                                    const titleText = String(
                                        element.getAttribute('title') || ''
                                    );
                                    const classes = String(
                                        element.className || ''
                                    );

                                    return (
                                        text === '×' ||
                                        text === 'X' ||
                                        /^close$/i.test(label) ||
                                        /^close$/i.test(titleText) ||
                                        /\\bclose\\b/i.test(classes)
                                    );
                                });

                                if (close) {
                                    close.click();
                                    return true;
                                }
                            }

                            panel = panel.parentElement;
                        }
                    }

                    return false;
                }"""
            )
            if closed:
                return
        except Exception:
            continue

    # Fallback: click the Providers table header again to toggle the panel.
    for frame in all_frames(context):
        header = visible_locator(
            frame.get_by_text(
                re.compile(r"^\s*Providers?\s*$", re.IGNORECASE)
            )
        )
        if header is None:
            continue
        try:
            _safe_click(header)
            return
        except Exception:
            continue
