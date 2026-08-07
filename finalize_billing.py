from __future__ import annotations

import logging
import re

from playwright.sync_api import BrowserContext, Frame, Locator

from browser_utils import all_frames, newest_page, screenshot, visible_locator

LOCATION_PATTERNS = (
    re.compile(r"^\s*(?:Location:\s*)?11:?\s*Office\s*$", re.I),
    re.compile(
        r"^\s*(?:Location:\s*)?02:?\s*Telehealth\s+Provided\s+Other\s+than\s+in\s+Patient[’\']s\s+Home\s*$",
        re.I,
    ),
)



def _click(locator: Locator, timeout: int = 1200) -> None:
    try:
        locator.click(timeout=timeout)
    except Exception:
        locator.click(force=True, timeout=timeout)


def _visible_exact_text(context: BrowserContext, text: str) -> tuple[Locator | None, Frame | None]:
    pattern = re.compile(rf"^\s*{re.escape(text)}\s*$", re.I)
    for frame in all_frames(context):
        item = visible_locator(frame.get_by_text(pattern))
        if item is not None:
            return item, frame
    return None, None


def _remove_filter_chip(context: BrowserContext, chip_text: str) -> bool:
    """Remove the exact billed-person filter chip from the top filter row.

    CentralReach normally displays text filters with quotation marks, for example
    ``"Alex Example"``.  Match either quoted or unquoted text, then click only
    the X immediately to the right of that top-row chip.
    """
    escaped = re.escape(chip_text)
    pattern = re.compile(rf'^\s*["“”]?\s*{escaped}\s*["“”]?\s*$', re.I)

    for frame in all_frames(context):
        candidates = frame.get_by_text(pattern)
        for i in range(candidates.count()):
            label = candidates.nth(i)
            try:
                if not label.is_visible():
                    continue
                label_box = label.bounding_box()
                if not label_box:
                    continue

                # Filter chips are in the compact row near the top of Billing.
                # Ignore provider names repeated in the table or floating panels.
                if label_box["y"] > 270:
                    continue

                node = label
                possible: list[tuple[float, Locator]] = []
                for _ in range(4):
                    parent = node.locator("xpath=..")
                    removers = parent.locator(
                        "button, [role='button'], .fa-times, .fa-close, "
                        "[class*='remove'], [class*='close'], [title*='remove' i], "
                        "[aria-label*='remove' i], [aria-label*='close' i]"
                    )
                    for j in range(removers.count()):
                        remove = removers.nth(j)
                        try:
                            if not remove.is_visible():
                                continue
                            text = (remove.inner_text(timeout=75) or "").strip().lower()
                            cls = (remove.get_attribute("class") or "").lower()
                            aria = (remove.get_attribute("aria-label") or "").lower()
                            title = (remove.get_attribute("title") or "").lower()
                            if not (
                                text in {"x", "×"}
                                or "remove" in cls or "close" in cls or "times" in cls
                                or "remove" in aria or "close" in aria
                                or "remove" in title or "close" in title
                            ):
                                continue
                            box = remove.bounding_box()
                            if not box:
                                continue
                            label_cy = label_box["y"] + label_box["height"] / 2
                            box_cy = box["y"] + box["height"] / 2
                            same_row = abs(box_cy - label_cy) <= max(20, label_box["height"])
                            right_side = box["x"] >= label_box["x"] + label_box["width"] - 10
                            close_enough = box["x"] <= label_box["x"] + label_box["width"] + 100
                            top_row = box["y"] < 285
                            if same_row and right_side and close_enough and top_row:
                                distance = abs(box["x"] - (label_box["x"] + label_box["width"]))
                                possible.append((distance, remove))
                        except Exception:
                            continue
                    node = parent

                if possible:
                    possible.sort(key=lambda item: item[0])
                    _click(possible[0][1])
                    newest_page(context).wait_for_timeout(60)
                    logging.info("Removed billed-person filter chip: %s", chip_text)
                    return True

                # Last-resort geometric click on the compact X segment immediately
                # to the right of the exact name chip. This avoids matching names in
                # the billing rows while supporting CentralReach's non-semantic X.
                page = newest_page(context)
                click_x = label_box["x"] + label_box["width"] + 20
                click_y = label_box["y"] + label_box["height"] / 2
                page.mouse.click(click_x, click_y)
                page.wait_for_timeout(60)

                # Confirm the quoted/unquoted top-row chip disappeared.
                still_visible = False
                for check_frame in all_frames(context):
                    checks = check_frame.get_by_text(pattern)
                    for k in range(checks.count()):
                        check = checks.nth(k)
                        try:
                            if check.is_visible():
                                box = check.bounding_box()
                                if box and box["y"] <= 270:
                                    still_visible = True
                                    break
                        except Exception:
                            continue
                    if still_visible:
                        break
                if not still_visible:
                    logging.info("Removed billed-person filter chip geometrically: %s", chip_text)
                    return True
            except Exception:
                continue
    return False



