import frappe


def is_mes_integration_enabled(company):
	if not company or not frappe.db.has_column("Company", "custom_enable_mes_integration"):
		return False

	return bool(frappe.db.get_value("Company", company, "custom_enable_mes_integration"))


def throw_mes_integration_disabled(company):
	frappe.throw(frappe._("公司 {0} 未启用 MES 集成。").format(company or ""))


def requires_sales_order_mes_sync(sales_order):
	"""Honor CRM's direct-fulfillment policy without making CRM a required MES dependency."""
	if not sales_order.get("custom_crm_order_no"):
		return True
	try:
		from crm_integration.crm_integration.production_routing import is_direct_fulfillment_order
	except ModuleNotFoundError as exc:
		if exc.name not in {"crm_integration", "crm_integration.crm_integration", "crm_integration.crm_integration.production_routing"}:
			raise
		return True
	return not is_direct_fulfillment_order(sales_order)
