"""
Shared data-transfer objects used across all agents.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UIComponent:
    type: str       # canonical type: button | text_input | email_input | password_input |
                    # number_input | textarea | select | checkbox | radio |
                    # file_input | date_input | link | form | etc.
    label: str      # visible text, placeholder, aria-label, or name
    purpose: str    # inferred purpose
    location: str   # header | nav | main | footer | sidebar | unknown

    # Rich selector data scraped from live DOM (not inferred from screenshot)
    tag: str = ""               # raw HTML tag: input, button, select, textarea, a
    input_type: str = ""        # input[type] attribute value
    selector: str = ""          # most specific CSS selector to target this element
    name: str = ""              # name attribute
    element_id: str = ""        # id attribute
    placeholder: str = ""       # placeholder attribute
    aria_label: str = ""        # aria-label attribute
    is_visible: bool = True     # element is in viewport / not hidden
    is_enabled: bool = True     # element is not disabled
    is_required: bool = False   # required attribute present
    dom_section: str = ""       # closest landmark: form#id, section, header, etc.


@dataclass
class LoginForm:
    detected: bool = False
    username_label: Optional[str] = None
    password_label: Optional[str] = None
    submit_button: Optional[str] = None


@dataclass
class PageAnalysis:
    url: str
    page_title: str
    requires_login: bool
    login_form: LoginForm
    components: list[UIComponent]
    page_description: str
    screenshot_b64: str = ""
    post_login: bool = False


@dataclass
class TestCase:
    test_case_id: str
    module: str
    test_case_title: str
    description: str
    preconditions: str
    test_steps: str
    expected_result: str
    priority: str
    test_type: str
    status: str = "Not Executed"
    auto_script: str = ""
    auto_result: str = ""
    auto_error: str = ""
    executed_at: str = ""
    comments: str = ""


@dataclass
class AutomationResult:
    test_case_id: str
    passed: bool
    error_message: str = ""
    executed_at: str = ""
    duration_ms: int = 0
    script: str = ""


@dataclass
class AgentContext:
    """Passed between agents to carry shared state."""
    url: str
    credentials: Optional[dict] = None
    # Optional: navigate here AFTER login before screenshotting
    target_url: Optional[str] = None
    # Optional: force all generated test cases into this module name
    module_name: Optional[str] = None
    page_analyses: list[PageAnalysis] = field(default_factory=list)
    test_cases: list[TestCase] = field(default_factory=list)
    automation_results: list[AutomationResult] = field(default_factory=list)
    excel_path: str = ""
    scripts_dir: str = ""   # populated by ScriptExportAgent
    headed: bool = False    # True → visible browser window during test execution
    slow_mo: int = 400      # ms delay between actions in headed mode
