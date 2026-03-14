"""
Shared data-transfer objects used across all agents.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UIComponent:
    type: str
    label: str
    purpose: str
    location: str


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
    script: str = ""


@dataclass
class AgentContext:
    """Passed between agents to carry shared state."""
    url: str
    credentials: Optional[dict] = None
    page_analyses: list[PageAnalysis] = field(default_factory=list)
    test_cases: list[TestCase] = field(default_factory=list)
    automation_results: list[AutomationResult] = field(default_factory=list)
    excel_path: str = ""
    scripts_dir: str = ""   # populated by ScriptExportAgent