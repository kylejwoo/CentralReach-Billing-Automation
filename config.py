from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILE = ROOT / "browser_profile"
DEFAULT_LOG = ROOT / "automation.log"
SCREENSHOTS = ROOT / "screenshots"

# Private, organization-specific configuration. These files are intentionally
# gitignored. Copy the matching *.example.json files and fill in local values.
PROVIDER_MAPPING = ROOT / "provider_location_mapping.json"
BULK_MERGE_CONFIG = ROOT / "bulk_merge_config.json"

CENTRALREACH_BILLING_URL = "https://members.centralreach.com/#billingmanager/billing/"
PAGE_ZOOM_PERCENT = 80
DEBUG_MODE = False
TARGET_LOCATION = "11 Office"
