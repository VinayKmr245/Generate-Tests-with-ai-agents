"""
Agent 1 — BrowserAgent
Responsibility: Open URLs, capture screenshots, detect & perform login.
Outputs: screenshot_b64 strings + post-login URL for downstream agents.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import base64

from playwright.async_api import Page, async_playwright

from config import (BROWSER_USER_AGENT, BROWSER_VIEWPORT, NETWORK_IDLE_TIMEOUT,
                    PAGE_LOAD_TIMEOUT)
from logger import log
from models import AgentContext

NAME = "BrowserAgent"

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


async def _screenshot(page: Page) -> str:
    data = await page.screenshot(full_page=True)
    return base64.b64encode(data).decode()


async def _safe_goto(page: Page, url: str):
    try:
        await page.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
    except Exception as e:
        log("warning", NAME, f"Page load warning: {e}")


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


async def run(ctx: AgentContext) -> dict:
    """
    Returns:
        {
          "pre_login":  { "screenshot_b64": str, "url": str },
          "post_login": { "screenshot_b64": str, "url": str } | None,
          "login_attempted": bool,
          "login_succeeded": bool,
        }
    """
    log("browser", NAME, f"Launching headless Chromium → {ctx.url}")

    result = {
        "pre_login": {},
        "post_login": None,
        "login_attempted": False,
        "login_succeeded": False,
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        bctx = await browser.new_context(
            viewport=BROWSER_VIEWPORT,
            user_agent=BROWSER_USER_AGENT,
        )
        page = await bctx.new_page()

        await _safe_goto(page, ctx.url)
        log("browser", NAME, "Initial page loaded — capturing screenshot")

        pre_shot = await _screenshot(page)
        result["pre_login"] = {"screenshot_b64": pre_shot, "url": page.url}

        # Login handling — deferred decision to VisionAgent output
        # But BrowserAgent exposes a helper the orchestrator can call back into
        # via the context. We store the page state info and return.
        # If credentials exist, we also attempt login right here.
        if ctx.credentials:
            log("browser", NAME, "Credentials provided — attempting login")
            result["login_attempted"] = True
            success = await _try_login(page, ctx.credentials)
            result["login_succeeded"] = success

            if success:
                log("success", NAME, "Login succeeded")
                # If caller wants tests for a specific page/module, navigate there now
                if ctx.target_url and ctx.target_url != ctx.url:
                    log("browser", NAME, f"Navigating to target → {ctx.target_url}")
                    await _safe_goto(page, ctx.target_url)
                post_shot = await _screenshot(page)
                result["post_login"] = {"screenshot_b64": post_shot, "url": page.url}
                log("browser", NAME, f"Post-login screenshot captured at {page.url}")
            else:
                log("warning", NAME, "Login attempt failed")

        await browser.close()

    return result
