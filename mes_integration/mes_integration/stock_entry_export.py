"""Stock Entry list export with the MES receipt callback display column."""

from io import BytesIO

import frappe
from frappe import _
from frappe.desk.reportview import _export_query, get_form_params
from frappe.desk.utils import pop_csv_params, provide_binary_file
from openpyxl import load_workbook

from mes_integration.mes_integration.stock_entry import get_mes_receipt_callback_statuses


CALLBACK_LABELS = {
    "Success": "已回传",
    "Failed": "回传失败",
    "Pending": "回传待确认",
    "No Record": "无回传记录",
}


def get_receipt_export_values(names):
    """Resolve statuses only for MES receipts the exporting user may read."""
    values = {}
    for start in range(0, len(names), 100):
        batch = names[start:start + 100]
        rows = frappe.get_list(
            "Stock Entry", filters={"name": ["in", batch]},
            fields=["name", "docstatus", "custom_mes_receipt"], limit_page_length=len(batch),
        )
        submitted = [row.name for row in rows if row.custom_mes_receipt and row.docstatus != 0]
        statuses = get_mes_receipt_callback_statuses(submitted) if submitted else {}
        for row in rows:
            if not row.custom_mes_receipt:
                values[row.name] = ""
            elif row.docstatus == 0:
                values[row.name] = _("待提交")
            else:
                values[row.name] = _(CALLBACK_LABELS.get(statuses.get(row.name, "No Record"), "无回传记录"))
    return values


@frappe.whitelist(methods=["POST"])
@frappe.read_only()
def export_stock_entries_with_receipt_status():
    """Use native permission-aware export, then add the list's virtual status."""
    params = get_form_params()
    position = params.pop("receipt_callback_position", None)
    fields = params.get("fields")
    if (
        params.get("doctype") != "Stock Entry"
        or params.get("file_format_type") != "Excel"
        or not isinstance(fields, list)
        or "name" not in fields
        or not isinstance(position, str)
        or not position.isdecimal()
        or int(position) > len(fields)
    ):
        frappe.throw(_("物料移动导出参数无效"))

    params["limit_page_length"] = None
    params["as_list"] = True
    csv_params = pop_csv_params(params)
    name_column = fields.index("name") + 2  # Native workbook starts with Sr.
    insert_column = int(position) + 2
    title, extension, content = _export_query(params, csv_params, populate_response=False)
    workbook = load_workbook(BytesIO(content))
    sheet = workbook.active
    names = [sheet.cell(row, name_column).value for row in range(2, sheet.max_row + 1)]
    values = get_receipt_export_values([name for name in names if name])
    sheet.insert_cols(insert_column)
    sheet.cell(1, insert_column, _("MES入库回传"))
    for row_number, name in enumerate(names, start=2):
        sheet.cell(row_number, insert_column, values.get(name, ""))

    output = BytesIO()
    workbook.save(output)
    provide_binary_file(_(title), extension, output.getvalue())
