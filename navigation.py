from __future__ import annotations
import logging,re
from typing import Optional
from playwright.sync_api import BrowserContext, Frame, Locator, Page
from config import CENTRALREACH_BILLING_URL
from browser_utils import all_frames, visible_locator, find_visible_in_frames, newest_page, screenshot

SEARCH_SELECTORS = [
    'input[placeholder*="Search billing entries" i]',
    'input[placeholder*="Search billing" i]',
    'input[placeholder*="billing entries" i]',
    'input[placeholder*="Please enter" i]',
    'input[aria-label*="Search billing" i]',
    'input[aria-label*="billing entries" i]',
    'input[type="search"]',
]

def find_search_box(context: BrowserContext) -> tuple[Optional[Locator], Optional[Frame]]:
    return find_visible_in_frames(context, SEARCH_SELECTORS)

def print_visible_inputs(context: BrowserContext) -> None:
    print("\nVisible inputs found:")
    for page_number,page in enumerate(context.pages):
        if page.is_closed(): continue
        print(f"\nPage {page_number}: {page.url}")
        for frame_number,frame in enumerate(page.frames):
            inputs=frame.locator("input:visible")
            for input_number in range(min(inputs.count(),30)):
                current=inputs.nth(input_number)
                try:
                    print(f"  Frame {frame_number}, input {input_number}:", {
                        "placeholder":current.get_attribute("placeholder"),
                        "type":current.get_attribute("type"),
                        "name":current.get_attribute("name"),
                        "aria-label":current.get_attribute("aria-label"),
                        "id":current.get_attribute("id"),
                    })
                except Exception: continue

def wait_for_user_login(context: BrowserContext) -> Page:
    page=newest_page(context)
    if not page.url or page.url=='about:blank':
        page.goto(CENTRALREACH_BILLING_URL, wait_until='domcontentloaded')
    input("\nLog in, complete MFA, set the Billing date range and filters, then press ENTER: ")
    box,_=find_search_box(context)
    if box is None:
        print_visible_inputs(context)
        screenshot(page,'billing_search_not_found',force=True)
        raise RuntimeError('Billing search input was not visible after setup.')
    return newest_page(context)

def _find_frame_with_search(context: BrowserContext) -> Optional[Frame]:
    for frame in all_frames(context):
        try:
            found=frame.evaluate("""(selectors)=>selectors.some((selector)=>{
                const elements=Array.from(document.querySelectorAll(selector));
                return elements.some((element)=>{
                    const style=getComputedStyle(element); const rect=element.getBoundingClientRect();
                    return style.display!=='none' && style.visibility!=='hidden' && rect.width>0 && rect.height>0;
                });
            })""", SEARCH_SELECTORS)
            if found: return frame
        except Exception: continue
    return None

def type_into_search_box(context: BrowserContext, value: str) -> None:
    page=newest_page(context)
    frame=_find_frame_with_search(context)
    if frame is None: raise RuntimeError('The CentralReach billing search input could not be found.')
    result=frame.evaluate("""({selectors,value})=>{
        function visible(element){const style=getComputedStyle(element);const rect=element.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&rect.width>0&&rect.height>0;}
        let input=null;
        for(const selector of selectors){const candidates=Array.from(document.querySelectorAll(selector));input=candidates.find(visible)||null;if(input)break;}
        if(!input)return {ok:false,reason:'not-found'};
        input.scrollIntoView({block:'center',inline:'nearest'});input.focus();
        const current=String(input.value||'').trim();
        if(current.toUpperCase()===value.trim().toUpperCase())return {ok:true,value:input.value,alreadyPresent:true};
        const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
        setter.call(input,'');input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'deleteContentBackward',data:null}));
        setter.call(input,value);input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:value}));
        input.dispatchEvent(new Event('change',{bubbles:true}));
        input.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true,key:value.slice(-1)||''}));
        return {ok:true,value:input.value,alreadyPresent:false};
    }""", {'selectors':SEARCH_SELECTORS,'value':value})
    logging.info('Atomic search input result: %r',result)
    if not result or not result.get('ok'): raise RuntimeError('The billing search input could not be updated.')
    page.wait_for_timeout(2200)

