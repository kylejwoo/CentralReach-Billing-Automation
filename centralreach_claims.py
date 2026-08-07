#!/usr/bin/env python3
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from playwright.sync_api import sync_playwright

from config import (
    CENTRALREACH_BILLING_URL,
    DEFAULT_LOG,
    PROVIDER_MAPPING,
    TARGET_LOCATION,
)
from browser_utils import (
    launch_context,
    newest_page,
    register_automation_page,
    register_manual_page,
    register_manual_descendants,
    screenshot,
)
from navigation import (
    return_from_claims_to_billing,
    search_contact_label,
    wait_for_user_login,
)
from locations import choose_location
from providers import load_provider_map, normalize_name, resolve_provider_name
from provider_filter import read_full_provider_names
from table_reader import read_all_rows, select_all_filtered_rows, select_fingerprints
from bulk_merge import open_bulk_merge
from finalize_billing import finalize_billed_person
from claims_page import (
    collapse_all_if_available,
    fill_and_sync_claim_fields,
    pause_until_valid,
    scroll_to_start_generation,
    wait_for_claims_page,
)

TELEHEALTH_LOCATION = "02 Telehealth Provided Other than in Patient's Home"


@dataclass(frozen=True)
class Phase:
    site: str
    location_name: str
    location_mode: str

    @property
    def description(self) -> str:
        return f"{self.site} {self.location_mode.title()}"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(DEFAULT_LOG, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _resolve_rows(
    rows,
    provider_map: dict[str, str],
    full_provider_names: list[str],
):
    provider_resolution: dict[str, str] = {}
    unknown: set[str] = set()

    for row in rows:
        displayed_key = normalize_name(row.provider)
        if displayed_key in provider_resolution:
            continue
        resolved = resolve_provider_name(
            row.provider,
            provider_map,
            full_provider_names,
        )
        if resolved is None:
            unknown.add(row.provider)
        else:
            provider_resolution[displayed_key] = resolved

    if unknown:
        raise RuntimeError(
            "Providers missing from mapping: " + ", ".join(sorted(unknown))
        )

    def site_for(row) -> str:
        full_name = provider_resolution[normalize_name(row.provider)]
        return provider_map[full_name]

    return provider_resolution, site_for


def _clear_visible_checked_rows(page) -> None:
    """Clear checked appointment rows before selecting the next site/location."""
    for _ in range(4):
        checked = page.locator('tbody input[type="checkbox"]:checked')
        count = checked.count()
        if count == 0:
            return
        for index in range(count - 1, -1, -1):
            item = checked.nth(index)
            try:
                item.click(timeout=1_500)
            except Exception:
                try:
                    item.click(force=True, timeout=1_500)
                except Exception:
                    pass
        page.wait_for_timeout(100)


def _run_phase(
    context,
    billed_person: str,
    provider_map: dict[str, str],
    full_provider_names: list[str],
    phase: Phase,
    first_phase: bool,
    payer_mode: str,
) -> bool:
    if not first_phase:
        return_from_claims_to_billing(context)

    page = choose_location(context, phase.location_name)
    if page is None:
        logging.info("Skipping %s because that location is not available in the current filtered results.", phase.description)
        print(f"\nSkipping {phase.description}: the location option does not exist for this billed person.")
        return False

    rows = read_all_rows(page)
    if not rows:
        logging.info("No rows found for %s.", phase.description)
        print(f"\nSkipping {phase.description}: no appointments found.")
        return False

    provider_resolution, site_for = _resolve_rows(
        rows,
        provider_map,
        full_provider_names,
    )
    target_rows = [row for row in rows if site_for(row) == phase.site]

    print("\n" + "=" * 72)
    print(f"Billed person: {billed_person}")
    print(f"Setup: {phase.description}")
    print(f"Appointments at this location: {len(rows)}")
    print(f"Appointments selected for {phase.site}: {len(target_rows)}")
    print(f"{phase.site} providers:")
    for displayed_provider in sorted({row.provider for row in target_rows}):
        count = sum(1 for row in target_rows if row.provider == displayed_provider)
        full_name = provider_resolution[normalize_name(displayed_provider)]
        if normalize_name(displayed_provider) == full_name:
            print(f"  {displayed_provider}: {count}")
        else:
            print(f"  {displayed_provider} -> {full_name.title()}: {count}")
    print("=" * 72)

    if not target_rows:
        print(f"Skipping {phase.description}: no matching providers.")
        return False

    _clear_visible_checked_rows(page)
    if len(target_rows) == len(rows):
        logging.info(
            "All %d filtered appointments belong to %s; using the billing-grid Select All checkbox.",
            len(rows),
            phase.site,
        )
        print(f"All {len(rows)} of {len(rows)} appointments are {phase.site}; using Select All.")
        selected = select_all_filtered_rows(page, expected_total=len(rows))
    else:
        selected = select_fingerprints(
            page,
            {row.fingerprint for row in target_rows},
        )
    if selected != len(target_rows):
        screenshot(page, "selection_count_mismatch", force=True)
        raise RuntimeError(
            f"Expected {len(target_rows)} selected rows for {phase.description}, "
            f"verified {selected}."
        )

    print(f"Verified selected {phase.description} rows: {selected}")
    open_bulk_merge(context)
    wait_for_claims_page(context)
    collapse_all_if_available(context)
    pause_until_valid(
        context,
        location_mode=phase.location_mode,
        payer_mode=payer_mode,
    )
    fill_and_sync_claim_fields(context, billed_person, site=phase.site)
    scroll_to_start_generation(context)
    return True


def process_name(
    context,
    billed_person: str,
    provider_map: dict[str, str],
    payer_mode: str,
    manage_billed_person_filter: bool,
) -> None:
    if manage_billed_person_filter:
        search_contact_label(context, billed_person)
    else:
        logging.info(
            "Manual billed-person filter mode: using the filter already applied by the user for %s.",
            billed_person,
        )
        print(
            f"\nUsing your existing billed-person filter for {billed_person}. "
            "The automation will not add or remove that name filter."
        )

    # Keep this panel open during appointment verification instead of closing it.
    # Do not force Office here. Each phase checks its own location and skips it
    # cleanly when that option is not available.
    full_provider_names = read_full_provider_names(context)

    phases = [
        Phase("AHC", TARGET_LOCATION, "office"),
        Phase("AHC", TELEHEALTH_LOCATION, "telehealth"),
        Phase("SAC", TARGET_LOCATION, "office"),
        Phase("SAC", TELEHEALTH_LOCATION, "telehealth"),
    ]

    completed_any = False
    currently_on_claims_page = False

    for phase in phases:
        # If the prior phase was skipped, we are already on the billing page.
        # If it completed, ENTER from the review prompt advances here and the
        # automation exits Separated Claims View before changing location.
        completed = _run_phase(
            context,
            billed_person,
            provider_map,
            full_provider_names,
            phase,
            first_phase=not currently_on_claims_page,
            payer_mode=payer_mode,
        )
        if completed:
            completed_any = True
            currently_on_claims_page = True
        else:
            currently_on_claims_page = False

    if currently_on_claims_page:
        return_from_claims_to_billing(context)

    if not completed_any:
        print(f"\nNo billable AHC or SAC Office/Telehealth appointments were found for {billed_person}.")
    else:
        print(f"\nFinished all available AHC and SAC Office/Telehealth setups for {billed_person}.")
        print("Finalizing filters and applying the Billed Insurance label...")
        finalize_billed_person(
            context,
            billed_person,
            remove_billed_person_filter=manage_billed_person_filter,
        )
        if manage_billed_person_filter:
            print(f"Finished labeling and clearing {billed_person} from the billing filters.")
        else:
            print(
                f"Finished labeling {billed_person}. The billed-person filter was left in place "
                "for you to remove or replace manually."
            )


def main() -> int:
    setup_logging()
    provider_map = load_provider_map(PROVIDER_MAPPING)

    print("\nCentralReach Claims Automation, v10-based v1.31")

    while True:
        print("\nChoose appointment type:")
        print("  1. Kaiser")
        print("  2. ESSC")
        payer_choice = input("Selection [1/2]: ").strip()
        if payer_choice == "1":
            payer_mode = "kaiser"
            break
        if payer_choice == "2":
            payer_mode = "essc"
            break
        print("Please enter 1 for Kaiser or 2 for ESSC.")

    print(f"Selected appointment type: {payer_mode.upper()}")


    with sync_playwright() as playwright:
        context = launch_context(playwright)
        page = newest_page(context)
        register_automation_page(page)
        # Any tab or popup opened from the manual window remains manual and is
        # permanently excluded from automation page/frame searches.
        register_manual_descendants(context)

        if not page.url or page.url == "about:blank":
            page.goto(CENTRALREACH_BILLING_URL, wait_until="domcontentloaded")

        try:
            wait_for_user_login(context)

            # Open a second real browser window in the same browser context.
            # It shares the exact CentralReach session, but all automation helpers
            # remain locked to the original automation page.
            try:
                with context.expect_page(timeout=8_000) as manual_info:
                    page.evaluate(
                        "url => window.open(url, 'centralreach_manual', "
                        "'popup=yes,width=1100,height=850,left=820,top=40')",
                        CENTRALREACH_BILLING_URL,
                    )
                manual = manual_info.value
                register_manual_page(manual)
                manual.wait_for_load_state("domcontentloaded", timeout=15_000)
                print(
                    "\nTwo CentralReach windows are open in the same login session."
                    "\nUse the original window for automation and the second window for manual edits."
                    "\nLinks opened from the manual window may create more tabs or popups; they stay manual."
                    "\nDo not close the automation window while the script is running."
                )
            except Exception as exc:
                logging.warning("Could not open the manual CentralReach window: %s", exc)
                print("\nThe automation window is ready, but the second manual window could not be opened.")

            while True:
                entry = input(
                    "\nEnter billed person name, type MANUAL for a user-managed name filter, "
                    "or Q to close: "
                ).strip()
                if entry.lower() == "q":
                    break
                if not entry:
                    continue

                manage_billed_person_filter = True
                name = entry

                if entry.lower() == "manual":
                    manage_billed_person_filter = False
                    print(
                        "\nMANUAL BILLED-PERSON FILTER MODE"
                        "\n1. Add the billed-person filter yourself in the automation window."
                        "\n2. Click APPLY and confirm only that billed person is filtered."
                        "\n3. Enter the billed person name below for claim bulk merge."
                        "\nThe automation will apply Billed Insurance but will leave the name filter in place."
                    )
                    name = input(
                        "Enter the billed person name after you have applied the filter, "
                        "or Q to cancel manual mode: "
                    ).strip()
                    if name.lower() == "q":
                        print("Manual mode cancelled. Returning to the billed-person prompt.")
                        continue
                    if not name:
                        print("No billed person name was entered. Returning to the main prompt.")
                        continue

                try:
                    process_name(
                        context,
                        name,
                        provider_map,
                        payer_mode,
                        manage_billed_person_filter,
                    )
                except Exception as exc:
                    logging.exception("Automation stopped: %s", exc)
                    screenshot(newest_page(context), "automation_error", force=True)
                    print(
                        "\nAutomation stopped safely."
                        f"\nError: {exc}"
                        "\nAn error screenshot was saved in the screenshots folder."
                    )
        finally:
            context.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
