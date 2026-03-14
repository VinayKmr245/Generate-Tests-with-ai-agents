"""
Agent 2 — VisionAgent
Responsibility: Analyse screenshots using Groq vision model.
Outputs: structured PageAnalysis objects.
"""
import json
import re

from config import GROQ_API_KEY, VISION_MODEL
from groq import Groq
from logger import log
from models import AgentContext, LoginForm, PageAnalysis, UIComponent

NAME   = "VisionAgent"
client = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = """You are an expert QA analyst. Analyse the given web page screenshot
and return ONLY a valid JSON object — no markdown, no preamble."""

ANALYSIS_PROMPT = """Analyse this web page screenshot carefully and return a JSON object:

{
  "page_title": "Page name / title visible on screen",
  "requires_login": true | false,
  "login_form": {
    "detected": true | false,
    "username_label": "label text or null",
    "password_label": "label text or null",
    "submit_button": "button text or null"
  },
  "components": [
    {
      "type": "button | input | form | table | nav | modal | dropdown | link | image | card | tab | sidebar | etc",
      "label": "visible text or placeholder",
      "purpose": "what this element does",
      "location": "header | top | bottom | left | right | center | footer | sidebar"
    }
  ],
  "page_description": "2-3 sentence summary of page purpose and key functionality"
}

List ALL visible UI elements. Return ONLY valid JSON."""


def _parse(raw: str, url: str, post_login: bool) -> PageAnalysis:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}

    lf_raw  = data.get("login_form", {})
    lf      = LoginForm(
        detected        = lf_raw.get("detected", False),
        username_label  = lf_raw.get("username_label"),
        password_label  = lf_raw.get("password_label"),
        submit_button   = lf_raw.get("submit_button"),
    )

    components = [
        UIComponent(
            type     = c.get("type", ""),
            label    = c.get("label", ""),
            purpose  = c.get("purpose", ""),
            location = c.get("location", ""),
        )
        for c in data.get("components", [])
    ]

    return PageAnalysis(
        url              = url,
        page_title       = data.get("page_title", url),
        requires_login   = data.get("requires_login", False),
        login_form       = lf,
        components       = components,
        page_description = data.get("page_description", ""),
        post_login       = post_login,
    )


def analyse_screenshot(screenshot_b64: str, url: str, post_login: bool = False) -> PageAnalysis:
    log("vision", NAME, f"Sending screenshot to {VISION_MODEL}")

    response = client.chat.completions.create(
        model    = VISION_MODEL,
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                    },
                    {"type": "text", "text": ANALYSIS_PROMPT},
                ],
            },
        ],
        max_tokens  = 2000,
        temperature = 0.1,
    )

    raw      = response.choices[0].message.content
    analysis = _parse(raw, url, post_login)
    analysis.screenshot_b64 = screenshot_b64

    log("success", NAME, (
        f"'{analysis.page_title}' — "
        f"{len(analysis.components)} components, "
        f"login_required={analysis.requires_login}"
    ))
    return analysis


def run(ctx: AgentContext, browser_result: dict) -> list[PageAnalysis]:
    """Analyse all available screenshots and attach results to ctx."""
    analyses: list[PageAnalysis] = []

    pre = browser_result.get("pre_login", {})
    if pre.get("screenshot_b64"):
        a = analyse_screenshot(pre["screenshot_b64"], pre["url"], post_login=False)
        analyses.append(a)

    post = browser_result.get("post_login")
    if post and post.get("screenshot_b64"):
        a = analyse_screenshot(post["screenshot_b64"], post["url"], post_login=True)
        analyses.append(a)

    ctx.page_analyses = analyses
    return analyses
