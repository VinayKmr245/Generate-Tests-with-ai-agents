"""
Orchestrator — wires all agents into a multi-agent pipeline.

Pipeline modes
──────────────
full       : BrowserAgent → VisionAgent → TestGenerationAgent
             → ExcelAgent(write) → ScriptGenerationAgent → ScriptExportAgent
             → TestExecutionAgent → ExcelAgent(update)

generate   : Same as full but stops after ExcelAgent(write)
             (no automation)

automate   : Reads an existing Excel file, generates scripts,
             runs them, and writes results back.

Usage
─────
  python orchestrator.py full    <url> [username] [password]
  python orchestrator.py generate <url> [username] [password]
  python orchestrator.py automate <excel_path> <url> [username] [password]
"""
import asyncio
import sys

import browser_agent
import excel_agent
import script_export_agent as script_export_agent
import script_generation_agent as script_gen_agent
import test_execution_agent as exec_agent
import test_generation_agent as test_gen_agent
import vision_agent
from logger import log, section
from models import AgentContext

# ──────────────────────────────────────────────────────────────────────────────
# Sub-pipelines
# ──────────────────────────────────────────────────────────────────────────────

async def _browser_phase(ctx: AgentContext) -> dict:
    section("Agent 1 — Browser Agent")
    return await browser_agent.run(ctx)


def _vision_phase(ctx: AgentContext, browser_result: dict):
    section("Agent 2 — Vision Agent")
    vision_agent.run(ctx, browser_result)

    # If login was needed but not yet attempted (no creds passed), prompt now
    needs_login = any(
        a.requires_login or a.login_form.detected
        for a in ctx.page_analyses
    )
    if needs_login and not ctx.credentials and not browser_result.get("login_attempted"):
        log("warning", "Orchestrator", "Login required but no credentials provided.")
        username = input("  Enter username: ").strip()
        password = input("  Enter password: ").strip()
        ctx.credentials = {"username": username, "password": password}
        return True   # signal: re-run browser with creds
    return False


def _generation_phase(ctx: AgentContext):
    section("Agent 3 — Test Generation Agent")
    test_gen_agent.run(ctx)


def _excel_write_phase(ctx: AgentContext) -> str:
    section("Agent 4 — Excel Agent  [write]")
    return excel_agent.run(ctx, mode="write")


def _script_phase(ctx: AgentContext):
    section("Agent 5 — Script Generation Agent")
    script_gen_agent.run(ctx)


def _execution_phase(ctx: AgentContext):
    section("Agent 6 — Test Execution Agent")
    exec_agent.run(ctx)


def _script_export_phase(ctx: AgentContext):
    section("Agent 7 — Script Export Agent")
    script_export_agent.run(ctx)
    log("success", "Orchestrator", f"Scripts folder → {ctx.scripts_dir}")


def _excel_update_phase(ctx: AgentContext):
    section("Agent 4 — Excel Agent  [update results]")
    excel_agent.run(ctx, mode="update")


# ──────────────────────────────────────────────────────────────────────────────
# Public pipeline entry points
# ──────────────────────────────────────────────────────────────────────────────

async def run_full_pipeline(url: str, credentials: dict | None = None) -> str:
    """Full pipeline: browse → analyse → generate → excel → automate → update."""
    ctx = AgentContext(url=url, credentials=credentials)

    # Phase 1 — Browse
    browser_result = await _browser_phase(ctx)

    # Phase 2 — Vision (may prompt for creds)
    needs_retry = _vision_phase(ctx, browser_result)
    if needs_retry:
        browser_result = await _browser_phase(ctx)
        _vision_phase(ctx, browser_result)

    # Phase 3 — Generate manual test cases
    _generation_phase(ctx)

    # Phase 4 — Write initial Excel
    _excel_write_phase(ctx)
    log("excel", "Orchestrator", f"Excel saved → {ctx.excel_path}")

    # Phase 5 — Generate Playwright scripts
    _script_phase(ctx)

    # Phase 5b — Export each script to its own .py file
    _script_export_phase(ctx)

    # Phase 6 — Execute scripts
    _execution_phase(ctx)

    # Phase 7 — Update Excel with results
    _excel_update_phase(ctx)

    section("Pipeline Complete")
    log("success", "Orchestrator", f"Excel report  → {ctx.excel_path}")
    log("success", "Orchestrator", f"Scripts folder → {ctx.scripts_dir}")
    return ctx.excel_path


async def run_generate_only(url: str, credentials: dict | None = None) -> str:
    """Browse, analyse, generate test cases, write Excel only (no automation)."""
    ctx = AgentContext(url=url, credentials=credentials)

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


async def run_automate_existing(excel_path: str, url: str,
                                credentials: dict | None = None) -> str:
    """
    Read test cases from an existing Excel, generate scripts,
    execute them, and write results back to the same file.
    """
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


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def _usage():
    print(__doc__)
    sys.exit(1)


async def main():
    args = sys.argv[1:]
    if not args:
        _usage()

    mode = args[0].lower()

    if mode == "full":
        if len(args) < 2:
            _usage()
        url   = args[1]
        creds = {"username": args[2], "password": args[3]} if len(args) >= 4 else None
        await run_full_pipeline(url, creds)

    elif mode == "generate":
        if len(args) < 2:
            _usage()
        url   = args[1]
        creds = {"username": args[2], "password": args[3]} if len(args) >= 4 else None
        await run_generate_only(url, creds)

    elif mode == "automate":
        if len(args) < 3:
            _usage()
        excel = args[1]
        url   = args[2]
        creds = {"username": args[3], "password": args[4]} if len(args) >= 5 else None
        await run_automate_existing(excel, url, creds)

    else:
        _usage()


if __name__ == "__main__":
    asyncio.run(main())