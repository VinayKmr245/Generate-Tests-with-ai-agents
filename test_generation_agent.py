"""
Agent 3 — TestGenerationAgent
Responsibility: Generate manual test cases from PageAnalysis objects using Groq LLM.
Outputs: List[TestCase] attached to AgentContext.

Module override
───────────────
If ctx.module_name is set (e.g. "User Profile"), every generated test case will
have its module field forced to that value AND the LLM prompt will be scoped
specifically to that feature area, producing much more targeted test cases.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import re

from groq import Groq

from config import GROQ_API_KEY, TEXT_MODEL
from logger import log
from models import AgentContext, PageAnalysis, TestCase

NAME   = "TestGenerationAgent"
client = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = """You are a senior QA engineer. Generate thorough manual test cases
for web pages. Return ONLY a valid JSON array — no markdown, no commentary."""


def _build_prompt(analysis: PageAnalysis, module_name: str | None = None) -> str:
    components_json = json.dumps(
        [
            {
                "type":        c.type,
                "label":       c.label,
                "purpose":     c.purpose,
                "location":    c.location,
                "selector":    c.selector,
                "dom_section": c.dom_section,
                "is_required": c.is_required,
                "input_type":  c.input_type,
                "placeholder": c.placeholder,
            }
            for c in analysis.components
        ],
        indent=2,
    )
    ctx_label = "after successful login" if analysis.post_login else "before login / public view"

    # When a module name is provided, tighten the prompt focus
    if module_name:
        module_instruction = f"""
IMPORTANT: You are generating test cases SPECIFICALLY for the "{module_name}" module/feature.
- Every test case's "module" field MUST be exactly "{module_name}"
- Focus ALL test cases on "{module_name}" functionality visible on this screen
- Do NOT generate test cases for unrelated features (navigation, footer, etc.)
"""
        coverage = f"""
Coverage required for "{module_name}":
- All CRUD operations visible (create, read, update, delete if present)
- Form field validation (required fields, format checks, length limits)
- Happy-path functional flows specific to {module_name}
- Negative / edge-case tests (invalid input, boundary values, empty fields)
- UI / UX validation (labels, error messages, success messages, layout)
- Permission / access control if relevant
- Data persistence (save and re-open to verify data was saved)
"""
    else:
        module_instruction = ""
        coverage = """
Coverage required:
- Happy-path functional tests for every component
- Negative / edge-case tests (empty fields, invalid input, boundary values)
- UI / UX validation (labels, placeholders, error messages, layout)
- Navigation / flow tests
- Accessibility basics (keyboard navigation, visible focus)
"""

    return f"""Generate 10–15 comprehensive manual test cases for the following web page.
{module_instruction}
URL: {analysis.url}
Page Title: {analysis.page_title}
Context: {ctx_label}
Description: {analysis.page_description}

Detected UI Components:
{components_json}
{coverage}
Return ONLY a JSON array where every item has these exact keys:
[
  {{
    "test_case_id": "TC001",
    "module": "{module_name or 'module name matching the feature area'}",
    "test_case_title": "concise title",
    "description": "what is being validated",
    "preconditions": "any required setup",
    "test_steps": "1. First step\\n2. Second step\\n3. Third step",
    "expected_result": "observable outcome that proves pass",
    "priority": "High | Medium | Low",
    "test_type": "Functional | UI | Negative | Integration | Accessibility"
  }}
]"""


def _parse_test_cases(raw: str, id_offset: int = 0,
                      module_override: str | None = None) -> list[TestCase]:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        log("error", NAME, "Failed to parse JSON from LLM — returning empty list")
        return []

    test_cases = []
    for i, item in enumerate(items, start=id_offset + 1):
        # module_override wins over whatever the LLM returned
        module = module_override or item.get("module", "General")
        tc = TestCase(
            test_case_id    = f"TC{i:03d}",
            module          = module,
            test_case_title = item.get("test_case_title", ""),
            description     = item.get("description", ""),
            preconditions   = item.get("preconditions", ""),
            test_steps      = item.get("test_steps", ""),
            expected_result = item.get("expected_result", ""),
            priority        = item.get("priority", "Medium"),
            test_type       = item.get("test_type", "Functional"),
        )
        test_cases.append(tc)
    return test_cases


def generate_for_analysis(analysis: PageAnalysis, id_offset: int = 0,
                           module_name: str | None = None) -> list[TestCase]:
    label = f"'{analysis.page_title}'"
    if module_name:
        label += f" [module: {module_name}]"
    log("llm", NAME, f"Generating test cases for {label}")

    response = client.chat.completions.create(
        model    = TEXT_MODEL,
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": _build_prompt(analysis, module_name)},
        ],
        max_tokens  = 4000,
        temperature = 0.3,
    )

    raw   = response.choices[0].message.content
    cases = _parse_test_cases(raw, id_offset, module_override=module_name)
    log("success", NAME, f"Generated {len(cases)} test cases"
        + (f" for module '{module_name}'" if module_name else ""))
    return cases


def run(ctx: AgentContext) -> list[TestCase]:
    """Generate test cases for every PageAnalysis in context."""
    # Only use the post-login analysis when a module is targeted —
    # no point generating login-page tests when you want User Profile tests
    analyses = ctx.page_analyses
    if ctx.module_name:
        post_login_analyses = [a for a in analyses if a.post_login]
        if post_login_analyses:
            log("info", NAME,
                f"Module '{ctx.module_name}' specified — using only post-login analysis")
            analyses = post_login_analyses

    all_cases: list[TestCase] = []
    for analysis in analyses:
        cases = generate_for_analysis(
            analysis,
            id_offset   = len(all_cases),
            module_name = ctx.module_name,
        )
        all_cases.extend(cases)

    ctx.test_cases = all_cases
    log("success", NAME, f"Total test cases generated: {len(all_cases)}")
    return all_cases
