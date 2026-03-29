"""
Central configuration for the Web Test Agent system.
All models, timeouts, paths and column mappings live here.
"""
import os
from pathlib import Path

# ── Groq Models ──────────────────────────────────────────────────────────────
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
TEXT_MODEL   = "llama-3.3-70b-versatile"
GROQ_API_KEY = "YOUR_GROQ_API"
# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# ── Browser ───────────────────────────────────────────────────────────────────
BROWSER_VIEWPORT   = {"width": 1440, "height": 900}
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
PAGE_LOAD_TIMEOUT    = 30_000   # ms
NETWORK_IDLE_TIMEOUT = 10_000   # ms

# ── Execution mode ────────────────────────────────────────────────────────────
# Set HEADLESS=False to watch every test run in a visible browser window.
# Controlled via --headed CLI flag or PLAYWRIGHT_HEADED=1 env var.
HEADLESS: bool = os.getenv("PLAYWRIGHT_HEADED", "0") != "1"

# Slow-motion delay (ms) between each Playwright action in headed mode.
# Makes it easier to follow what the browser is doing.
SLOW_MO: int = int(os.getenv("PLAYWRIGHT_SLOW_MO", "400"))

# ── Excel Column Layout ───────────────────────────────────────────────────────
# (1-indexed, matches openpyxl column numbers)
COL = {
    "id":            1,
    "module":        2,
    "title":         3,
    "description":   4,
    "preconditions": 5,
    "steps":         6,
    "expected":      7,
    "priority":      8,
    "test_type":     9,
    "status":        10,
    "auto_script":   11,   # generated Playwright script
    "auto_result":   12,   # Pass / Fail / Error
    "auto_error":    13,   # error message / traceback snippet
    "executed_at":   14,   # execution timestamp
    "comments":      15,
}

HEADERS = [
    "Test Case ID", "Module", "Test Case Title", "Description",
    "Preconditions", "Test Steps", "Expected Result", "Priority",
    "Test Type", "Status", "Auto Script", "Auto Result",
    "Error / Notes", "Executed At", "Comments",
]

COL_WIDTHS = [14, 20, 35, 40, 30, 50, 40, 12, 18, 14, 60, 14, 40, 22, 30]

# ── Excel Style Colours ───────────────────────────────────────────────────────
COLOUR = {
    "header_bg":  "1F4E79",
    "pass_bg":    "C6EFCE",
    "fail_bg":    "FFC7CE",
    "error_bg":   "FFEB9C",
    "alt_row":    "D6E4F0",
    "high_pri":   "FFE0E0",
    "med_pri":    "FFF3CD",
    "low_pri":    "E8F5E9",
}
