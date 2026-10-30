import hashlib
import json
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, flt, now

from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.stock.doctype.item.item import get_item_defaults

try:
    from crm_integration.crm_integration.sales_order import (
        CRM_STATUS_PRODUCTION_PROGRESS_REPORTED,
        DELIVERABLE,
        PENDING_FINAL_PAYMENT,
        PENDING_PRODUCTION,
        PARTIALLY_DELIVERED,
        enqueue_sales_order_status_to_crm,
        set_process_status,
    )
except ModuleNotFoundError as exc:
    if not exc.name or not exc.name.startswith("crm_integration"):
        raise

    CRM_INTEGRATION_AVAILABLE = False
    CRM_STATUS_PRODUCTION_PROGRESS_REPORTED = "PRODUCTION_PROGRESS_REPORTED"
    DELIVERABLE = "Deliverable"
    PENDING_FINAL_PAYMENT = "Pending Final Payment"
    PENDING_PRODUCTION = "Pending Production"
    PARTIALLY_DELIVERED = "Partially Delivered"

    def enqueue_sales_order_status_to_crm(*args, **kwargs):
        return None

    def set_process_status(*args, **kwargs):
        return None
else:
    CRM_INTEGRATION_AVAILABLE = True
from mes_integration.mes_integration.integration_log import create_mes_log, update_mes_log
from mes_integration.mes_integration.settings import is_mes_integration_enabled, requires_sales_order_mes_sync, throw_mes_integration_disabled
from mes_integration.mes_integration.stock_entry import (
    MES_DELIVERY_WAREHOUSE_FIELD,
    get_mes_company_warehouse,
    get_mes_item_largest_stock_warehouse,
    get_mes_status_callback_url,
    post_stock_entry_status_to_mes,
    validate_mes_api_user,
    validate_stock_entry_status_response,
)


MES_DELIVERY_REQUEST_FIELD = "custom_mes_delivery_request_no"
MES_DELIVERY_IDEMPOTENCY_LOCK_TIMEOUT_SECONDS = 30
MES_DELIVERY_STATUS_QUEUE = "short"
MES_DELIVERY_STATUS_TIMEOUT = 300


class MESDeliveryNoteIdentityConflict(frappe.ValidationError):
    http_status_code = 409


@frappe.whitelist(methods=["POST"])
def create_draft_delivery_note_from_mes(data=None):
    payload = parse_json_if_needed(data)

    if not isinstance(payload, dict):
        frappe.throw(_("缺少请求数据或数据格式不正确"))

    validate_mes_api_user()
    frappe.throw(_("MES 销售出库草稿接口已停用，请由 CRM 发货接口创建并提交销售出库。"))


def enqueue_crm_production_progress_event(payload, delivery_note):
    if not CRM_INTEGRATION_AVAILABLE:
        return

    try:
        items = get_crm_production_progress_items(payload.get("items"))
        enqueue_sales_order_status_to_crm(
            sales_order_name=payload.get("sales_order"),
            external_status=CRM_STATUS_PRODUCTION_PROGRESS_REPORTED,
            triggered_status=PENDING_FINAL_PAYMENT,
            trigger_event="production_progress_reported",
            delivery_note_name=delivery_note.name,
            items=items,
            production_batch_no=get_crm_production_batch_no(payload, delivery_note),
            remark=get_crm_production_progress_remark(delivery_note.name, items),
        )
    except Exception:
        frappe.log_error(
            title="Failed to enqueue CRM production progress event",
            message=frappe.get_traceback(),
        )


