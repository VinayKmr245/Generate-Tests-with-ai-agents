"""
Agent 1 — BrowserAgent
Responsibility:
  • Navigate to the target URL (handling login if credentials are supplied)
  • Capture a full-page screenshot for reference / reporting
  • Scrape all interactive DOM elements from the live page
    (buttons, inputs, textareas, selects, links, checkboxes, radios, …)
    and return their full attribute metadata so VisionAgent can build
    UIComponent objects from real DOM data rather than screenshot guesswork.

DOM scraping strategy
─────────────────────
A single JavaScript snippet runs inside the page via page.evaluate().
It queries every interactive element, walks the DOM to determine which
landmark section it lives in (form / header / nav / main / footer /
section / aside), and returns a structured list that maps 1-to-1 onto
UIComponent fields.

Only VISIBLE, ENABLED elements are returned by default.  Hidden inputs,
disabled controls, and script/style tags are excluded.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import base64
import json

from playwright.async_api import Page, async_playwright

from config import (BROWSER_USER_AGENT, BROWSER_VIEWPORT, NETWORK_IDLE_TIMEOUT,
                    PAGE_LOAD_TIMEOUT)
from logger import log
from models import AgentContext

NAME = "BrowserAgent"

# ---------------------------------------------------------------------------
# Selectors
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
    'button[type="submit"]', 'input[type="submit"]',
    'button:has-text("Login")', 'button:has-text("Sign In")',
    'button:has-text("Log In")', 'button:has-text("Continue")',
    '[role="button"]:has-text("Login")',
]

# ---------------------------------------------------------------------------
# DOM scraping script (runs inside the browser via page.evaluate)
# ---------------------------------------------------------------------------

# This JS snippet collects every interactive element on the page.
# It is injected as a string so it runs in the page context.
_DOM_SCRAPER_JS = """
() => {
  // ── Helper: find the closest meaningful landmark section ────────────────
  function getSection(el) {
    const landmarks = ['form', 'header', 'nav', 'main', 'footer',
                       'aside', 'section', 'article', '[role="dialog"]',
                       '[role="navigation"]', '[role="main"]'];
    for (const selector of landmarks) {
      const ancestor = el.closest(selector);
      if (ancestor) {
        const tag  = ancestor.tagName.toLowerCase();
        const id   = ancestor.id   ? '#' + ancestor.id   : '';
        const name = ancestor.name ? '[name=' + ancestor.name + ']' : '';
        const cls  = ancestor.className && typeof ancestor.className === 'string'
                     ? '.' + ancestor.className.trim().split(/\\s+/)[0] : '';
        return tag + (id || name || cls || '');
      }
    }
    return 'body';
  }

  // ── Helper: get a unique-enough CSS selector for an element ─────────────
  function getSelector(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const tag   = el.tagName.toLowerCase();
    const type  = el.type  ? '[type="' + el.type  + '"]' : '';
    const name  = el.name  ? '[name="' + CSS.escape(el.name)  + '"]' : '';
    const ph    = el.placeholder ? '[placeholder="' + CSS.escape(el.placeholder) + '"]' : '';
    const text  = (el.textContent || '').trim().slice(0, 30);
    const aria  = el.getAttribute('aria-label')
                  ? '[aria-label="' + CSS.escape(el.getAttribute('aria-label')) + '"]' : '';
    if (name)  return tag + type + name;
    if (aria)  return tag + aria;
    if (ph)    return tag + type + ph;
    if (type)  return tag + type;
    return tag;
  }

  // ── Helper: resolve the visible label for an element ────────────────────
  function getLabel(el) {
    // 1. aria-label
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();

    // 2. <label for="id">
    if (el.id) {
      const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lbl) return lbl.textContent.trim();
    }

    // 3. Wrapping <label>
    const wrapper = el.closest('label');
    if (wrapper) return wrapper.textContent.trim().replace(el.value || '', '').trim();

    // 4. placeholder / value / name / textContent
    return el.placeholder || el.value || el.name
           || (el.textContent || '').trim().slice(0, 60)
           || el.getAttribute('aria-labelledby') || '';
  }

  // ── Helper: get the coarse location of an element ───────────────────────
  function getLocation(el) {
    const rect   = el.getBoundingClientRect();
    const height = window.innerHeight;
    const width  = window.innerWidth;
    if (rect.top < height * 0.15)  return 'header';
    if (rect.top > height * 0.85)  return 'footer';
    if (rect.left < width * 0.15)  return 'sidebar';
    return 'main';
  }

  // ── Helper: map raw tag+type → canonical component type ─────────────────
  function getType(el) {
    const tag  = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    if (tag === 'button' || type === 'button' || type === 'submit' || type === 'reset')
      return 'button';
    if (tag === 'select')   return 'select';
    if (tag === 'textarea') return 'textarea';
    if (tag === 'a')        return 'link';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio')    return 'radio';
    if (type === 'file')     return 'file_input';
    if (type === 'date' || type === 'datetime-local' || type === 'time') return 'date_input';
    if (type === 'number' || type === 'range') return 'number_input';
    if (type === 'email')    return 'email_input';
    if (type === 'password') return 'password_input';
    if (type === 'search')   return 'search_input';
    if (type === 'tel')      return 'tel_input';
    if (type === 'url')      return 'url_input';
    if (type === 'hidden')   return null;   // skip hidden
    if (tag === 'input')     return 'text_input';
    return tag;
  }

  // ── Interactive element selectors ────────────────────────────────────────
  const QUERY = [
    'button:not([disabled])',
    'input:not([type="hidden"]):not([disabled])',
    'textarea:not([disabled])',
    'select:not([disabled])',
    'a[href]',
    '[role="button"]:not([disabled])',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="combobox"]',
    '[role="textbox"]',
    '[role="searchbox"]',
    '[role="spinbutton"]',
    '[role="switch"]',
    '[contenteditable="true"]',
  ].join(', ');

  const seen      = new Set();
  const elements  = [];

  document.querySelectorAll(QUERY).forEach(el => {
    // Skip duplicates
    if (seen.has(el)) return;
    seen.add(el);

    // Skip truly invisible elements (display:none / visibility:hidden / zero size)
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;

    const canonicalType = getType(el);
    if (!canonicalType) return;   // skip hidden inputs etc.

    // Infer purpose
    const label   = getLabel(el);
    const section = getSection(el);
    const tag     = el.tagName.toLowerCase();
    const type    = (el.type || '').toLowerCase();

    // Skip navigation-only links that are not form-related
    // (keep links inside forms or with suggestive text)
    if (canonicalType === 'link') {
      const text = (el.textContent || '').trim().toLowerCase();
      const formLink = el.closest('form') !== null;
      const actionWords = ['login', 'sign', 'register', 'forgot', 'reset',
                           'submit', 'cancel', 'delete', 'edit', 'save',
                           'create', 'update', 'confirm', 'next', 'back',
                           'continue', 'apply', 'remove', 'add', 'view'];
      if (!formLink && !actionWords.some(w => text.includes(w))) return;
    }

    elements.push({
      type:        canonicalType,
      tag:         tag,
      input_type:  type,
      label:       label,
      selector:    getSelector(el),
      name:        el.name        || '',
      element_id:  el.id          || '',
      placeholder: el.placeholder || '',
      aria_label:  el.getAttribute('aria-label') || '',
      is_visible:  style.display !== 'none' && style.visibility !== 'hidden',
      is_enabled:  !el.disabled,
      is_required: el.required || el.getAttribute('aria-required') === 'true',
      location:    getLocation(el),
      dom_section: section,
      purpose:     ''   // filled in by VisionAgent using Groq
    });
  });

  return {
    title:       document.title,
    url:         window.location.href,
    elements:    elements,
    form_count:  document.querySelectorAll('form').length,
    has_login:   document.querySelector('input[type="password"]') !== null,
  };
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _screenshot(page: Page) -> str:
    data = await page.screenshot(full_page=True)
    return base64.b64encode(data).decode()


