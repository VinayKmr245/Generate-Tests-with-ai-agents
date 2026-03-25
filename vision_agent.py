"""
Agent 2 — VisionAgent  (DOM-first, vision-assisted)
====================================================

Previous approach: screenshot → Groq vision model → infer UI elements
New approach:      live DOM scrape (via BrowserAgent) → structured elements
                   → Groq LLM enriches purpose & page_description (text only)

Why this is better
──────────────────
• 100% accurate element detection  — real selectors, types, attributes
• Only INTERACTIVE elements included (inputs, buttons, selects, textareas,
  action links, checkboxes, radios, file inputs, date pickers, …)
• Static / decorative elements (images, plain text, divs) are excluded
• Every UIComponent carries a concrete CSS selector usable in Playwright
• Still uses Groq to infer human-readable purpose per element and a
  plain-English page description (both useful for test generation)
• Screenshot is kept for the Excel report but NOT used for element detection

Flow
────
1. Receive DOM data from BrowserAgent (list of raw element dicts)
2. Build UIComponent objects directly — no LLM needed for this step
3. Call Groq text model once per page to:
     a. Infer purpose for each element (optional enrichment)
     b. Write a page_description
4. Detect login form from DOM (has_login flag + password input presence)
5. Return PageAnalysis with fully-populated UIComponent list
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import re

from groq import Groq

from config import GROQ_API_KEY, TEXT_MODEL
from logger import log
from models import AgentContext, LoginForm, PageAnalysis, UIComponent

NAME   = "VisionAgent"
client = Groq(api_key=GROQ_API_KEY)

# ---------------------------------------------------------------------------
# Interactive element type groups
# ---------------------------------------------------------------------------

# Canonical types we keep — everything not in this set is discarded
INTERACTIVE_TYPES = {
    "button", "text_input", "email_input", "password_input", "number_input",
    "search_input", "tel_input", "url_input", "date_input", "file_input",
    "textarea", "select", "checkbox", "radio", "link",
    # ARIA roles that are interactive
    "combobox", "textbox", "searchbox", "spinbutton", "switch",
}

# Human-readable label for each type (used in LLM prompts)
TYPE_LABEL = {
    "button":         "Button",
    "text_input":     "Text input",
    "email_input":    "Email input",
    "password_input": "Password input",
    "number_input":   "Number input",
    "search_input":   "Search input",
    "tel_input":      "Phone input",
    "url_input":      "URL input",
    "date_input":     "Date picker",
    "file_input":     "File upload",
    "textarea":       "Text area",
    "select":         "Dropdown / select",
    "checkbox":       "Checkbox",
    "radio":          "Radio button",
    "link":           "Action link",
    "combobox":       "Combobox",
    "textbox":        "Text box",
    "searchbox":      "Search box",
    "spinbutton":     "Spin button",
    "switch":         "Toggle switch",
}


# ---------------------------------------------------------------------------
# Step 1 — Build UIComponents from raw DOM data (no LLM)
# ---------------------------------------------------------------------------

def _build_components_from_dom(dom_elements: list[dict]) -> list[UIComponent]:
    """
    Convert raw JS-scraped element dicts into UIComponent objects.
    Only interactive element types are kept.
    Duplicate elements (same selector) are deduplicated.
    """
    seen_selectors: set[str] = set()
    components: list[UIComponent] = []

    for el in dom_elements:
        el_type = el.get("type", "")
        if el_type not in INTERACTIVE_TYPES:
            continue

        selector = el.get("selector", "")
        if selector and selector in seen_selectors:
            continue
        if selector:
            seen_selectors.add(selector)

        # Resolve best label
        label = (
            el.get("aria_label")
            or el.get("placeholder")
            or el.get("label")
            or el.get("name")
            or el.get("element_id")
            or TYPE_LABEL.get(el_type, el_type)
        ).strip()

        comp = UIComponent(
            type        = el_type,
            label       = label,
            purpose     = "",            # filled in by LLM enrichment step
            location    = el.get("location", "main"),
            tag         = el.get("tag", ""),
            input_type  = el.get("input_type", ""),
            selector    = selector,
            name        = el.get("name", ""),
            element_id  = el.get("element_id", ""),
            placeholder = el.get("placeholder", ""),
            aria_label  = el.get("aria_label", ""),
            is_visible  = el.get("is_visible", True),
            is_enabled  = el.get("is_enabled", True),
            is_required = el.get("is_required", False),
            dom_section = el.get("dom_section", ""),
        )
        components.append(comp)

    return components


# ---------------------------------------------------------------------------
# Step 2 — Enrich purposes + page_description via Groq text model
# ---------------------------------------------------------------------------

_ENRICH_SYSTEM = (
    "You are a senior QA analyst. Given a list of interactive UI elements "
    "scraped from a web page, write a short purpose for each element and a "
    "brief page description. Return ONLY valid JSON — no markdown."
)

_ENRICH_PROMPT = """\
Page URL   : {url}
Page Title : {title}

Interactive elements found on this page:
{elements_json}

Return a JSON object:
{{
  "page_description": "2-3 sentence description of the page and its main functionality",
  "purposes": {{
    "<selector>": "short purpose string (max 10 words)",
    ...
  }}
}}