def get_crm_production_batch_no(payload, delivery_note):
    """Return the batch identity used to keep production retries idempotent."""
    for key in ("production_batch_no", "productionBatchNo", "batch_no", "batchNo"):
        batch_no = str(payload.get(key) or "").strip()
        if batch_no:
            return batch_no

    batch_numbers = {
        str(row.get(key) or "").strip()
        for row in payload.get("items") or []
        if isinstance(row, dict)
        for key in ("production_batch_no", "productionBatchNo", "batch_no", "batchNo")
        if str(row.get(key) or "").strip()
    }
    if len(batch_numbers) == 1:
        return batch_numbers.pop()
    if len(batch_numbers) > 1:
        return ",".join(sorted(batch_numbers))

    # Older MES payloads did not carry a batch number. The ERP draft created for
    # this one report is still a stable, unique event identity for retries.
    return delivery_note.name


def get_crm_production_progress_items(mes_items):
    qty_by_item = {}
    for row in mes_items or []:
        if not isinstance(row, dict):
            continue
        item_code = row.get("item_code")
        qty = flt(row.get("qty"))
        if not item_code or qty <= 0:
            continue
        qty_by_item[item_code] = flt(qty_by_item.get(item_code)) + qty

    return [
        {"externalItemId": item_code, "quantity": qty}
        for item_code, qty in sorted(qty_by_item.items())
    ]


def get_crm_production_progress_remark(delivery_note_name, items):
    if not items:
        return _("MES已汇报生产进度")

    item_descriptions = [
        _("{0} {1}个").format(item.get("externalItemId"), item.get("quantity"))
        for item in items
    ]
    return _("本次生产：{0}").format(", ".join(item_descriptions))


def create_draft_delivery_note(payload, request_key=None):
    ensure_crm_integration_available()

    sales_order_name = payload.get("sales_order")
    if not sales_order_name:
        frappe.throw(_("缺少销售订单编号 sales_order"))

    if not frappe.db.exists("Sales Order", sales_order_name):
        frappe.throw(_("未找到销售订单 {0}").format(sales_order_name))

    sales_order = frappe.get_doc("Sales Order", sales_order_name)
    sales_order.check_permission("read")
    validate_sales_order_for_mes_delivery_note(sales_order)

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        frappe.throw(_("销售出库明细不能为空"))

    allocations = allocate_delivery_note_items(sales_order, items)

    delivery_note = make_delivery_note(
        sales_order.name,
        kwargs={"skip_item_mapping": True, "for_reserved_stock": False},
    )
    delivery_note.set("items", [])

    for allocation in allocations:
        append_delivery_note_item(delivery_note, allocation)

    delivery_note.run_method("set_missing_values")
    delivery_note.run_method("set_po_nos")
    delivery_note.run_method("calculate_taxes_and_totals")
    delivery_note.run_method("set_use_serial_batch_fields")
    if request_key and frappe.db.has_column("Delivery Note", MES_DELIVERY_REQUEST_FIELD):
        delivery_note.set(MES_DELIVERY_REQUEST_FIELD, request_key)
    delivery_note.insert()

    if sales_order.get("custom_process_status") == PENDING_PRODUCTION:
        set_process_status(sales_order, PENDING_FINAL_PAYMENT)
    if sales_order.get("custom_process_status") == PARTIALLY_DELIVERED:
        delivery_note.db_set("custom_delivery_readiness_status", "Ready to Deliver", update_modified=False)
    return delivery_note


def get_mes_delivery_request_key(payload):
    candidates = [
        payload.get("request_id"),
        payload.get("idempotency_key"),
        payload.get("production_batch_no"),
        payload.get("productionBatchNo"),
        payload.get("batch_no"),
        payload.get("batchNo"),
    ]
    request_key = next(
        (
            str(candidate).strip()
            for candidate in candidates
            if candidate is not None and str(candidate).strip()
        ),
        None,
    )
    if not request_key:
        frappe.throw(
            _("缺少销售出库幂等请求号，请传 request_id 或 production_batch_no。")
        )
    if len(request_key) > 140:
        frappe.throw(_("销售出库幂等请求号长度不能超过 140 个字符。"))
    return request_key


