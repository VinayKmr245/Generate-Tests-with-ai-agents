"""
Agent 1 — BrowserAgent
Responsibility:
  • Navigate to the target URL, handle login, navigate to module target URL
  • Capture full-page screenshots for reporting
  • Scrape all interactive DOM elements from the live page
  • Honour ctx.headed / ctx.slow_mo for both browsing and testing phases

Bug fixes in this version
──────────────────────────
1. headed mode — pw.chromium.launch() now reads ctx.headed and ctx.slow_mo
   so the browsing/login phase is also visible when --headed is passed

2. target_url navigation — fixed the redirect race:
   - After login we wait for networkidle AND verify the URL has settled
   - We always navigate to target_url using goto() rather than checking
     against ctx.url (which is the login page URL, not the post-login URL)
   - Added explicit wait_for_load_state("domcontentloaded") + networkidle
     so SPA routers have time to mount the target page
   - Retries the target navigation once if the first attempt lands on a
     different URL (handles apps that redirect back to dashboard first)

3. SPA / redirect handling — after login, waits for the page to stop
   navigating before proceeding (uses wait_for_load_state chain)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import base64

from playwright.async_api import BrowserContext, Page, async_playwright

from config import (BROWSER_USER_AGENT, BROWSER_VIEWPORT, NETWORK_IDLE_TIMEOUT,
                    PAGE_LOAD_TIMEOUT)
from logger import log
from models import AgentContext

NAME = "BrowserAgent"

# ---------------------------------------------------------------------------
# Login selectors
# ---------------------------------------------------------------------------

USERNAME_SELECTORS = [
    'input[type="email"]',
    'input[type="text"][name*="user"]',
    'input[type="text"][name*="email"]',
    'input[type="text"][name*="login"]',
    'input[id*="user"]', 'input[id*="email"]', 'input[id*="login"]',
    'input[placeholder*="email" i]', 'input[placeholder*="username" i]',
    'input[name="username"]', 'input[name="email"]',
]
PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]', 'input[id*="password"]', 'input[id*="pass"]',
]
SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Login")',
    'button:has-text("Sign In")',
    'button:has-text("Log In")',
    'button:has-text("Sign in")',
    'button:has-text("Continue")',
    'button:has-text("Submit")',
    '[role="button"]:has-text("Login")',
    '[role="button"]:has-text("Sign In")',
]

# ---------------------------------------------------------------------------
# DOM scraper JS — runs inside the page via page.evaluate()
# Returns all interactive elements with full attribute metadata
# ---------------------------------------------------------------------------

_DOM_SCRAPER_JS = r"""
() => {
  function getSection(el) {
    const landmarks = ['form', 'header', 'nav', 'main', 'footer',
                       'aside', 'section', 'article',
                       '[role="dialog"]', '[role="navigation"]', '[role="main"]'];
    for (const sel of landmarks) {
      const anc = el.closest(sel);
      if (anc) {
        const tag = anc.tagName.toLowerCase();
        const id  = anc.id ? '#' + anc.id : '';
        const cls = typeof anc.className === 'string' && anc.className.trim()
                    ? '.' + anc.className.trim().split(/\s+/)[0] : '';
        return tag + (id || cls || '');
      }
    }
    return 'body';
  }

  function getSelector(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const tag  = el.tagName.toLowerCase();
    const type = el.type  ? '[type="' + el.type  + '"]' : '';
    const name = el.name  ? '[name="' + CSS.escape(el.name) + '"]' : '';
    const ph   = el.placeholder
                 ? '[placeholder="' + CSS.escape(el.placeholder) + '"]' : '';
    const aria = el.getAttribute('aria-label')
                 ? '[aria-label="' + CSS.escape(el.getAttribute('aria-label')) + '"]' : '';
    if (name) return tag + type + name;
    if (aria) return tag + aria;
    if (ph)   return tag + type + ph;
    if (type) return tag + type;
    return tag;
  }

  function getLabel(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    if (el.id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lbl) return lbl.textContent.trim();
    }
    const wrapper = el.closest('label');
    if (wrapper) return wrapper.textContent.trim().replace(el.value || '', '').trim();
    return el.placeholder || el.name
           || (el.textContent || '').trim().slice(0, 60)
           || '';
  }

  function getLocation(el) {
    const rect = el.getBoundingClientRect();
    const h = window.innerHeight, w = window.innerWidth;
    if (rect.top < h * 0.15) return 'header';
    if (rect.top > h * 0.85) return 'footer';
    if (rect.left < w * 0.15) return 'sidebar';
    return 'main';
  }

  function getType(el) {
    const tag  = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    if (tag === 'button' || type === 'button' || type === 'submit' || type === 'reset')
      return 'button';
    if (tag === 'select')    return 'select';
    if (tag === 'textarea')  return 'textarea';
    if (tag === 'a')         return 'link';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio')    return 'radio';
    if (type === 'file')     return 'file_input';
    if (type === 'date' || type === 'datetime-local' || type === 'time') return 'date_input';
    if (type === 'number' || type === 'range')  return 'number_input';
    if (type === 'email')    return 'email_input';
    if (type === 'password') return 'password_input';
    if (type === 'search')   return 'search_input';
    if (type === 'tel')      return 'tel_input';
    if (type === 'url')      return 'url_input';
    if (type === 'hidden')   return null;
    if (tag === 'input')     return 'text_input';
    return tag;
  }

  const QUERY = [
    'button:not([disabled])',
    'input:not([type="hidden"]):not([disabled])',
    'textarea:not([disabled])',
    'select:not([disabled])',
    'a[href]',
    '[role="button"]:not([disabled])',
    '[role="checkbox"]', '[role="radio"]',
    '[role="combobox"]', '[role="textbox"]',
    '[role="searchbox"]', '[role="spinbutton"]',
    '[role="switch"]',
    '[contenteditable="true"]',
  ].join(', ');

  const seen = new Set();
  const out  = [];

  document.querySelectorAll(QUERY).forEach(el => {
    if (seen.has(el)) return;
    seen.add(el);

    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;

    const canonicalType = getType(el);
    if (!canonicalType) return;

    if (canonicalType === 'link') {
      const text = (el.textContent || '').trim().toLowerCase();
      const inForm = el.closest('form') !== null;
      const action = ['login','sign','register','forgot','reset','submit',
                      'cancel','delete','edit','save','create','update',
                      'confirm','next','back','continue','apply','remove','add','view'];
      if (!inForm && !action.some(w => text.includes(w))) return;
    }

    out.push({
      type:        canonicalType,
      tag:         el.tagName.toLowerCase(),
      input_type:  (el.type || '').toLowerCase(),
      label:       getLabel(el),
      selector:    getSelector(el),
      name:        el.name        || '',
      element_id:  el.id          || '',
      placeholder: el.placeholder || '',
      aria_label:  el.getAttribute('aria-label') || '',
      is_visible:  true,
      is_enabled:  !el.disabled,
      is_required: el.required || el.getAttribute('aria-required') === 'true',
      location:    getLocation(el),
      dom_section: getSection(el),
      purpose:     '',
    });
  });

  return {
    title:      document.title,
    url:        window.location.href,
    elements:   out,
    form_count: document.querySelectorAll('form').length,
    has_login:  document.querySelector('input[type="password"]') !== null,
  };
}
"""

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

async def _screenshot(page: Page) -> str:
    data = await page.screenshot(full_page=True)
    return base64.b64encode(data).decode()


async def _wait_for_stable(page: Page, timeout: int = 8000) -> None:
    """
    Wait for the page to reach a stable state after navigation or clicks.
    Uses a two-stage wait:
      1. domcontentloaded — DOM is parsed
      2. networkidle     — no pending network requests (catches SPA routers)
    Silently ignores timeout errors (some apps never reach networkidle).
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass


