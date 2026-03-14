"""
Agent 7 — ScriptExportAgent
Responsibility: Write each generated Playwright script to its own .py file,
organised under  output/scripts/<run_id>/  with a shared conftest.py,
a pytest runner, and a README so the folder is immediately usable.

Output layout
─────────────
output/
└── scripts/
    └── <run_id>/
        ├── conftest.py
        ├── pytest.ini
        ├── requirements.txt
        ├── README.md
        ├── TC001_page_loads_successfully/
        │   └── test_TC001.py
        ├── TC002_login_with_valid_credentials/
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

# These are the exact import lines the script_generation_agent always emits.
# We strip them from the body so we can re-emit them at the top of the file
# in the correct order alongside pytest.
_IMPORT_LINES_TO_STRIP = {
    "import asyncio",
    "from playwright.async_api import async_playwright, expect",
    "from playwright.async_api import async_playwright",
    "import pytest",
}

# The fixed import block that every exported file must start with
_FILE_IMPORTS = """\
import asyncio
import pytest
from playwright.async_api import async_playwright, expect
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_name(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    return slug[:max_len]


def _make_run_id(ctx: AgentContext) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(ctx.url).hostname or "site"
        host = re.sub(r"^www\.", "", host)
        host = _safe_name(host, 20)
    except Exception:
        host = "site"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{host}_{ts}"


def _build_file(tc: TestCase, url: str) -> str:
    """
    Produce a clean, self-contained pytest file for one test case.

    Structure
    ---------
    1. Module-level docstring  (test metadata)
    2. Imports block           (asyncio + playwright + pytest — always present)
    3. run_test()              (core Playwright logic, stripped of duplicate imports)
    4. pytest entry-point      (test_TC00X async function with @pytest.mark.asyncio)
    5. Standalone __main__     (python test_TC001.py works too)
    """
    raw_script = tc.auto_script.strip()

    # ------------------------------------------------------------------
    # 1. Strip all known import lines from the raw script body so we
    #    never end up with duplicates — we'll emit them ourselves at
    #    the top of the file in the correct order.
    # ------------------------------------------------------------------
    clean_lines = []
    for line in raw_script.splitlines():
        if line.strip() in _IMPORT_LINES_TO_STRIP:
            continue
        clean_lines.append(line)
    # Remove any leading blank lines left behind by the stripped imports
    script_body = "\n".join(clean_lines).lstrip("\n")

    safe_title   = tc.test_case_title.replace('"', "'").replace("\n", " ")
    test_fn_name = f"test_{tc.test_case_id.lower()}"

    # ------------------------------------------------------------------
    # 2. Build the file section by section using a plain list of lines
    #    — no f-string nesting, no textwrap.dedent on dynamic content.
    # ------------------------------------------------------------------
    lines: list[str] = []

    # ── Metadata docstring ────────────────────────────────────────────
    lines += [
        '"""',
        f"Test Case  : {tc.test_case_id}",
        f"Title      : {tc.test_case_title}",
        f"Module     : {tc.module}",
        f"Priority   : {tc.priority}",
        f"Type       : {tc.test_type}",
        f"Description: {tc.description}",
        "",
        "Preconditions:",
        tc.preconditions or "  N/A",
        "",
        "Test Steps:",
        tc.test_steps or "  N/A",
        "",
        "Expected Result:",
        tc.expected_result or "  N/A",
        '"""',
        "",
    ]

    # ── Imports — always the same three lines ─────────────────────────
    lines += [
        "import asyncio",
        "import pytest",
        "from playwright.async_api import async_playwright, expect",
        "",
        "",
    ]

    # ── Core test logic ───────────────────────────────────────────────
    lines += [
        "# ── Core test logic (run standalone or via pytest) " + "─" * 20,
    ]
    lines += script_body.splitlines()
    lines += ["", ""]

    # ── Pytest entry-point ────────────────────────────────────────────
    lines += [
        "# ── Pytest entry-point " + "─" * 47,
        "@pytest.mark.asyncio",
        f"async def {test_fn_name}():",
        f'    """{safe_title}"""',
        "    result = await run_test()",
        "    assert result is True, (",
        f'        f"{tc.test_case_id} — {safe_title} — run_test() did not return True"',
        "    )",
        "",
        "",
    ]

    # ── Standalone runner ─────────────────────────────────────────────
    lines += [
        "# ── Standalone runner " + "─" * 48,
        'if __name__ == "__main__":',
        "    asyncio.run(run_test())",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shared support files
# ---------------------------------------------------------------------------

def _conftest(url: str) -> str:
    return (
        '"""\nShared pytest fixtures for the generated Playwright test suite.\n"""\n'
        "import pytest\n"
        "from playwright.async_api import async_playwright\n"
        "\n"
        "\n"
        f'BASE_URL = "{url}"\n'
        "\n"
        "\n"
        "@pytest.fixture(scope='session')\n"
        "def base_url():\n"
        "    return BASE_URL\n"
        "\n"
        "\n"
        "@pytest.fixture(scope='session')\n"
        "async def browser_context():\n"
        "    async with async_playwright() as p:\n"
        "        browser = await p.chromium.launch(headless=True)\n"
        '        context = await browser.new_context(viewport={"width": 1440, "height": 900})\n'
        "        yield context\n"
        "        await browser.close()\n"
        "\n"
        "\n"
        "@pytest.fixture\n"
        "async def page(browser_context):\n"
        "    page = await browser_context.new_page()\n"
        f'    await page.goto(BASE_URL, wait_until="networkidle", timeout=30000)\n'
        "    yield page\n"
        "    await page.close()\n"
    )


def _pytest_ini() -> str:
    return (
        "[pytest]\n"
        "asyncio_mode = auto\n"
        "testpaths = .\n"
        "python_files = test_*.py\n"
        "python_functions = test_*\n"
        "addopts = -v --tb=short\n"
        "markers =\n"
        "    smoke: quick sanity checks\n"
        "    regression: full regression suite\n"
    )


def _requirements() -> str:
    return (
        "playwright>=1.44.0\n"
        "pytest>=8.0.0\n"
        "pytest-asyncio>=0.23.0\n"
        "pytest-playwright>=0.4.4\n"
    )


def _readme(run_id: str, url: str, test_cases: list[TestCase]) -> str:
    tc_rows = "\n".join(
        f"| {tc.test_case_id} | {tc.module} | {tc.test_case_title} | {tc.priority} |"
        for tc in test_cases
    )
    return (
        "# Generated Playwright Test Suite\n\n"
        f"**Run ID**    : `{run_id}`\n"
        f"**Target URL**: {url}\n"
        f"**Generated** : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"**Tests**     : {len(test_cases)}\n\n"
        "---\n\n"
        "## Setup\n\n"
        "```bash\n"
        "pip install -r requirements.txt\n"
        "playwright install chromium\n"
        "```\n\n"
        "---\n\n"
        "## Running tests\n\n"
        "```bash\n"
        "# All tests\n"
        "pytest .\n\n"
        "# One test file directly\n"
        "pytest TC001_page_loads_successfully/test_TC001.py -v\n\n"
        "# Filter by module keyword\n"
        "pytest -k auth -v\n\n"
        "# Run a file standalone (no pytest needed)\n"
        "python TC001_page_loads_successfully/test_TC001.py\n\n"
        "# HTML report\n"
        "pytest . --html=report.html --self-contained-html\n"
        "```\n\n"
        "---\n\n"
        "## Test inventory\n\n"
        "| ID | Module | Title | Priority |\n"
        "|----|--------|-------|----------|\n"
        f"{tc_rows}\n\n"
        "---\n\n"
        "## Extending a test\n\n"
        "Edit `run_test()` in any file — the pytest wrapper calls it automatically.\n\n"
        "```python\n"
        "async def run_test() -> bool:\n"
        "    async with async_playwright() as p:\n"
        "        browser = await p.chromium.launch(headless=False)  # show browser\n"
        "        ...\n"
        "        await page.screenshot(path='debug.png')            # save screenshot\n"
        "        ...\n"
        "```\n"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_scripts(ctx: AgentContext) -> str:
    """Write one .py file per test case into a timestamped scripts folder."""
    if not ctx.test_cases:
        log("warning", NAME, "No test cases to export")
        return ""

    run_id  = _make_run_id(ctx)
    run_dir = SCRIPTS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Shared files
    (run_dir / "conftest.py").write_text(_conftest(ctx.url), encoding="utf-8")
    (run_dir / "pytest.ini").write_text(_pytest_ini(), encoding="utf-8")
    (run_dir / "requirements.txt").write_text(_requirements(), encoding="utf-8")
    (run_dir / "README.md").write_text(_readme(run_id, ctx.url, ctx.test_cases), encoding="utf-8")

    exported = 0
    for tc in ctx.test_cases:
        if not tc.auto_script:
            log("warning", NAME, f"Skipping {tc.test_case_id} — no script generated")
            continue

        folder_name = f"{tc.test_case_id}_{_safe_name(tc.test_case_title)}"
        tc_dir      = run_dir / folder_name
        tc_dir.mkdir(exist_ok=True)

        file_path = tc_dir / f"test_{tc.test_case_id}.py"
        content   = _build_file(tc, ctx.url)

        # Sanity-check: compile before writing
        try:
            compile(content, str(file_path), "exec")
        except SyntaxError as e:
            log("error", NAME, f"Syntax error in {tc.test_case_id} — {e}. Skipping.")
            continue

        file_path.write_text(content, encoding="utf-8")
        log("info", NAME,
            f"Exported {tc.test_case_id} → {folder_name}/test_{tc.test_case_id}.py")
        exported += 1

    log("success", NAME, f"Exported {exported}/{len(ctx.test_cases)} scripts → {run_dir}")
    return str(run_dir)


def run(ctx: AgentContext) -> str:
    """Agent entry-point called by the orchestrator."""
    log("agent", NAME, f"Exporting {len(ctx.test_cases)} scripts to individual files")
    scripts_dir   = export_scripts(ctx)
    ctx.scripts_dir = scripts_dir
    return scripts_dir