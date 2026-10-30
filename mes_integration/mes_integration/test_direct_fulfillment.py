"""Direct NSF fulfillment must not create MES status logs or outbound requests."""

import builtins
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from mes_integration.mes_integration import delivery_note, sales_order, settings


def order(code="NSF4030112104", **values):
	return frappe._dict(name="TEST-NSF-SO", company="MX", docstatus=1, custom_crm_order_no="TEST-CRM",
		custom_process_status="Pending Deposit Confirmation",
		items=[frappe._dict(item_code=code, qty=10)], **values)


class TestDirectFulfillmentMESCallbacks(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.enterContext(patch("crm_integration.crm_integration.production_routing.get_company_direct_product_prefixes", return_value=frozenset({"NSF"})))

	def test_policy_is_limited_to_crm_nsf_orders(self):
		self.assertFalse(settings.requires_sales_order_mes_sync(order()))
		self.assertTrue(settings.requires_sales_order_mes_sync(order("NBD4030000101")))
		native = order(); native.custom_crm_order_no = None
		self.assertTrue(settings.requires_sales_order_mes_sync(native))
		mixed = order(); mixed["items"].append(frappe._dict(item_code="NBD4030000101", qty=10))
		self.assertTrue(settings.requires_sales_order_mes_sync(mixed))

	def test_missing_crm_policy_keeps_mes_optional_dependency_compatible(self):
		original = builtins.__import__
		def unavailable(name, *args, **kwargs):
			if name == "crm_integration.crm_integration.production_routing":
				raise ModuleNotFoundError("missing optional policy", name=name)
			return original(name, *args, **kwargs)
		with patch("builtins.__import__", side_effect=unavailable):
			self.assertTrue(settings.requires_sales_order_mes_sync(order()))

	def test_internal_import_errors_are_not_silently_treated_as_missing_optional_crm(self):
		original = builtins.__import__
		def broken(name, *args, **kwargs):
			if name == "crm_integration.crm_integration.production_routing":
				raise ModuleNotFoundError("broken internal dependency", name="internal_dependency")
			return original(name, *args, **kwargs)
		with patch("builtins.__import__", side_effect=broken), self.assertRaises(ModuleNotFoundError):
			settings.requires_sales_order_mes_sync(order())

	def test_sales_order_callback_skips_payload_logs_and_network(self):
		with patch.object(sales_order, "build_sales_order_status_payload") as payload, patch.object(sales_order, "create_mes_log") as log, patch.object(sales_order, "post_stock_entry_status_to_mes") as post:
			result = sales_order.push_sales_order_status_doc_to_mes(order(), triggered_status="Deliverable")
		self.assertEqual(result["status"], "skipped")
		self.assertEqual(result["reason"], "NO_MES_PRODUCTION")
		payload.assert_not_called(); log.assert_not_called(); post.assert_not_called()

	def test_queued_and_manual_sales_order_callbacks_use_the_same_skip_policy(self):
		doc = order()
		doc.check_permission = lambda permission: None
		with patch.object(frappe, "get_doc", return_value=doc), patch.object(sales_order, "is_mes_integration_enabled", return_value=True), patch.object(sales_order, "post_stock_entry_status_to_mes") as post:
			self.assertEqual(sales_order.push_sales_order_status_to_mes_job(doc.name)["status"], "skipped")
			self.assertEqual(sales_order.retry_push_sales_order_status_to_mes(doc.name)["status"], "skipped")
		post.assert_not_called()

	def test_delivery_callback_skips_nsf_order_but_processes_other_linked_orders(self):
		delivery = frappe._dict(name="TEST-DN", company="MX")
		nsf, production = order(), order("NBD4030000101")
		production.name = "TEST-MES-SO"
		def get_doc(doctype, name):
			return delivery if doctype == "Delivery Note" else nsf if name == nsf.name else production
		with (
			patch.object(frappe, "get_doc", side_effect=get_doc),
			patch.object(delivery_note, "is_mes_integration_enabled", return_value=True),
			patch.object(delivery_note, "get_linked_sales_orders", return_value=[nsf.name, production.name]),
			patch.object(delivery_note, "build_delivery_note_status_payload", return_value={"documentNo": delivery.name}) as payload,
			patch.object(delivery_note, "get_mes_status_callback_url", return_value="https://mes.invalid/status"),
			patch.object(delivery_note, "create_mes_log") as log,
			patch.object(delivery_note, "update_mes_log"),
			patch.object(delivery_note, "post_stock_entry_status_to_mes", return_value={"success": True}) as post,
			patch.object(delivery_note, "validate_stock_entry_status_response"),
		):
			result = delivery_note.push_delivery_note_status_to_mes(delivery.name)
		self.assertEqual([row["status"] for row in result], ["skipped", "success"])
		payload.assert_called_once_with(delivery, production.name)
		post.assert_called_once(); log.assert_called_once()
