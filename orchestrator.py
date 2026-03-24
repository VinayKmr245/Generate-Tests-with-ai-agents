"""
Web Test Agent — Orchestrator
═════════════════════════════

Pipeline modes
──────────────
  full       Browse → Analyse → Generate tests → Export scripts
             → Execute → Update Excel
             (complete end-to-end run)

  generate   Browse → Analyse → Generate tests → Write Excel only
             (no script execution)

  automate   Read existing Excel → Generate scripts → Execute → Update Excel
             (re-run automation on a previously generated test suite)

─────────────────────────────────────────────────────────────────────────────
Usage
──────────────────────────────────────────────────────────────────────────────

  # Public page — no login required
  python orchestrator.py full https://example.com

  # Page with login
  python orchestrator.py full https://app.example.com --user admin@x.com --pass secret

  # Login then navigate to a specific page and generate tests for ONE module
  python orchestrator.py full https://app.example.com \\
      --user admin@x.com --pass secret \\
      --target https://app.example.com/profile \\
      --module "User Profile"

  # Generate tests for the Settings page after login
  python orchestrator.py full https://app.example.com \\
      --user admin@x.com --pass secret \\
      --target https://app.example.com/settings \\
      --module "Settings"

  # Generate only (no automation / execution)
  python orchestrator.py generate https://app.example.com \\
      --user admin@x.com --pass secret \\
      --target https://app.example.com/dashboard \\
      --module "Dashboard"

  # Automate an existing Excel
  python orchestrator.py automate output/test_cases_Profile_20260314.xlsx \\
      https://app.example.com --user admin@x.com --pass secret

─────────────────────────────────────────────────────────────────────────────
Arguments
──────────────────────────────────────────────────────────────────────────────

  Positional
  ──────────
  mode            full | generate | automate
  url             Login URL  (or base URL for public pages)
                  For 'automate': path to the existing .xlsx file

  Named (optional)
  ────────────────
  --user  <str>   Username / email for login
  --pass  <str>   Password for login
  --target <url>  URL to navigate to AFTER login before capturing the
                  screenshot for test generation.
                  Use this to scope tests to a specific page/feature.
                  Default: stays on the post-login landing page.
  --module <str>  Module name to assign to ALL generated test cases.
                  The LLM prompt is also scoped to this feature area,
                  producing much more focused and relevant tests.
                  Example: "User Profile", "Settings", "Order History"
                  Default: LLM infers a module name from the page content.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import browser_agent as browser_agent
import excel_agent as excel_agent
import script_export_agent as script_export_agent
import script_generation_agent as script_gen_agent
import test_execution_agent as exec_agent
import test_generation_agent as test_gen_agent
import vision_agent as vision_agent
from logger import log, section
from models import AgentContext

# ─────────────────────────────────────────────────────────────────────────────
# Phase helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _browser_phase(ctx: AgentContext) -> dict:
    section("Agent 1 — Browser Agent")
    return await browser_agent.run(ctx)


def _vision_phase(ctx: AgentContext, browser_result: dict) -> bool:
    """Returns True if a re-run with credentials is needed."""
    section("Agent 2 — Vision Agent")
    vision_agent.run(ctx, browser_result)

    needs_login = any(
        a.requires_login or a.login_form.detected
        for a in ctx.page_analyses
    )
    if needs_login and not ctx.credentials and not browser_result.get("login_attempted"):
        log("warning", "Orchestrator", "Login required but no credentials provided.")
        username = input("  Enter username: ").strip()
        password = input("  Enter password: ").strip()
        ctx.credentials = {"username": username, "password": password}
        return True
    return False


def _generation_phase(ctx: AgentContext):
    section("Agent 3 — Test Generation Agent")
    if ctx.module_name:
        log("info", "Orchestrator",
            f"Module scope: '{ctx.module_name}' — tests will be targeted to this feature")
    if ctx.target_url:
        log("info", "Orchestrator",
            f"Target URL: {ctx.target_url}")
    test_gen_agent.run(ctx)


def _excel_write_phase(ctx: AgentContext) -> str:
    section("Agent 4 — Excel Agent  [write]")
    return excel_agent.run(ctx, mode="write")


def _script_phase(ctx: AgentContext):
    section("Agent 5 — Script Generation Agent")
    script_gen_agent.run(ctx)


def _script_export_phase(ctx: AgentContext):
    section("Agent 7 — Script Export Agent")
    script_export_agent.run(ctx)
    log("success", "Orchestrator", f"Scripts folder → {ctx.scripts_dir}")


def _execution_phase(ctx: AgentContext):
    section("Agent 6 — Test Execution Agent")
    exec_agent.run(ctx)


def _excel_update_phase(ctx: AgentContext):
    section("Agent 4 — Excel Agent  [update results]")
    excel_agent.run(ctx, mode="update")


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline entry points
# ─────────────────────────────────────────────────────────────────────────────

async def run_full_pipeline(
    url: str,
    credentials: dict | None = None,
    target_url: str | None   = None,
    module_name: str | None  = None,
    headed: bool             = False,
    slow_mo: int             = 400,
) -> str:
    """Full pipeline: browse → analyse → generate → excel → scripts → execute → update."""
    ctx             = AgentContext(url=url, credentials=credentials)
    ctx.target_url  = target_url
    ctx.module_name = module_name
    ctx.headed      = headed
    ctx.slow_mo     = slow_mo if headed else 0

    browser_result = await _browser_phase(ctx)

    needs_retry = _vision_phase(ctx, browser_result)
    if needs_retry:
        browser_result = await _browser_phase(ctx)
        _vision_phase(ctx, browser_result)

    _generation_phase(ctx)
    _excel_write_phase(ctx)
    log("excel", "Orchestrator", f"Excel saved → {ctx.excel_path}")

    _script_phase(ctx)
    _script_export_phase(ctx)
    _execution_phase(ctx)
    _excel_update_phase(ctx)

    section("Pipeline Complete")
    log("success", "Orchestrator", f"Excel report   → {ctx.excel_path}")
    log("success", "Orchestrator", f"Scripts folder → {ctx.scripts_dir}")
    return ctx.excel_path


async def run_generate_only(
    url: str,
    credentials: dict | None = None,
    target_url: str | None   = None,
    module_name: str | None  = None,
    headed: bool             = False,
    slow_mo: int             = 400,
) -> str:
    """Browse → analyse → generate test cases → write Excel. No script execution."""
    ctx             = AgentContext(url=url, credentials=credentials)
    ctx.target_url  = target_url
    ctx.module_name = module_name
    ctx.headed      = headed
    ctx.slow_mo     = slow_mo if headed else 0

    browser_result = await _browser_phase(ctx)
    needs_retry    = _vision_phase(ctx, browser_result)
    if needs_retry:
        browser_result = await _browser_phase(ctx)
        _vision_phase(ctx, browser_result)

    _generation_phase(ctx)
    _excel_write_phase(ctx)

    section("Generate-Only Pipeline Complete")
    log("success", "Orchestrator", f"Excel saved → {ctx.excel_path}")
    return ctx.excel_path


async def run_automate_existing(
    excel_path: str,
    url: str,
    credentials: dict | None = None,
) -> str:
    """Read existing Excel → generate & execute scripts → write results back."""
    ctx            = AgentContext(url=url, credentials=credentials)
    ctx.excel_path = excel_path

    section("Agent 4 — Excel Agent  [read]")
    ctx.test_cases = excel_agent.read_test_cases(excel_path)
    log("excel", "Orchestrator", f"Loaded {len(ctx.test_cases)} test cases")

    _script_phase(ctx)
    _script_export_phase(ctx)
    _execution_phase(ctx)
    _excel_update_phase(ctx)

    section("Automate-Existing Pipeline Complete")
    log("success", "Orchestrator", f"Results written → {ctx.excel_path}")
    return ctx.excel_path


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog        = "orchestrator.py",
        description = "Web Test Agent — generate and execute Playwright test cases",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
Examples:
  # Public page
  python orchestrator.py full https://example.com

  # Login only (stays on post-login landing page)
  python orchestrator.py full https://app.example.com --user admin@x.com --pass secret

  # Login + navigate to User Profile + scope tests to that module
  python orchestrator.py full https://app.example.com \\
      --user admin@x.com --pass secret \\
      --target https://app.example.com/profile \\
      --module "User Profile"

  # Generate only, no execution
  python orchestrator.py generate https://app.example.com \\
      --user admin@x.com --pass secret \\
      --target https://app.example.com/settings \\
      --module "Settings"

  # Re-run automation on an existing Excel
  python orchestrator.py automate output/test_cases_Profile.xlsx \\
      https://app.example.com --user admin@x.com --pass secret
        """
    )

    parser.add_argument(
        "mode",
        choices=["full", "generate", "automate"],
        help="Pipeline mode: full | generate | automate",
    )
    parser.add_argument(
        "url",
        help=(
            "Login / base URL of the application. "
            "For 'automate' mode: path to the existing .xlsx file, "
            "followed by the base URL as the second positional argument."
        ),
    )
    # automate mode needs the base URL as a second positional
    parser.add_argument(
        "base_url",
        nargs="?",
        default=None,
        help="Base URL (required for 'automate' mode — url arg is the .xlsx path)",
    )

    auth = parser.add_argument_group("Authentication")
    auth.add_argument("--user", metavar="USERNAME", help="Login username / email")
    auth.add_argument("--pass", dest="password", metavar="PASSWORD", help="Login password")

    targeting = parser.add_argument_group("Module targeting")
    targeting.add_argument(
        "--target",
        metavar="URL",
        help=(
            "URL to navigate to AFTER login before generating tests. "
            "Use this to scope tests to a specific feature page. "
            "Example: https://app.example.com/profile"
        ),
    )
    targeting.add_argument(
        "--module",
        metavar="NAME",
        help=(
            'Module name to assign to all generated test cases AND to focus '
            'the LLM prompt on. Example: "User Profile", "Settings", "Orders"'
        ),
    )

    execution = parser.add_argument_group("Execution display")
    execution.add_argument(
        "--headed",
        action="store_true",
        default=False,
        help=(
            "Run tests in a visible browser window instead of headless. "
            "Tests execute one-at-a-time so windows don't stack up. "
            "Useful for debugging or watching tests run live."
        ),
    )
    execution.add_argument(
        "--slow-mo",
        dest="slow_mo",
        type=int,
        default=400,
        metavar="MS",
        help=(
            "Milliseconds of delay between each Playwright action in headed "
            "mode. Higher = easier to follow. Default: 400ms. Ignored when "
            "running headless."
        ),
    )

    return parser


