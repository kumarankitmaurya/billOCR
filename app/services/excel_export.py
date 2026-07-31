"""Turns one or more ExtractionResult objects into a styled .xlsx workbook.

Each bill gets its own pair of sheets: "<n> Summary" and "<n> Items", so
multiple bills processed in one request end up in a single downloadable
file instead of one file per bill.
"""

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.models import ExtractionResult

_HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_ALT_ROW_FILL = PatternFill(start_color="F3F4F6", end_color="F3F4F6", fill_type="solid")
_THIN_BORDER = Border(*(Side(style="thin", color="D1D5DB"),) * 4)

_ITEM_HEADERS = [
    ("Serial No.", "serial_no"),
    ("Item Name", "item_name"),
    ("Description", "description"),
    ("HSN/SAC", "hsn_sac_code"),
    ("Quantity", "quantity"),
    ("Unit", "unit"),
    ("Rate", "rate"),
    ("Discount", "discount"),
    ("Tax Rate (%)", "tax_rate"),
    ("Tax Amount", "tax_amount"),
    ("Total", "total"),
]


def _autofit(sheet: Worksheet) -> None:
    """Widen each column to roughly fit its longest cell value."""
    for col_cells in sheet.columns:
        length = max((len(str(cell.value)) for cell in col_cells if cell.value is not None), default=0)
        sheet.column_dimensions[get_column_letter(col_cells[0].column)].width = max(10, length + 2)


def _write_summary_sheet(sheet: Worksheet, result: ExtractionResult) -> None:
    bill = result.bill
    sheet.append(["Bill Summary", ""])
    sheet["A1"].font = Font(size=14, bold=True)
    sheet.merge_cells("A1:B1")

    rows = [
        ("Source File", result.source_filename),
        ("Extraction Engine", result.engine),
        ("Vendor Name", bill.vendor_name),
        ("Vendor Address", bill.vendor_address),
        ("Bill Number", bill.bill_number),
        ("Bill Date", bill.bill_date),
        ("Customer Name", bill.customer_name),
        ("Subtotal", bill.subtotal),
        ("Tax Total", bill.tax_total),
        ("Grand Total", bill.grand_total),
        ("Payment Method", bill.payment_method),
    ]
    for label, value in rows:
        sheet.append([label, value])
        row = sheet.max_row
        sheet.cell(row=row, column=1).font = Font(bold=True)
        sheet.cell(row=row, column=1).fill = _ALT_ROW_FILL

    _autofit(sheet)


def _write_items_sheet(sheet: Worksheet, result: ExtractionResult) -> None:
    headers = [label for label, _ in _ITEM_HEADERS]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center")
        cell.border = _THIN_BORDER

    for index, item in enumerate(result.bill.line_items, start=1):
        row = [getattr(item, field) for _, field in _ITEM_HEADERS]
        sheet.append(row)
        fill = _ALT_ROW_FILL if index % 2 == 0 else None
        for cell in sheet[sheet.max_row]:
            cell.border = _THIN_BORDER
            if fill:
                cell.fill = fill

    _autofit(sheet)


def _safe_sheet_title(base: str, suffix: str, used: set[str]) -> str:
    """Excel sheet titles must be <=31 chars and unique within the workbook."""
    title = f"{base} {suffix}"[:31]
    original = title
    counter = 2
    while title in used:
        title = f"{original[:28]}-{counter}"
        counter += 1
    used.add(title)
    return title


def build_excel(results: list[ExtractionResult]) -> io.BytesIO:
    """Build the workbook and return it as an in-memory buffer ready to stream."""
    workbook = Workbook()
    # Drop the default blank sheet openpyxl creates.
    workbook.remove(workbook.active)

    used_titles: set[str] = set()
    for index, result in enumerate(results, start=1):
        base_name = f"Bill {index}"
        summary_sheet = workbook.create_sheet(_safe_sheet_title(base_name, "Summary", used_titles))
        _write_summary_sheet(summary_sheet, result)

        items_sheet = workbook.create_sheet(_safe_sheet_title(base_name, "Items", used_titles))
        _write_items_sheet(items_sheet, result)

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer
