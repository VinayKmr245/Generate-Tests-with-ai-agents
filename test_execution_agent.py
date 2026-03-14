"""
Agent 6 — TestExecutionAgent
Responsibility:
  • Execute each generated Playwright script from its exported .py file
    (or fall back to running the script string directly if no file exists).
  • Capture: pass/fail, error message, duration (ms), execution timestamp.
  • Store AutomationResult objects on AgentContext for ExcelAgent to consume.

Execution strategy
──────────────────
1. If ctx.scripts_dir is set, run the exported test_TC00X.py files via
   `python test_TCXXX.py` inside the correct subfolder — this exercises the
   real files the user will build on.
2. Fallback: if no scripts_dir, execute tc.auto_script as an inline string
   in a subprocess (original behaviour).

Both paths run each test in its own subprocess with a configurable timeout.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import os
import textwrap
import traceback
from datetime import datetime

from logger import log
from models import AgentContext, AutomationResult, TestCase

NAME         = "TestExecutionAgent"
EXEC_TIMEOUT = 60   # seconds per test


# ─────────────────────────────────────────────────────────────────────────────
# Low-level subprocess runner
# ─────────────────────────────────────────────────────────────────────────────

async def _run_file(file_path: str, tc_id: str) -> tuple[bool, str, int]:
    """
    Run a test_TC00X.py file directly.
    Returns (passed, error_message, duration_ms).
    """
    start = datetime.now()
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(file_path).parent),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=EXEC_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            duration = int((datetime.now() - start).total_seconds() * 1000)
            return False, f"Timeout after {EXEC_TIMEOUT}s", duration

        duration = int((datetime.now() - start).total_seconds() * 1000)
        if proc.returncode == 0:
            return True, "", duration
        else:
            err = stderr.decode(errors="replace").strip()
            # Trim to last 1200 chars to keep the most relevant part
            err = err[-1200:] if len(err) > 1200 else err
            return False, err, duration

    except Exception:
        duration = int((datetime.now() - start).total_seconds() * 1000)
        return False, traceback.format_exc(limit=4), duration


async def _run_inline(script: str, tc_id: str) -> tuple[bool, str, int]:
    """
    Run a raw script string as an inline subprocess (fallback).
    Returns (passed, error_message, duration_ms).
    """
    wrapper = textwrap.dedent(f"""
import asyncio, sys
{script}

async def _main():
    try:
        await run_test()
        sys.exit(0)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

asyncio.run(_main())
""")
    start = datetime.now()
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
            duration = int((datetime.now() - start).total_seconds() * 1000)
            return False, f"Timeout after {EXEC_TIMEOUT}s", duration

        duration = int((datetime.now() - start).total_seconds() * 1000)
        if proc.returncode == 0:
            return True, "", duration
        else:
            err = stderr.decode(errors="replace").strip()
            err = err[-1200:] if len(err) > 1200 else err
            return False, err, duration

    except Exception:
        duration = int((datetime.now() - start).total_seconds() * 1000)
        return False, traceback.format_exc(limit=4), duration


def _find_script_file(scripts_dir: str, tc_id: str) -> str | None:
    """
    Locate  output/scripts/<run_id>/TC001_*/test_TC001.py  for a given tc_id.
    """
    base = Path(scripts_dir)
    for folder in base.iterdir():
        if not folder.is_dir():
            continue
        if not folder.name.upper().startswith(tc_id.upper()):
            continue
        candidate = folder / f"test_{tc_id}.py"
        if candidate.exists():
            return str(candidate)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Main execution loop
# ─────────────────────────────────────────────────────────────────────────────

async def _execute_all(
    test_cases: list[TestCase],
    scripts_dir: str = "",
) -> list[AutomationResult]:

    results: list[AutomationResult] = []
    total   = len(test_cases)

    for idx, tc in enumerate(test_cases, 1):
        log("run", NAME,
            f"[{idx}/{total}] Running {tc.test_case_id}: {tc.test_case_title[:55]}")

        if not tc.auto_script:
            log("warning", NAME, f"  No script — skipping {tc.test_case_id}")
            results.append(AutomationResult(
                test_case_id  = tc.test_case_id,
                passed        = False,
                error_message = "No script was generated for this test case",
                executed_at   = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                duration_ms   = 0,
                script        = "",
            ))
            continue

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Strategy 1: run exported file
        if scripts_dir:
            file_path = _find_script_file(scripts_dir, tc.test_case_id)
            if file_path:
                passed, error, duration = await _run_file(file_path, tc.test_case_id)
                source = f"file:{Path(file_path).name}"
            else:
                log("warning", NAME,
                    f"  Script file not found for {tc.test_case_id} — using inline")
                passed, error, duration = await _run_inline(tc.auto_script, tc.test_case_id)
                source = "inline"
        else:
            # Strategy 2: inline fallback
            passed, error, duration = await _run_inline(tc.auto_script, tc.test_case_id)
            source = "inline"

        icon = "✅" if passed else "❌"
        log(
            "success" if passed else "error",
            NAME,
            f"  {icon} {tc.test_case_id} → {'PASS' if passed else 'FAIL'} "
            f"({duration}ms) [{source}]",
        )
        if not passed and error:
            # Print first 200 chars of error inline for quick diagnosis
            snippet = error.splitlines()[-1] if error.splitlines() else error
            log("error", NAME, f"     └─ {snippet[:200]}")

        results.append(AutomationResult(
            test_case_id  = tc.test_case_id,
            passed        = passed,
            error_message = error,
            executed_at   = ts,
            duration_ms   = duration,
            script        = tc.auto_script,
        ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def run(ctx: AgentContext) -> list[AutomationResult]:
    """Execute all test cases and store results on AgentContext."""
    log("agent", NAME,
        f"Executing {len(ctx.test_cases)} tests  "
        f"[scripts_dir={'set' if ctx.scripts_dir else 'not set'}]")

    # Guard: asyncio.run() can't be called inside an already-running loop
    try:
        loop = asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                _execute_all(ctx.test_cases, ctx.scripts_dir)
            )
            results = future.result()
    except RuntimeError:
        results = asyncio.run(_execute_all(ctx.test_cases, ctx.scripts_dir))

    ctx.automation_results = results

    passed   = sum(1 for r in results if r.passed)
    failed   = len(results) - passed
    avg_ms   = int(sum(r.duration_ms for r in results) / max(len(results), 1))

    log("success", NAME,
        f"Done — ✅ Passed: {passed}  ❌ Failed: {failed}  "
        f"⏱ Avg: {avg_ms}ms")
    return results