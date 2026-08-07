from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from playwright.sync_api import Locator, Page

from browser_utils import visible_locator, screenshot
from providers import normalize_name

def normalize_text(value:str)->str:
    import re
    return re.sub(r"\s+"," ",value or "").strip()

def first_visible(locator):
    return visible_locator(locator)

def safe_click(locator, timeout=10000):
    locator.scroll_into_view_if_needed(timeout=timeout)
    try: locator.click(timeout=timeout)
    except Exception: locator.click(force=True,timeout=timeout)


@dataclass(frozen=True)
class BillingRow:
    page_number: int
    row_number: int
    provider: str
    client: str
    date: str
    location: str
    row_text: str
    fingerprint: str


def _candidate_header_map(table: Locator) -> dict[str, int] | None:
    """Return a billing-grid header map for this table, or None if it is not the grid."""
    header_rows = table.locator("thead tr")
    best: dict[str, int] | None = None

    for row_index in range(header_rows.count()):
        headers = header_rows.nth(row_index).locator("th, td")
        mapping: dict[str, int] = {}
        for index in range(headers.count()):
            try:
                text = normalize_text(headers.nth(index).inner_text()).lower()
            except Exception:
                continue
            if not text:
                continue
            if (text == "provider" or text == "providers" or text.startswith("provider ") or text.startswith("providers ")):
                mapping.setdefault("provider", index)
            elif text == "client" or text.startswith("client "):
                mapping.setdefault("client", index)
            elif text == "date" or text.startswith("date "):
                mapping.setdefault("date", index)
            elif text == "location" or text.startswith("location "):
                mapping.setdefault("location", index)

        if {"provider", "client", "date", "location"}.issubset(mapping):
            if best is None or len(mapping) > len(best):
                best = mapping

    return best


def _visible_data_table(page: Page) -> Locator:
    """Choose the real billing appointment grid, ignoring floating filter-panel tables."""
    tables = page.locator("table:visible")
    ranked: list[tuple[int, Locator]] = []
    diagnostics: list[str] = []

    for index in range(tables.count()):
        table = tables.nth(index)
        try:
            box = table.bounding_box()
            row_count = table.locator("tbody tr").count()
            checkbox_count = table.locator('tbody input[type="checkbox"]').count()
            column_count = table.locator("thead tr").last.locator("th, td").count()
            mapping = _candidate_header_map(table)
            text = normalize_text(table.inner_text())[:240]
        except Exception:
            continue

        diagnostics.append(
            f"table[{index}] rows={row_count} checks={checkbox_count} cols={column_count} "
            f"box={box} mapped={sorted(mapping) if mapping else []} text={text!r}"
        )

        if mapping is None:
            continue
        if row_count <= 0 or checkbox_count <= 0:
            continue

        width = int((box or {}).get("width", 0))
        height = int((box or {}).get("height", 0))
        # The billing grid is wide and contains many columns and row checkboxes.
        # Floating Providers/Locations panels are narrow and have no appointment checkboxes.
        score = (checkbox_count * 100_000) + (row_count * 1_000) + (column_count * 100) + width + height
        ranked.append((score, table))

    if not ranked:
        for line in diagnostics:
            logging.warning("Billing table candidate: %s", line)
        screenshot(page, "billing_table_not_found")
        raise RuntimeError("Could not identify the CentralReach billing appointment table.")

    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _header_map(table: Locator) -> dict[str, int]:
    mapping = _candidate_header_map(table)
    if mapping is None:
        visible_headers: list[str] = []
        rows = table.locator("thead tr")
        for row_index in range(rows.count()):
            cells = rows.nth(row_index).locator("th, td")
            row_values: list[str] = []
            for index in range(cells.count()):
                try:
                    value = normalize_text(cells.nth(index).inner_text())
                except Exception:
                    value = ""
                if value:
                    row_values.append(value)
            if row_values:
                visible_headers.append(" | ".join(row_values))
        raise RuntimeError(
            "Could not map billing table columns. Visible header rows: "
            + (" || ".join(visible_headers) if visible_headers else "none")
        )
    return mapping