async def _goto(page: Page, url: str, label: str = "") -> bool:
    """
    Navigate to url with full stability wait.
    Returns True if the final page URL starts with the requested URL
    (handles trailing slashes, query params, hash fragments).
    """
    desc = f" [{label}]" if label else ""
    log("browser", NAME, f"Navigating{desc} → {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        await _wait_for_stable(page, timeout=NETWORK_IDLE_TIMEOUT)
    except Exception as e:
        log("warning", NAME, f"Navigation warning{desc}: {e}")

    final_url = page.url
    # Normalise: strip trailing slash and fragment for comparison
    def _norm(u: str) -> str:
        return u.rstrip("/").split("#")[0].split("?")[0]

    landed_on_target = _norm(final_url).startswith(_norm(url))
    log("browser", NAME, f"Landed on: {final_url}"
        + (" ✅" if landed_on_target else " ⚠️  (redirected)"))
    return landed_on_target


async def _scrape_dom(page: Page) -> dict:
    """Inject DOM scraper and return structured element data."""
    try:
        # Wait a moment for any lazy-loaded content to render
        await page.wait_for_timeout(500)
        result = await page.evaluate(_DOM_SCRAPER_JS)
        return result
    except Exception as e:
        log("warning", NAME, f"DOM scrape error: {e}")
        return {"title": "", "url": page.url, "elements": [],
                "form_count": 0, "has_login": False}


async def _try_login(page: Page, credentials: dict) -> bool:
    """
    Fill and submit the login form.
    Returns True if the password field disappears after submit
    (reliable sign that login succeeded across most apps).
    """
    # Fill username
    for sel in USERNAME_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.fill(credentials["username"])
                log("browser", NAME, f"  Username filled via {sel}")
                break
        except Exception:
            continue

    # Fill password
    for sel in PASSWORD_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.fill(credentials["password"])
                log("browser", NAME, f"  Password filled via {sel}")
                break
        except Exception:
            continue

    # Click submit
    for sel in SUBMIT_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                log("browser", NAME, f"  Clicking submit via {sel}")
                await el.click()
                # Wait for navigation triggered by the form submit
                await _wait_for_stable(page, timeout=NETWORK_IDLE_TIMEOUT)
                break
        except Exception:
            continue

    # Verify login: password field should be gone on success
    await page.wait_for_timeout(500)
    password_still_visible = await page.locator('input[type="password"]').count()
    success = password_still_visible == 0
    log("browser", NAME,
        f"Login {'✅ succeeded' if success else '❌ failed'} "
        f"— current URL: {page.url}")
    return success


async def _navigate_to_target(page: Page, target_url: str, current_url: str) -> bool:
    """
    Navigate to target_url after login.

    Strategy:
    1. If already on the target page (SPA deep-link after login), skip.
    2. Otherwise call goto() and wait for stable state.
    3. If the app redirected us away, try goto() once more.
    Returns True when the final URL matches the target.
    """
    def _norm(u: str) -> str:
        return u.rstrip("/").split("#")[0].split("?")[0]

    if _norm(page.url) == _norm(target_url):
        log("browser", NAME, f"Already on target page: {target_url}")
        return True

    log("browser", NAME, f"Navigating to target module URL → {target_url}")
    landed = await _goto(page, target_url, label="target")

    if not landed:
        # Some apps redirect to dashboard first, then allow navigation.
        # Wait a moment and retry once.
        log("browser", NAME, "Retry navigation to target after short delay…")
        await page.wait_for_timeout(1500)
        landed = await _goto(page, target_url, label="target retry")

    return landed


# ---------------------------------------------------------------------------
# Public run() — called by orchestrator
# ---------------------------------------------------------------------------

async def run(ctx: AgentContext) -> dict:
    """
    Execute the full browser phase:
      1. Open login URL
      2. Scrape pre-login DOM + screenshot
      3. Login if credentials provided
      4. Navigate to target_url if specified
      5. Scrape post-login DOM + screenshot

    Headed mode (ctx.headed=True):
      The Chromium window is visible throughout the entire browsing phase.
      slow_mo is applied so you can follow every action.

    Returns a dict with pre_login and post_login data including
    both screenshot_b64 and the full DOM scrape result.
    """
    headed  = getattr(ctx, "headed",  False)
    slow_mo = getattr(ctx, "slow_mo", 0) if headed else 0

    mode_label = f"{'headed' if headed else 'headless'}" + \
                 (f", slow_mo={slow_mo}ms" if slow_mo else "")
    log("browser", NAME, f"Launching Chromium [{mode_label}] → {ctx.url}")

    result = {
        "pre_login":       {},
        "post_login":      None,
        "login_attempted": False,
        "login_succeeded": False,
    }

    async with async_playwright() as pw:
        # ── Launch — honours headed flag ───────────────────────────────────
        launch_kwargs: dict = {"headless": not headed}
        if headed and slow_mo > 0:
            launch_kwargs["slow_mo"] = slow_mo

        browser = await pw.chromium.launch(**launch_kwargs)
        bctx    = await browser.new_context(
            viewport   = BROWSER_VIEWPORT,
            user_agent = BROWSER_USER_AGENT,
        )
        page = await bctx.new_page()

        # ── Pre-login: open the login/home URL ─────────────────────────────
        await _goto(page, ctx.url, label="login page")

        pre_dom  = await _scrape_dom(page)
        pre_shot = await _screenshot(page)
        result["pre_login"] = {
            "screenshot_b64": pre_shot,
            "url":            page.url,
            "dom":            pre_dom,
        }
        log("browser", NAME,
            f"Pre-login: {len(pre_dom.get('elements', []))} interactive elements "
            f"| title: '{pre_dom.get('title', '')}'")

        # ── Login ──────────────────────────────────────────────────────────
        if ctx.credentials:
            log("browser", NAME, "Credentials provided — attempting login")
            result["login_attempted"] = True
            success = await _try_login(page, ctx.credentials)
            result["login_succeeded"] = success

            if success:
                # ── Navigate to target module URL ──────────────────────────
                if ctx.target_url:
                    await _navigate_to_target(page, ctx.target_url, ctx.url)
                else:
                    # Stay on post-login landing page — just wait for it to settle
                    await _wait_for_stable(page, timeout=NETWORK_IDLE_TIMEOUT)

                post_dom  = await _scrape_dom(page)
                post_shot = await _screenshot(page)
                result["post_login"] = {
                    "screenshot_b64": post_shot,
                    "url":            page.url,
                    "dom":            post_dom,
                }
                log("browser", NAME,
                    f"Post-login: {len(post_dom.get('elements', []))} interactive elements "
                    f"| URL: {page.url} "
                    f"| title: '{post_dom.get('title', '')}'")
            else:
                log("warning", NAME,
                    "Login failed — test generation will use the login page content")
        else:
            # No credentials — public page. Use pre_login data as post_login too.
            log("browser", NAME, "No credentials — treating as public page")

        await browser.close()

    return result