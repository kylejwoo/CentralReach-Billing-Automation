from __future__ import annotations

import logging
import re

from playwright.sync_api import BrowserContext, Page

from browser_utils import all_frames, newest_page, screenshot, visible_locator

OFFICE_LOCATION = "11 Office"
TELEHEALTH_LOCATION = "02 Telehealth Provided Other than in Patient's Home"
KNOWN_LOCATIONS = (OFFICE_LOCATION, TELEHEALTH_LOCATION)


def _visible_exact_text(context: BrowserContext, text: str):
    pattern = re.compile(rf"^\s*{re.escape(text)}\s*$", re.I)
    for frame in all_frames(context):
        locator = visible_locator(frame.get_by_text(pattern))
        if locator is not None:
            return locator
    return None


def _visible_known_location_options(context: BrowserContext) -> dict[str, object]:
    """Return exact Office/Telehealth options currently visible in the Locations panel.

    This intentionally ignores top filter chips because those include a Location: prefix.
    Exact matching also avoids appointment-grid values such as 11: Office or 02: Tele....
    """
    found: dict[str, object] = {}
    for location in KNOWN_LOCATIONS:
        locator = _visible_exact_text(context, location)
        if locator is not None:
            found[location] = locator
    return found


def _click_exact_option(option, location_name: str) -> None:
    try:
        option.click(timeout=3_000)
    except Exception:
        option.click(force=True, timeout=3_000)
    logging.info("Selected exact Location option: %s", location_name)


def choose_location(context: BrowserContext, location_name: str) -> Page | None:
    """Choose one exact appointment Location filter value without blind clicks.

    If the Locations panel is already open, inspect its exact available options first.
    When only Office or only Telehealth exists and the requested location is absent,
    return None immediately instead of clicking an unrelated control or filter chip.
    """
    page = newest_page(context)

    # Safest path: the floating Locations panel is already open. Use only an exact
    # option from that panel. If it exposes at least one known location but not the
    # requested one, the requested phase genuinely does not exist and is skipped.
    visible_options = _visible_known_location_options(context)
    if location_name in visible_options:
        _click_exact_option(visible_options[location_name], location_name)
        page.wait_for_timeout(250)
        return newest_page(context)
    if visible_options:
        logging.info(
            "Skipping unavailable Location %s. Locations panel currently offers: %s",
            location_name,
            ", ".join(sorted(visible_options)),
        )
        return None

    # No known options are visible, so open the real billing-grid Location filter.
    opened = False
    for frame in all_frames(context):
        headers = frame.get_by_text(re.compile(r"^\s*Location\s*$", re.I))
        for index in range(headers.count()):
            header = headers.nth(index)
            try:
                if not header.is_visible():
                    continue
                box = header.bounding_box()
                if not box or box["y"] < 120:
                    continue

                parent = header.locator("xpath=..")
                button = visible_locator(
                    parent.locator(
                        "button, [role='button'], .fa-filter, .icon-filter, "
                        "[class*='filter'], [class*='menu']"
                    )
                )
                target = button if button is not None else header
                try:
                    target.click(timeout=3_000)
                except Exception:
                    target.click(force=True, timeout=3_000)
                opened = True
                break
            except Exception:
                continue
        if opened:
            break

    if not opened:
        screenshot(page, "location_filter_not_found", force=True)
        raise RuntimeError("Could not open the Location filter.")

    page.wait_for_timeout(200)
    visible_options = _visible_known_location_options(context)
    option = visible_options.get(location_name)
    if option is None:
        logging.info(
            "Location option is not available and will be skipped: %s. Visible options: %s",
            location_name,
            ", ".join(sorted(visible_options)) if visible_options else "none",
        )
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return None

    _click_exact_option(option, location_name)
    page.wait_for_timeout(250)
    return newest_page(context)


def choose_office_location(context: BrowserContext, location_name: str) -> Page | None:
    return choose_location(context, location_name)