Write a purpose for EVERY selector listed. Return ONLY valid JSON."""


def _enrich_with_llm(
    components: list[UIComponent],
    url: str,
    title: str,
) -> tuple[str, dict[str, str]]:
    """
    Call Groq once to get:
      - page_description (str)
      - purposes dict {selector: purpose}

    Falls back gracefully on any error.
    """
    if not components:
        return "No interactive elements found.", {}

    # Build a compact element summary for the prompt
    el_list = [
        {
            "selector":    c.selector or c.label,
            "type":        TYPE_LABEL.get(c.type, c.type),
            "label":       c.label,
            "dom_section": c.dom_section,
            "required":    c.is_required,
        }
        for c in components
    ]

    prompt = _ENRICH_PROMPT.format(
        url          = url,
        title        = title,
        elements_json = json.dumps(el_list, indent=2),
    )

    try:
        response = client.chat.completions.create(
            model    = TEXT_MODEL,
            messages = [
                {"role": "system", "content": _ENRICH_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            max_tokens  = 1500,
            temperature = 0.1,
        )
        raw  = response.choices[0].message.content.strip()
        raw  = re.sub(r"^```(?:json)?\s*", "", raw)
        raw  = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        return (
            data.get("page_description", ""),
            data.get("purposes", {}),
        )
    except Exception as e:
        log("warning", NAME, f"LLM enrichment failed: {e} — using defaults")
        return f"Web page at {url}", {}


# ---------------------------------------------------------------------------
# Main analysis function
# ---------------------------------------------------------------------------

def analyse_page(dom: dict, screenshot_b64: str, url: str,
                 post_login: bool = False) -> PageAnalysis:
    """
    Build a PageAnalysis from live DOM data + optional LLM enrichment.

    dom  — dict returned by BrowserAgent._scrape_dom():
           { title, url, elements:[...], form_count, has_login }
    """
    raw_elements = dom.get("elements", [])
    title        = dom.get("title", url)
    has_login    = dom.get("has_login", False)

    log("vision", NAME,
        f"DOM scan: {len(raw_elements)} raw elements → filtering to interactive only")

    # Step 1 — build components from DOM (no LLM)
    components = _build_components_from_dom(raw_elements)

    log("success", NAME,
        f"Kept {len(components)} interactive components "
        f"from {len(raw_elements)} total DOM elements")

    # Log a quick breakdown by type
    type_counts: dict[str, int] = {}
    for c in components:
        type_counts[c.type] = type_counts.get(c.type, 0) + 1
    if type_counts:
        breakdown = "  |  ".join(f"{t}: {n}" for t, n in sorted(type_counts.items()))
        log("info", NAME, f"  Breakdown → {breakdown}")

    # Step 2 — LLM enrichment for purpose + page_description
    log("llm", NAME, "Enriching element purposes via LLM")
    page_description, purposes = _enrich_with_llm(components, url, title)

    # Apply purposes to components
    for comp in components:
        key = comp.selector or comp.label
        if key in purposes:
            comp.purpose = purposes[key]
        elif not comp.purpose:
            comp.purpose = f"{TYPE_LABEL.get(comp.type, comp.type)} — {comp.label}"

    # Detect login form from DOM (reliable — not screenshot guesswork)
    password_inputs = [c for c in components if c.type == "password_input"]
    login_detected  = has_login or bool(password_inputs)
    submit_buttons  = [c for c in components
                       if c.type == "button"
                       and any(w in c.label.lower()
                               for w in ("login", "sign in", "log in", "submit"))]

    lf = LoginForm(
        detected       = login_detected,
        username_label = next(
            (c.label for c in components
             if c.type in ("email_input", "text_input")
             and any(w in (c.label + c.placeholder + c.name).lower()
                     for w in ("user", "email", "login"))),
            None,
        ),
        password_label = password_inputs[0].label if password_inputs else None,
        submit_button  = submit_buttons[0].label  if submit_buttons  else None,
    )

    analysis = PageAnalysis(
        url              = url,
        page_title       = title,
        requires_login   = login_detected and not post_login,
        login_form       = lf,
        components       = components,
        page_description = page_description,
        screenshot_b64   = screenshot_b64,
        post_login       = post_login,
    )

    log("success", NAME,
        f"'{title}' — {len(components)} interactive components, "
        f"login_detected={login_detected}")
    return analysis


# ---------------------------------------------------------------------------
# Agent entry-point
# ---------------------------------------------------------------------------

def run(ctx: AgentContext, browser_result: dict) -> list[PageAnalysis]:
    """
    Build PageAnalysis objects from BrowserAgent result.
    Each page (pre-login and post-login) is analysed separately.
    """
    analyses: list[PageAnalysis] = []

    pre = browser_result.get("pre_login", {})
    if pre.get("dom"):
        log("vision", NAME, "Analysing pre-login page")
        a = analyse_page(
            dom            = pre["dom"],
            screenshot_b64 = pre.get("screenshot_b64", ""),
            url            = pre.get("url", ctx.url),
            post_login     = False,
        )
        analyses.append(a)

    post = browser_result.get("post_login")
    if post and post.get("dom"):
        log("vision", NAME, "Analysing post-login page")
        a = analyse_page(
            dom            = post["dom"],
            screenshot_b64 = post.get("screenshot_b64", ""),
            url            = post.get("url", ctx.url),
            post_login     = True,
        )
        analyses.append(a)

    ctx.page_analyses = analyses
    return analyses
