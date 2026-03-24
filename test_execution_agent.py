"""
Agent 6 — TestExecutionAgent
Responsibility:
  • Execute each generated Playwright script from its exported .py file
    (or inline fallback).
  • Capture: pass/fail, error message, duration (ms), execution timestamp.
  • Honour ctx.headed / ctx.slow_mo — when headed=True each test opens a
    visible browser window so you can watch every action live.

Headed mode
───────────
When ctx.headed is True:
  • Tests run ONE AT A TIME (no parallelism) so windows don't pile up
  • The PLAYWRIGHT_HEADED=1 env var is passed to each subprocess so the
    script's own `headless=False` launch arg is respected
  • ctx.slow_mo controls the delay (ms) between actions (default 400ms)
    — slows the browser enough to follow what is happening
  • Timeout is automatically extended to 120s (from 60s) to accommodate
    the slower pace
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

NAME              = "TestExecutionAgent"
EXEC_TIMEOUT      = 60    # seconds — headless
EXEC_TIMEOUT_HEADED = 120  # seconds — headed (slower due to slow_mo + visible rendering)


# ─────────────────────────────────────────────────────────────────────────────
# Subprocess environment builder
# ─────────────────────────────────────────────────────────────────────────────

def _make_env(headed: bool, slow_mo: int) -> dict:
    """Build the subprocess environment dict with Playwright settings injected."""
    env = os.environ.copy()
    if headed:
        env["PLAYWRIGHT_HEADED"]  = "1"
        env["PLAYWRIGHT_SLOW_MO"] = str(slow_mo)
    else:
        env.pop("PLAYWRIGHT_HEADED",  None)
        env.pop("PLAYWRIGHT_SLOW_MO", None)
    return env


# ─────────────────────────────────────────────────────────────────────────────
# Low-level runners
# ─────────────────────────────────────────────────────────────────────────────

async def _run_file(
    file_path: str,
    tc_id: str,
    headed: bool = False,
    slow_mo: int = 0,
) -> tuple[bool, str, int]:
    """
    Run a test_TC00X.py file in its own subfolder.
    Returns (passed, error_message, duration_ms).
    """
    timeout = EXEC_TIMEOUT_HEADED if headed else EXEC_TIMEOUT
    env     = _make_env(headed, slow_mo)
    start   = datetime.now()

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, file_path,
            stdout = asyncio.subprocess.PIPE,
            stderr = asyncio.subprocess.PIPE,
            cwd    = str(Path(file_path).parent),
            env    = env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            duration = int((datetime.now() - start).total_seconds() * 1000)
            return False, f"Timeout after {timeout}s", duration

        duration = int((datetime.now() - start).total_seconds() * 1000)
        if proc.returncode == 0:
            return True, "", duration
        err = stderr.decode(errors="replace").strip()
        return False, err[-1200:] if len(err) > 1200 else err, duration

    except Exception:
        duration = int((datetime.now() - start).total_seconds() * 1000)
        return False, traceback.format_exc(limit=4), duration


async def _run_inline(
    script: str,
    tc_id: str,
    headed: bool = False,
    slow_mo: int = 0,
) -> tuple[bool, str, int]:
    """
    Run a raw script string in a subprocess (fallback when no file exists).
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
    timeout = EXEC_TIMEOUT_HEADED if headed else EXEC_TIMEOUT
    env     = _make_env(headed, slow_mo)
    start   = datetime.now()

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", wrapper,
            stdout = asyncio.subprocess.PIPE,
            stderr = asyncio.subprocess.PIPE,
            env    = env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            duration = int((datetime.now() - start).total_seconds() * 1000)
            return False, f"Timeout after {timeout}s", duration

        duration = int((datetime.now() - start).total_seconds() * 1000)
        if proc.returncode == 0:
            return True, "", duration
        err = stderr.decode(errors="replace").strip()
        return False, err[-1200:] if len(err) > 1200 else err, duration

    except Exception:
        duration = int((datetime.now() - start).total_seconds() * 1000)
        return False, traceback.format_exc(limit=4), duration


def _find_script_file(scripts_dir: str, tc_id: str) -> str | None:
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
    headed: bool = False,
    slow_mo: int = 0,
) -> list[AutomationResult]:

    results: list[AutomationResult] = []
    total   = len(test_cases)

    if headed:
        log("info", NAME,
            f"Headed mode ON — browsers will be visible  "
            f"[slow_mo={slow_mo}ms, timeout={EXEC_TIMEOUT_HEADED}s]")
        log("info", NAME,
            "Tests run sequentially so browser windows don't stack up")

    for idx, tc in enumerate(test_cases, 1):
        log("run", NAME,
            f"[{idx}/{total}] {'👁  ' if headed else ''}Running "
            f"{tc.test_case_id}: {tc.test_case_title[:55]}")

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

        if scripts_dir:
            file_path = _find_script_file(scripts_dir, tc.test_case_id)
            if file_path:
                passed, error, duration = await _run_file(
                    file_path, tc.test_case_id, headed=headed, slow_mo=slow_mo
                )
                source = f"file:{Path(file_path).name}"
            else:
                log("warning", NAME,
                    f"  Script file not found for {tc.test_case_id} — using inline")
                passed, error, duration = await _run_inline(
                    tc.auto_script, tc.test_case_id, headed=headed, slow_mo=slow_mo
                )
                source = "inline"
        else:
            passed, error, duration = await _run_inline(
                tc.auto_script, tc.test_case_id, headed=headed, slow_mo=slow_mo
            )
            source = "inline"

        icon = "✅" if passed else "❌"
        log(
            "success" if passed else "error",
            NAME,
            f"  {icon} {tc.test_case_id} → {'PASS' if passed else 'FAIL'} "
            f"({duration}ms) [{source}]",
        )
        if not passed and error:
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

        # In headed mode pause briefly between tests so the user can see
        # what just finished before the next window opens
        if headed and idx < total:
            await asyncio.sleep(0.8)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def run(ctx: AgentContext) -> list[AutomationResult]:
    """Execute all test cases and store results on AgentContext."""
    headed  = getattr(ctx, "headed",  False)
    slow_mo = getattr(ctx, "slow_mo", 400) if headed else 0

    log("agent", NAME,
        f"Executing {len(ctx.test_cases)} tests  "
        f"[{'👁  headed' if headed else 'headless'}"
        + (f", slow_mo={slow_mo}ms" if slow_mo else "") + "]")

    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                _execute_all(ctx.test_cases, ctx.scripts_dir, headed, slow_mo)
            )
            results = future.result()
    except RuntimeError:
        results = asyncio.run(
            _execute_all(ctx.test_cases, ctx.scripts_dir, headed, slow_mo)
        )

    ctx.automation_results = results
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    avg_ms = int(sum(r.duration_ms for r in results) / max(len(results), 1))

    log("success", NAME,
        f"Done — ✅ Passed: {passed}  ❌ Failed: {failed}  ⏱ Avg: {avg_ms}ms")
    return results
    return results
