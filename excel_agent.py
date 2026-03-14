"""
Agent 4 — ExcelAgent
Responsibility:
  • Write manual test cases to a new Excel workbook (write_workbook)
  • Read test cases back from an existing workbook (read_test_cases)
  • Update automation results in-place (update_results)
"""
import re
import sys
from datetime import datetime
from pathlib import Path

from config import COL, COL_WIDTHS, COLOUR, HEADERS, OUTPUT_DIR
from logger import log
from models import AgentContext, AutomationResult, TestCase
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

NAME = "ExcelAgent"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Style helpers ─────────────────────────────────────────────────────────────

THIN = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"),  bottom=Side(style="thin"),
)
FONT_BASE   = Font(name="Arial", size=10)
FONT_HEADER = Font(name="Arial", size=11, bold=True, color="FFFFFF")
FONT_BOLD   = Font(name="Arial", size=10, bold=True)

FILL_HEADER = PatternFill("solid", fgColor=COLOUR["header_bg"])
FILL_ALT    = PatternFill("solid", fgColor=COLOUR["alt_row"])
FILL_PASS   = PatternFill("solid", fgColor=COLOUR["pass_bg"])
FILL_FAIL   = PatternFill("solid", fgColor=COLOUR["fail_bg"])
FILL_ERR    = PatternFill("solid", fgColor=COLOUR["error_bg"])

PRI_FILL = {
    "High":   PatternFill("solid", fgColor=COLOUR["high_pri"]),
    "Medium": PatternFill("solid", fgColor=COLOUR["med_pri"]),
    "Low":    PatternFill("solid", fgColor=COLOUR["low_pri"]),
}

RESULT_FILL = {"Pass": FILL_PASS, "Fail": FILL_FAIL, "Error": FILL_ERR}


def _header_cell(cell, text: str):
    cell.value     = text
    cell.font      = FONT_HEADER
    cell.fill      = FILL_HEADER
    cell.border    = THIN
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _data_cell(cell, value, alt_row: bool = False, wrap: bool = True):
    cell.value     = value
    cell.font      = FONT_BASE
    cell.border    = THIN
    cell.alignment = Alignment(vertical="top", wrap_text=wrap)
    if alt_row:
        cell.fill = FILL_ALT


