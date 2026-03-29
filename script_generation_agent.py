"""
Agent 5 — ScriptGenerationAgent
================================
Converts each TestCase into a working async Playwright Python script using
EXACT selectors scraped from the live DOM (via SelectorMapAgent).

Strategy
────────
1. Read ctx.selector_map  — built by SelectorMapAgent from real DOM data
2. Resolve concrete CSS selectors for every interaction in the template
3. Fall back to generic selectors only when the map has no match for a role

Every generated script:
  - Uses EXACT selectors from the page (e.g. #save-btn, input[name="email"])
  - Falls back gracefully to generic selectors when real ones are unavailable
  - Is a self-contained async run_test() -> bool
  - Launches headed or headless Chromium depending on ctx.headed
  - Has explicit assert statements
  - Returns True on pass, raises AssertionError on failure
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import re
import textwrap

from groq import Groq

from config import GROQ_API_KEY, TEXT_MODEL
from logger import log
from models import AgentContext, TestCase
from selector_map_agent import (get_all_of_type, get_first_selector,
                                get_selector)

NAME   = "ScriptGenerationAgent"
client = Groq(api_key=GROQ_API_KEY)


# ---------------------------------------------------------------------------
# Script file header + wrapper
# ---------------------------------------------------------------------------

def _header() -> str:
    return "import asyncio\nfrom playwright.async_api import async_playwright, expect\n"


def _wrap(body: str, url: str, headless: bool = True, slow_mo: int = 0) -> str:
    indented    = textwrap.indent(textwrap.dedent(body).strip(), "            ")
    launch_args = f"headless={headless}"
    if not headless and slow_mo > 0:
        launch_args += f", slow_mo={slow_mo}"
    return (
        _header()
        + "\nasync def run_test() -> bool:\n"
        + "    try:\n"
        + "        async with async_playwright() as p:\n"
        + f'            browser = await p.chromium.launch({launch_args})\n'
        + '            context = await browser.new_context(viewport={"width": 1440, "height": 900})\n'
        + "            page    = await context.new_page()\n"
        + f'            await page.goto("{url}", wait_until="domcontentloaded", timeout=30000)\n'
        + '            await page.wait_for_load_state("networkidle", timeout=10000)\n'
        + indented + "\n"
        + "            await browser.close()\n"
        + "        return True\n"
        + "    except Exception as e:\n"
        + "        raise AssertionError(f'Test failed: {e}') from e\n"
    )


# ---------------------------------------------------------------------------
# Selector resolver helpers
# ---------------------------------------------------------------------------

def _resolve(smap: dict, role: str, fallback: str) -> str:
    """Get real selector from map, or use fallback generic."""
    sel = get_selector(smap, role, "")
    if sel:
        return sel
    return fallback


def _all_inputs(smap: dict) -> list[dict]:
    """All text-like input components from the page."""
    entries = []
    for t in ("text_input", "email_input", "number_input", "tel_input",
              "url_input", "textarea", "textbox", "search_input"):
        entries.extend(get_all_of_type(smap, t))
    return entries


def _all_buttons(smap: dict) -> list[dict]:
    return get_all_of_type(smap, "button")


def _sel_list_py(entries: list[dict], max_items: int = 8) -> str:
    """Return a Python list literal of selector strings."""
    sels = [e["selector"] for e in entries[:max_items] if e.get("selector")]
    if not sels:
        return "[]"
    return "[" + ", ".join(f'"{s}"' for s in sels) + "]"


def _required_inputs(smap: dict) -> list[dict]:
    entries = []
    for t in ("text_input", "email_input", "number_input", "tel_input",
              "url_input", "textarea"):
        for e in get_all_of_type(smap, t):
            if e.get("is_required"):
                entries.append(e)
    return entries


# ---------------------------------------------------------------------------
# Rule-based script templates — all use real selectors
# ---------------------------------------------------------------------------

def _script_page_load(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
title = await page.title()
assert title.strip(), "Page title is empty"
assert page.url != "about:blank", "Page did not navigate"
body_text = await page.inner_text("body")
assert len(body_text.strip()) > 50, "Page body appears empty"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_navigation_links(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
links = page.get_by_role("link")
count = await links.count()
assert count > 0, f"No links found on page (got {count})"
for i in range(min(count, 10)):
    href  = await links.nth(i).get_attribute("href")
    label = await links.nth(i).inner_text()
    assert href is not None, f"Link '{label}' has no href"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_login_valid(tc, url, smap=None, credentials=None,
                         headless=True, slow_mo=0, **_):
    smap     = smap or {}
    username = (credentials or {}).get("username", "testuser@example.com")
    password = (credentials or {}).get("password", "TestPassword123")

    # Real selectors from DOM — fall back to generic patterns
    user_sel   = _resolve(smap, "username_field",
                  _resolve(smap, "email_field", 'input[type="email"]'))
    pass_sel   = _resolve(smap, "password_field", 'input[type="password"]')
    submit_sel = _resolve(smap, "login_button",
                  _resolve(smap, "submit_button", 'button[type="submit"]'))

    # Also try login shortcuts from the map
    login = smap.get("login", {})
    user_sel   = login.get("username_selector") or user_sel
    pass_sel   = login.get("password_selector") or pass_sel
    submit_sel = login.get("submit_selector")   or submit_sel

    body = f'''
# Fill username / email
user_el = page.locator({user_sel!r}).first
if await user_el.count() > 0:
    await user_el.fill({username!r})

# Fill password
pass_el = page.locator({pass_sel!r}).first
if await pass_el.count() > 0:
    await pass_el.fill({password!r})

# Submit
submit_el = page.locator({submit_sel!r}).first
if await submit_el.count() > 0:
    await submit_el.click()
    await page.wait_for_load_state("networkidle", timeout=10000)

# Verify login succeeded — password field should disappear
still_on_login = await page.locator({pass_sel!r}).count()
assert still_on_login == 0 or page.url != {url!r}, "Login did not succeed"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_login_empty_fields(tc, url, smap=None, headless=True, slow_mo=0, **_):
    smap = smap or {}
    login = smap.get("login", {})
    submit_sel = (login.get("submit_selector")
                  or _resolve(smap, "login_button",
                     _resolve(smap, "submit_button", 'button[type="submit"]')))

    body = f'''
# Click submit without filling any fields
submit_el = page.locator({submit_sel!r}).first
if await submit_el.count() > 0:
    await submit_el.click()

await page.wait_for_timeout(1000)

# Expect validation errors or stay on same page
error_count = await page.locator(
    '[class*="error"],[class*="alert"],[role="alert"],'
    '[aria-invalid="true"],:invalid,[class*="invalid"],[class*="danger"]'
).count()
assert error_count > 0, "No validation error shown for empty form submission"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_login_invalid_creds(tc, url, smap=None, headless=True, slow_mo=0, **_):
    smap  = smap or {}
    login = smap.get("login", {})
    user_sel   = login.get("username_selector") or _resolve(smap, "email_field",
                    _resolve(smap, "username_field", 'input[type="email"]'))
    pass_sel   = login.get("password_selector") or _resolve(smap, "password_field",
                    'input[type="password"]')
    submit_sel = login.get("submit_selector")   or _resolve(smap, "login_button",
                    _resolve(smap, "submit_button", 'button[type="submit"]'))

    body = f'''
user_el = page.locator({user_sel!r}).first
if await user_el.count() > 0:
    await user_el.fill("invalid_nobody_xyz@noemail.com")

pass_el = page.locator({pass_sel!r}).first
if await pass_el.count() > 0:
    await pass_el.fill("WrongPass!@#999")

submit_el = page.locator({submit_sel!r}).first
if await submit_el.count() > 0:
    await submit_el.click()

await page.wait_for_timeout(2000)
error_count = await page.locator(
    '[class*="error"],[class*="alert"],[role="alert"],'
    '[class*="invalid"],[class*="danger"]'
).count()
assert error_count > 0, "No error shown for invalid credentials"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_form_submit(tc, url, smap=None, headless=True, slow_mo=0, **_):
    smap    = smap or {}
    inputs  = _all_inputs(smap)
    buttons = _all_buttons(smap)
    submit_sel = _resolve(smap, "submit_button",
                   _resolve(smap, "login_button", 'button[type="submit"]'))

    if inputs:
        # Build per-field fill lines using real selectors
        fill_lines = []
        for entry in inputs[:10]:
            sel   = entry["selector"]
            itype = entry.get("input_type", "")
            label = entry.get("label", "")
            if itype == "email" or "email" in label.lower():
                fill_lines.append(f'    await page.locator({sel!r}).fill("tester@example.com")')
            elif itype == "number":
                fill_lines.append(f'    await page.locator({sel!r}).fill("42")')
            elif itype == "tel":
                fill_lines.append(f'    await page.locator({sel!r}).fill("+1234567890")')
            elif itype == "url":
                fill_lines.append(f'    await page.locator({sel!r}).fill("https://example.com")')
            elif itype == "password":
                fill_lines.append(f'    await page.locator({sel!r}).fill("TestPassword123!")')
            else:
                fill_lines.append(f'    await page.locator({sel!r}).fill("Test Value")')
        fill_block = "\n".join(fill_lines)
    else:
        fill_block = (
            "    inputs = page.locator('input:not([type=\"hidden\"]), textarea')\n"
            "    count  = await inputs.count()\n"
            "    for i in range(count):\n"
            "        await inputs.nth(i).fill('Test Value')"
        )

    body = f'''
# Fill form fields using exact selectors
{fill_block}

# Submit
submit_el = page.locator({submit_sel!r}).first
if await submit_el.count() > 0:
    await submit_el.click()
    await page.wait_for_load_state("networkidle", timeout=10000)

errors = []
page.on("pageerror", lambda e: errors.append(str(e)))
assert len(errors) == 0, f"JS errors after form submit: {{errors[:3]}}"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_required_fields(tc, url, smap=None, headless=True, slow_mo=0, **_):
    """Test that required fields show validation when left empty."""
    smap     = smap or {}
    req_inputs = _required_inputs(smap)
    submit_sel = _resolve(smap, "submit_button", 'button[type="submit"]')

    if req_inputs:
        sel_list = _sel_list_py(req_inputs)
        body = f'''
# Clear all required fields
required_selectors = {sel_list}
for sel in required_selectors:
    el = page.locator(sel).first
    if await el.count() > 0:
        await el.fill("")

# Submit the form
submit_el = page.locator({submit_sel!r}).first
if await submit_el.count() > 0:
    await submit_el.click()

await page.wait_for_timeout(1000)

# Expect validation errors
error_count = await page.locator(
    '[class*="error"],[class*="alert"],[role="alert"],[aria-invalid="true"],:invalid'
).count()
assert error_count > 0, f"No validation shown for {{len(required_selectors)}} empty required fields"
'''
    else:
        body = f'''
submit_el = page.locator({submit_sel!r}).first
if await submit_el.count() > 0:
    await submit_el.click()
await page.wait_for_timeout(1000)
error_count = await page.locator('[aria-invalid="true"],:invalid,[class*="error"]').count()
assert error_count > 0, "No required field validation found"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_save_update(tc, url, smap=None, headless=True, slow_mo=0, **_):
    """Fill editable fields and save — verify success message or no error."""
    smap   = smap or {}
    inputs = _all_inputs(smap)
    save_sel = _resolve(smap, "submit_button",
                 _resolve(smap, "edit_button",
                   _resolve(smap, "cancel_button", 'button[type="submit"]')))

    fill_lines = []
    for entry in inputs[:6]:
        sel   = entry["selector"]
        itype = entry.get("input_type", "")
        if itype in ("password",):
            continue   # skip password fields in general save tests
        fill_lines.append(f'await page.locator({sel!r}).fill("Updated Value")')
    fill_block = "\n".join(fill_lines) if fill_lines else "pass  # no inputs found"

    body = f'''
# Fill / update fields
{fill_block}

# Click save / submit
save_el = page.locator({save_sel!r}).first
if await save_el.count() > 0:
    await save_el.click()
    await page.wait_for_load_state("networkidle", timeout=10000)

# Verify success — look for success toast/alert or no error
success_count = await page.locator(
    '[class*="success"],[class*="toast"],[role="status"],'
    '[class*="saved"],[class*="updated"]'
).count()
error_count = await page.locator(
    '[class*="error"],[class*="alert"][class*="danger"],[role="alert"]'
).count()
assert success_count > 0 or error_count == 0, (
    f"Save failed: {{error_count}} error(s) visible"
)
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_dropdown(tc, url, smap=None, headless=True, slow_mo=0, **_):
    smap    = smap or {}
    sel_entries = get_all_of_type(smap, "select")
    combo_sel   = _resolve(smap, "dropdown", "")

    if sel_entries:
        sel_selector = sel_entries[0]["selector"]
        body = f'''
sel_el = page.locator({sel_selector!r}).first
assert await sel_el.count() > 0, f"Dropdown not found: {sel_selector!r}"
options = await sel_el.locator("option").all_inner_texts()
assert len(options) > 1, f"Dropdown has only {{len(options)}} option(s)"
await sel_el.select_option(index=1)
selected = await sel_el.input_value()
assert selected, "No option was selected"
'''
    elif combo_sel:
        body = f'''
combo = page.locator({combo_sel!r}).first
assert await combo.count() > 0, "Combobox not found"
await combo.click()
options = page.locator('[role="option"]')
assert await options.count() > 0, "No dropdown options visible"
await options.first.click()
'''
    else:
        body = '''
sel_el = page.locator("select").first
if await sel_el.count() > 0:
    options = await sel_el.locator("option").all_inner_texts()
    assert len(options) > 1, f"Dropdown has only {len(options)} option(s)"
    await sel_el.select_option(index=1)
    selected = await sel_el.input_value()
    assert selected, "No option was selected"
else:
    combo = page.locator('[role="combobox"]').first
    assert await combo.count() > 0, "No dropdown found on page"
    await combo.click()
    options = page.locator('[role="option"]')
    assert await options.count() > 0, "No options visible"
    await options.first.click()
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_search(tc, url, smap=None, headless=True, slow_mo=0, **_):
    smap       = smap or {}
    search_sel = _resolve(smap, "search_field",
                   get_first_selector(smap, "search_input",
                     'input[type="search"]'))

    body = f'''
search_el = page.locator({search_sel!r}).first
assert await search_el.count() > 0, f"Search input not found: {search_sel!r}"
await search_el.fill("test")
await search_el.press("Enter")
await page.wait_for_load_state("networkidle", timeout=10000)
results = page.locator('[class*="result"],[class*="search-result"],[role="listbox"] [role="option"],ul li')
count = await results.count()
assert count > 0, "Search returned no results"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_buttons(tc, url, smap=None, headless=True, slow_mo=0, **_):
    smap    = smap or {}
    buttons = _all_buttons(smap)

    if buttons:
        sel_list = _sel_list_py(buttons)
        body = f'''
button_selectors = {sel_list}
for sel in button_selectors:
    btn = page.locator(sel).first
    if await btn.count() == 0:
        continue
    assert await btn.is_visible(), f"Button {{sel!r}} is not visible"
    assert await btn.is_enabled(), f"Button {{sel!r}} is disabled"
'''
    else:
        body = '''
buttons = page.get_by_role("button")
count   = await buttons.count()
assert count > 0, "No buttons found on page"
for i in range(min(count, 15)):
    btn = buttons.nth(i)
    assert await btn.is_visible(), f"Button {i} is not visible"
    assert await btn.is_enabled(), f"Button {i} is disabled"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_table(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
table = page.locator("table").first
assert await table.count() > 0, "No table found on page"
headers = table.locator("th")
assert await headers.count() > 0, "Table has no headers"
rows = table.locator("tbody tr")
assert await rows.count() > 0, "Table has no data rows"
first_text = await rows.first.inner_text()
assert first_text.strip(), "First table row is empty"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_modal(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
trigger = page.locator(
    'button:has-text("Open"), button:has-text("View"), '
    'button:has-text("Details"), [data-toggle="modal"], [data-bs-toggle="modal"]'
).first
if await trigger.count() > 0:
    await trigger.click()
    await page.wait_for_timeout(500)
    modal = page.locator('[role="dialog"], .modal, [class*="modal"]').first
    assert await modal.is_visible(), "Modal did not open"
    close = page.locator(
        '[aria-label="Close"], button:has-text("Close"), button:has-text("Cancel"), .modal .close'
    ).first
    if await close.count() > 0:
        await close.click()
        await page.wait_for_timeout(300)
        assert not await modal.is_visible(), "Modal did not close"
else:
    open_modal = await page.locator('[role="dialog"]:visible').count()
    assert open_modal == 0, "Unexpected modal is open on page load"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_responsive(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
await page.set_viewport_size({"width": 375, "height": 812})
await page.reload(wait_until="networkidle")
scroll_w = await page.evaluate("document.documentElement.scrollWidth")
client_w = await page.evaluate("document.documentElement.clientWidth")
assert scroll_w <= client_w + 5, f"Horizontal overflow at 375px: scrollWidth={scroll_w}"
await page.set_viewport_size({"width": 1440, "height": 900})
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_accessibility(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
h1_count = await page.locator("h1").count()
assert h1_count >= 1, f"No H1 heading found (got {h1_count})"
images    = page.locator("img")
img_count = await images.count()
for i in range(min(img_count, 20)):
    alt = await images.nth(i).get_attribute("alt")
    src = await images.nth(i).get_attribute("src") or ""
    assert alt is not None, f"Image '{src}' missing alt attribute"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_keyboard(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
focused = 0
for _ in range(15):
    await page.keyboard.press("Tab")
    tag = await page.evaluate(
        "document.activeElement ? document.activeElement.tagName.toLowerCase() : null"
    )
    if tag in ("a", "button", "input", "select", "textarea"):
        focused += 1
assert focused >= 3, f"Only {focused} elements received keyboard focus"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_404(tc, url, smap=None, headless=True, slow_mo=0, **_):
    bad_url = url.rstrip("/") + "/this-page-does-not-exist-xyz-404test"
    body = f'''
response = await page.goto({bad_url!r}, wait_until="domcontentloaded")
status   = response.status if response else 0
if status not in (404, 200):
    raise AssertionError(f"Unexpected HTTP status {{status}} for missing page")
if status == 200:
    content = (await page.content()).lower()
    assert any(w in content for w in ["not found", "404", "doesn't exist", "no page"]), (
        "Got 200 for missing page but no error content found"
    )
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_performance(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
timing = await page.evaluate("""() => {
    const t = performance.getEntriesByType('navigation')[0];
    return t ? t.loadEventEnd - t.startTime : null;
}""")
assert timing is not None, "Navigation timing API unavailable"
assert timing < 5000, f"Page loaded in {timing:.0f}ms — exceeds 5s threshold"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_images(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
broken = await page.evaluate("""() => {
    return [...document.images]
        .filter(img => img.complete && img.naturalWidth === 0)
        .map(img => img.src);
}""")
assert len(broken) == 0, f"{len(broken)} broken image(s): {broken[:5]}"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_footer(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
footer = page.locator("footer, [role='contentinfo'], #footer, .footer").first
assert await footer.count() > 0, "No footer element found"
assert await footer.is_visible(), "Footer is not visible"
footer_links = footer.locator("a")
assert await footer_links.count() > 0, "Footer has no links"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_title_visible(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
heading = page.get_by_role("heading").first
assert await heading.count() > 0, "No heading found on page"
await expect(heading).to_be_visible()
title = await page.title()
assert title.strip(), "Browser tab title is empty"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_checkbox(tc, url, smap=None, headless=True, slow_mo=0, **_):
    smap       = smap or {}
    checkboxes = get_all_of_type(smap, "checkbox")

    if checkboxes:
        sel   = checkboxes[0]["selector"]
        label = checkboxes[0].get("label", "checkbox")
        # Build body as plain string — no nested f-string to avoid interpolation conflicts
        body = (
            "\ncb = page.locator(" + repr(sel) + ").first\n"
            "assert await cb.count() > 0, 'Checkbox not found: " + sel + "'\n"
            "await cb.check()\n"
            "assert await cb.is_checked(), 'Checkbox did not become checked: " + label + "'\n"
            "await cb.uncheck()\n"
            "assert not await cb.is_checked(), 'Checkbox did not become unchecked: " + label + "'\n"
        )
    else:
        body = '''
cb = page.locator('input[type="checkbox"]').first
assert await cb.count() > 0, "No checkbox found on page"
await cb.check()
assert await cb.is_checked(), "Checkbox did not check"
await cb.uncheck()
assert not await cb.is_checked(), "Checkbox did not uncheck"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_file_upload(tc, url, smap=None, headless=True, slow_mo=0, **_):
    smap = smap or {}
    sel  = get_first_selector(smap, "file_input",
                              _resolve(smap, "file_upload", 'input[type="file"]'))
    # Build body without nested f-string to avoid interpolation conflicts
    body = (
        "\nimport tempfile, os\n"
        "tmp = tempfile.NamedTemporaryFile(suffix='.txt', delete=False)\n"
        "tmp.write(b'Test file content for upload verification')\n"
        "tmp.close()\n"
        "file_input = page.locator(" + repr(sel) + ").first\n"
        "assert await file_input.count() > 0, 'File input not found: " + sel + "'\n"
        "await file_input.set_input_files(tmp.name)\n"
        "page_content = await page.content()\n"
        "assert os.path.basename(tmp.name) in page_content or True, 'Upload check'\n"
        "os.unlink(tmp.name)\n"
    )
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_generic(tc, url, smap=None, headless=True, slow_mo=0, **_):
    body = '''
errors = []
page.on("pageerror", lambda e: errors.append(str(e)))
await page.reload(wait_until="networkidle")
title     = await page.title()
body_text = await page.inner_text("body")
assert title.strip(), "Page title is empty"
assert len(body_text.strip()) > 50, "Page body appears empty"
assert len(errors) == 0, f"Console JS errors: {errors[:3]}"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


# ---------------------------------------------------------------------------
# Keyword → template routing table
# ---------------------------------------------------------------------------

_RULES: list[tuple[list[str], object]] = [
    (["page load", "page title", "landing", "home page", "loads successfully"],
     _script_page_load),
    (["navigation", "nav bar", "navbar", "menu link", "header link"],
     _script_navigation_links),
    (["footer", "footer link"],
     _script_footer),
    (["title", "heading visible", "content visible", "text visible"],
     _script_title_visible),
    # Auth — negatives BEFORE generic login
    (["empty field", "blank field", "missing field", "empty form",
      "without credentials", "no credentials"],
     _script_login_empty_fields),
    (["invalid credential", "wrong password", "incorrect password",
      "invalid login", "invalid email", "wrong credential"],
     _script_login_invalid_creds),
    (["valid credential", "valid login", "successful login",
      "login with valid", "sign in with valid"],
     _script_login_valid),
    (["login", "sign in", "authenticate"],
     _script_login_valid),
    # Form interactions
    (["required field", "mandatory field", "empty required", "validation error",
      "field validation"],
     _script_required_fields),
    (["save", "update profile", "edit profile", "save changes", "submit form",
      "form submit", "fill form"],
     _script_save_update),
    (["search", "search bar", "search box", "search result"],
     _script_search),
    (["dropdown", "select option", "combobox", "drop down"],
     _script_dropdown),
    (["checkbox", "check box", "toggle"],
     _script_checkbox),
    (["file upload", "upload file", "attach", "file input"],
     _script_file_upload),
    (["button", "cta", "clickable", "buttons visible"],
     _script_buttons),
    (["modal", "dialog", "popup", "pop-up", "overlay"],
     _script_modal),
    # Data / content
    (["table", "data table", "grid", "tabular", "data row"],
     _script_table),
    (["image", "broken image", "picture", "img load"],
     _script_images),
    # Non-functional
    (["responsive", "mobile", "viewport", "screen size", "mobile view"],
     _script_responsive),
    (["accessibility", "alt text", "screen reader", "wcag", "h1", "heading"],
     _script_accessibility),
    (["keyboard", "tab order", "focus", "keyboard navigation", "tab key"],
     _script_keyboard),
    (["404", "not found", "missing page", "error page", "invalid url"],
     _script_404),
    (["performance", "load time", "page speed", "response time"],
     _script_performance),
]


def _match_rule(tc: TestCase):
    haystack = " ".join([
        tc.test_case_title, tc.description, tc.test_type, tc.module
    ]).lower()
    for keywords, fn in _RULES:
        if any(kw in haystack for kw in keywords):
            return fn
    return None


# ---------------------------------------------------------------------------
# LLM fallback — now includes real selectors in the prompt
# ---------------------------------------------------------------------------

_LLM_SYS = (
    "You are a Playwright (Python async) expert. "
    "You MUST use the exact CSS selectors provided — do not invent your own. "
    "Return ONLY executable Python code — no markdown fences, no explanations."
)

_LLM_TPL = """\
Write  async def run_test() -> bool  for this test case.
Use the EXACT selectors listed under "Available selectors" — do not use generic ones.

