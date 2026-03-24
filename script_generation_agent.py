"""
Agent 5 — ScriptGenerationAgent
Responsibility: Convert each TestCase into a working async Playwright Python script.

Strategy (two-tier):
  1. Rule-based engine  — pattern-matches test_type / title keywords and emits
                          concrete, parameterised Playwright scripts instantly.
  2. LLM fallback       — for cases the rule engine cannot fully cover, Groq
                          fills the gap with a targeted prompt.

Every generated script:
  - is a self-contained async function  run_test() -> bool
  - launches headless Chromium
  - has explicit assert statements tied to the expected result
  - returns True on pass, raises Exception on failure
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import re
import textwrap

from groq import Groq

from config import GROQ_API_KEY, TEXT_MODEL
from logger import log
from models import AgentContext, TestCase

NAME   = "ScriptGenerationAgent"
client = Groq(api_key=GROQ_API_KEY)


# ---------------------------------------------------------------------------
# Shared script helpers
# ---------------------------------------------------------------------------

def _header() -> str:
    return "import asyncio\nfrom playwright.async_api import async_playwright, expect\n"


def _wrap(body: str, url: str, headless: bool = True, slow_mo: int = 0) -> str:
    """Wrap body into run_test() with headless/headed Chromium boilerplate.

    headless=False  → browser window is visible during execution
    slow_mo         → ms delay between Playwright actions (useful in headed mode)
    """
    indented = textwrap.indent(textwrap.dedent(body).strip(), "            ")
    launch_args = f"headless={headless}"
    if not headless and slow_mo > 0:
        launch_args += f", slow_mo={slow_mo}"
    return (
        _header()
        + "\nasync def run_test() -> bool:\n"
        + "    try:\n"
        + f'        async with async_playwright() as p:\n'
        + f'            browser = await p.chromium.launch({launch_args})\n'
        + f'            context = await browser.new_context(viewport={{"width": 1440, "height": 900}})\n'
        + f'            page    = await context.new_page()\n'
        + f'            await page.goto("{url}", wait_until="networkidle", timeout=30000)\n'
        + indented + "\n"
        + "            await browser.close()\n"
        + "        return True\n"
        + "    except Exception as e:\n"
        + "        raise AssertionError(f'Test failed: {e}') from e\n"
    )


# ---------------------------------------------------------------------------
# Rule-based template library
# ---------------------------------------------------------------------------

def _script_page_load(tc, url, headless=True, slow_mo=0, **_):
    body = '''
title = await page.title()
assert title.strip(), "Page title is empty"
assert page.url != "about:blank", "Page did not navigate"
body_text = await page.inner_text("body")
assert len(body_text.strip()) > 50, "Page body appears empty"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_navigation_links(tc, url, headless=True, slow_mo=0, **_):
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


def _script_login_valid(tc, url, credentials=None, headless=True, slow_mo=0, **_):
    username = (credentials or {}).get("username", "testuser@example.com")
    password = (credentials or {}).get("password", "TestPassword123")
    body = f'''
for sel in ['input[type="email"]', 'input[name="username"]', 'input[name="email"]',
            'input[placeholder*="email" i]', 'input[placeholder*="username" i]']:
    el = page.locator(sel).first
    if await el.count() > 0:
        await el.fill("{username}")
        break
for sel in ['input[type="password"]', 'input[name="password"]']:
    el = page.locator(sel).first
    if await el.count() > 0:
        await el.fill("{password}")
        break
for sel in ['button[type="submit"]','input[type="submit"]',
            'button:has-text("Login")','button:has-text("Sign in")']:
    el = page.locator(sel).first
    if await el.count() > 0:
        await el.click()
        break
await page.wait_for_load_state("networkidle", timeout=10000)
password_still_visible = await page.locator('input[type="password"]').count()
assert password_still_visible == 0 or page.url != "{url}", "Login did not succeed"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_login_empty_fields(tc, url, headless=True, slow_mo=0, **_):
    body = '''
for sel in ['button[type="submit"]','input[type="submit"]',
            'button:has-text("Login")','button:has-text("Sign in")']:
    el = page.locator(sel).first
    if await el.count() > 0:
        await el.click()
        break
await page.wait_for_timeout(1000)
error_count = await page.locator(
    '[class*="error"],[class*="alert"],[role="alert"],[aria-invalid="true"],:invalid'
).count()
assert error_count > 0, "No validation error shown for empty form submission"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_login_invalid_creds(tc, url, headless=True, slow_mo=0, **_):
    body = '''
for sel in ['input[type="email"]','input[name="username"]','input[name="email"]']:
    el = page.locator(sel).first
    if await el.count() > 0:
        await el.fill("invalid_xyz_nobody@noemail.com")
        break
for sel in ['input[type="password"]','input[name="password"]']:
    el = page.locator(sel).first
    if await el.count() > 0:
        await el.fill("WrongPass999!")
        break
for sel in ['button[type="submit"]','input[type="submit"]',
            'button:has-text("Login")','button:has-text("Sign in")']:
    el = page.locator(sel).first
    if await el.count() > 0:
        await el.click()
        break
await page.wait_for_timeout(2000)
error_count = await page.locator(
    '[class*="error"],[class*="alert"],[role="alert"],[class*="invalid"],[class*="danger"]'
).count()
assert error_count > 0, "No error shown for invalid credentials"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_search(tc, url, headless=True, slow_mo=0, **_):
    body = '''
search_box = page.locator(
    'input[type="search"],input[name*="search" i],'
    'input[placeholder*="search" i],input[aria-label*="search" i]'
).first
assert await search_box.count() > 0, "No search input found"
await search_box.fill("test")
await search_box.press("Enter")
await page.wait_for_load_state("networkidle", timeout=10000)
results = page.locator('[class*="result"],[class*="search"],[role="listbox"],[role="list"] li,ul li')
count   = await results.count()
assert count > 0, f"Search returned no results"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_form_submit(tc, url, headless=True, slow_mo=0, **_):
    body = '''
inputs = page.locator('input[type="text"],input[type="email"],textarea')
count  = await inputs.count()
assert count > 0, "No form inputs found"
for i in range(count):
    inp      = inputs.nth(i)
    inp_type = await inp.get_attribute("type") or "text"
    if inp_type in ("text","search"):
        await inp.fill("Test Value")
    elif inp_type == "email":
        await inp.fill("tester@example.com")
btn = page.locator('button[type="submit"],input[type="submit"]').first
if await btn.count() > 0:
    await btn.click()
    await page.wait_for_load_state("networkidle", timeout=10000)
errors = []
page.on("pageerror", lambda e: errors.append(str(e)))
assert len(errors) == 0, f"JS errors after form submit: {errors[:3]}"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_dropdown(tc, url, headless=True, slow_mo=0, **_):
    body = '''
selects = page.locator("select")
if await selects.count() > 0:
    sel     = selects.first
    options = await sel.locator("option").all_inner_texts()
    assert len(options) > 1, f"Dropdown has only {len(options)} option"
    await sel.select_option(index=1)
    selected = await sel.input_value()
    assert selected, "No option was selected"
else:
    combo = page.locator('[role="combobox"],[role="listbox"]').first
    assert await combo.count() > 0, "No dropdown found on page"
    await combo.click()
    items = page.locator('[role="option"]')
    assert await items.count() > 0, "Dropdown opened but no options visible"
    await items.first.click()
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_table(tc, url, headless=True, slow_mo=0, **_):
    body = '''
table = page.locator("table").first
assert await table.count() > 0, "No table found"
headers = table.locator("th")
assert await headers.count() > 0, "Table has no headers"
rows = table.locator("tbody tr")
assert await rows.count() > 0, "Table has no data rows"
first_row_text = await rows.first.inner_text()
assert first_row_text.strip(), "First table row is empty"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_modal(tc, url, headless=True, slow_mo=0, **_):
    body = '''
trigger = page.locator(
    'button:has-text("Open"),button:has-text("View"),'
    'button:has-text("Details"),[data-toggle="modal"],[data-bs-toggle="modal"]'
).first
if await trigger.count() > 0:
    await trigger.click()
    await page.wait_for_timeout(500)
    modal = page.locator('[role="dialog"],.modal,[class*="modal"]').first
    assert await modal.is_visible(), "Modal did not open"
    close = page.locator('[aria-label="Close"],button:has-text("Close"),button:has-text("Cancel")').first
    if await close.count() > 0:
        await close.click()
        await page.wait_for_timeout(300)
        assert not await modal.is_visible(), "Modal did not close"
else:
    open_modal = await page.locator('[role="dialog"]:visible').count()
    assert open_modal == 0, "Unexpected modal is open on page load"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_responsive(tc, url, headless=True, slow_mo=0, **_):
    body = '''
await page.set_viewport_size({"width": 375, "height": 812})
await page.reload(wait_until="networkidle")
scroll_w = await page.evaluate("document.documentElement.scrollWidth")
client_w = await page.evaluate("document.documentElement.clientWidth")
assert scroll_w <= client_w + 5, f"Horizontal overflow at 375px: scrollWidth={scroll_w}"
await page.set_viewport_size({"width": 1440, "height": 900})
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_accessibility(tc, url, headless=True, slow_mo=0, **_):
    body = '''
h1_count = await page.locator("h1").count()
assert h1_count >= 1, f"No H1 heading found (got {h1_count})"
assert h1_count <= 3, f"Too many H1 headings: {h1_count}"
images    = page.locator("img")
img_count = await images.count()
for i in range(min(img_count, 20)):
    alt = await images.nth(i).get_attribute("alt")
    src = await images.nth(i).get_attribute("src") or ""
    assert alt is not None, f"Image '{src}' missing alt attribute"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_keyboard(tc, url, headless=True, slow_mo=0, **_):
    body = '''
focused = 0
for _ in range(15):
    await page.keyboard.press("Tab")
    tag = await page.evaluate("document.activeElement ? document.activeElement.tagName.toLowerCase() : null")
    if tag in ("a","button","input","select","textarea"):
        focused += 1
assert focused >= 3, f"Only {focused} elements received keyboard focus"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_404(tc, url, headless=True, slow_mo=0, **_):
    bad_url = url.rstrip("/") + "/this-page-does-not-exist-xyz-404test"
    body = f'''
response = await page.goto("{bad_url}", wait_until="domcontentloaded")
status   = response.status if response else 0
if status not in (404, 200):
    raise AssertionError(f"Unexpected HTTP status {{status}} for missing page")
if status == 200:
    content = (await page.content()).lower()
    assert any(w in content for w in ["not found","404","doesn't exist","no page"]), (
        "Got 200 for missing page but no error content found"
    )
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_performance(tc, url, headless=True, slow_mo=0, **_):
    body = '''
timing = await page.evaluate("""() => {
    const t = performance.getEntriesByType('navigation')[0];
    return t ? t.loadEventEnd - t.startTime : null;
}""")
assert timing is not None, "Navigation timing API unavailable"
assert timing < 5000, f"Page loaded in {timing:.0f}ms — exceeds 5s threshold"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_images(tc, url, headless=True, slow_mo=0, **_):
    body = '''
broken = await page.evaluate("""() => {
    return [...document.images]
        .filter(img => img.complete && img.naturalWidth === 0)
        .map(img => img.src);
}""")
assert len(broken) == 0, f"{len(broken)} broken image(s): {broken[:5]}"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_footer(tc, url, headless=True, slow_mo=0, **_):
    body = '''
footer = page.locator("footer,[role='contentinfo'],#footer,.footer").first
assert await footer.count() > 0, "No footer element found"
assert await footer.is_visible(), "Footer is not visible"
footer_links = footer.locator("a")
assert await footer_links.count() > 0, "Footer has no links"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_buttons(tc, url, headless=True, slow_mo=0, **_):
    body = '''
buttons = page.get_by_role("button")
count   = await buttons.count()
assert count > 0, "No buttons found on page"
for i in range(min(count, 15)):
    btn     = buttons.nth(i)
    label   = await btn.inner_text()
    assert await btn.is_visible(), f"Button '{label}' not visible"
    assert await btn.is_enabled(), f"Button '{label}' unexpectedly disabled"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_title_visible(tc, url, headless=True, slow_mo=0, **_):
    body = '''
heading = page.get_by_role("heading").first
assert await heading.count() > 0, "No heading found on page"
await expect(heading).to_be_visible()
title = await page.title()
assert title.strip(), "Browser tab title is empty"
'''
    return _wrap(body, url, headless=headless, slow_mo=slow_mo)


def _script_generic(tc, url, headless=True, slow_mo=0, **_):
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

_RULES = [
    # ── Page load / structure ────────────────────────────────────────────────
    (["page load","page title","landing","home page","loads successfully"],   _script_page_load),
    (["navigation","nav bar","navbar","menu link","header link"],              _script_navigation_links),
    (["footer","footer link"],                                                 _script_footer),
    (["button","cta","clickable"],                                             _script_buttons),
    (["title","heading visible","content visible","text visible"],             _script_title_visible),
    # ── Auth — negatives BEFORE the generic login rule ───────────────────────
    (["empty field","blank field","missing field","empty form",
      "without credentials","no credentials"],                                 _script_login_empty_fields),
    (["invalid credential","wrong password","incorrect password",
      "invalid login","invalid email","wrong credential"],                     _script_login_invalid_creds),
    (["valid credential","valid login","successful login",
      "login with valid","sign in with valid"],                                _script_login_valid),
    (["login","sign in","authenticate"],                                       _script_login_valid),
    # ── Interactions ─────────────────────────────────────────────────────────
    (["search","search bar","search box","search result"],                     _script_search),
    (["form submit","submit form","form validation","input form",
      "form field","required field","fill form"],                              _script_form_submit),
    (["dropdown","select option","combobox","drop down"],                      _script_dropdown),
    (["modal","dialog","popup","pop-up","overlay"],                            _script_modal),
    # ── Data / content ───────────────────────────────────────────────────────
    (["table","data table","grid","tabular","data row"],                       _script_table),
    (["image","broken image","picture","img load"],                            _script_images),
    # ── Non-functional ───────────────────────────────────────────────────────
    (["responsive","mobile","viewport","screen size","mobile view"],           _script_responsive),
    (["accessibility","alt text","screen reader","wcag","h1","heading"],       _script_accessibility),
    (["keyboard","tab order","focus","keyboard navigation","tab key"],         _script_keyboard),
    (["404","not found","missing page","error page","invalid url"],            _script_404),
    (["performance","load time","page speed","response time"],                 _script_performance),
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
# LLM fallback
# ---------------------------------------------------------------------------

_LLM_SYS = (
    "You are a Playwright (Python async) expert. "
    "Return ONLY executable Python code — no markdown fences."
)

_LLM_TPL = """\
Write run_test() -> bool for:
URL: {url}
ID: {id}  Title: {title}
Steps:
{steps}
Expected: {expected}