async def _safe_goto(page: Page, url: str):
    try:
        await page.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
    except Exception as e:
        log("warning", NAME, f"Page load warning: {e}")


async def _scrape_dom(page: Page) -> dict:
    """Run the DOM scraper JS and return structured element data."""
    try:
        result = await page.evaluate(_DOM_SCRAPER_JS)
        return result
    except Exception as e:
        log("warning", NAME, f"DOM scrape error: {e}")
        return {"title": "", "url": "", "elements": [], "form_count": 0, "has_login": False}


async def _try_login(page: Page, credentials: dict) -> bool:
    for sel in USERNAME_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.fill(credentials["username"])
                break
        except Exception:
            continue

    for sel in PASSWORD_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.fill(credentials["password"])
                break
        except Exception:
            continue

    for sel in SUBMIT_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT)
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Public run() — called by orchestrator
# ---------------------------------------------------------------------------

async def run(ctx: AgentContext) -> dict:
    """
    Returns:
    {
      "pre_login":  {
          "screenshot_b64": str,
          "url": str,
          "dom": { title, url, elements:[...], form_count, has_login }
      },
      "post_login": { same structure } | None,
      "login_attempted": bool,
      "login_succeeded": bool,
    }
    """
    log("browser", NAME, f"Launching headless Chromium → {ctx.url}")

    result = {
        "pre_login":       {},
        "post_login":      None,
        "login_attempted": False,
        "login_succeeded": False,
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        bctx    = await browser.new_context(
            viewport   = BROWSER_VIEWPORT,
            user_agent = BROWSER_USER_AGENT,
        )
        page = await bctx.new_page()

        # ── Pre-login ──────────────────────────────────────────────────────
        await _safe_goto(page, ctx.url)
        log("browser", NAME, "Page loaded — scraping DOM + screenshot")

        pre_dom  = await _scrape_dom(page)
        pre_shot = await _screenshot(page)
        result["pre_login"] = {
            "screenshot_b64": pre_shot,
            "url":            page.url,
            "dom":            pre_dom,
        }
        log("browser", NAME,
            f"Pre-login: {len(pre_dom.get('elements', []))} interactive elements found")

        # ── Login ──────────────────────────────────────────────────────────
        if ctx.credentials:
            log("browser", NAME, "Credentials provided — attempting login")
            result["login_attempted"] = True
            success = await _try_login(page, ctx.credentials)
            result["login_succeeded"] = success

            if success:
                log("success", NAME, "Login succeeded")
                if ctx.target_url and ctx.target_url != ctx.url:
                    log("browser", NAME, f"Navigating to target → {ctx.target_url}")
                    await _safe_goto(page, ctx.target_url)

                post_dom  = await _scrape_dom(page)
                post_shot = await _screenshot(page)
                result["post_login"] = {
                    "screenshot_b64": post_shot,
                    "url":            page.url,
                    "dom":            post_dom,
                }
                log("browser", NAME,
                    f"Post-login: {len(post_dom.get('elements', []))} "
                    f"interactive elements at {page.url}")
            else:
                log("warning", NAME, "Login attempt failed")

        await browser.close()

    return result
