from __future__ import annotations
import re
from playwright.sync_api import BrowserContext
from browser_utils import all_frames,visible_locator,newest_page,screenshot

def open_bulk_merge(context:BrowserContext)->None:
    page=newest_page(context); actions=None
    for frame in all_frames(context):
        actions=visible_locator(frame.get_by_text(re.compile(r'^\s*Actions\s*$',re.I)))
        if actions is not None: break
    if actions is None:
        screenshot(page,'actions_not_found',force=True); raise RuntimeError('Could not find Actions menu.')
    try: actions.click(timeout=5000)
    except Exception: actions.click(force=True,timeout=5000)
    page.wait_for_timeout(500)
    merge=None
    for frame in all_frames(context):
        merge=visible_locator(frame.get_by_text(re.compile(r'^\s*Bulk[-\s]?merge\s+Claims\s*$',re.I)))
        if merge is not None: break
    if merge is None:
        screenshot(page,'bulk_merge_not_found',force=True); raise RuntimeError('Could not find Bulk-merge Claims.')
    try: merge.click(timeout=5000)
    except Exception: merge.click(force=True,timeout=5000)
    page.wait_for_timeout(1500)