def _remove_location_filter_chip(context: BrowserContext, pattern: re.Pattern[str]) -> bool:
    """Remove one Office/Telehealth chip from the TOP filter row only.

    CentralReach also repeats the same location text inside the floating Locations
    panel.  That option must never be treated as the removable filter chip.
    """
    for frame in all_frames(context):
        candidates = frame.get_by_text(pattern)
        for i in range(candidates.count()):
            label = candidates.nth(i)
            try:
                if not label.is_visible():
                    continue
                label_box = label.bounding_box()
                if not label_box:
                    continue

                # The active filter chips are near the top of Billing. The same
                # text in the floating Locations panel appears much lower.
                if label_box["y"] > 260:
                    continue

                node = label
                possible: list[tuple[float, Locator]] = []
                compact_container_box = None
                for _ in range(4):
                    parent = node.locator("xpath=..")
                    try:
                        parent_box = parent.bounding_box()
                        if parent_box and parent_box["height"] <= 80 and parent_box["width"] <= 900:
                            compact_container_box = parent_box
                    except Exception:
                        parent_box = None

                    removers = parent.locator(
                        "button, [role='button'], .fa-times, .fa-close, "
                        "[class*='remove'], [class*='close'], [title*='remove' i], "
                        "[aria-label*='remove' i], [aria-label*='close' i]"
                    )
                    for j in range(removers.count()):
                        remove = removers.nth(j)
                        try:
                            if not remove.is_visible():
                                continue
                            text = (remove.inner_text(timeout=75) or "").strip().lower()
                            cls = (remove.get_attribute("class") or "").lower()
                            aria = (remove.get_attribute("aria-label") or "").lower()
                            title = (remove.get_attribute("title") or "").lower()
                            if not (
                                text in {"x", "×"}
                                or "remove" in cls or "close" in cls or "times" in cls
                                or "remove" in aria or "close" in aria
                                or "remove" in title or "close" in title
                            ):
                                continue
                            box = remove.bounding_box()
                            if not box or box["y"] > 280:
                                continue
                            label_mid_y = label_box["y"] + label_box["height"] / 2
                            remove_mid_y = box["y"] + box["height"] / 2
                            same_row = abs(remove_mid_y - label_mid_y) <= max(24, label_box["height"])
                            right_side = box["x"] >= label_box["x"] + label_box["width"] - 20
                            close_enough = box["x"] <= label_box["x"] + label_box["width"] + 140
                            if same_row and right_side and close_enough:
                                distance = abs(box["x"] - (label_box["x"] + label_box["width"]))
                                possible.append((distance, remove))
                        except Exception:
                            continue
                    node = parent

                if possible:
                    possible.sort(key=lambda item: item[0])
                    _click(possible[0][1])
                    frame.page.wait_for_timeout(60)
                    logging.info("Removed top-row location filter chip matching: %s", pattern.pattern)
                    return True

                # Geometry fallback for CentralReach chips whose X is a plain
                # sibling span with no useful role/class. Click the far-right
                # edge of the nearest compact top-row container.
                if compact_container_box:
                    x = compact_container_box["x"] + compact_container_box["width"] - 12
                    y = compact_container_box["y"] + compact_container_box["height"] / 2
                    frame.page.mouse.click(x, y)
                    frame.page.wait_for_timeout(60)
                    logging.info(
                        "Clicked top-row location chip X geometrically at x=%.1f y=%.1f for %s",
                        x, y, pattern.pattern,
                    )
                    return True
            except Exception:
                continue
    return False