@contextmanager
def lock_mes_delivery_note_request(company, sales_order_name, request_key):
    """Serialize retries for one MES delivery request until commit or rollback."""
    if (
        not company
        or not sales_order_name
        or not request_key
        or getattr(frappe.db, "db_type", None) != "mariadb"
    ):
        yield
        return

    lock_name = "mes_delivery_" + hashlib.sha256(
        f"{getattr(frappe.local, 'site', '')}|{company}|{sales_order_name}|{request_key}".encode(
            "utf-8"
        )
    ).hexdigest()
    result = frappe.db.sql(
        "SELECT GET_LOCK(%s, %s)",
        (lock_name, MES_DELIVERY_IDEMPOTENCY_LOCK_TIMEOUT_SECONDS),
    )
    if not result or cint(result[0][0]) != 1:
        raise frappe.QueryDeadlockError(
            f"Timed out waiting for MES Delivery Note idempotency lock: {request_key}"
        )

    try:
        yield
    finally:
        release_lock = lambda: release_mes_delivery_note_lock(lock_name)
        frappe.db.after_commit.add(release_lock)
        frappe.db.after_rollback.add(release_lock)


def release_mes_delivery_note_lock(lock_name):
    frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))


def lock_sales_order_for_mes_delivery_note(sales_order_name):
    if not sales_order_name or getattr(frappe.db, "db_type", None) == "sqlite":
        return

    frappe.db.sql(
        "SELECT name FROM `tabSales Order` WHERE name = %s FOR UPDATE",
        (sales_order_name,),
    )


def get_existing_mes_delivery_note(company, request_key, payload):
    if not frappe.db.has_column("Delivery Note", MES_DELIVERY_REQUEST_FIELD):
        frappe.throw(
            _("缺少 Delivery Note MES 幂等字段，请先执行 bench migrate。")
        )

    names = frappe.get_all(
        "Delivery Note",
        filters={
            "company": company,
            MES_DELIVERY_REQUEST_FIELD: request_key,
        },
        pluck="name",
        order_by="creation asc",
        limit_page_length=2,
    )
    if len(names) > 1:
        throw_mes_delivery_note_identity_conflict(
            f"MES 销售出库请求号 {request_key} 已对应多张 Delivery Note：{', '.join(names)}。"
        )
    if not names:
        return None

    delivery_note = frappe.get_doc("Delivery Note", names[0])
    if delivery_note.docstatus == 2:
        throw_mes_delivery_note_identity_conflict(
            f"MES 销售出库请求号 {request_key} 已对应取消单据 {delivery_note.name}。"
        )

    mismatches = get_mes_delivery_note_identity_mismatches(delivery_note, payload)
    if mismatches:
        throw_mes_delivery_note_identity_conflict(
            f"MES 销售出库请求号 {request_key} 已对应 {delivery_note.name}，"
            f"但本次请求字段不一致：{', '.join(mismatches)}。"
        )

    return delivery_note


def get_mes_delivery_note_identity_mismatches(delivery_note, payload):
    mismatches = []
    requested_sales_order = payload.get("sales_order")
    existing_sales_orders = get_linked_sales_orders(delivery_note)
    if existing_sales_orders != [requested_sales_order]:
        mismatches.append("sales_order")

    requested_items = payload.get("items") or []
    if get_mes_delivery_note_qty_by_item(requested_items) != get_mes_delivery_note_qty_by_item(
        delivery_note.get("items") or []
    ):
        mismatches.append("items.qty")

    if any(row.get("warehouse") for row in requested_items if isinstance(row, dict)):
        if get_mes_delivery_note_qty_by_item_field(
            requested_items, "warehouse"
        ) != get_mes_delivery_note_qty_by_item_field(
            delivery_note.get("items") or [], "warehouse"
        ):
            mismatches.append("items.warehouse")

    if any(row.get("batch_no") for row in requested_items if isinstance(row, dict)):
        if get_mes_delivery_note_qty_by_item_field(
            requested_items, "batch_no"
        ) != get_mes_delivery_note_qty_by_item_field(
            delivery_note.get("items") or [], "batch_no"
        ):
            mismatches.append("items.batch_no")

    return mismatches