def _read_current_page(page: Page, page_number: int) -> list[BillingRow]:
    table = _visible_data_table(page)
    mapping = _header_map(table)
    rows = table.locator('tbody tr:has(input[type="checkbox"])')
    output: list[BillingRow] = []
    logging.info("Billing grid candidate contains %d checkbox rows before parsing.", rows.count())

    for row_index in range(rows.count()):
        row = rows.nth(row_index)
        cells = row.locator("td, th")
        if cells.count() <= max(mapping.values()):
            continue
        checkbox = row.locator('input[type="checkbox"]')
        if checkbox.count() == 0:
            continue
        try:
            provider = normalize_text(cells.nth(mapping["provider"]).inner_text())
            client = normalize_text(cells.nth(mapping["client"]).inner_text())
            date = normalize_text(cells.nth(mapping["date"]).inner_text())
            location = normalize_text(cells.nth(mapping["location"]).inner_text())
            row_text = normalize_text(row.inner_text())
        except Exception:
            continue
        # Ignore header/filter rows that CentralReach may place inside tbody.
        # A real appointment row must contain usable values for all four key fields.
        provider_key = normalize_name(provider)
        if not date or not client or not location or not provider_key:
            continue
        header_noise = (
            "SORT BY PROVDER",
            "SORT BY PROVIDER",
            "SHOW IN ACTIVE EMPLOYEES ONLY",
            "SHOW IN-ACTIVE EMPLOYEES ONLY",
            "SHOW ACTIVE EMPLOYEES ONLY",
        )
        if provider_key == "PROVIDERS" or any(noise in provider_key for noise in header_noise):
            logging.debug("Skipped non-appointment provider/filter row: %s", provider)
            continue

        # Use only stable billing-grid identity fields. CentralReach can change
        # nonessential row text between the read and selection passes, which
        # previously caused valid rows to stop matching.
        fingerprint = " | ".join([
            normalize_name(date),
            normalize_name(client),
            normalize_name(provider),
            normalize_name(location),
        ])
        output.append(
            BillingRow(
                page_number=page_number,
                row_number=row_index,
                provider=provider,
                client=client,
                date=date,
                location=location,
                row_text=row_text,
                fingerprint=fingerprint,
            )
        )
    return output


def _next_button(page: Page) -> Locator | None:
    selectors = [
        'button[aria-label*="Next" i]',
        '[role="button"][aria-label*="Next" i]',
        'a[aria-label*="Next" i]',
        'button:has-text("Next")',
        'a:has-text("Next")',
    ]
    for selector in selectors:
        button = first_visible(page.locator(selector))
        if button is None:
            continue
        try:
            if button.is_disabled():
                continue
            classes = (button.get_attribute("class") or "").lower()
            aria_disabled = (button.get_attribute("aria-disabled") or "").lower()
            if "disabled" in classes or aria_disabled == "true":
                continue
        except Exception:
            pass
        return button
    return None


def _first_page_button(page: Page) -> Locator | None:
    selectors = [
        'button[aria-label*="First" i]',
        '[role="button"][aria-label*="First" i]',
        'a[aria-label*="First" i]',
    ]
    for selector in selectors:
        item = first_visible(page.locator(selector))
        if item is not None:
            return item
    return None


def return_to_first_page(page: Page) -> None:
    first = _first_page_button(page)
    if first is not None:
        try:
            safe_click(first)
            page.wait_for_timeout(1_000)
            return
        except Exception:
            pass

    # Do not click a generic button named "1". CentralReach contains many
    # unrelated row controls with that text. If there is no explicit First
    # control, the grid is already treated as the current page.


def read_all_rows(page: Page) -> list[BillingRow]:
    return_to_first_page(page)
    all_rows: list[BillingRow] = []
    seen_page_signatures: set[str] = set()
    page_number = 1

    while True:
        rows = _read_current_page(page, page_number)
        signature = "\n".join(row.fingerprint for row in rows[:5]) + f"|count={len(rows)}"
        if signature in seen_page_signatures:
            raise RuntimeError("Pagination repeated a page. Stopping to prevent a loop.")
        seen_page_signatures.add(signature)
        all_rows.extend(rows)
        logging.info("Read page %d: %d appointment rows", page_number, len(rows))

        next_button = _next_button(page)
        if next_button is None:
            break
        safe_click(next_button)
        page.wait_for_timeout(1_100)
        page_number += 1
        if page_number > 1_000:
            raise RuntimeError("Unexpected pagination count above 1,000 pages.")

    return all_rows



