import json

import frappe
from frappe.utils import cint


BATCH_BIN_FIELDS = [
    "item_code",
    "warehouse",
    "actual_qty",
    "reserved_qty",
    "projected_qty",
    "stock_uom",
]


@frappe.whitelist(methods=["POST"])
def create_stock_entry(data=None, stock_entry=None, submit=None):
    """Create a MES receipt; explicit submit=0 or review mode keeps a draft."""
    from mes_integration.mes_integration.stock_entry import (
        create_draft_stock_entry_from_mes,
    )

    if submit is None:
        submit = True

    return create_draft_stock_entry_from_mes(
        data=data,
        stock_entry=stock_entry,
        submit=submit,
    )


@frappe.whitelist(methods=["POST"])
def create_and_submit_stock_entry(data=None, stock_entry=None):
    """Create and submit a MES receipt Stock Entry."""
    from mes_integration.mes_integration.stock_entry import (
        create_and_submit_stock_entry_from_mes,
    )

    return create_and_submit_stock_entry_from_mes(
        data=data,
        stock_entry=stock_entry,
    )


@frappe.whitelist(methods=["POST"])
def update_sales_order_fields_batch(data=None):
    """Apply an idempotent batch of MES ODT and delivery-date changes."""
    from mes_integration.mes_integration.sales_order_field_change import (
        update_sales_order_fields_batch as update_batch,
    )

    return update_batch(data=data)


@frappe.whitelist(methods=["GET", "POST"])
def get_sales_order_field_change_result(operation_id=None):
    """Return the persisted result for one MES field-change operation."""
    from mes_integration.mes_integration.sales_order_field_change import (
        get_sales_order_field_change_result as get_result,
    )

    return get_result(operation_id=operation_id)


@frappe.whitelist(methods=["POST"])
def create_material_request(data=None, material_request=None):
    """Short public alias for MES to enqueue a Material Request creation task."""
    from mes_integration.mes_integration.material_request import (
        create_and_submit_material_request_from_mes,
    )

    return create_and_submit_material_request_from_mes(
        data=data, material_request=material_request
    )


@frappe.whitelist(methods=["GET", "POST"])
def get_material_request_task_status(task_id=None, request_id=None):
    """Short public alias for MES to query an async Material Request task."""
    from mes_integration.mes_integration.material_request import (
        get_material_request_task_status as get_task_status,
    )

    return get_task_status(task_id=task_id, request_id=request_id)


@frappe.whitelist(methods=["GET", "POST"])
def get_batch_bin_rows(item_codes=None):
    """Return inventory rows for multiple item codes using the current user's permissions.

    ``item_codes`` may be a JSON array (POST) or a comma-separated string (GET).
    The response deliberately uses a fixed field set so callers do not need to
    send a long ``fields`` query parameter.

    An item that exists in ERPNext but has no Bin row is returned as a
    synthetic zero-stock row, not an invalid request.  This keeps clients that
    identify returned items from ``rows`` compatible while
    ``no_stock_item_codes`` still identifies why the row contains zero stock.
    Disabled and non-stock items are returned in dedicated lists and do not
    receive synthetic inventory rows. Only codes missing from Item master are
    returned as ``invalid_item_codes``.
    """
    from mes_integration.mes_integration.stock_entry import validate_mes_api_user

    validate_mes_api_user()

    requested_item_codes = _normalize_item_codes(item_codes)
    if not requested_item_codes:
        frappe.throw(frappe._("缺少有效的 item_codes 参数"))

    if not frappe.has_permission("Bin", "read"):
        frappe.throw(frappe._("当前用户缺少 Bin 的读取权限"), frappe.PermissionError)

    rows = frappe.get_list(
        "Bin",
        filters={"item_code": ["in", requested_item_codes]},
        fields=BATCH_BIN_FIELDS,
        order_by="item_code asc, warehouse asc",
        limit_page_length=0,
    )

    item_rows = frappe.get_list(
        "Item",
        filters={"name": ["in", requested_item_codes]},
        fields=["name", "stock_uom", "is_stock_item", "disabled"],
        limit_page_length=0,
    )
    items_by_code = {row.get("name"): row for row in item_rows}
    existing_item_codes = set(items_by_code)
    disabled_item_codes = [
        item_code
        for item_code in requested_item_codes
        if item_code in existing_item_codes
        and cint(items_by_code[item_code].get("disabled"))
    ]
    non_stock_item_codes = [
        item_code
        for item_code in requested_item_codes
        if item_code in existing_item_codes
        and not cint(items_by_code[item_code].get("disabled"))
        and not cint(items_by_code[item_code].get("is_stock_item"))
    ]
    available_item_codes = {
        item_code
        for item_code in requested_item_codes
        if item_code in existing_item_codes
        and not cint(items_by_code[item_code].get("disabled"))
        and cint(items_by_code[item_code].get("is_stock_item"))
    }
    rows = [row for row in rows if row.get("item_code") in available_item_codes]
    returned_item_codes = {row.get("item_code") for row in rows}
    no_stock_item_codes = [
        item_code
        for item_code in requested_item_codes
        if item_code in available_item_codes and item_code not in returned_item_codes
    ]
    invalid_item_codes = [
        item_code
        for item_code in requested_item_codes
        if item_code not in existing_item_codes
    ]

    rows.extend(
        {
            "item_code": item_code,
            "warehouse": None,
            "actual_qty": 0,
            "reserved_qty": 0,
            "projected_qty": 0,
            "stock_uom": items_by_code[item_code].get("stock_uom"),
        }
        for item_code in no_stock_item_codes
    )
    rows.sort(key=lambda row: (row.get("item_code") or "", row.get("warehouse") or ""))

    return {
        "success": True,
        "rows": rows,
        "no_stock_item_codes": no_stock_item_codes,
        "disabled_item_codes": disabled_item_codes,
        "non_stock_item_codes": non_stock_item_codes,
        "unavailable_item_codes": disabled_item_codes + non_stock_item_codes,
        "invalid_item_codes": invalid_item_codes,
        # Keep the old key for MES clients that still read it.  It now means
        # genuinely unknown Item codes, not valid items with zero stock.
        "missing_item_codes": invalid_item_codes,
    }


def _normalize_item_codes(item_codes):
    """Normalize POST arrays and GET comma-separated item code values."""
    if item_codes is None and getattr(frappe.local, "request", None):
        request_json = frappe.request.get_json(silent=True)
        if isinstance(request_json, dict):
            item_codes = request_json.get("item_codes")

    if isinstance(item_codes, str):
        value = item_codes.strip()
        if not value:
            return []

        try:
            parsed_value = json.loads(value)
        except ValueError:
            parsed_value = None

        if isinstance(parsed_value, list):
            item_codes = parsed_value
        else:
            item_codes = value.split(",")

    if not isinstance(item_codes, (list, tuple)):
        return []

    normalized_item_codes = []
    seen_item_codes = set()
    for item_code in item_codes:
        if not isinstance(item_code, str):
            continue

        item_code = item_code.strip()
        if item_code and item_code not in seen_item_codes:
            normalized_item_codes.append(item_code)
            seen_item_codes.add(item_code)

    return normalized_item_codes