def get_mes_delivery_note_qty_by_item(items):
    quantities = {}
    for row in items or []:
        if not hasattr(row, "get"):
            continue
        item_code = row.get("item_code")
        if item_code:
            quantities[item_code] = flt(
                flt(quantities.get(item_code)) + flt(row.get("qty")),
                9,
            )
    return quantities


def get_mes_delivery_note_qty_by_item_field(items, fieldname):
    quantities = {}
    for row in items or []:
        if not hasattr(row, "get"):
            continue
        item_code = row.get("item_code")
        field_value = row.get(fieldname)
        if not item_code:
            continue
        key = (item_code, field_value or "")
        quantities[key] = flt(flt(quantities.get(key)) + flt(row.get("qty")), 9)
    return quantities


def throw_mes_delivery_note_identity_conflict(message):
    frappe.response["error_code"] = "ERP_DELIVERY_NOTE_IDENTITY_CONFLICT"
    frappe.throw(
        f"ERP_DELIVERY_NOTE_IDENTITY_CONFLICT: {message}",
        exc=MESDeliveryNoteIdentityConflict,
        title=_("MES 销售出库幂等编号冲突"),
    )


def validate_sales_order_for_mes_delivery_note(sales_order):
    if sales_order.docstatus != 1:
        frappe.throw(_("销售订单 {0} 必须已提交").format(sales_order.name))

    process_status = sales_order.get("custom_process_status")
    if process_status not in (PENDING_PRODUCTION, PENDING_FINAL_PAYMENT, DELIVERABLE,PARTIALLY_DELIVERED):
        frappe.throw(
            _("销售订单 {0} 状态为 {1}，不允许创建出库草稿").format(
                sales_order.name, process_status or ""
            )
        )


def allocate_delivery_note_items(sales_order, items):
    remaining_by_item = get_sales_order_remaining_qty_by_item(sales_order)
    allocations = []

    for index, request_row in enumerate(items, start=1):
        row = frappe._dict(request_row or {})
        item_code = row.get("item_code")
        qty = flt(row.get("qty"))

        if not item_code:
            frappe.throw(_("第 {0} 行缺少物料编码 item_code").format(index))

        if qty <= 0:
            frappe.throw(_("第 {0} 行出库数量必须大于 0").format(index))

        sales_order_rows = remaining_by_item.get(item_code)
        if not sales_order_rows:
            frappe.throw(_("物料 {0} 不在销售订单 {1} 的明细中").format(item_code, sales_order.name))

        available_qty = sum(flt(so_row.remaining_qty) for so_row in sales_order_rows)
        if qty > available_qty:
            frappe.throw(
                _("物料 {0} 出库数量 {1} 超过剩余可出库数量 {2}").format(
                    item_code, format_float(qty), format_float(available_qty)
                )
            )

        warehouse = get_mes_delivery_note_item_warehouse(
            item_code,
            sales_order.company,
            row.get("warehouse"),
        )

        qty_to_allocate = qty
        for so_row in sales_order_rows:
            if qty_to_allocate <= 0:
                break

            row_remaining_qty = flt(so_row.remaining_qty)
            if row_remaining_qty <= 0:
                continue

            allocated_qty = min(qty_to_allocate, row_remaining_qty)
            allocations.append(
                frappe._dict(
                    sales_order=sales_order,
                    sales_order_item=so_row.doc,
                    qty=allocated_qty,
                    warehouse=warehouse,
                    batch_no=row.get("batch_no"),
                )
            )

            so_row.remaining_qty = row_remaining_qty - allocated_qty
            qty_to_allocate -= allocated_qty

    return allocations


