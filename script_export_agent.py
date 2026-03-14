"""
Agent 7 — ScriptExportAgent
Responsibility: Write each generated Playwright script to its own .py file,
organised under  output/scripts/<run_id>/  with a shared conftest.py,
a pytest runner, and a README so the folder is immediately usable.

Output layout
─────────────
output/
└── scripts/
    └── <run_id>/                    ← e.g. wikipedia_20260314_153012
        ├── conftest.py              ← shared fixtures (base_url, browser)
        ├── pytest.ini               ← pytest settings
        ├── requirements.txt         ← playwright + pytest pins
        ├── README.md                ← how to run
        ├── TC001_page_load/
        │   └── test_TC001.py
        ├── TC002_navigation_links/
        │   └── test_TC002.py
        └── ...
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import re
import textwrap
from datetime import datetime

from config import OUTPUT_DIR
from logger import log, section
from models import AgentContext, TestCase

NAME = "ScriptExportAgent"

SCRIPTS_ROOT = OUTPUT_DIR / "scripts"
SCRIPTS_ROOT.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_name(text: str, max_len: int = 40) -> str:
    """Convert arbitrary text into a safe directory / file name fragment."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    return slug[:max_len]


def _make_run_id(ctx: AgentContext) -> str:
    """Derive a human-readable run identifier from the URL + timestamp."""
    try:
        from urllib.parse import urlparse
        host = urlparse(ctx.url).hostname or "site"
        host = re.sub(r"^www\.", "", host)
        host = _safe_name(host, 20)
    except Exception:
        host = "site"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{host}_{ts}"


def _wrap_as_pytest(tc: TestCase, url: str) -> str:
    """
    Convert a raw  async def run_test()  script into a proper pytest test file.
    The file:
      - keeps the original run_test() logic intact
      - adds a  test_<id>()  pytest-compatible async wrapper
      - includes metadata as a module-level docstring
    """
    script = tc.auto_script.strip()

    # If script already looks like a pytest file, return as-is
    if "def test_" in script and "pytest" in script:
        return script

    # Determine which imports are missing
    has_asyncio    = "import asyncio"            in script
    has_playwright = "from playwright.async_api" in script

    extra_imports = []
    if not has_asyncio:
        extra_imports.append("import asyncio")
    if not has_playwright:
        extra_imports.append("from playwright.async_api import async_playwright, expect")
    extra_imports.append("import pytest")

    # Strip duplicated import lines from the script body
    clean_lines = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped in ("import asyncio",
                        "import pytest",
                        "from playwright.async_api import async_playwright, expect",
                        "from playwright.async_api import async_playwright"):
            continue
        clean_lines.append(line)
    script_body = "\n".join(clean_lines).strip()

    safe_title   = tc.test_case_title.replace('"', "'")
    test_fn_name = f"test_{tc.test_case_id.lower()}"
    imports_str  = "\n".join(extra_imports)

    parts = []

    # 1. Metadata docstring
    parts.append('"""')
    parts.append(f"Test Case  : {tc.test_case_id}")
    parts.append(f"Title      : {tc.test_case_title}")
    parts.append(f"Module     : {tc.module}")
    parts.append(f"Priority   : {tc.priority}")
    parts.append(f"Type       : {tc.test_type}")
    parts.append(f"Description: {tc.description}")
    parts.append("")
    parts.append("Preconditions:")
    parts.append(tc.preconditions or "  N/A")
    parts.append("")
    parts.append("Test Steps:")
    parts.append(tc.test_steps or "  N/A")
    parts.append("")
    parts.append("Expected Result:")
    parts.append(tc.expected_result or "  N/A")
    parts.append('"""')
    parts.append("")

    # 2. Imports
    parts.append(imports_str)
    parts.append("")
    parts.append("")

    # 3. Core test logic separator + script body
    parts.append("# ── Core test logic (run standalone or via pytest) " + "─" * 20)
    parts.append(script_body)
    parts.append("")
    parts.append("")

    # 4. Pytest entry-point
    parts.append("# ── Pytest entry-point " + "─" * 47)
    parts.append("@pytest.mark.asyncio")
    parts.append(f"async def {test_fn_name}():")
    parts.append(f'    """{safe_title}"""')
    parts.append("    result = await run_test()")
    parts.append("    assert result is True, (")
    parts.append(f'        f"{tc.test_case_id} — {safe_title} — run_test() did not return True"')
    parts.append("    )")
    parts.append("")
    parts.append("")

    # 5. Standalone runner
    parts.append("# ── Standalone runner " + "─" * 48)
    parts.append('if __name__ == "__main__":')
    parts.append("    asyncio.run(run_test())")
    parts.append("")

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Shared support files
# ─────────────────────────────────────────────────────────────────────────────