def find_exact_contact_label(context: BrowserContext, billed_person: str) -> list[Locator]:
    pattern=re.compile(rf"^\s*Contact\s*Label:\s*{re.escape(billed_person)}\s*$",re.I)
    found=[]; seen=set()
    for frame in all_frames(context):
        candidates=frame.get_by_text(pattern)
        for i in range(candidates.count()):
            c=candidates.nth(i)
            try:
                if not c.is_visible(): continue
                b=c.bounding_box(); t=' '.join(c.inner_text().split()).upper(); key=(t,round(b['x']) if b else -1,round(b['y']) if b else -1)
                if key not in seen: seen.add(key); found.append(c)
            except Exception: continue
    return found

def search_contact_label(context: BrowserContext,billed_person:str)->Page:
    page=newest_page(context)
    box,_=find_search_box(context)
    if box is None:
        print_visible_inputs(context); screenshot(page,f'search_box_not_found_{billed_person}',force=True)
        raise RuntimeError('Could not locate the billing search input.')

    type_into_search_box(context,billed_person)
    matches=find_exact_contact_label(context,billed_person)

    if not matches:
        screenshot(page,f'contact_label_problem_{billed_person}',force=True)
        raise RuntimeError(f'No exact Contact Label result was found for {billed_person}.')

    # CentralReach can show the same exact Contact Label twice: once as the
    # live search result and once under SEARCH HISTORY. The live result is the
    # upper row. Sort by its on-screen Y position and choose the topmost exact
    # Contact Label. This still excludes the Provider result because the text
    # pattern only matches rows beginning with Contact Label.
    ranked=[]
    for match in matches:
        try:
            box=match.bounding_box()
            y=float(box['y']) if box else float('inf')
            ranked.append((y,match))
        except Exception:
            continue

    if not ranked:
        screenshot(page,f'contact_label_unrankable_{billed_person}',force=True)
        raise RuntimeError(f'Contact Label results were found for {billed_person}, but none could be ranked.')

    ranked.sort(key=lambda item:item[0])
    selected=ranked[0][1]
    logging.info(
        "Found %d exact Contact Label rows for %s; selecting the top live result.",
        len(matches),
        billed_person,
    )

    selected.scroll_into_view_if_needed()
    try: selected.click(timeout=10000)
    except Exception: selected.click(force=True,timeout=10000)
    page.wait_for_timeout(2200)
    logging.info('Selected exact Contact Label: %s',billed_person)
    return newest_page(context)


def return_from_claims_to_billing(context: BrowserContext) -> Page:
    """Leave Separated Claims View through the in-page '< Billing' breadcrumb."""
    page = newest_page(context)
    target = None

    for frame in all_frames(context):
        candidates = frame.get_by_text(re.compile(r"^\s*Billing\s*$", re.I))
        ranked = []
        for index in range(candidates.count()):
            item = candidates.nth(index)
            try:
                if not item.is_visible():
                    continue
                box = item.bounding_box()
                if not box:
                    continue
                # The claims breadcrumb is near the upper-left under the main nav.
                if box["y"] < 130 and box["x"] < 180:
                    ranked.append((box["y"], box["x"], item))
            except Exception:
                continue
        if ranked:
            ranked.sort(key=lambda value: (value[0], value[1]))
            target = ranked[0][2]
            break

    if target is None:
        screenshot(page, "claims_billing_back_not_found", force=True)
        raise RuntimeError("Could not find the '< Billing' link in Separated Claims View.")

    try:
        target.click(timeout=4_000)
    except Exception:
        target.click(force=True, timeout=4_000)

    # Wait for the billing-entry table/search page to return.
    for _ in range(30):
        box, _ = find_search_box(context)
        if box is not None:
            newest_page(context).wait_for_timeout(250)
            return newest_page(context)
        newest_page(context).wait_for_timeout(150)

    screenshot(newest_page(context), "billing_page_did_not_return", force=True)
    raise RuntimeError("Clicked Billing, but the billing-entry page did not return.")