def get_sales_order_remaining_qty_by_item(sales_order):
    draft_qty_by_so_detail = get_draft_delivery_note_qty_by_so_detail(sales_order.name)
    remaining_by_item = {}

    for row in sales_order.get("items", []):
        if not row.get("item_code") or row.get("delivered_by_supplier"):
            continue

        remaining_qty = flt(row.get("qty")) - flt(row.get("delivered_qty")) - flt(
            draft_qty_by_so_detail.get(row.name)
        )
        if remaining_qty <= 0:
            continue

        remaining_by_item.setdefault(row.item_code, []).append(
            frappe._dict(doc=row, remaining_qty=remaining_qty)
        )

    return remaining_by_item


def get_draft_delivery_note_qty_by_so_detail(sales_order):
    rows = frappe.db.sql(
        """
        SELECT dni.so_detail, SUM(IFNULL(dni.qty, 0)) AS qty
        FROM `tabDelivery Note Item` dni
        INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
        WHERE dni.against_sales_order = %s
            AND dn.docstatus = 0
            AND IFNULL(dni.so_detail, '') != ''
        GROUP BY dni.so_detail
        """,
        (sales_order,),
        as_dict=True,
    )
    return {row.so_detail: flt(row.qty) for row in rows}


def format_float(value):
    return frappe.format_value(flt(value), {"fieldtype": "Float"})


def get_mes_delivery_note_item_warehouse(item_code, company, requested_warehouse=None):
    if requested_warehouse:
        validate_warehouse_company(requested_warehouse, company)
        return requested_warehouse

    default_warehouse = (get_item_defaults(item_code, company) or {}).get("default_warehouse")
    if default_warehouse:
        validate_warehouse_company(default_warehouse, company)
        return default_warehouse

    configured_warehouse = get_mes_company_warehouse(
        company,
        MES_DELIVERY_WAREHOUSE_FIELD,
    )
    if configured_warehouse:
        validate_warehouse_company(configured_warehouse, company)
        return configured_warehouse

    # Backward-compatible final fallback for existing companies. New
    # integrations should send warehouse explicitly or configure the company.
    stock_warehouse = get_mes_item_largest_stock_warehouse(item_code, company)
    if stock_warehouse:
        return stock_warehouse

    frappe.throw(
        _(
            "物料 {0} 缺少出库仓库；请传 warehouse、设置物料默认仓库，"
            "或配置公司 MES 默认出库仓。"
        ).format(item_code)
    )


def validate_warehouse_company(warehouse, company):
    warehouse_doc = frappe.db.get_value(
        "Warehouse",
        warehouse,
        ["name", "company", "is_group", "disabled"],
        as_dict=True,
    )

    if not warehouse_doc:
        frappe.throw(_("未找到仓库 {0}").format(warehouse))

    if warehouse_doc.is_group or warehouse_doc.disabled:
        frappe.throw(_("仓库 {0} 不可用于出库").format(warehouse))

    if warehouse_doc.company and warehouse_doc.company != company:
        frappe.throw(_("仓库 {0} 不属于公司 {1}").format(warehouse, company))


def append_delivery_note_item(delivery_note, allocation):
    sales_order_item = allocation.sales_order_item
    qty = flt(allocation.qty)
    conversion_factor = flt(sales_order_item.get("conversion_factor")) or 1
    rate = flt(sales_order_item.get("rate"))
    base_rate = flt(sales_order_item.get("base_rate"))

    if not allocation.warehouse:
        frappe.throw(_("物料 {0} 缺少出库仓库").format(sales_order_item.item_code))

    row = delivery_note.append(
        "items",
        {
            "item_code": sales_order_item.item_code,
            "item_name": sales_order_item.item_name,
            "description": sales_order_item.description,
            "qty": qty,
            "stock_qty": qty * conversion_factor,
            "uom": sales_order_item.uom,
            "stock_uom": sales_order_item.stock_uom,
            "conversion_factor": conversion_factor,
            "warehouse": allocation.warehouse,
            "rate": rate,
            "base_rate": base_rate,
            "amount": qty * rate,
            "base_amount": qty * base_rate,
            "against_sales_order": allocation.sales_order.name,
            "so_detail": sales_order_item.name,
            "delivery_date": sales_order_item.get("delivery_date") or allocation.sales_order.get("delivery_date"),
        },
    )

    if allocation.batch_no and frappe.db.get_value("Item", sales_order_item.item_code, "has_batch_no"):
        row.batch_no = allocation.batch_no