def _location_filter_visible(context: BrowserContext) -> bool:
    """Return True only when a removable location FILTER CHIP still exists.

    The same Office/Telehealth text can also appear in billing table rows and in
    the floating Locations panel. Those are not filters. A real active filter
    must be in the upper filter strip and have a nearby remove/close control on
    the same compact row.
    """
    for pattern in LOCATION_PATTERNS:
        for frame in all_frames(context):
            candidates = frame.get_by_text(pattern)
            for i in range(candidates.count()):
                try:
                    label = candidates.nth(i)
                    if not label.is_visible():
                        continue
                    label_box = label.bounding_box()
                    if not label_box or label_box["y"] > 240:
                        continue

                    node = label
                    for _ in range(6):
                        parent = node.locator("xpath=..")
                        try:
                            parent_box = parent.bounding_box()
                        except Exception:
                            parent_box = None

                        # Filter chips are compact. Table rows and floating panels
                        # are much taller/wider and should never qualify.
                        if parent_box and parent_box["height"] <= 70 and parent_box["width"] <= 900:
                            removers = parent.locator(
                                "button, [role='button'], .fa-times, .fa-close, "
                                "[class*='remove'], [class*='close'], [title*='remove' i], "
                                "[aria-label*='remove' i], [aria-label*='close' i]"
                            )
                            for j in range(removers.count()):
                                remove = removers.nth(j)
                                try:
                                    if not remove.is_visible():
                                        continue
                                    box = remove.bounding_box()
                                    if not box:
                                        continue
                                    text = (remove.inner_text(timeout=75) or "").strip().lower()
                                    cls = (remove.get_attribute("class") or "").lower()
                                    aria = (remove.get_attribute("aria-label") or "").lower()
                                    title = (remove.get_attribute("title") or "").lower()
                                    looks_like_remove = (
                                        text in {"x", "×"}
                                        or "remove" in cls or "close" in cls or "times" in cls
                                        or "remove" in aria or "close" in aria
                                        or "remove" in title or "close" in title
                                    )
                                    if not looks_like_remove:
                                        continue
                                    label_mid_y = label_box["y"] + label_box["height"] / 2
                                    remove_mid_y = box["y"] + box["height"] / 2
                                    same_row = abs(remove_mid_y - label_mid_y) <= max(22, label_box["height"])
                                    right_side = box["x"] >= label_box["x"] + label_box["width"] - 20
                                    close_enough = box["x"] <= label_box["x"] + label_box["width"] + 140
                                    if same_row and right_side and close_enough:
                                        return True
                                except Exception:
                                    continue
                        node = parent
                except Exception:
                    continue
    return False

def _click_apply(context: BrowserContext) -> None:
    pattern = re.compile(r"^\s*APPLY\s*$", re.I)
    candidates: list[tuple[float, Locator]] = []
    for frame in all_frames(context):
        for locator in (frame.get_by_role("button", name=pattern), frame.get_by_text(pattern)):
            for i in range(locator.count()):
                item = locator.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    box = item.bounding_box()
                    if box:
                        candidates.append((box["y"], item))
                except Exception:
                    continue
    if not candidates:
        raise RuntimeError("Could not find the APPLY button for the billing filters.")
    # The filter APPLY control is generally the lowest visible exact APPLY button.
    candidates.sort(key=lambda value: value[0], reverse=True)
    _click(candidates[0][1])
    newest_page(context).wait_for_timeout(180)
    logging.info("Applied billing filter changes.")


def _remove_location_filters(context: BrowserContext) -> None:
    # Exactly one Office or Telehealth chip should be active at finalization.
    # Remove one matching top-row chip, then apply once. Do not loop because the
    # same location text remains visible inside the floating Locations panel.
    removed = False
    for pattern in LOCATION_PATTERNS:
        if _remove_location_filter_chip(context, pattern):
            removed = True
            break

    if not removed:
        raise RuntimeError(
            "Could not remove the Office/Telehealth location filter. "
            "The automation stopped before selecting or labeling any appointments."
        )

    _click_apply(context)

    if _location_filter_visible(context):
        raise RuntimeError(
            "The Office/Telehealth location filter is still visible in the top filter row after clicking APPLY. "
            "The automation stopped before selecting or labeling any appointments."
        )
    logging.info("Confirmed that the top-row Office/Telehealth location filter was removed.")


def _select_all_rows(context: BrowserContext) -> None:
    # Prefer a visible header checkbox above the appointment rows.
    ranked: list[tuple[float, float, Locator]] = []
    for frame in all_frames(context):
        checks = frame.locator('input[type="checkbox"]:visible')
        for i in range(checks.count()):
            item = checks.nth(i)
            try:
                box = item.bounding_box()
                if not box or box["y"] > 360:
                    continue
                ranked.append((box["y"], box["x"], item))
            except Exception:
                continue
    if not ranked:
        raise RuntimeError("Could not find the select-all checkbox.")
    ranked.sort(key=lambda value: (value[0], value[1]))
    checkbox = ranked[0][2]
    try:
        checked = checkbox.is_checked()
    except Exception:
        checked = False
    if not checked:
        _click(checkbox)
    newest_page(context).wait_for_timeout(100)
    logging.info("Selected all currently filtered billing rows.")