def _conftest(url: str) -> str:
    return textwrap.dedent(f"""\
        \"\"\"
        Shared pytest fixtures for the generated Playwright test suite.
        \"\"\"
        import pytest
        from playwright.async_api import async_playwright


        BASE_URL = "{url}"


        @pytest.fixture(scope="session")
        def base_url():
            return BASE_URL


        @pytest.fixture(scope="session")
        async def browser_context():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={{"width": 1440, "height": 900}}
                )
                yield context
                await browser.close()


        @pytest.fixture
        async def page(browser_context):
            page = await browser_context.new_page()
            await page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
            yield page
            await page.close()
    """)


def _pytest_ini(run_id: str) -> str:
    return textwrap.dedent(f"""\
        [pytest]
        asyncio_mode = auto
        testpaths = .
        python_files = test_*.py
        python_functions = test_*
        addopts = -v --tb=short
        markers =
            smoke: quick sanity checks
            regression: full regression suite
    """)


def _requirements() -> str:
    return textwrap.dedent("""\
        playwright>=1.44.0
        pytest>=8.0.0
        pytest-asyncio>=0.23.0
        pytest-playwright>=0.4.4
    """)


def _readme(run_id: str, url: str, test_cases: list[TestCase]) -> str:
    tc_list = "\n".join(
        f"| {tc.test_case_id} | {tc.module} | {tc.test_case_title} | {tc.priority} |"
        for tc in test_cases
    )
    return textwrap.dedent(f"""\
        # Generated Playwright Test Suite

        **Run ID** : `{run_id}`
        **Target** : {url}
        **Generated** : {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        **Tests** : {len(test_cases)}

        ---

        ## Setup

        ```bash
        pip install -r requirements.txt
        playwright install chromium
        ```

        ---

        ## Running tests

        ```bash
        # Run all tests
        pytest .

        # Run a single test file
        pytest TC001_page_load/test_TC001.py -v

        # Run by module
        pytest -k "auth" -v

        # Run by priority
        pytest -k "TC001 or TC002 or TC003" -v

        # Run with HTML report
        pytest . --html=report.html --self-contained-html
        ```

        ---

        ## Test inventory

        | ID | Module | Title | Priority |
        |----|--------|-------|----------|
        {tc_list}

        ---

        ## Structure

        Each test folder contains a single `test_<ID>.py` file.  
        Every file has:
        - A module-level docstring with full test case metadata
        - `run_test()` — the core async Playwright logic (edit this)
        - `test_<id>()` — the pytest entry-point (do not rename)
        - A standalone `__main__` block so you can run the file directly

        ---

        ## Extending a test

        Open the file and edit `run_test()`.  
        The pytest wrapper calls `run_test()` automatically — no other changes needed.

        ```python
        # Example: add a screenshot on failure
        async def run_test() -> bool:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False)   # show browser
                ...
                await page.screenshot(path="debug.png")             # save screenshot
                ...
        ```
    """)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def export_scripts(ctx: AgentContext) -> str:
    """
    Write one .py file per test case into a timestamped scripts folder.
    Returns the path to the folder.
    """
    if not ctx.test_cases:
        log("warning", NAME, "No test cases to export")
        return ""

    run_id   = _make_run_id(ctx)
    run_dir  = SCRIPTS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # ── Shared files ──────────────────────────────────────────────────────────
    (run_dir / "conftest.py").write_text(_conftest(ctx.url))
    (run_dir / "pytest.ini").write_text(_pytest_ini(run_id))
    (run_dir / "requirements.txt").write_text(_requirements())
    (run_dir / "README.md").write_text(_readme(run_id, ctx.url, ctx.test_cases))

    # ── Per-test files ────────────────────────────────────────────────────────
    exported = 0
    for tc in ctx.test_cases:
        if not tc.auto_script:
            log("warning", NAME, f"Skipping {tc.test_case_id} — no script generated")
            continue

        # Folder name:  TC001_page_loads_successfully/
        folder_name = f"{tc.test_case_id}_{_safe_name(tc.test_case_title)}"
        tc_dir = run_dir / folder_name
        tc_dir.mkdir(exist_ok=True)

        file_path = tc_dir / f"test_{tc.test_case_id}.py"
        pytest_script = _wrap_as_pytest(tc, ctx.url)
        file_path.write_text(pytest_script, encoding="utf-8")

        log("info", NAME,
            f"Exported {tc.test_case_id} → {folder_name}/test_{tc.test_case_id}.py")
        exported += 1

    log("success", NAME,
        f"Exported {exported} scripts → {run_dir}")
    return str(run_dir)


def run(ctx: AgentContext) -> str:
    """Agent entry-point called by the orchestrator."""
    section_label = f"Agent 7 — Script Export Agent"
    log("agent", NAME, f"Exporting {len(ctx.test_cases)} scripts to individual files")
    scripts_dir = export_scripts(ctx)
    ctx.scripts_dir = scripts_dir      # store on context for orchestrator to report
    return scripts_dir