def select_all_filtered_rows(page: Page, expected_total: int) -> int:
    """Select every appointment row using the billing-grid header checkbox.

    CentralReach's select-all checkbox applies to the rows on the current page,
    so this repeats the action on each page when pagination is present.
    """
    return_to_first_page(page)
    selected = 0
    seen_signatures: set[str] = set()

    while True:
        table = _visible_data_table(page)
        mapping = _header_map(table)
        candidate_rows = table.locator('tbody tr:has(input[type="checkbox"])')

        # Build the list of real appointment rows only. CentralReach can expose
        # extra checked controls in the grid, including the header Select All
        # checkbox and hidden/filter controls. Those must not count as selected
        # appointments.
        valid_rows: list[Locator] = []
        valid_checkboxes: list[Locator] = []
        for index in range(candidate_rows.count()):
            row = candidate_rows.nth(index)
            cells = row.locator("td, th")
            if cells.count() <= max(mapping.values()):
                continue
            checkbox = row.locator('input[type="checkbox"]').first
            if checkbox.count() == 0:
                continue
            try:
                provider = normalize_text(cells.nth(mapping["provider"]).inner_text())
                client = normalize_text(cells.nth(mapping["client"]).inner_text())
                date = normalize_text(cells.nth(mapping["date"]).inner_text())
                location = normalize_text(cells.nth(mapping["location"]).inner_text())
            except Exception:
                continue
            provider_key = normalize_name(provider)
            if not date or not client or not location or not provider_key:
                continue
            header_noise = (
                "SORT BY PROVDER",
                "SORT BY PROVIDER",
                "SHOW IN ACTIVE EMPLOYEES ONLY",
                "SHOW IN-ACTIVE EMPLOYEES ONLY",
                "SHOW ACTIVE EMPLOYEES ONLY",
            )
            if provider_key == "PROVIDERS" or any(noise in provider_key for noise in header_noise):
                continue
            valid_rows.append(row)
            valid_checkboxes.append(checkbox)

        page_rows = len(valid_rows)
        logging.info(
            "Select All verification found %d real appointment rows from %d checkbox-row candidates.",
            page_rows,
            candidate_rows.count(),
        )

        if page_rows:
            header_checkbox = first_visible(table.locator('thead input[type="checkbox"]'))
            if header_checkbox is not None:
                try:
                    if not header_checkbox.is_checked():
                        safe_click(header_checkbox)
                except Exception:
                    safe_click(header_checkbox)
                page.wait_for_timeout(120)

            # Verify only real appointment-row checkboxes. Fall back to an
            # individual click if CentralReach did not retain Select All.
            for checkbox in valid_checkboxes:
                try:
                    checked = checkbox.is_checked()
                except Exception:
                    checked = False
                if not checked:
                    safe_click(checkbox)
                if checkbox.is_checked():
                    selected += 1
                else:
                    raise RuntimeError("A real appointment row did not stay selected after using Select All.")

        signature_parts: list[str] = []
        for row in valid_rows[:5]:
            try:
                signature_parts.append(normalize_text(row.inner_text()))
            except Exception:
                pass
        signature = "\n".join(signature_parts) + f"|count={page_rows}"
        if signature in seen_signatures:
            raise RuntimeError("Pagination repeated while selecting all rows.")
        seen_signatures.add(signature)

        next_button = _next_button(page)
        if next_button is None:
            break
        safe_click(next_button)
        page.wait_for_timeout(250)

    if selected != expected_total:
        raise RuntimeError(
            f"Select All expected {expected_total} rows but verified {selected}."
        )
    return selected

def select_fingerprints(page: Page, target_fingerprints: set[str]) -> int:
    return_to_first_page(page)
    selected = 0
    page_number = 1
    seen_signatures: set[str] = set()

    while True:
        table = _visible_data_table(page)
        mapping = _header_map(table)
        rows = table.locator('tbody tr:has(input[type="checkbox"])')
        page_signature_parts: list[str] = []

        for row_index in range(rows.count()):
            row = rows.nth(row_index)
            cells = row.locator("td, th")
            if cells.count() <= max(mapping.values()):
                continue
            checkbox = row.locator('input[type="checkbox"]').first
            if checkbox.count() == 0:
                continue
            try:
                provider = normalize_text(cells.nth(mapping["provider"]).inner_text())
                client = normalize_text(cells.nth(mapping["client"]).inner_text())
                date = normalize_text(cells.nth(mapping["date"]).inner_text())
                location = normalize_text(cells.nth(mapping["location"]).inner_text())
                row_text = normalize_text(row.inner_text())
            except Exception:
                continue

            fingerprint = " | ".join([
                normalize_name(date),
                normalize_name(client),
                normalize_name(provider),
                normalize_name(location),
            ])
            page_signature_parts.append(fingerprint)

            if fingerprint not in target_fingerprints:
                continue
            if not checkbox.is_checked():
                safe_click(checkbox)
            if checkbox.is_checked():
                selected += 1
            else:
                raise RuntimeError(
                    f"Checkbox did not stay selected for {date}, {client}, {provider}."
                )

        page_signature = "\n".join(page_signature_parts[:5]) + f"|count={len(page_signature_parts)}"
        if page_signature in seen_signatures:
            raise RuntimeError("Pagination repeated while selecting rows.")
        seen_signatures.add(page_signature)

        next_button = _next_button(page)
        if next_button is None:
            break
        safe_click(next_button)
        page.wait_for_timeout(1_000)
        page_number += 1

    return selected
