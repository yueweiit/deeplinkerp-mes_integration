from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from mes_integration.mes_integration.integration_log import log_inbound_stock_entry
from mes_integration.mes_integration.stock_entry import (
	build_issue_confirm_payload,
	build_mes_stock_entry_response,
	build_stock_entry_status_payload,
	create_and_submit_stock_entry_from_mes,
	enqueue_mes_stock_entry_status_callback,
	get_dlm_processed_count,
	get_existing_mes_receipt_stock_entry,
	get_mes_receipt_item_identity,
	get_sales_order_by_reference,
	is_mes_receipt_stock_entry,
	notify_mes_stock_entry_status,
	push_to_mes,
	set_mes_stock_entry_default_target_warehouses,
	set_mes_stock_entry_sales_order,
	validate_issue_confirm_response,
	validate_mes_receipt_identity,
	validate_mes_receipt_stock_entry_type,
)


class TestMESStockEntry(UnitTestCase):
	def make_issue_entry(self, name, warehouse, quantity):
		return frappe._dict(
			name=name,
			posting_date="2026-11-09",
			posting_time="08:09:10",
			purpose="Material Issue",
			items=[
				frappe._dict(
					name=f"{name}-row-1",
					idx=1,
					material_request="MAT-MR-MES-TEST",
					material_request_item="MR-ITEM-1",
					item_code="ITEM-1",
					s_warehouse=warehouse,
					qty=quantity,
					transfer_qty=quantity,
					stock_uom="kg",
				)
			],
		)

	def test_issue_confirm_v2_preserves_each_issue_warehouse_for_same_request_row(self):
		with (
			patch("mes_integration.mes_integration.stock_entry.validate_stock_entry_for_issue_confirm"),
			patch("mes_integration.mes_integration.stock_entry.get_issue_confirm_payload_version", return_value=2),
		):
			first = build_issue_confirm_payload(self.make_issue_entry("STE-1", "Warehouse A", 6))
			second = build_issue_confirm_payload(self.make_issue_entry("STE-2", "Warehouse B", 4))

		self.assertEqual(first["items"], [{"item_code": "ITEM-1", "total_issued_qty": 6}])
		self.assertEqual(second["items"], [{"item_code": "ITEM-1", "total_issued_qty": 4}])
		self.assertEqual(first["payload_version"], second["payload_version"])
		self.assertEqual(first["payload_version"], 2)
		self.assertEqual(first["posting_time"], "08:09:10")
		self.assertEqual(
			[(payload["stock_entry"], payload["issue_lines"][0]["material_request_item"],
				payload["issue_lines"][0]["s_warehouse"], payload["issue_lines"][0]["issued_qty"])
				for payload in (first, second)],
			[("STE-1", "MR-ITEM-1", "Warehouse A", 6), ("STE-2", "MR-ITEM-1", "Warehouse B", 4)],
		)
		self.assertEqual(first["issue_lines"][0]["stock_entry_detail"], "STE-1-row-1")
		self.assertEqual(second["issue_lines"][0]["stock_entry_detail"], "STE-2-row-1")
		self.assertEqual(first["issue_lines"][0]["stock_uom"], "kg")

	def test_issue_confirm_v1_keeps_existing_payload(self):
		with (
			patch("mes_integration.mes_integration.stock_entry.validate_stock_entry_for_issue_confirm"),
			patch("mes_integration.mes_integration.stock_entry.get_issue_confirm_payload_version", return_value=1),
		):
			payload = build_issue_confirm_payload(self.make_issue_entry("STE-1", "Warehouse A", 6))

		self.assertEqual(
			payload,
			{
				"material_request": "MAT-MR-MES-TEST",
				"stock_entry": "STE-1",
				"posting_date": "2026-11-09",
				"items": [{"item_code": "ITEM-1", "total_issued_qty": 6}],
			},
		)

	def test_issue_confirm_v2_rejects_unlinked_detail(self):
		entry = self.make_issue_entry("STE-1", "Warehouse A", 6)
		entry["items"][0].material_request_item = None
		with (
			patch("mes_integration.mes_integration.stock_entry.validate_stock_entry_for_issue_confirm"),
			patch("mes_integration.mes_integration.stock_entry.get_issue_confirm_payload_version", return_value=2),
			self.assertRaises(frappe.ValidationError),
		):
			build_issue_confirm_payload(entry)

	def test_issue_confirm_v2_requires_posting_time(self):
		entry = self.make_issue_entry("STE-1", "Warehouse A", 6)
		entry.posting_time = None
		with (
			patch("mes_integration.mes_integration.stock_entry.validate_stock_entry_for_issue_confirm"),
			patch("mes_integration.mes_integration.stock_entry.get_issue_confirm_payload_version", return_value=2),
			self.assertRaises(frappe.ValidationError),
		):
			build_issue_confirm_payload(entry)

	def test_issue_confirm_v2_keeps_warehouse_when_item_code_is_repeated(self):
		entry = self.make_issue_entry("STE-1", "Warehouse A", 6)
		second_row = frappe._dict(entry["items"][0].copy())
		second_row.name = "STE-1-row-2"
		second_row.idx = 2
		second_row.material_request_item = "MR-ITEM-2"
		second_row.s_warehouse = "Warehouse B"
		second_row.qty = second_row.transfer_qty = 4
		entry["items"].append(second_row)
		with (
			patch("mes_integration.mes_integration.stock_entry.validate_stock_entry_for_issue_confirm"),
			patch("mes_integration.mes_integration.stock_entry.get_issue_confirm_payload_version", return_value=2),
		):
			payload = build_issue_confirm_payload(entry)

		self.assertEqual(payload["items"], [{"item_code": "ITEM-1", "total_issued_qty": 10}])
		self.assertEqual(
			[(line["material_request_item"], line["s_warehouse"], line["issued_qty"])
				for line in payload["issue_lines"]],
			[("MR-ITEM-1", "Warehouse A", 6), ("MR-ITEM-2", "Warehouse B", 4)],
		)

	def test_push_checks_write_permission_before_external_request(self):
		stock_entry = MagicMock()
		stock_entry.name = "MAT-STE-TEST"
		stock_entry.docstatus = 1
		stock_entry.get.side_effect = lambda field: {
			"company": "Test Company",
			"custom_mes_status": "Unpushed",
		}.get(field)
		def check_permission(kind):
			if kind == "write":
				raise frappe.PermissionError("No write access")

		stock_entry.check_permission.side_effect = check_permission
		with (
			patch("mes_integration.mes_integration.stock_entry.frappe.get_doc", return_value=stock_entry),
			patch("mes_integration.mes_integration.stock_entry.is_mes_integration_enabled", return_value=True),
			patch("mes_integration.mes_integration.stock_entry.is_dlm_issue_stock_entry", return_value=True),
			patch("mes_integration.mes_integration.stock_entry.is_mes_source_stock_entry", return_value=True),
			patch("mes_integration.mes_integration.stock_entry.build_issue_confirm_payload") as build_payload,
			patch("mes_integration.mes_integration.stock_entry.create_mes_log") as create_log,
			patch("mes_integration.mes_integration.stock_entry.post_issue_confirm") as post_issue,
			self.assertRaises(frappe.PermissionError),
		):
			push_to_mes(stock_entry.name)
		build_payload.assert_not_called()
		create_log.assert_not_called()
		post_issue.assert_not_called()

	def test_receipt_warehouse_uses_company_config_not_stock_balance(self):
		stock_entry_data = {
			"company": "Test Company",
			"items": [{"item_code": "ITEM-A", "qty": 1}],
		}

		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch(
				"mes_integration.mes_integration.stock_entry.get_item_defaults",
				return_value={},
			),
			patch(
				"mes_integration.mes_integration.stock_entry.get_mes_company_warehouse",
				return_value="MES Receipt - TC",
			),
			patch(
				"mes_integration.mes_integration.stock_entry.validate_mes_warehouse_company"
			) as validate_warehouse,
			patch(
				"mes_integration.mes_integration.stock_entry.get_mes_item_largest_stock_warehouse"
			) as largest_stock_warehouse,
		):
			set_mes_stock_entry_default_target_warehouses(stock_entry_data)

		self.assertEqual(
			stock_entry_data["items"][0]["t_warehouse"],
			"MES Receipt - TC",
		)
		validate_warehouse.assert_called_once_with(
			"MES Receipt - TC", "Test Company", "入库"
		)
		largest_stock_warehouse.assert_not_called()

	def test_receipt_warehouse_requires_explicit_default_or_company_config(self):
		stock_entry_data = {
			"company": "Test Company",
			"items": [{"item_code": "ITEM-A", "qty": 1}],
		}

		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch(
				"mes_integration.mes_integration.stock_entry.get_item_defaults",
				return_value={},
			),
			patch(
				"mes_integration.mes_integration.stock_entry.get_mes_company_warehouse",
				return_value=None,
			),
			self.assertRaises(frappe.ValidationError),
		):
			set_mes_stock_entry_default_target_warehouses(stock_entry_data)

	def test_manual_stock_entry_is_not_written_to_mes_log(self):
		stock_entry = frappe._dict(
			doctype="Stock Entry",
			name="MAT-STE-MANUAL",
			company="Test Company",
			stock_entry_type="Material Receipt",
			items=[],
			flags=frappe._dict(),
		)

		with patch(
			"mes_integration.mes_integration.integration_log.log_inbound_document"
		) as log_document:
			log_inbound_stock_entry(stock_entry, "on_submit")

		log_document.assert_not_called()

	def test_mes_receipt_is_written_to_mes_log(self):
		stock_entry = frappe._dict(
			doctype="Stock Entry",
			name="MAT-STE-MES",
			company="Test Company",
			stock_entry_type="Finished Goods Receipt",
			docstatus=1,
			items=[],
			flags=frappe._dict(),
		)

		with patch(
			"mes_integration.mes_integration.integration_log.log_inbound_document"
		) as log_document:
			log_inbound_stock_entry(stock_entry, "on_submit")

		log_document.assert_called_once_with(stock_entry, "on_submit")

	def test_dlm_issue_confirm_accepts_processed_status(self):
		validate_issue_confirm_response(
			{"success": True, "data": {"status": "processed"}},
			{"stock_entry": "MAT-STE-2026-00001"},
		)

	def test_dlm_issue_confirm_v2_requires_explicit_line_acknowledgement(self):
		payload = {
			"payload_version": 2,
			"stock_entry": "STE-1",
			"issue_lines": [{"stock_entry_detail": "STE-1-row-1"}],
		}
		response = {
			"success": True,
			"data": {
				"status": "processed",
				"payload_version": 2,
				"stock_entry": "STE-1",
				"issue_lines_processed": 1,
			},
		}
		validate_issue_confirm_response(response, payload)
		self.assertEqual(get_dlm_processed_count(response), 1)
		validate_issue_confirm_response(
			{**response, "data": {**response["data"], "status": "partial"}}, payload
		)
		validate_issue_confirm_response(
			{**response, "data": {**response["data"], "status": "already_issued"}}, payload
		)
		with (
			patch("mes_integration.mes_integration.stock_entry.log_mes_push_error"),
			self.assertRaises(frappe.ValidationError),
		):
			validate_issue_confirm_response(
				{"success": True, "data": {"status": "already_issued"}}, payload
			)
		with (
			patch("mes_integration.mes_integration.stock_entry.log_mes_push_error"),
			self.assertRaises(frappe.ValidationError),
		):
			validate_issue_confirm_response(
				{**response, "data": {**response["data"], "issue_lines_processed": 0}}, payload
			)

	def test_dlm_issue_confirm_rejects_partial_status(self):
		with (
			patch("mes_integration.mes_integration.stock_entry.log_mes_push_error"),
			self.assertRaises(frappe.ValidationError),
		):
			validate_issue_confirm_response(
				{"success": True, "data": {"status": "partial"}},
				{"stock_entry": "MAT-STE-2026-00001"},
			)

	def test_dlm_issue_confirm_rejects_missing_status(self):
		with (
			patch("mes_integration.mes_integration.stock_entry.log_mes_push_error"),
			self.assertRaises(frappe.ValidationError),
		):
			validate_issue_confirm_response(
				{"success": True, "data": {}},
				{"stock_entry": "MAT-STE-2026-00001"},
			)

	def test_receipt_type_accepts_standard_material_receipt(self):
		validate_mes_receipt_stock_entry_type("Material Receipt")

	def test_standard_material_receipt_requires_mes_request_context(self):
		stock_entry = frappe._dict(stock_entry_type="Material Receipt")
		stock_entry.flags = frappe._dict()

		with patch(
			"mes_integration.mes_integration.stock_entry.is_mes_api_user",
			return_value=False,
		):
			self.assertFalse(is_mes_receipt_stock_entry(stock_entry))

		stock_entry.flags.mes_receipt_request = True
		self.assertTrue(is_mes_receipt_stock_entry(stock_entry))

	def test_manual_standard_material_receipt_does_not_trigger_mes_callback(self):
		stock_entry = frappe._dict(stock_entry_type="Material Receipt")
		stock_entry.flags = frappe._dict()

		with patch(
			"mes_integration.mes_integration.stock_entry.is_mes_api_user",
			return_value=False,
		), patch(
			"mes_integration.mes_integration.stock_entry.frappe.local.request",
			frappe._dict(path="/api/method/frappe.desk.form.save.savedocs"),
			create=True,
		):
			self.assertFalse(is_mes_receipt_stock_entry(stock_entry))

	def test_persisted_mes_receipt_marker_triggers_callback(self):
		stock_entry = frappe._dict(
			stock_entry_type="Material Receipt",
			custom_mes_receipt=1,
		)
		stock_entry.flags = frappe._dict()

		with patch(
			"mes_integration.mes_integration.stock_entry.is_mes_api_user",
			return_value=False,
		):
			self.assertTrue(is_mes_receipt_stock_entry(stock_entry))

	def test_mes_receipt_marker_is_added_when_field_exists(self):
		stock_entry_data = {}

		with patch(
			"mes_integration.mes_integration.stock_entry.frappe.db.has_column",
			return_value=True,
		):
			from mes_integration.mes_integration.stock_entry import mark_mes_receipt_stock_entry

			mark_mes_receipt_stock_entry(stock_entry_data)

		self.assertEqual(stock_entry_data["custom_mes_receipt"], 1)

	def test_create_and_submit_alias_submits(self):
		with patch(
			"mes_integration.mes_integration.stock_entry.create_draft_stock_entry_from_mes",
			return_value={"status": "success"},
		) as create_stock_entry:
			result = create_and_submit_stock_entry_from_mes(
				data={"stock_entry_type": "Material Receipt"}
			)

		create_stock_entry.assert_called_once_with(
			data={"stock_entry_type": "Material Receipt"},
			stock_entry=None,
			submit=True,
		)
		self.assertEqual(result["status"], "success")

	def test_sales_order_reference_resolves_crm_order_number(self):
		sales_order = frappe._dict(
			name="SAL-ORD-2026-00218",
			custom_crm_order_no="SHOP20260831001",
			company="YUEWEI CN悦为中国",
		)

		with patch(
			"mes_integration.mes_integration.stock_entry.frappe.db.exists",
			return_value=False,
		), patch(
			"mes_integration.mes_integration.stock_entry.frappe.db.has_column",
			return_value=True,
		), patch(
			"mes_integration.mes_integration.stock_entry.frappe.get_list",
			return_value=[sales_order.name],
		) as get_list, patch(
			"mes_integration.mes_integration.stock_entry.frappe.get_doc",
			return_value=sales_order,
		) as get_doc:
			result = get_sales_order_by_reference(
				{"sales_order": "SHOP20260831001"}, {}, required=True
			)

		self.assertEqual(result.name, "SAL-ORD-2026-00218")
		get_doc.assert_called_once_with("Sales Order", "SAL-ORD-2026-00218")
		self.assertEqual(get_list.call_args.kwargs["filters"], {
			"custom_crm_order_no": "SHOP20260831001", "docstatus": 1,
		})

	def test_sales_order_reference_prefers_erp_order_number(self):
		sales_order = frappe._dict(
			name="SAL-ORD-2026-00218",
			custom_crm_order_no="CRM-2026-00218",
		)

		with patch(
			"mes_integration.mes_integration.stock_entry.frappe.db.exists",
			return_value=True,
		), patch(
			"mes_integration.mes_integration.stock_entry.frappe.get_doc",
			return_value=sales_order,
		) as get_doc, patch(
			"mes_integration.mes_integration.stock_entry.frappe.get_list"
		) as get_list:
			result = get_sales_order_by_reference(
				{"sales_order": "SAL-ORD-2026-00218"}, {}, required=True
			)

		self.assertEqual(result.name, "SAL-ORD-2026-00218")
		get_doc.assert_called_once_with("Sales Order", "SAL-ORD-2026-00218")
		get_list.assert_not_called()

	def test_resolved_sales_order_is_written_to_stock_entry_payload(self):
		stock_entry_data = {}
		sales_order = frappe._dict(name="SAL-ORD-2026-00218")

		with patch(
			"mes_integration.mes_integration.stock_entry.frappe.db.has_column",
			return_value=True,
		):
			set_mes_stock_entry_sales_order(stock_entry_data, sales_order)

		self.assertEqual(stock_entry_data["custom_sales_order"], sales_order.name)

	def test_receipt_status_is_enqueued_after_commit(self):
		stock_entry = frappe._dict(
			name="MAT-STE-2026-00001",
			company="Test Company",
			docstatus=1,
		)

		with (
			patch(
				"mes_integration.mes_integration.stock_entry.is_mes_integration_enabled",
				return_value=True,
			),
			patch(
				"mes_integration.mes_integration.stock_entry.is_mes_receipt_stock_entry",
				return_value=True,
			),
			patch.object(type(frappe.db.after_commit), "add") as add_callback,
			patch(
				"mes_integration.mes_integration.stock_entry.enqueue_mes_stock_entry_status_callback"
			) as enqueue_callback,
		):
			notify_mes_stock_entry_status(stock_entry, "on_submit")
			add_callback.assert_called_once()
			add_callback.call_args.args[0]()

		enqueue_callback.assert_called_once_with(stock_entry.name, 1)

	def test_receipt_status_payload_uses_event_docstatus(self):
		stock_entry = frappe._dict(
			name="MAT-STE-2026-00001",
			docstatus=2,
			custom_stock_entry_no="MES-RECEIPT-001",
		)

		payload = build_stock_entry_status_payload(stock_entry, docstatus=1)

		self.assertEqual(payload["docstatus"], 1)
		self.assertEqual(payload["erpStatus"], "submitted")

	def test_mes_receipt_item_identity_is_order_independent(self):
		first = get_mes_receipt_item_identity(
			[
				{"item_code": "ITEM-B", "qty": 2},
				{"item_code": "ITEM-A", "qty": 1},
			]
		)
		second = get_mes_receipt_item_identity(
			[
				{"item_code": "ITEM-A", "qty": 1},
				{"item_code": "ITEM-B", "qty": 2},
			]
		)

		self.assertEqual(first, second)

	def test_mes_receipt_identity_rejects_changed_request(self):
		existing = frappe._dict(
			name="MAT-STE-2026-00001",
			company="Test Company",
			stock_entry_type="Finished Goods Receipt",
			custom_stock_entry_no="MES-RECEIPT-001",
			custom_sales_order="SAL-ORD-2026-00001",
			items=[{"item_code": "ITEM-A", "qty": 1}],
		)
		request_data = {
			"company": "Test Company",
			"stock_entry_type": "Finished Goods Receipt",
			"custom_stock_entry_no": "MES-RECEIPT-001",
			"items": [{"item_code": "ITEM-B", "qty": 1}],
		}

		with self.assertRaises(frappe.ValidationError):
			validate_mes_receipt_identity(existing, request_data, None)

		self.assertEqual(
			frappe.response.get("error_code"),
			"ERP_STOCK_ENTRY_IDENTITY_CONFLICT",
		)

	def test_mes_receipt_identity_rejects_changed_batch(self):
		existing = frappe._dict(
			name="MAT-STE-2026-00001",
			company="Test Company",
			stock_entry_type="Finished Goods Receipt",
			custom_stock_entry_no="MES-RECEIPT-001",
			custom_sales_order="SAL-ORD-2026-00001",
			posting_date="2026-09-19",
			items=[
				{
					"item_code": "ITEM-A",
					"qty": 1,
					"batch_no": "BATCH-001",
				}
			],
		)
		request_data = {
			"company": "Test Company",
			"stock_entry_type": "Finished Goods Receipt",
			"custom_stock_entry_no": "MES-RECEIPT-001",
			"posting_date": "2026-09-19",
			"items": [
				{
					"item_code": "ITEM-A",
					"qty": 1,
					"batch_no": "BATCH-002",
				}
			],
		}

		with self.assertRaises(frappe.ValidationError):
			validate_mes_receipt_identity(
				existing,
				request_data,
				frappe._dict(name="SAL-ORD-2026-00001"),
			)

	def test_mes_receipt_identity_rejects_changed_posting_date(self):
		existing = frappe._dict(
			name="MAT-STE-2026-00001",
			company="Test Company",
			stock_entry_type="Finished Goods Receipt",
			custom_stock_entry_no="MES-RECEIPT-001",
			custom_sales_order="SAL-ORD-2026-00001",
			posting_date="2026-09-18",
			items=[{"item_code": "ITEM-A", "qty": 1}],
		)
		request_data = {
			"company": "Test Company",
			"stock_entry_type": "Finished Goods Receipt",
			"custom_stock_entry_no": "MES-RECEIPT-001",
			"posting_date": "2026-09-19",
			"items": [{"item_code": "ITEM-A", "qty": 1}],
		}

		with self.assertRaises(frappe.ValidationError):
			validate_mes_receipt_identity(
				existing,
				request_data,
				frappe._dict(name="SAL-ORD-2026-00001"),
			)

	def test_duplicate_mes_receipt_numbers_are_not_auto_selected(self):
		with (
			patch(
				"mes_integration.mes_integration.stock_entry.frappe.db.has_column",
				return_value=True,
			),
			patch(
				"mes_integration.mes_integration.stock_entry.frappe.get_all",
				return_value=["MAT-STE-2026-00001", "MAT-STE-2026-00002"],
			),
		):
			with self.assertRaises(frappe.ValidationError):
				get_existing_mes_receipt_stock_entry("Test Company", "MES-RECEIPT-001")

	def test_mes_receipt_lookup_excludes_material_issue_scope(self):
		with (
			patch(
				"mes_integration.mes_integration.stock_entry.frappe.db.has_column",
				return_value=True,
			),
			patch(
				"mes_integration.mes_integration.stock_entry.frappe.get_all",
				return_value=[],
			) as get_all,
		):
			result = get_existing_mes_receipt_stock_entry(
				"Test Company", "MES-RECEIPT-001"
			)

		self.assertIsNone(result)
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{
				"company": "Test Company",
				"custom_stock_entry_no": "MES-RECEIPT-001",
				"stock_entry_type": [
					"in",
					[
						"Finished Goods Receipt",
						"Material Receipt",
						"Semi Finished Goods Receipt",
					],
				],
				"custom_mes_receipt": 1,
			},
		)

	def test_distinct_payload_with_shared_receipt_number_is_rejected(self):
		existing = frappe._dict(
			name="MAT-STE-2026-00001",
			company="Test Company",
			stock_entry_type="Semi Finished Goods Receipt",
			custom_stock_entry_no="MES-RECEIPT-001",
			custom_sales_order="SAL-ORD-2026-00001",
			items=[{"item_code": "ITEM-A", "qty": 50}],
		)
		request_data = {
			"company": "Test Company",
			"stock_entry_type": "Semi Finished Goods Receipt",
			"custom_stock_entry_no": "MES-RECEIPT-001",
			"items": [{"item_code": "ITEM-A", "qty": 4}],
		}
		sales_order = frappe._dict(name="SAL-ORD-2026-00001")

		with (
			patch(
				"mes_integration.mes_integration.stock_entry.frappe.db.has_column",
				return_value=True,
			),
			patch(
				"mes_integration.mes_integration.stock_entry.frappe.get_all",
				return_value=[existing.name],
			),
			patch(
				"mes_integration.mes_integration.stock_entry.frappe.get_doc",
				return_value=existing,
			),
		):
			with self.assertRaises(frappe.ValidationError):
				get_existing_mes_receipt_stock_entry(
					"Test Company",
					"MES-RECEIPT-001",
					request_data=request_data,
					sales_order_doc=sales_order,
				)

		self.assertEqual(
			frappe.response.get("error_code"),
			"ERP_STOCK_ENTRY_IDENTITY_CONFLICT",
		)

	def test_exact_retry_with_shared_receipt_number_reuses_existing_entry(self):
		existing = frappe._dict(
			name="MAT-STE-2026-00001",
			company="Test Company",
			stock_entry_type="Semi Finished Goods Receipt",
			custom_stock_entry_no="MES-RECEIPT-001",
			custom_sales_order="SAL-ORD-2026-00001",
			items=[{"item_code": "ITEM-A", "qty": 50}],
		)
		request_data = {
			"company": "Test Company",
			"stock_entry_type": "Semi Finished Goods Receipt",
			"custom_stock_entry_no": "MES-RECEIPT-001",
			"items": [{"item_code": "ITEM-A", "qty": 50}],
		}
		sales_order = frappe._dict(name="SAL-ORD-2026-00001")

		with (
			patch(
				"mes_integration.mes_integration.stock_entry.frappe.db.has_column",
				return_value=True,
			),
			patch(
				"mes_integration.mes_integration.stock_entry.frappe.get_all",
				return_value=[existing.name],
			),
			patch(
				"mes_integration.mes_integration.stock_entry.frappe.get_doc",
				return_value=existing,
			),
		):
			result = get_existing_mes_receipt_stock_entry(
				"Test Company",
				"MES-RECEIPT-001",
				request_data=request_data,
				sales_order_doc=sales_order,
			)

		self.assertIs(result, existing)

	def test_mes_receipt_identity_conflict_uses_http_409(self):
		from mes_integration.mes_integration.stock_entry import (
			MESStockEntryIdentityConflict,
			throw_mes_receipt_identity_conflict,
		)

		with self.assertRaises(MESStockEntryIdentityConflict) as raised:
			throw_mes_receipt_identity_conflict("duplicate receipt")

		self.assertEqual(raised.exception.http_status_code, 409)
		self.assertEqual(
			frappe.response.get("error_code"),
			"ERP_STOCK_ENTRY_IDENTITY_CONFLICT",
		)

	def test_reused_mes_receipt_response_marks_idempotent_reuse(self):
		stock_entry = frappe._dict(
			name="MAT-STE-2026-00001",
			stock_entry_type="Finished Goods Receipt",
			docstatus=1,
		)

		response = build_mes_stock_entry_response(stock_entry, None, reused=True)

		self.assertTrue(response["idempotent_reuse"])
		self.assertEqual(response["stock_entry"], stock_entry.name)
		self.assertTrue(response["submitted"])

	def test_receipt_status_enqueue_has_stable_deduplication_key(self):
		with patch("mes_integration.mes_integration.stock_entry.frappe.enqueue") as enqueue:
			enqueue_mes_stock_entry_status_callback("MAT-STE-2026-00001", 2)

		enqueue.assert_called_once_with(
			"mes_integration.mes_integration.stock_entry.push_stock_entry_status_to_mes_job",
			queue="short",
			timeout=300,
			job_id="mes-stock-entry-status:MAT-STE-2026-00001:2",
			deduplicate=True,
			stock_entry_name="MAT-STE-2026-00001",
			docstatus=2,
		)