PENDING_RELEASE = "Pending Release"
READY_TO_DELIVER = "Ready to Deliver"
DELIVERED = "Delivered"
DELIVERY_NOTE_STATUS_EVENT = "Delivery Note Status Callback"
DELIVERY_NOTE_STATUS_DOCUMENT_TYPE = "delivery_note"


def set_delivery_readiness_status(doc, method=None):
    if not CRM_INTEGRATION_AVAILABLE or not is_mes_integration_enabled(doc.get("company")):
        return

    doc.custom_delivery_readiness_status = get_delivery_readiness_status(doc)


def set_delivered_readiness_status(doc, method=None):
    if not CRM_INTEGRATION_AVAILABLE or not is_mes_integration_enabled(doc.get("company")):
        return

    status = get_delivery_readiness_status(doc)
    if frappe.db.has_column("Delivery Note", "custom_delivery_readiness_status"):
        doc.db_set("custom_delivery_readiness_status", status, update_modified=False)


def clear_delivery_readiness_status(doc, method=None):
    if not CRM_INTEGRATION_AVAILABLE or not is_mes_integration_enabled(doc.get("company")):
        return

    if frappe.db.has_column("Delivery Note", "custom_delivery_readiness_status"):
        doc.db_set("custom_delivery_readiness_status", None, update_modified=False)


def get_delivery_readiness_status(doc):
    if not CRM_INTEGRATION_AVAILABLE:
        return None

    statuses = get_linked_sales_order_process_statuses(doc)
    if not statuses:
        return None

    if doc.docstatus == 1:
        return DELIVERED

    if doc.docstatus != 0:
        return None

    if all(status in (DELIVERABLE,PARTIALLY_DELIVERED) for status in statuses):
        return READY_TO_DELIVER

    return PENDING_RELEASE


def get_linked_sales_order_process_statuses(doc):
    sales_orders = sorted(
        {
            row.get("against_sales_order")
            for row in doc.get("items", [])
            if row.get("against_sales_order")
        }
    )
    if not sales_orders:
        return []

    rows = frappe.get_all(
        "Sales Order",
        filters={"name": ["in", sales_orders]},
        pluck="custom_process_status",
    )
    return [status for status in rows if status]


def update_delivery_readiness_status_for_sales_orders(sales_orders):
    if not CRM_INTEGRATION_AVAILABLE:
        return

    if isinstance(sales_orders, str):
        sales_orders = [sales_orders]

    sales_orders = [name for name in sales_orders if name]
    if not sales_orders or not frappe.db.has_column("Delivery Note", "custom_delivery_readiness_status"):
        return

    delivery_notes = get_draft_delivery_notes_for_sales_orders(sales_orders)
    for delivery_note_name in delivery_notes:
        delivery_note = frappe.get_doc("Delivery Note", delivery_note_name)
        if not is_mes_integration_enabled(delivery_note.get("company")):
            continue

        status = get_delivery_readiness_status(delivery_note)
        if delivery_note.get("custom_delivery_readiness_status") != status:
            delivery_note.db_set("custom_delivery_readiness_status", status, update_modified=False)


