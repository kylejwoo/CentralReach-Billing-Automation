# CentralReach Billing Automation

A Playwright-based browser automation project for CentralReach billing and claim setup workflows. The current build supports Kaiser and ESSC workflows, separates two configurable provider groups (`AHC` and `SAC`), handles Office and Telehealth phases independently, validates claim setup, performs bulk-merge field setup, and applies a billing-complete label after review.

This repository is intentionally **sanitized for GitHub**. Real provider names, organization search values, organization IDs, browser session data, screenshots, and billing exports are not included.

## Current version

This repository snapshot is based on **v1.31** of the automation.

## Private configuration

Two production configuration files are intentionally excluded from Git:

- `provider_location_mapping.json`
- `bulk_merge_config.json`

Example templates are included instead:

- `provider_location_mapping.example.json`
- `bulk_merge_config.example.json`

Before running locally, create your private copies:

```bash
cp provider_location_mapping.example.json provider_location_mapping.json
cp bulk_merge_config.example.json bulk_merge_config.json
```

Then replace every example value with your own local CentralReach configuration. The real files are covered by `.gitignore`, so `git add .` will not stage them.

### Provider mapping example

```json
{
  "ALEX EXAMPLE": "AHC",
  "JAMIE SAMPLE": "SAC"
}
```

### Bulk-merge configuration example

```json
{
  "AHC": {
    "organization_query": "Example Organization A",
    "organization_option_regex": "Example Organization A.*\\(ID:\\s*123456\\)"
  },
  "SAC": {
    "organization_query": "Example Organization B",
    "organization_option_regex": "Example Organization B.*\\(ID:\\s*654321\\)"
  }
}
```

The regex should match the organization option shown by CentralReach after the Facility or Billing search box is filled.

## Main workflow

For each billed person, the automation processes available groups in this order:

1. AHC Office
2. AHC Telehealth
3. SAC Office
4. SAC Telehealth
5. Apply the billing-complete label to the completed billed-person set

If one of the four location/site combinations does not exist, it is skipped and the automation continues.

## Kaiser and ESSC modes

The program asks which payer workflow to use when it starts.

### Kaiser

- Office claims must not contain modifier `95`.
- Telehealth claims require modifier `95`.
- Modifier `52` is allowed when already present.
- Modifier `MO` is flagged for removal.

### ESSC

- Office claims must not contain modifier `95`.
- Telehealth claims must not contain modifier `95`.
- Modifier `52` is allowed when already present.
- Modifier `MO` is flagged for removal.

## Bulk-merge claim setup

For each selected phase, the automation:

- Verifies the selected appointment count.
- Opens Bulk-merge Claims.
- Validates authorization numbers, diagnosis codes, pointers, and modifier rules.
- Pauses if manual corrections are needed and rechecks after you press Enter.
- Fills and syncs Provider, Facility, Billing, and Provider Supplier.
- Applies completed field values to all claims.
- Scrolls to Start claims generation but does not click the generation button automatically.

Facility and Billing organization values are loaded from the local `bulk_merge_config.json`, not hard-coded in the public repository.

## Provider/location mapping

The local `provider_location_mapping.json` maps each provider to `AHC` or `SAC`. Abbreviated provider names shown in the billing grid are resolved against full names visible in CentralReach before the mapping is applied.

If a provider is missing from the local mapping, the automation stops instead of guessing.

## Two-window mode

The automation opens two CentralReach windows inside the same browser context and login session:

- **Automation window:** reserved for Playwright.
- **Manual window:** available for your own CentralReach work.

Tabs and popups opened from the manual window stay in the manual group and are excluded from automation page searches.

Avoid editing the same claim or appointment in both windows at the same time.

## Billed-person filter modes

At the normal billed-person prompt, enter a name for automatic filter handling or type `MANUAL` to manage the billed-person filter yourself for that person.

In manual mode, the automation still runs all available AHC/SAC Office/Telehealth phases and labeling, but intentionally leaves the billed-person filter in place afterward.

## Project structure

```text
centralreach_claims.py                  Main CLI and phase orchestration
browser_utils.py                        Browser context and page isolation
navigation.py                           Login, billed-person search, return to Billing
locations.py                            Office/Telehealth selection and skipping
provider_filter.py                      Reads full provider names from CentralReach
providers.py                            Provider normalization and site mapping resolution
table_reader.py                         Billing grid detection and row selection
bulk_merge.py                           Opens Bulk-merge Claims
claims_page.py                          Claim validation and bulk-merge field setup
finalize_billing.py                     Labeling and filter cleanup
config.py                               Paths and runtime settings
provider_location_mapping.example.json Sanitized provider mapping template
bulk_merge_config.example.json          Sanitized organization-value template
requirements.txt                        Python dependencies
setup_mac.command                       macOS first-run setup
run_mac.command                         macOS launcher
```

## Requirements

- macOS or another environment capable of running Python and Playwright
- Python 3.9+
- Chromium installed through Playwright
- A valid CentralReach account with access to the required billing workflows

## macOS setup

Create your private config files first, then run:

```bash
./setup_mac.command
./run_mac.command
```

Terminal alternative:

```bash
source .venv/bin/activate
python centralreach_claims.py
```

## Files that must stay private

Do not commit or publish:

- `provider_location_mapping.json`
- `bulk_merge_config.json`
- `browser_profile/`
- `screenshots/`
- `automation.log*`
- billing exports such as `.xlsx`, `.xls`, or `.csv`
- any file containing client, provider, claim, authorization, or account information

The included `.gitignore` blocks these common production files.

## Safety behavior

The automation is designed to stop rather than guess when it encounters uncertain state. It pauses on provider mapping problems, appointment-count mismatches, missing claim fields, invalid modifiers, and unsafe final-labeling states.

The final labeling workflow includes an explicit Terminal confirmation before the label is applied.

## CentralReach UI changes

This project automates a third-party web application rather than a public API. CentralReach may change its DOM, labels, layout, or behavior at any time. Test carefully after UI changes before using the automation on a full billing batch.

## License

MIT. See [LICENSE](LICENSE).
