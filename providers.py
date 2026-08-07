from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional


def normalize_name(value: str) -> str:
    value = re.sub(r"[.,]", "", value or "")
    return re.sub(r"\s+", " ", value).strip().upper()


def load_provider_map(path: Path) -> dict[str, str]:
    if not path.exists():
        raise RuntimeError(
            "Missing provider_location_mapping.json. Copy "
            "provider_location_mapping.example.json to provider_location_mapping.json "
            "and replace the example providers with your local mapping."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, str] = {}

    for name, site in raw.items():
        normalized_site = str(site).strip().upper()
        if normalized_site not in {"AHC", "SAC"}:
            raise ValueError(f"Invalid site for {name}: {site}")
        result[normalize_name(str(name))] = normalized_site

    return result


def _abbreviation_matches(
    displayed_name: str,
    full_names: list[str],
) -> list[str]:
    displayed = normalize_name(displayed_name)

    if displayed in full_names:
        return [displayed]

    parts = displayed.split()
    if len(parts) < 2 or len(parts[0]) != 1:
        return []

    first_initial = parts[0]
    displayed_suffix = parts[1:]
    matches: list[str] = []

    for full_name in full_names:
        normalized_full = normalize_name(full_name)
        full_parts = normalized_full.split()

        if len(full_parts) < 2:
            continue
        if not full_parts[0].startswith(first_initial):
            continue
        if len(full_parts) < len(displayed_suffix) + 1:
            continue
        if full_parts[-len(displayed_suffix):] == displayed_suffix:
            matches.append(normalized_full)

    return sorted(set(matches))


def resolve_provider_name(
    displayed_name: str,
    provider_map: dict[str, str],
    current_full_names: list[str] | None = None,
) -> Optional[str]:
    """
    Resolve an abbreviated table name using the full names currently displayed
    in CentralReach's Providers filter.

    The local provider mapping remains the source for AHC/SAC, but the open
    filter is the source for expanding abbreviations such as:
      A. EXAMPLE -> ALEX EXAMPLE
      J. SAMPLE  -> JAMIE SAMPLE
    """
    displayed = normalize_name(displayed_name)

    if displayed in provider_map:
        return displayed

    if current_full_names:
        current_matches = _abbreviation_matches(
            displayed,
            [normalize_name(name) for name in current_full_names],
        )

        if len(current_matches) == 1:
            resolved = current_matches[0]
            logging.info(
                "Resolved abbreviated provider %s from Providers filter to %s",
                displayed_name,
                resolved,
            )
            return resolved

        if len(current_matches) > 1:
            raise RuntimeError(
                f"Abbreviated provider {displayed_name} is ambiguous within "
                f"the current Providers filter. Possible matches: "
                f"{', '.join(current_matches)}"
            )

    # Defensive fallback for cases where the provider panel cannot list a
    # selected provider because of a CentralReach rendering issue.
    mapping_matches = _abbreviation_matches(
        displayed,
        list(provider_map),
    )

    if len(mapping_matches) == 1:
        resolved = mapping_matches[0]
        logging.warning(
            "Resolved abbreviated provider %s from local provider mapping fallback "
            "to %s",
            displayed_name,
            resolved,
        )
        return resolved

    if len(mapping_matches) > 1:
        raise RuntimeError(
            f"Abbreviated provider {displayed_name} is ambiguous in "
            f"providers.json. Possible matches: "
            f"{', '.join(mapping_matches)}"
        )

    return None