def get_draft_delivery_notes_for_sales_orders(sales_orders):
    placeholders = ", ".join(["%s"] * len(sales_orders))
    rows = frappe.db.sql(
        f"""
        SELECT DISTINCT dn.name
        FROM `tabDelivery Note` dn
        INNER JOIN `tabDelivery Note Item` dni ON dni.parent = dn.name
        WHERE dn.docstatus = 0
            AND dni.against_sales_order IN ({placeholders})
        """,
        tuple(sales_orders),
        as_dict=True,
    )
    return [row.name for row in rows]


def enqueue_delivery_note_status_callback(delivery_note_name):
    try:
        event_docstatus = cint(
            frappe.db.get_value("Delivery Note", delivery_note_name, "docstatus")
        )
        frappe.enqueue(
            "mes_integration.mes_integration.delivery_note.push_delivery_note_status_to_mes_job",
            queue=MES_DELIVERY_STATUS_QUEUE,
            timeout=MES_DELIVERY_STATUS_TIMEOUT,
            enqueue_after_commit=True,
            job_id=f"mes-delivery-note-status:{delivery_note_name}:{event_docstatus}",
            deduplicate=True,
            delivery_note_name=delivery_note_name,
        )
    except Exception:
        frappe.log_error(
            title="Failed to enqueue MES Delivery Note status callback",
            message=frappe.get_traceback(),
        )


def push_delivery_note_status_to_mes_job(delivery_note_name):
    """Run the callback in RQ and request a retry when any linked order fails."""
    results = push_delivery_note_status_to_mes(delivery_note_name)
    if not any(result.get("status") == "failed" for result in results):
        return results

    try:
        frappe.db.commit()
    except Exception:
        pass
    raise frappe.RetryBackgroundJobError(
        f"MES Delivery Note status callback failed for {delivery_note_name}"
    )


def push_delivery_note_status_to_mes(delivery_note_name):
    if not CRM_INTEGRATION_AVAILABLE:
        return []

    delivery_note = frappe.get_doc("Delivery Note", delivery_note_name)
    if not is_mes_integration_enabled(delivery_note.get("company")):
        return []

    results = []
    for sales_order_name in get_linked_sales_orders(delivery_note):
        try:
            results.append(
                push_delivery_note_sales_order_status_to_mes(delivery_note, sales_order_name)
            )
        except Exception:
            # The failure has already been recorded in MES Integration Log.
            # Continue so one Sales Order does not block callbacks for the rest.
            results.append({"sales_order": sales_order_name, "status": "failed"})

    return results


def push_delivery_note_sales_order_status_to_mes(delivery_note, sales_order_name):
    sales_order = frappe.get_doc("Sales Order", sales_order_name)
    if not requires_sales_order_mes_sync(sales_order):
        return {"status": "skipped", "reason": "NO_MES_PRODUCTION", "sales_order": sales_order_name}
    payload = build_delivery_note_status_payload(delivery_note, sales_order_name)
    mes_log = create_mes_log(
        direction="Outbound",
        event=DELIVERY_NOTE_STATUS_EVENT,
        status="Pending",
        reference_doctype="Delivery Note",
        reference_name=delivery_note.name,
        source="DeeplinkERP",
        request_payload=payload,
    )

    try:
        request_url = get_mes_status_callback_url()
        update_mes_log(mes_log, request_url=request_url)

        frappe.logger().info(
            f"回写销售出库 {delivery_note.name} / 销售订单 {sales_order_name} 状态到 MES: {frappe.as_json(payload)}"
        )

        response = post_stock_entry_status_to_mes(payload, request_url)
        validate_stock_entry_status_response(response, payload)
    except Exception:
        update_mes_log(
            mes_log,
            status="Failed",
            error_message=frappe.get_traceback(),
        )
        raise

    update_mes_log(
        mes_log,
        status="Success",
        response_payload=response,
        trace_id=response.get("traceId"),
        http_status_code=200,
    )

    return {
        "status": "success",
        "delivery_note": delivery_note.name,
        "sales_order": sales_order_name,
        "trace_id": response.get("traceId"),
        "timestamp": now(),
    }


