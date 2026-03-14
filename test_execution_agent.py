"""
Agent 6 — TestExecutionAgent
Responsibility: Execute each generated Playwright script in an isolated
subprocess, capture pass/fail, error messages, and timestamps.
"""
import asyncio
import sys
import textwrap
import traceback
from datetime import datetime

from logger import log
from models import AgentContext, AutomationResult, TestCase

NAME = "TestExecutionAgent"
EXEC_TIMEOUT = 60  # seconds per test


async def _run_script_subprocess(script: str, tc_id: str) -> tuple[bool, str]:
    """
    Execute a script string in a fresh Python subprocess.
    Returns (passed: bool, message: str).
    """
    # Wrap script so subprocess can invoke run_test()
    wrapper = textwrap.dedent(f"""
import asyncio, sys

{script}

async def _main():
    try:
        result = await run_test()
        sys.exit(0)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

asyncio.run(_main())
""")

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", wrapper,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=EXEC_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            return False, f"Timeout after {EXEC_TIMEOUT}s"

        if proc.returncode == 0:
            return True, ""
        else:
            err = stderr.decode().strip()
            return False, err[-1000:] if len(err) > 1000 else err   # cap at 1000 chars

    except Exception as e:
        return False, f"Execution error: {traceback.format_exc(limit=3)}"


async def _execute_all(test_cases: list[TestCase]) -> list[AutomationResult]:
    results: list[AutomationResult] = []

    for tc in test_cases:
        if not tc.auto_script:
            log("warning", NAME, f"No script for {tc.test_case_id} — skipping")
            results.append(AutomationResult(
                test_case_id  = tc.test_case_id,
                passed        = False,
                error_message = "No script generated",
                executed_at   = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                script        = "",
            ))
            continue

        log("run", NAME, f"Executing {tc.test_case_id}: {tc.test_case_title[:50]}")
        start = datetime.now()
        passed, error = await _run_script_subprocess(tc.auto_script, tc.test_case_id)
        ts = start.strftime("%Y-%m-%d %H:%M:%S")

        status = "✅ Pass" if passed else "❌ Fail"
        log("success" if passed else "error", NAME,
            f"{tc.test_case_id} → {status}  (executed at {ts})")

        results.append(AutomationResult(
            test_case_id  = tc.test_case_id,
            passed        = passed,
            error_message = error,
            executed_at   = ts,
            script        = tc.auto_script,
        ))

    return results


async def run(ctx: AgentContext) -> list[AutomationResult]:
    """Execute all generated scripts and store results in context."""
    log("agent", NAME, f"Starting execution of {len(ctx.test_cases)} tests")
    results = await _execute_all(ctx.test_cases)
    ctx.automation_results = results

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    log("success", NAME, f"Execution complete — Passed: {passed} | Failed: {failed}")
    return results
