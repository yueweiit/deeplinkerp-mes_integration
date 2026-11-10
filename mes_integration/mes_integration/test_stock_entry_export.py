from io import BytesIO
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase
from openpyxl import Workbook, load_workbook

from mes_integration.mes_integration.stock_entry_export import (
    export_stock_entries_with_receipt_status,
    get_receipt_export_values,
)


class TestStockEntryReceiptExport(UnitTestCase):
    def test_native_reportview_export_accepts_receipt_column_without_matching_rows(self):
        params = frappe._dict(
            doctype="Stock Entry", file_format_type="Excel", fields=["name", "stock_entry_type"],
            filters=[["Stock Entry", "name", "=", "__MES_EXPORT_NO_MATCH__"]], or_filters=[],
            order_by="name desc", start=0,
            receipt_callback_position="1", strict=None,
        )
        with (
            patch("mes_integration.mes_integration.stock_entry_export.get_form_params", return_value=params),
            patch("mes_integration.mes_integration.stock_entry_export.provide_binary_file") as download,
        ):
            export_stock_entries_with_receipt_status()
        sheet = load_workbook(BytesIO(download.call_args.args[2])).active
        self.assertEqual(sheet.max_row, 1)
        self.assertEqual(sheet.max_column, 5)
        self.assertEqual(sheet.cell(1, 3).value, frappe._("MES入库回传"))

    def test_statuses_use_only_readable_mes_rows_and_current_draft_state(self):
        rows = [frappe._dict(name="MES-1", docstatus=1, custom_mes_receipt=1),
                frappe._dict(name="MES-DRAFT", docstatus=0, custom_mes_receipt=1),
                frappe._dict(name="MANUAL", docstatus=1, custom_mes_receipt=0)]
        with (
            patch("mes_integration.mes_integration.stock_entry_export.frappe.get_list", return_value=rows),
            patch("mes_integration.mes_integration.stock_entry_export.get_mes_receipt_callback_statuses", return_value={"MES-1": "Success"}) as statuses,
        ):
            values = get_receipt_export_values(["MES-1", "MES-DRAFT", "MANUAL", "UNREADABLE"])
        statuses.assert_called_once_with(["MES-1"])
        self.assertEqual(values, {"MES-1": frappe._("已回传"), "MES-DRAFT": frappe._("待提交"), "MANUAL": ""})

    def test_virtual_callback_column_keeps_native_order_and_owner_last(self):
        native = Workbook()
        sheet = native.active
        sheet.append(["Sr", "Name", "Type", "Owner"])
        sheet.append([1, "MAT-STE-1", "Material Receipt", "creator@example.com"])
        sheet.append([2, "MAT-STE-2", "Material Receipt", "creator@example.com"])
        source = BytesIO()
        native.save(source)
        params = frappe._dict(doctype="Stock Entry", file_format_type="Excel",
                              fields=["name", "stock_entry_type"], receipt_callback_position="1")
        with (
            patch("mes_integration.mes_integration.stock_entry_export.get_form_params", return_value=params),
            patch("mes_integration.mes_integration.stock_entry_export._export_query", return_value=("Stock Entry", "xlsx", source.getvalue())) as native_export,
            patch("mes_integration.mes_integration.stock_entry_export.get_receipt_export_values", return_value={"MAT-STE-1": "已回传", "MAT-STE-2": "待提交"}) as statuses,
            patch("mes_integration.mes_integration.stock_entry_export.provide_binary_file") as download,
        ):
            export_stock_entries_with_receipt_status()

        self.assertNotIn("receipt_callback_position", native_export.call_args.args[0])
        statuses.assert_called_once_with(["MAT-STE-1", "MAT-STE-2"])
        result = load_workbook(BytesIO(download.call_args.args[2])).active
        self.assertEqual(list(result.values), [
            ("Sr", "Name", frappe._("MES入库回传"), "Type", "Owner"),
            (1, "MAT-STE-1", "已回传", "Material Receipt", "creator@example.com"),
            (2, "MAT-STE-2", "待提交", "Material Receipt", "creator@example.com"),
        ])

    def test_rejects_other_doctypes_before_native_export(self):
        params = frappe._dict(doctype="Sales Invoice", file_format_type="Excel",
                              fields=["name"], receipt_callback_position="0")
        with (
            patch("mes_integration.mes_integration.stock_entry_export.get_form_params", return_value=params),
            patch("mes_integration.mes_integration.stock_entry_export._export_query") as native_export,
            self.assertRaises(frappe.ValidationError),
        ):
            export_stock_entries_with_receipt_status()
        native_export.assert_not_called()
