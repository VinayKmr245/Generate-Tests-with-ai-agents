# Web Test Agent — Multi-Agent System

A modular, multi-agent Python application that:
1. **Browses** any URL with a headless browser
2. **Analyses** the UI via Groq Vision (LLaMA-4 Scout)
3. **Generates** manual test cases via Groq LLM (LLaMA-3.3-70B)
4. **Writes** a formatted Excel workbook
5. **Generates** async Playwright scripts for each test case
6. **Executes** scripts in isolated subprocesses
7. **Updates** the same Excel with Pass/Fail, error details & timestamps

---

## Project Structure

```
web_test_agent/
│
├── orchestrator.py              # Pipeline entry-point (CLI)
│
├── agents/
│   ├── browser_agent.py         # Agent 1 — navigate, screenshot, login
│   ├── vision_agent.py          # Agent 2 — Groq Vision analysis
│   ├── test_generation_agent.py # Agent 3 — LLM manual test case generation
│   ├── excel_agent.py           # Agent 4 — read/write/update Excel
│   ├── script_generation_agent.py # Agent 5 — LLM Playwright script generation
│   └── test_execution_agent.py  # Agent 6 — execute scripts, capture results
│
├── core/
│   ├── config.py                # All constants (models, paths, column map)
│   └── models.py                # Shared dataclasses (TestCase, PageAnalysis…)
│
├── utils/
│   └── logger.py                # Timestamped console logger
│
├── output/                      # Generated Excel reports (auto-created)
└── requirements.txt
```

---

## Setup

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install Playwright browser
playwright install chromium

# 3. Set Groq API key
export GROQ_API_KEY="gsk_your_key_here"
```

Get a free key at https://console.groq.com

---

## Usage

### Mode 1 — Full pipeline (browse → generate → automate → update Excel)

```bash
# Public page
python orchestrator.py full https://example.com

# Page requiring login (credentials as args)
python orchestrator.py full https://app.example.com admin@test.com mypassword

# Page requiring login (will prompt interactively)
python orchestrator.py full https://app.example.com
```

### Mode 2 — Generate only (no automation)

```bash
python orchestrator.py generate https://example.com
python orchestrator.py generate https://app.example.com user@x.com pass123
```

### Mode 3 — Automate an existing Excel

Read previously-generated test cases, run them, and write results back:

```bash
python orchestrator.py automate output/test_cases_My_App_20260314.xlsx https://app.example.com
python orchestrator.py automate output/test_cases_My_App_20260314.xlsx https://app.example.com user pass
```

---

## Excel Workbook Structure

| Sheet | Contents |
|-------|----------|
| **Summary** | URL, timestamp, total/priority/pass/fail counts |
| **Test Cases** | All test cases — every column |
| **\<Module\>** | One sheet per module for quick navigation |

### Columns

| Column | Description |
|--------|-------------|
| Test Case ID | TC001, TC002 … |
| Module | Feature area |
| Test Case Title | Concise name |
| Description | What is validated |
| Preconditions | Setup required |
| Test Steps | Numbered steps |
| Expected Result | Observable pass criterion |
| Priority | High / Medium / Low (colour-coded) |
| Test Type | Functional / UI / Negative / Integration / Accessibility |
| Status | Not Executed → Executed |
| **Auto Script** | Generated Playwright Python code |
| **Auto Result** | ✅ Pass / ❌ Fail / Error (colour-coded) |
| **Error / Notes** | Failure message or traceback snippet |
| **Executed At** | ISO timestamp of script run |
| Comments | Free text |

---

## Agent Data Flow

```
CLI args
   │
   ▼
AgentContext (shared state)
   │
   ├─► BrowserAgent     → screenshot_b64, post-login screenshot
   ├─► VisionAgent      → PageAnalysis (components, login info)
   ├─► TestGenAgent     → List[TestCase]
   ├─► ExcelAgent       → writes .xlsx, returns path
   ├─► ScriptGenAgent   → TestCase.auto_script populated
   ├─► ExecAgent        → List[AutomationResult]
   └─► ExcelAgent       → updates Auto Result, Error, Executed At columns
```

Each agent is independently importable and testable.  The orchestrator
simply calls `agent.run(ctx, ...)` in sequence.
