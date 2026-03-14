"""
Agent 3 — TestGenerationAgent
Responsibility: Generate manual test cases from PageAnalysis objects using Groq LLM.
Outputs: List[TestCase] attached to AgentContext.
"""
import json
import re

from config import GROQ_API_KEY, TEXT_MODEL
from groq import Groq
from logger import log
from models import AgentContext, PageAnalysis, TestCase

NAME   = "TestGenerationAgent"
client = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = """You are a senior QA engineer. Generate thorough manual test cases
for web pages. Return ONLY a valid JSON array — no markdown, no commentary."""


def _build_prompt(analysis: PageAnalysis) -> str:
    components_json = json.dumps(
        [{"type": c.type, "label": c.label, "purpose": c.purpose, "location": c.location}
         for c in analysis.components],
        indent=2,
    )
    ctx = "after successful login" if analysis.post_login else "before login / public view"

    return f"""Generate 10–15 comprehensive manual test cases for the following web page.

URL: {analysis.url}
Page Title: {analysis.page_title}
Context: {ctx}
Description: {analysis.page_description}

Detected UI Components:
{components_json}

Coverage required:
- Happy-path functional tests for every component
- Negative / edge-case tests (empty fields, invalid input, boundary values)
- UI / UX validation (labels, placeholders, error messages, layout)
- Navigation / flow tests
- Accessibility basics (keyboard navigation, visible focus)

Return ONLY a JSON array where every item has these exact keys:
[
  {{
    "test_case_id": "TC001",
    "module": "module name matching the feature area",
    "test_case_title": "concise title",
    "description": "what is being validated",
    "preconditions": "any required setup",
    "test_steps": "1. First step\\n2. Second step\\n3. Third step",
    "expected_result": "observable outcome that proves pass",
    "priority": "High | Medium | Low",
    "test_type": "Functional | UI | Negative | Integration | Accessibility"
  }}
]"""


def _parse_test_cases(raw: str, id_offset: int = 0) -> list[TestCase]:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        log("error", NAME, "Failed to parse JSON from LLM — returning empty list")
        return []

    test_cases = []
    for i, item in enumerate(items, start=id_offset + 1):
        tc = TestCase(
            test_case_id    = f"TC{i:03d}",
            module          = item.get("module", "General"),
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


def generate_for_analysis(analysis: PageAnalysis, id_offset: int = 0) -> list[TestCase]:
    log("llm", NAME, f"Generating test cases for '{analysis.page_title}'")

    response = client.chat.completions.create(
        model    = TEXT_MODEL,
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": _build_prompt(analysis)},
        ],
        max_tokens  = 4000,
        temperature = 0.3,
    )

    raw   = response.choices[0].message.content
    cases = _parse_test_cases(raw, id_offset)
    log("success", NAME, f"Generated {len(cases)} test cases")
    return cases


def run(ctx: AgentContext) -> list[TestCase]:
    """Generate test cases for every PageAnalysis in context."""
    all_cases: list[TestCase] = []

    for analysis in ctx.page_analyses:
        cases = generate_for_analysis(analysis, id_offset=len(all_cases))
        all_cases.extend(cases)

    ctx.test_cases = all_cases
    log("success", NAME, f"Total test cases generated: {len(all_cases)}")
    return all_cases