def _open_label_selected(context: BrowserContext) -> None:
    pattern = re.compile(r"^\s*Label selected\s*$", re.I)
    for frame in all_frames(context):
        item = visible_locator(frame.get_by_text(pattern))
        if item is not None:
            _click(item)
            newest_page(context).wait_for_timeout(100)
            logging.info("Opened Label selected.")
            return
    raise RuntimeError("Could not find Label selected after selecting all rows.")


def _find_apply_labels_input(context: BrowserContext) -> Locator | None:
    for frame in all_frames(context):
        # First use the Apply Labels heading as an anchor.
        heading = visible_locator(frame.get_by_text(re.compile(r"^\s*Apply Labels\s*$", re.I)))
        if heading is not None:
            try:
                section = heading.locator("xpath=..").locator("xpath=..")
                box = visible_locator(section.locator(
                    'input:visible, textarea:visible, [contenteditable="true"]:visible, [role="textbox"]:visible'
                ))
                if box is not None:
                    return box
            except Exception:
                pass
        # Fallback to likely placeholders inside the visible modal.
        box = visible_locator(frame.locator(
            'input[placeholder*="label" i]:visible, input[placeholder*="tag" i]:visible, '
            'input:visible, [role="textbox"]:visible'
        ))
        if box is not None:
            return box
    return None


def _apply_billed_insurance_label(context: BrowserContext, billed_person: str) -> None:
    _open_label_selected(context)
    box = _find_apply_labels_input(context)
    if box is None:
        raise RuntimeError("Could not find the Apply Labels text box.")
    try:
        box.fill("Billed Insurance")
    except Exception:
        _click(box)
        box.press("ControlOrMeta+A")
        box.type("Billed Insurance", delay=35)
    newest_page(context).wait_for_timeout(120)
    box.press("ArrowDown")
    box.press("ArrowDown")
    box.press("Enter")
    newest_page(context).wait_for_timeout(100)

    print("\nCONFIRM BEFORE LABELING")
    print(f"Billed person: {billed_person}")
    print("Only the Office/Telehealth location filter should have been removed.")
    print("The billed-person filter and all other filters should still be visible.")
    answer = input("Review the filtered appointments and label selection. Press ENTER to apply, or type N to stop: ").strip().lower()
    if answer in {"n", "no", "q", "quit", "stop"}:
        raise RuntimeError("Billed Insurance label application cancelled by user.")

    pattern = re.compile(r"^\s*Apply Label Changes\s*$", re.I)
    for frame in all_frames(context):
        button = visible_locator(frame.get_by_role("button", name=pattern))
        if button is None:
            button = visible_locator(frame.get_by_text(pattern))
        if button is not None:
            _click(button)
            newest_page(context).wait_for_timeout(200)
            logging.info("Applied Billed Insurance label to selected rows after confirmation.")
            return
    raise RuntimeError("Could not find Apply Label Changes.")


def finalize_billed_person(
    context: BrowserContext,
    billed_person: str,
    remove_billed_person_filter: bool = True,
) -> None:
    """Label the person's filtered rows and optionally clear their name filter.

    In manual filter mode, the user owns the billed-person filter. The automation
    still removes only Office/Telehealth, applies Billed Insurance, and then leaves
    the billed-person chip untouched for the user to remove or replace.
    """
    page = newest_page(context)
    try:
        _remove_location_filters(context)
        _select_all_rows(context)
        _apply_billed_insurance_label(context, billed_person)

        if not remove_billed_person_filter:
            logging.info(
                "Finished billed-person labeling and left the user-managed filter in place: %s",
                billed_person,
            )
            return

        # Automatic mode removes the billed-person filter only after label changes.
        if not _remove_filter_chip(context, billed_person):
            raise RuntimeError(f"Could not remove the billed-person filter chip for {billed_person}.")
        _click_apply(context)
        logging.info("Finished and cleared billed person: %s", billed_person)
    except Exception:
        screenshot(page, f"finalize_billed_person_error_{billed_person}", force=True)
        raise
