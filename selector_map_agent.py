"""
Agent 8 — SelectorMapAgent
===========================
Responsibility:
  • Read UIComponent objects from ctx.page_analyses (populated by VisionAgent)
  • Build a structured selector map keyed by component role/type
  • Save the map as a JSON file alongside the Excel report
  • Store the map on ctx.selector_map so ScriptGenerationAgent can use
    exact page-specific selectors instead of generic guesses

Selector map structure
──────────────────────
{
  "url": "https://app.example.com/userProfile",
  "page_title": "User Profile",
  "scraped_at": "2026-03-14 15:30:00",

  "by_type": {
    "button":         [{"selector": "#save-btn",      "label": "Save Changes", ...}],
    "email_input":    [{"selector": "#email",          "label": "Email Address", ...}],
    "text_input":     [{"selector": "#firstName",      "label": "First Name",   ...}],
    "password_input": [{"selector": "#currentPassword","label": "Current Password",...}],
    "select":         [{"selector": "#country",        "label": "Country",      ...}],
    ...
  },

  "by_role": {
    "submit_button":   "#save-btn",
    "primary_input":   "#firstName",
    "email_field":     "#email",
    "password_field":  "#currentPassword",
    "username_field":  "#username",
    "search_field":    "#search",
    "cancel_button":   "#cancel-btn",
    "delete_button":   "#delete-btn",
    "dropdown":        "#country",
    ...
  },

  "login": {
    "username_selector": "input[name='email']",
    "password_selector": "input[type='password']",
    "submit_selector":   "button[type='submit']"
  },

  "all": [
    { "type": "button", "label": "Save", "selector": "#save-btn",
      "element_id": "save-btn", "name": "save", "dom_section": "form.profile",
      "is_required": false, "input_type": "" }
  ]
}

The JSON file is saved to:
  output/selectors/<run_id>.json
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import re
from datetime import datetime

from config import OUTPUT_DIR
from logger import log
from models import AgentContext, UIComponent

NAME = "SelectorMapAgent"

SELECTORS_DIR = OUTPUT_DIR / "selectors"
SELECTORS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Role inference — maps component types + labels to semantic roles
# ---------------------------------------------------------------------------

# Keywords that identify a button's semantic role from its label text
_BUTTON_ROLE_KEYWORDS = {
    "submit_button":  ["save", "submit", "confirm", "apply", "done", "finish",
                       "create", "add", "update", "ok", "yes"],
    "cancel_button":  ["cancel", "discard", "abort", "no", "close", "dismiss"],
    "delete_button":  ["delete", "remove", "destroy", "trash", "deactivate"],
    "edit_button":    ["edit", "modify", "change", "update", "pencil"],
    "login_button":   ["login", "sign in", "log in", "signin", "authenticate"],
    "logout_button":  ["logout", "sign out", "log out", "signout"],
    "search_button":  ["search", "find", "go", "query"],
    "upload_button":  ["upload", "attach", "browse", "choose file"],
    "next_button":    ["next", "continue", "forward", "proceed"],
    "back_button":    ["back", "previous", "prev", "return"],
}

# Keywords for field roles
_FIELD_ROLE_KEYWORDS = {
    "username_field":   ["user", "username", "login", "account"],
    "email_field":      ["email", "e-mail", "mail"],
    "password_field":   ["password", "passwd", "pass", "secret", "pin"],
    "search_field":     ["search", "query", "find", "filter", "keyword"],
    "name_field":       ["name", "fullname", "full name", "first name", "last name",
                         "firstname", "lastname", "givenname", "surname"],
    "phone_field":      ["phone", "mobile", "tel", "telephone", "cell"],
    "address_field":    ["address", "street", "city", "state", "zip", "postal"],
    "date_field":       ["date", "dob", "birthday", "birth", "expiry"],
    "amount_field":     ["amount", "price", "cost", "fee", "salary", "rate"],
    "description_field":["description", "note", "comment", "message", "bio", "about"],
    "title_field":      ["title", "subject", "heading", "caption"],
    "url_field":        ["url", "website", "link", "href"],
}


def _infer_button_role(label: str) -> str | None:
    lbl = label.lower().strip()
    for role, keywords in _BUTTON_ROLE_KEYWORDS.items():
        if any(kw in lbl for kw in keywords):
            return role
    return None


def _infer_field_role(comp: UIComponent) -> str | None:
    haystack = " ".join([
        comp.label, comp.placeholder, comp.name,
        comp.aria_label, comp.element_id
    ]).lower()

    # Type-based shortcuts
    if comp.type == "email_input":
        return "email_field"
    if comp.type == "password_input":
        return "password_field"
    if comp.type == "search_input":
        return "search_field"

    for role, keywords in _FIELD_ROLE_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            return role
    return None


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def _safe_run_id(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or "site"
        path = urlparse(url).path.strip("/").replace("/", "_") or "root"
        host = re.sub(r"^www\.", "", host)
        host = re.sub(r"[^\w]", "_", host)
        path = re.sub(r"[^\w]", "_", path)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{host}_{path}_{ts}"
    except Exception:
        return f"selectors_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def build_selector_map(ctx: AgentContext) -> dict:
    """
    Build the full selector map from all PageAnalysis components.
    Uses post-login analyses when available (they have the real app pages).
    """
    # Prefer post-login analyses; fall back to all
    analyses = [a for a in ctx.page_analyses if a.post_login] or ctx.page_analyses
    if not analyses:
        log("warning", NAME, "No page analyses found — selector map will be empty")
        return {}

    # Use the last (most specific/targeted) analysis
    analysis  = analyses[-1]
    components = analysis.components

    log("info", NAME,
        f"Building selector map from {len(components)} components "
        f"on '{analysis.page_title}'")

    # ── by_type: group all components by their canonical type ────────────────
    by_type: dict[str, list[dict]] = {}
    for comp in components:
        if not comp.selector:
            continue
        entry = {
            "selector":    comp.selector,
            "label":       comp.label,
            "element_id":  comp.element_id,
            "name":        comp.name,
            "placeholder": comp.placeholder,
            "aria_label":  comp.aria_label,
            "dom_section": comp.dom_section,
            "is_required": comp.is_required,
            "input_type":  comp.input_type,
            "purpose":     comp.purpose,
        }
        by_type.setdefault(comp.type, []).append(entry)

    # ── by_role: first match for each semantic role ───────────────────────────
    by_role: dict[str, str] = {}

    for comp in components:
        if not comp.selector:
            continue

        # Button roles
        if comp.type == "button":
            role = _infer_button_role(comp.label)
            if role and role not in by_role:
                by_role[role] = comp.selector

        # Field roles
        elif comp.type in ("text_input", "email_input", "password_input",
                            "search_input", "tel_input", "url_input",
                            "number_input", "date_input", "textarea",
                            "combobox", "textbox", "searchbox"):
            role = _infer_field_role(comp)
            if role and role not in by_role:
                by_role[role] = comp.selector

        # Select / dropdown
        elif comp.type == "select":
            if "dropdown" not in by_role:
                by_role["dropdown"] = comp.selector

        # Checkbox
        elif comp.type == "checkbox":
            role_name = f"checkbox_{comp.name or comp.label or 'check'}"
            role_name = re.sub(r"[^\w]", "_", role_name.lower())[:40]
            by_role.setdefault(role_name, comp.selector)

        # Radio
        elif comp.type == "radio":
            role_name = f"radio_{comp.name or comp.label or 'radio'}"
            role_name = re.sub(r"[^\w]", "_", role_name.lower())[:40]
            by_role.setdefault(role_name, comp.selector)

        # File input
        elif comp.type == "file_input":
            by_role.setdefault("file_upload", comp.selector)

    # ── login shortcuts ───────────────────────────────────────────────────────
    login_info = {
        "username_selector": by_role.get("username_field")
                             or by_role.get("email_field")
                             or next((c.selector for c in components
                                      if c.type in ("email_input", "text_input")
                                      and any(w in (c.label + c.name + c.placeholder).lower()
                                              for w in ("user", "email", "login"))), ""),
        "password_selector": by_role.get("password_field")
                             or next((c.selector for c in components
                                      if c.type == "password_input"), ""),
        "submit_selector":   by_role.get("login_button")
                             or by_role.get("submit_button")
                             or next((c.selector for c in components
                                      if c.type == "button"
                                      and any(w in c.label.lower()
                                              for w in ("login","sign","submit","continue"))), ""),
    }

    # ── flat "all" list ───────────────────────────────────────────────────────
    all_elements = [
        {
            "type":        c.type,
            "label":       c.label,
            "selector":    c.selector,
            "element_id":  c.element_id,
            "name":        c.name,
            "placeholder": c.placeholder,
            "aria_label":  c.aria_label,
            "dom_section": c.dom_section,
            "is_required": c.is_required,
            "input_type":  c.input_type,
            "purpose":     c.purpose,
        }
        for c in components if c.selector
    ]

    selector_map = {
        "url":         analysis.url,
        "page_title":  analysis.page_title,
        "scraped_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "by_type":     by_type,
        "by_role":     by_role,
        "login":       login_info,
        "all":         all_elements,
    }

    # Summary log
    log("success", NAME,
        f"Selector map built — "
        f"{len(all_elements)} selectors, "
        f"{len(by_type)} types, "
        f"{len(by_role)} roles")

    return selector_map


def save_selector_map(selector_map: dict, ctx: AgentContext) -> str:
    """Save selector_map to JSON and return the file path."""
    run_id    = _safe_run_id(selector_map.get("url", ctx.url))
    json_path = SELECTORS_DIR / f"{run_id}.json"
    json_path.write_text(
        json.dumps(selector_map, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log("info", NAME, f"Selectors saved → {json_path}")
    return str(json_path)


def load_selector_map(json_path: str) -> dict:
    """Load a previously saved selector map from a JSON file."""
    path = Path(json_path)
    if not path.exists():
        log("warning", NAME, f"Selector map not found: {json_path}")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Convenience accessors used by ScriptGenerationAgent
# ---------------------------------------------------------------------------

def get_selector(selector_map: dict, role: str,
                 fallback: str = "") -> str:
    """
    Get the best selector for a semantic role.
    Looks in by_role first, then falls back to the first entry in by_type.
    """
    if not selector_map:
        return fallback
    # by_role exact match
    by_role = selector_map.get("by_role", {})
    if role in by_role:
        return by_role[role]
    # by_type first entry
    by_type = selector_map.get("by_type", {})
    if role in by_type and by_type[role]:
        return by_type[role][0]["selector"]
    return fallback


def get_all_of_type(selector_map: dict, type_name: str) -> list[dict]:
    """Return all components of a given type."""
    return selector_map.get("by_type", {}).get(type_name, [])


def get_first_selector(selector_map: dict, type_name: str,
                       fallback: str = "") -> str:
    """Return the selector of the first component of a given type."""
    entries = get_all_of_type(selector_map, type_name)
    return entries[0]["selector"] if entries else fallback


# ---------------------------------------------------------------------------
# Agent entry-point
# ---------------------------------------------------------------------------

def run(ctx: AgentContext) -> dict:
    """Build selector map, save JSON, store on ctx. Returns the map."""
    log("agent", NAME, "Building selector map from DOM components")

    selector_map = build_selector_map(ctx)
    if selector_map:
        json_path = save_selector_map(selector_map, ctx)
        ctx.selector_map        = selector_map
        ctx.selectors_json_path = json_path
    else:
        ctx.selector_map        = {}
        ctx.selectors_json_path = ""

    return selector_map
    return selector_map