def _apply_col_widths(ws):
    for i, w in enumerate(COL_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _write_header_row(ws):
    for i, h in enumerate(HEADERS, 1):
        _header_cell(ws.cell(row=1, column=i), h)
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"


def _write_tc_row(ws, row: int, tc: TestCase):
    alt = row % 2 == 0
    vals = [
        tc.test_case_id, tc.module, tc.test_case_title, tc.description,
        tc.preconditions, tc.test_steps, tc.expected_result, tc.priority,
        tc.test_type, tc.status, tc.auto_script, tc.auto_result,
        tc.auto_error, tc.executed_at, tc.comments,
    ]
    for col_idx, val in enumerate(vals, 1):
        _data_cell(ws.cell(row=row, column=col_idx), val, alt_row=alt)

    # Priority colouring
    pri_cell      = ws.cell(row=row, column=COL["priority"])
    pri_cell.fill = PRI_FILL.get(tc.priority.strip().title(), PatternFill())
    pri_cell.font = FONT_BOLD
    pri_cell.alignment = Alignment(horizontal="center", vertical="top")

    # Auto result colouring
    res_cell      = ws.cell(row=row, column=COL["auto_result"])
    res_fill      = RESULT_FILL.get(tc.auto_result.strip().title(), PatternFill())
    res_cell.fill = res_fill
    res_cell.alignment = Alignment(horizontal="center", vertical="top")

    ws.row_dimensions[row].height = 60


# ── Public API ────────────────────────────────────────────────────────────────

def write_workbook(ctx: AgentContext) -> str:
    """Create a new workbook from ctx.test_cases. Returns path."""
    test_cases = ctx.test_cases
    url        = ctx.url
    page_title = ctx.page_analyses[-1].page_title if ctx.page_analyses else url

    wb = Workbook()
    wb.remove(wb.active)

    # ── Summary sheet ──────────────────────────────────────────────────
    ss = wb.create_sheet("Summary")
    ss.column_dimensions["A"].width = 28
    ss.column_dimensions["B"].width = 60

    ss["A1"] = "Test Report Summary"
    ss["A1"].font = Font(name="Arial", size=16, bold=True, color=COLOUR["header_bg"])
    ss.merge_cells("A1:B1")
    ss["A1"].alignment = Alignment(horizontal="center")

    summary_rows = [
        ("Report Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("URL Tested", url),
        ("Page Title", page_title),
        ("Total Test Cases", len(test_cases)),
        ("High Priority", sum(1 for t in test_cases if t.priority.title() == "High")),
        ("Medium Priority", sum(1 for t in test_cases if t.priority.title() == "Medium")),
        ("Low Priority", sum(1 for t in test_cases if t.priority.title() == "Low")),
    ]
    for r, (lbl, val) in enumerate(summary_rows, start=3):
        ss.cell(row=r, column=1, value=lbl).font  = FONT_BOLD
        ss.cell(row=r, column=2, value=val).font  = FONT_BASE

    # ── All Test Cases sheet ───────────────────────────────────────────
    ts = wb.create_sheet("Test Cases")
    _write_header_row(ts)
    _apply_col_widths(ts)
    for i, tc in enumerate(test_cases, start=2):
        _write_tc_row(ts, i, tc)

    # ── Per-module sheets ──────────────────────────────────────────────
    modules: dict[str, list[TestCase]] = {}
    for tc in test_cases:
        modules.setdefault(tc.module, []).append(tc)

    for mod_name, mod_cases in modules.items():
        safe = re.sub(r"[\\/*?:\[\]]", "_", mod_name)[:31]
        ms   = wb.create_sheet(safe)
        _write_header_row(ms)
        _apply_col_widths(ms)
        for i, tc in enumerate(mod_cases, start=2):
            _write_tc_row(ms, i, tc)

    # ── Save ───────────────────────────────────────────────────────────
    ts_stamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = re.sub(r"[^\w\-]", "_", page_title)[:40]
    path       = OUTPUT_DIR / f"test_cases_{safe_title}_{ts_stamp}.xlsx"
    wb.save(str(path))

    ctx.excel_path = str(path)
    log("excel", NAME, f"Workbook written → {path}")
    return str(path)


def read_test_cases(excel_path: str) -> list[TestCase]:
    """Read TestCase rows from the 'Test Cases' sheet of an existing workbook."""
    wb = load_workbook(excel_path, data_only=True)

    if "Test Cases" not in wb.sheetnames:
        log("error", NAME, "Sheet 'Test Cases' not found in workbook")
        return []

    ws = wb["Test Cases"]
    test_cases: list[TestCase] = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row[0]:           # empty row guard
            continue
        tc = TestCase(
            test_case_id    = str(row[COL["id"] - 1]            or ""),
            module          = str(row[COL["module"] - 1]        or ""),
            test_case_title = str(row[COL["title"] - 1]         or ""),
            description     = str(row[COL["description"] - 1]   or ""),
            preconditions   = str(row[COL["preconditions"] - 1] or ""),
            test_steps      = str(row[COL["steps"] - 1]         or ""),
            expected_result = str(row[COL["expected"] - 1]      or ""),
            priority        = str(row[COL["priority"] - 1]      or "Medium"),
            test_type       = str(row[COL["test_type"] - 1]     or "Functional"),
            status          = str(row[COL["status"] - 1]        or "Not Executed"),
            auto_script     = str(row[COL["auto_script"] - 1]   or ""),
            auto_result     = str(row[COL["auto_result"] - 1]   or ""),
            auto_error      = str(row[COL["auto_error"] - 1]    or ""),
            executed_at     = str(row[COL["executed_at"] - 1]   or ""),
            comments        = str(row[COL["comments"] - 1]      or ""),
        )
        test_cases.append(tc)

    log("excel", NAME, f"Read {len(test_cases)} test cases from {excel_path}")
    return test_cases


def update_results(excel_path: str, results: list[AutomationResult]):
    """
    Update automation columns (auto_result, auto_error, executed_at, auto_script,
    status) in-place for every sheet that contains matching test case IDs.
    """
    result_map = {r.test_case_id: r for r in results}

    wb = load_workbook(excel_path)

    for sheet_name in wb.sheetnames:
        if sheet_name == "Summary":
            continue
        ws = wb[sheet_name]

        for row_idx in range(2, ws.max_row + 1):
            tc_id = ws.cell(row=row_idx, column=COL["id"]).value
            if not tc_id or str(tc_id) not in result_map:
                continue

            r   = result_map[str(tc_id)]
            alt = row_idx % 2 == 0

            def _upd(col_key: str, value):
                cell        = ws.cell(row=row_idx, column=COL[col_key])
                cell.value  = value
                cell.font   = FONT_BASE
                cell.border = THIN
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if alt and col_key not in ("auto_result",):
                    cell.fill = FILL_ALT

            _upd("auto_script",  r.script)
            _upd("auto_error",   r.error_message)
            _upd("executed_at",  r.executed_at)

            result_label = "Pass" if r.passed else ("Error" if "Error" in r.error_message else "Fail")
            res_cell     = ws.cell(row=row_idx, column=COL["auto_result"])
            res_cell.value     = result_label
            res_cell.font      = FONT_BOLD
            res_cell.border    = THIN
            res_cell.fill      = RESULT_FILL.get(result_label, PatternFill())
            res_cell.alignment = Alignment(horizontal="center", vertical="top")

            status_cell        = ws.cell(row=row_idx, column=COL["status"])
            status_cell.value  = "Executed"
            status_cell.font   = FONT_BASE
            status_cell.border = THIN

    # Refresh summary counts
    if "Summary" in wb.sheetnames:
        ss    = wb["Summary"]
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed = total - passed

        extra = [
            ("Automated Tests Run", total),
            ("Passed", passed),
            ("Failed", failed),
            ("Last Run", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ]
        start_row = 11
        for i, (lbl, val) in enumerate(extra):
            ss.cell(row=start_row + i, column=1, value=lbl).font = FONT_BOLD
            ss.cell(row=start_row + i, column=2, value=val).font = FONT_BASE

    wb.save(excel_path)
    log("excel", NAME, f"Updated {len(results)} results in {excel_path}")


def run(ctx: AgentContext, mode: str = "write") -> str:
    """
    mode='write'  → write new workbook from ctx.test_cases
    mode='update' → update results in ctx.excel_path
    """
    if mode == "write":
        return write_workbook(ctx)
    elif mode == "update":
        update_results(ctx.excel_path, ctx.automation_results)
        return ctx.excel_path
    else:
        raise ValueError(f"Unknown ExcelAgent mode: {mode}")
