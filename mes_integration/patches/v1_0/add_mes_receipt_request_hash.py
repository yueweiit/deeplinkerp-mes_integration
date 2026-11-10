"""Keep the original MES receipt identity while ERP users review an editable draft."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	create_custom_fields({
		"Stock Entry": [{
			"fieldname": "custom_mes_receipt_request_hash",
			"fieldtype": "Data",
			"insert_after": "custom_mes_receipt",
			"label": "MES入库原请求指纹",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
		}]
	})