@frappe.whitelist(methods=["POST"])
def retry_push_delivery_note_status_to_mes(delivery_note_name):
    ensure_crm_integration_available()

    delivery_note = frappe.get_doc("Delivery Note", delivery_note_name)
    delivery_note.check_permission("read")
    if not is_mes_integration_enabled(delivery_note.get("company")):
        throw_mes_integration_disabled(delivery_note.get("company"))

    return push_delivery_note_status_to_mes(delivery_note.name)


def build_delivery_note_status_payload(delivery_note, sales_order_name):
    sales_order_status = frappe.db.get_value(
        "Sales Order", sales_order_name, "custom_process_status"
    )
    delivery_note_status = get_delivery_note_status(delivery_note)

    return {
        "documentType": DELIVERY_NOTE_STATUS_DOCUMENT_TYPE,
        "erpDeliveryNoteNo": delivery_note.name,
        "delivery_note": delivery_note.name,
        "documentNo": delivery_note.name,
        "erpSalesOrderNo": sales_order_name,
        "sales_order": sales_order_name,
        "referenceNo": sales_order_name,
        "docstatus": delivery_note.docstatus,
        "deliveryNoteStatus": delivery_note_status,
        "erpDeliveryNoteStatus": delivery_note_status,
        "salesOrderStatus": sales_order_status,
        "erpSalesOrderStatus": sales_order_status,
        "message": get_delivery_note_status_message(
            delivery_note, sales_order_name, sales_order_status, delivery_note_status
        ),
        "items": get_delivery_note_status_items(delivery_note, sales_order_name),
    }


def get_delivery_note_status_message(
    delivery_note, sales_order_name, sales_order_status, delivery_note_status
):
    action = {
        0: "为草稿",
        1: "已提交",
        2: "已取消",
    }.get(delivery_note.docstatus, f"状态为 {delivery_note_status}")

    return (
        f"ERP Delivery Note {delivery_note.name} {action}，"
        f"Sales Order {sales_order_name} 状态：{sales_order_status or ''}"
    )


def get_delivery_note_status(delivery_note):
    readiness_status = delivery_note.get("custom_delivery_readiness_status")
    if readiness_status:
        return readiness_status

    if delivery_note.get("status"):
        return delivery_note.get("status")

    return {
        0: "Draft",
        1: "Submitted",
        2: "Cancelled",
    }.get(delivery_note.docstatus, "Unknown")


def get_delivery_note_status_items(delivery_note, sales_order_name):
    qty_by_item = {}
    for row in delivery_note.get("items", []):
        if row.get("against_sales_order") != sales_order_name:
            continue

        item_code = row.get("item_code")
        shipped_qty = flt(row.get("stock_qty") or row.get("qty"))
        if not item_code or shipped_qty <= 0:
            continue

        qty_by_item[item_code] = flt(qty_by_item.get(item_code)) + shipped_qty

    return [
        {
            "itemCode": item_code,
            "item_code": item_code,
            "shippedQty": shipped_qty,
            "shipped_qty": shipped_qty,
        }
        for item_code, shipped_qty in sorted(qty_by_item.items())
    ]


def get_linked_sales_orders(doc):
    return sorted(
        {
            row.get("against_sales_order")
            for row in doc.get("items", [])
            if row.get("against_sales_order")
        }
    )


def validate_mes_delivery_note_permissions():
    if not frappe.has_permission("Delivery Note", "create"):
        frappe.throw(
            _("当前用户缺少 {0} 的 {1} 权限").format("Delivery Note", "create"),
            frappe.PermissionError,
        )


def ensure_crm_integration_available():
    if not CRM_INTEGRATION_AVAILABLE:
        frappe.throw(_("MES 销售出库流程需要安装 crm_integration 应用。"))


def parse_json_if_needed(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def get_request_url():
    if not getattr(frappe.local, "request", None):
        return None

    return getattr(frappe.request, "url", None)