Rules: import only asyncio + playwright.async_api, headless Chromium,
assert the expected result, return True on pass, raise AssertionError on fail."""


def _llm_script(tc: TestCase, url: str, credentials=None) -> str:
    cred = (f"\nCredentials: {credentials['username']} / {credentials['password']}"
            if credentials else "")
    try:
        r = client.chat.completions.create(
            model    = TEXT_MODEL,
            messages = [
                {"role": "system", "content": _LLM_SYS},
                {"role": "user",   "content": _LLM_TPL.format(
                    url=url + cred, id=tc.test_case_id, title=tc.test_case_title,
                    steps=tc.test_steps, expected=tc.expected_result,
                )},
            ],
            max_tokens=1500, temperature=0.15,
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

def generate_script(tc: TestCase, url: str, credentials=None,
                    headless: bool = True, slow_mo: int = 0) -> str:
    fn = _match_rule(tc)
    if fn:
        log("info", NAME, f"{tc.test_case_id} → rule [{fn.__name__}]")
        return fn(tc=tc, url=url, credentials=credentials,
                  headless=headless, slow_mo=slow_mo)
    log("info", NAME, f"{tc.test_case_id} → LLM fallback")
    return _llm_script(tc, url, credentials)


def run(ctx: AgentContext) -> list[TestCase]:
    headless = not getattr(ctx, "headed", False)
    slow_mo  = getattr(ctx, "slow_mo", 0) if not headless else 0
    log("agent", NAME,
        f"Generating scripts for {len(ctx.test_cases)} test cases  "
        f"[{'headed' if not headless else 'headless'}"
        + (f", slow_mo={slow_mo}ms" if slow_mo else "") + "]")
    rule_n = llm_n = 0
    for tc in ctx.test_cases:
        fn = _match_rule(tc)
        if fn:
            tc.auto_script = fn(tc=tc, url=ctx.url, credentials=ctx.credentials,
                                headless=headless, slow_mo=slow_mo)
            rule_n += 1
        else:
            tc.auto_script = _llm_script(tc, ctx.url, ctx.credentials)
            llm_n += 1
    log("success", NAME, f"Scripts ready — rule-based: {rule_n} | LLM: {llm_n}")
    return ctx.test_cases