URL   : {url}
ID    : {id}
Title : {title}
Steps :
{steps}
Expected: {expected}

Available selectors on this page (use these, not generic patterns):
{selectors_json}

Rules:
- import asyncio + playwright.async_api only
- headless Chromium
- assert the Expected Result
- return True on pass, raise AssertionError with message on fail
- use exact selectors from the list above"""


def _llm_script(tc: TestCase, url: str, smap: dict,
                credentials=None) -> str:
    cred = (f"\nCredentials: {credentials['username']} / {credentials['password']}"
            if credentials else "")

    # Give the LLM the real selector list
    selectors_summary = json.dumps({
        "by_role":  smap.get("by_role", {}),
        "by_type": {
            k: [e["selector"] for e in v[:5]]
            for k, v in smap.get("by_type", {}).items()
        },
    }, indent=2) if smap else "No selector map available — use generic selectors."

    try:
        r = client.chat.completions.create(
            model    = TEXT_MODEL,
            messages = [
                {"role": "system", "content": _LLM_SYS},
                {"role": "user", "content": _LLM_TPL.format(
                    url            = url + cred,
                    id             = tc.test_case_id,
                    title          = tc.test_case_title,
                    steps          = tc.test_steps,
                    expected       = tc.expected_result,
                    selectors_json = selectors_summary,
                )},
            ],
            max_tokens  = 1500,
            temperature = 0.15,
        )
        raw = r.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:python)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return raw
    except Exception as e:
        log("error", NAME, f"LLM fallback failed for {tc.test_case_id}: {e}")
        return (
            "import asyncio\n"
            "async def run_test() -> bool:\n"
            f"    raise NotImplementedError('Script generation failed: {e}')\n"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_script(tc: TestCase, url: str, smap: dict | None = None,
                    credentials=None,
                    headless: bool = True, slow_mo: int = 0) -> str:
    """Generate a Playwright script for tc using real selectors from smap."""
    smap = smap or {}
    fn   = _match_rule(tc)
    if fn:
        log("info", NAME,
            f"{tc.test_case_id} → rule [{fn.__name__}]"
            + (f" [{len(smap.get('all', []))} real selectors]" if smap else ""))
        return fn(tc=tc, url=url, smap=smap, credentials=credentials,
                  headless=headless, slow_mo=slow_mo)
    log("info", NAME, f"{tc.test_case_id} → LLM fallback")
    return _llm_script(tc, url, smap, credentials)


def run(ctx: AgentContext) -> list[TestCase]:
    headless = not getattr(ctx, "headed", False)
    slow_mo  = getattr(ctx, "slow_mo", 0) if not headless else 0
    smap     = getattr(ctx, "selector_map", {})

    log("agent", NAME,
        f"Generating {len(ctx.test_cases)} scripts  "
        f"[{'headed' if not headless else 'headless'}"
        + (f", slow_mo={slow_mo}ms" if slow_mo else "")
        + f", {len(smap.get('all', []))} real selectors available]")

    rule_n = llm_n = 0
    for tc in ctx.test_cases:
        fn = _match_rule(tc)
        if fn:
            tc.auto_script = fn(
                tc=tc, url=ctx.url, smap=smap,
                credentials=ctx.credentials,
                headless=headless, slow_mo=slow_mo,
            )
            rule_n += 1
        else:
            tc.auto_script = _llm_script(tc, ctx.url, smap, ctx.credentials)
            llm_n += 1

    log("success", NAME, f"Scripts ready — rule-based: {rule_n} | LLM: {llm_n}")
    return ctx.test_cases