async def main():
    # Support both old-style positional args and new --flag style
    # Old: python orchestrator.py full <url> [user] [pass]
    # New: python orchestrator.py full <url> --user x --pass y --target t --module m
    parser = _build_parser()

    # Legacy positional compatibility:
    # If 3rd/4th args don't start with '--' treat them as user/pass
    raw_args = sys.argv[1:]
    patched  = list(raw_args)

    # Detect legacy: "full <url> <user> <pass>" (no -- flags for credentials)
    if (len(patched) >= 4
            and patched[0] in ("full", "generate")
            and not patched[2].startswith("--")
            and not patched[3].startswith("--")):
        # Transform to new style
        patched = [patched[0], patched[1],
                   "--user", patched[2], "--pass", patched[3]] + patched[4:]

    # Legacy automate: "automate <excel> <url> [user] [pass]"
    if (len(patched) >= 5
            and patched[0] == "automate"
            and not patched[3].startswith("--")
            and not patched[4].startswith("--")):
        patched = [patched[0], patched[1], patched[2],
                   "--user", patched[3], "--pass", patched[4]] + patched[5:]

    args = parser.parse_args(patched)

    creds = (
        {"username": args.user, "password": args.password}
        if args.user and args.password
        else None
    )

    if args.mode == "full":
        await run_full_pipeline(
            url         = args.url,
            credentials = creds,
            target_url  = args.target,
            module_name = args.module,
            headed      = args.headed,
            slow_mo     = args.slow_mo,
        )

    elif args.mode == "generate":
        await run_generate_only(
            url         = args.url,
            credentials = creds,
            target_url  = args.target,
            module_name = args.module,
            headed      = args.headed,
            slow_mo     = args.slow_mo,
        )

    elif args.mode == "automate":
        if not args.base_url:
            parser.error("automate mode requires: orchestrator.py automate <excel_path> <url>")
        await run_automate_existing(
            excel_path  = args.url,        # first positional = excel path
            url         = args.base_url,   # second positional = base url
            credentials = creds,
        )


if __name__ == "__main__":
    asyncio.run(main())
