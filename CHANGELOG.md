# Changelog

## GitHub sanitized snapshot

- Replaced production provider mapping with an example template.
- Moved organization-specific bulk-merge search values and IDs into a gitignored local config file.
- Removed real provider/billed-person names from code comments and examples.
- Added SECURITY.md and stronger private-data ignore rules.

## v1.31

- Fixed pointer validation so DOB/header rows and unused pointer inputs are ignored.
- Validation checks only actual dated service rows with pointer/modifier inputs.

## v1.30

- Added per-person `MANUAL` billed-person filter mode.
- Manual mode leaves the billed-person filter in place after Billed Insurance labeling.

## v1.20

- Added payer selection for Kaiser and ESSC.
- ESSC Office and Telehealth reject modifier 95; modifier 52 remains allowed.

## v1.9

- Added manual-window isolation so tabs and popups opened from the manual CentralReach window remain excluded from automation control.

## v1.5

- Integrated tested bulk-merge field handling for Provider, Facility, Billing, and Provider Supplier.
