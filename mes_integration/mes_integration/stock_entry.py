import hashlib
import json
from contextlib import contextmanager

from requests.exceptions import RequestException

import frappe
from frappe import _
from frappe.utils import cint, flt, get_request_session, get_time, getdate, now

from erpnext.stock.doctype.item.item import get_item_defaults

from mes_integration.mes_integration.settings import is_mes_integration_enabled, throw_mes_integration_disabled
from mes_integration.mes_integration.integration_log import (
    create_mes_log,
    is_mes_api_user,
    update_mes_log,
)


class MESStockEntryIdentityConflict(frappe.ValidationError):
    http_status_code = 409


@frappe.whitelist(methods=["POST"])
def push_to_mes(stock_entry_name):
    """
    将已提交的 Stock Entry 发料结果回写到 MES。
    """
    stock_entry = frappe.get_doc("Stock Entry", stock_entry_name)
    stock_entry.check_permission("read")
    if not is_mes_integration_enabled(stock_entry.get("company")):
        throw_mes_integration_disabled(stock_entry.get("company"))

    if stock_entry.docstatus != 1:
        frappe.throw(frappe._("库存移动单必须已提交才能推送到 MES"))

    if not is_dlm_issue_stock_entry(stock_entry):
        frappe.throw(frappe._("只有其他出库、工单发料、注塑发料可以推送至 DLM"))

    if not is_mes_source_stock_entry(stock_entry):
        frappe.throw(frappe._("只有 MES 来源的物料移动可以推送至 DLM"))

    if stock_entry.get("custom_mes_status") == "Pushed":
        message = frappe._("此库存移动单已推送至 MES，无需重复推送")
        create_mes_log(
            direction="Outbound",
            event="Stock Entry Issue Confirm",
            status="Failed",
            reference_doctype="Stock Entry",
            reference_name=stock_entry.name,
            source="ERPNext",
            request_url=get_dlm_material_issue_callback_url(),
            error_message=message,
            batch_no=stock_entry.get("custom_stock_entry_no"),
        )
        frappe.db.commit()
        frappe.throw(message)

    payload = build_issue_confirm_payload(stock_entry)
    request_url = get_dlm_material_issue_callback_url()
    mes_log = create_mes_log(
        direction="Outbound",
        event="Stock Entry Issue Confirm",
        status="Pending",
        reference_doctype="Stock Entry",
        reference_name=stock_entry.name,
        source="ERPNext",
        request_url=request_url,
        request_payload=payload,
        batch_no=stock_entry.get("custom_stock_entry_no"),
    )

    frappe.logger().info(f"推送库存移动单 {stock_entry_name} 到 DLM: {frappe.as_json(payload)}")

    try:
        response = post_issue_confirm(payload, request_url)
        validate_issue_confirm_response(response, payload)
        stock_entry.check_permission("write")
        stock_entry.db_set("custom_mes_status", "Pushed")

        update_mes_log(
            mes_log,
            status="Success",
            response_payload=response,
            trace_id=response.get("traceId"),
            processed=get_dlm_processed_count(response),
        )
    except Exception:
        update_mes_log(
            mes_log,
            status="Failed",
            error_message=frappe.get_traceback(),
        )
        frappe.db.commit()
        raise

    return {
        "status": "success",
        "message": _("库存移动单 {0} 已成功推送到 DLM").format(stock_entry_name),
        "mes_status": "Pushed",
        "processed": get_dlm_processed_count(response),
        "trace_id": response.get("traceId"),
        "timestamp": now(),
    }


def build_issue_confirm_payload(stock_entry):
    validate_stock_entry_for_issue_confirm(stock_entry)

    posting_date = getdate(stock_entry.posting_date).isoformat() if stock_entry.posting_date else None
    material_request = get_issue_material_request(stock_entry)
    items_by_code = {}
    missing_fields = []

    for row in stock_entry.get("items", []):
        issued_qty = flt(row.get("transfer_qty") or row.get("qty"))

        if issued_qty <= 0:
            continue

        if not row.get("item_code"):
            missing_fields.append(f"第 {row.idx} 行缺少: item_code")
            continue

        item = items_by_code.setdefault(
            row.get("item_code"),
            {
                "item_code": row.get("item_code"),
                "total_issued_qty": 0,
            },
        )
        item["total_issued_qty"] = flt(item["total_issued_qty"]) + issued_qty

    if missing_fields:
        frappe.throw("<br>".join(missing_fields), title=frappe._("DLM 发料回调字段不完整"))

    items = list(items_by_code.values())
    if not items:
        frappe.throw(frappe._("没有可推送到 DLM 的发料明细"))

    return {
        "material_request": material_request,
        "stock_entry": stock_entry.name,
        "posting_date": posting_date,
        "items": items,
    }


def get_issue_material_request(stock_entry):
    material_requests = {
        row.get("material_request")
        for row in stock_entry.get("items", [])
        if row.get("material_request") and flt(row.get("transfer_qty") or row.get("qty")) > 0
    }

    if len(material_requests) > 1:
        frappe.throw(
            frappe._("一张物料移动只能回调一个 Material Request，当前包含：{0}").format(
                ", ".join(sorted(material_requests))
            )
        )

    return next(iter(material_requests), None)


def validate_stock_entry_for_issue_confirm(stock_entry):
    if not get_issue_material_request(stock_entry):
        frappe.throw(frappe._("请先关联原生 Material Request，再推送至 DLM"))

    if not is_mes_source_stock_entry(stock_entry):
        frappe.throw(frappe._("只有 MES 来源的物料移动可以推送至 DLM"))


def is_mes_source_stock_entry(stock_entry):
    """Return whether an issue Stock Entry came from a MES Material Request."""
    material_request_name = get_issue_material_request(stock_entry)
    if not material_request_name:
        return False

    material_request = frappe.get_cached_doc("Material Request", material_request_name)
    from mes_integration.mes_integration.material_request import is_mes_material_request

    return is_mes_material_request(material_request)


def post_issue_confirm(payload, url=None):
    url = url or get_dlm_material_issue_callback_url()
    timeout = flt(frappe.conf.get("mes_request_timeout") or 15)

    try:
        response = get_request_session().post(
            url,
            json=payload,
            headers=get_mes_push_headers(),
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except RequestException as exc:
        response = getattr(exc, "response", None)
        log_mes_push_error("DLM 发料回调接口 HTTP 调用失败", payload, response)
        frappe.throw(
            get_mes_http_error_message(response)
            or frappe._("DLM 发料回调接口调用失败：{0}").format(url)
        )
    except ValueError:
        log_mes_push_error("DLM 发料回调接口响应不是 JSON", payload)
        frappe.throw(frappe._("DLM 发料回调接口响应不是 JSON，请查看 Error Log"))


def validate_issue_confirm_response(response, payload):
    if not isinstance(response, dict):
        log_mes_push_error("DLM 发料回调接口响应格式异常", payload, response)
        frappe.throw(frappe._("DLM 发料回调接口响应格式异常"))

    if not is_successful_dlm_response(response):
        log_mes_push_error("DLM 发料回调接口返回失败", payload, response)
        frappe.throw(get_mes_error_message(response) or frappe._("DLM 发料回调失败"))

    data = response.get("data") or {}
    status = data.get("status")
    valid_statuses = {"processed", "already_issued"}

    if status not in valid_statuses:
        log_mes_push_error("DLM 发料回调业务状态异常", payload, response)
        frappe.throw(
            get_mes_error_message(response)
            or frappe._("DLM 发料回调业务状态异常：{0}").format(status or "missing")
        )


def is_successful_dlm_response(response):
    if response.get("code") is not None:
        return str(response.get("code")) == "0"

    return bool(response.get("success"))


def get_dlm_processed_count(response):
    data = response.get("data") or {}
    if "lines_updated" in data:
        return data.get("lines_updated")

    return data.get("processed")


def get_dlm_material_issue_callback_url():
    url = frappe.conf.get("dlm_material_issue_callback_url")
    if not url:
        frappe.throw(frappe._("缺少 DLM 接口配置：dlm_material_issue_callback_url"))
    return url


def get_mes_error_message(response):
    messages = []

    if response.get("message"):
        messages.append(response.get("message"))

    for error in response.get("errors") or []:
        if isinstance(error, dict):
            messages.append(error.get("message") or error.get("error") or frappe.as_json(error))
        else:
            messages.append(str(error))

    for error in (response.get("data") or {}).get("errors") or []:
        if isinstance(error, dict):
            messages.append(error.get("error") or error.get("message") or frappe.as_json(error))
        else:
            messages.append(str(error))

    return "<br>".join(filter(None, messages))


def log_mes_push_error(title, payload, response=None):
    message = {
        "payload": payload,
        "response": get_response_for_log(response),
        "traceback": frappe.get_traceback(),
    }
    frappe.log_error(title=title, message=frappe.as_json(message))


def get_response_for_log(response):
    if response is None:
        return None

    if isinstance(response, dict):
        return response

    response_data = {
        "status_code": response.status_code,
        "text": response.text,
    }

    try:
        response_data["json"] = response.json()
    except ValueError:
        pass

    return response_data


def get_mes_http_error_message(response):
    if response is None:
        return None

    try:
        data = response.json()
    except ValueError:
        data = {}

    message = data.get("message") or response.text
    error_code = data.get("errorCode")
    trace_id = data.get("traceId")

    parts = [frappe._("MES HTTP 状态码: {0}").format(response.status_code)]

    if error_code:
        parts.append(frappe._("错误码: {0}").format(error_code))

    if message:
        parts.append(frappe._("错误信息: {0}").format(message))

    if trace_id:
        parts.append(frappe._("Trace ID: {0}").format(trace_id))

    return "<br>".join(parts)


def is_dlm_issue_stock_entry(stock_entry):
    return stock_entry.get("stock_entry_type") in DLM_ISSUE_STOCK_ENTRY_TYPES


@frappe.whitelist(methods=["POST"])
def reset_mes_status(stock_entry_name):
    """
    重置库存转移单的 MES Status 为 Unpushed（当已推送的订单被修改时）
    """
    stock_entry = frappe.get_doc("Stock Entry", stock_entry_name)
    stock_entry.check_permission("read")
    if not is_mes_integration_enabled(stock_entry.get("company")):
        throw_mes_integration_disabled(stock_entry.get("company"))

    if stock_entry.get("custom_mes_status") == "Pushed":
        if not is_mes_source_stock_entry(stock_entry):
            return {
                "status": "skipped",
                "message": _("手动物料移动无需重置 DLM 状态"),
            }

        stock_entry.check_permission("write")
        stock_entry.db_set("custom_mes_status", "Unpushed")
        frappe.logger().info(f"库存转移单 {stock_entry_name} 已修改，MES 状态重置为 Unpushed")

        return {
            "status": "success",
            "message": _("库存转移单 {0} 的 MES 状态已重置为未推送").format(stock_entry_name),
            "mes_status": "Unpushed"
        }

    return {
        "status": "skipped",
        "message": _("无需重置")
    }

SEMI_FINISHED_GOODS_RECEIPT = "Semi Finished Goods Receipt"
FINISHED_GOODS_RECEIPT = "Finished Goods Receipt"
STANDARD_MATERIAL_RECEIPT = "Material Receipt"
MES_CUSTOM_RECEIPT_STOCK_ENTRY_TYPES = {
    SEMI_FINISHED_GOODS_RECEIPT,
    FINISHED_GOODS_RECEIPT,
}
MES_RECEIPT_STOCK_ENTRY_TYPES = {
    *MES_CUSTOM_RECEIPT_STOCK_ENTRY_TYPES,
    STANDARD_MATERIAL_RECEIPT,
}
MES_RECEIPT_WAREHOUSE_FIELD = "custom_mes_receipt_warehouse"
MES_DELIVERY_WAREHOUSE_FIELD = "custom_mes_delivery_warehouse"
MES_MANUFACTURING_WAREHOUSE_FIELD = "custom_mes_manufacturing_warehouse"
MES_RECEIPT_IDEMPOTENCY_LOCK_TIMEOUT_SECONDS = 30
DLM_ISSUE_STOCK_ENTRY_TYPES = {
    "Material Issue",
    "Material Transfer for Manufacture",
    "Injection Molding Issuance",
}


@frappe.whitelist(methods=["POST"])
def create_draft_stock_entry_from_mes(data=None, stock_entry=None, submit=False):
    """
    Create a Stock Entry from MES, optionally submitting it immediately.

    Accepted payloads:
    1. data={"sales_order": "CRM-ORDER-...", "stock_entry": {...}}
    2. data={...} with sales_order inside the Stock Entry payload
    """
    payload = parse_json_if_needed(data if data is not None else stock_entry)

    if not isinstance(payload, dict):
        frappe.throw(frappe._("缺少请求数据或数据格式不正确"))

    validate_mes_api_user()
    validate_mes_stock_entry_permissions()

    if payload.get("stock_entry"):
        stock_entry_data = parse_json_if_needed(payload.get("stock_entry"))
    else:
        stock_entry_data = payload

    if not isinstance(stock_entry_data, dict):
        frappe.throw(frappe._("缺少 Stock Entry 数据或数据格式不正确"))

    stock_entry_data = stock_entry_data.copy()
    sales_order_doc = get_sales_order_by_reference(payload, stock_entry_data, required=True)
    sales_order_doc.check_permission("read")

    stock_entry_type = stock_entry_data.get("stock_entry_type")
    validate_mes_receipt_stock_entry_type(stock_entry_type)

    stock_entry_data.pop("sales_order", None)
    stock_entry_data.pop("sales_order_name", None)
    if not stock_entry_data.get("company") and sales_order_doc:
        stock_entry_data["company"] = sales_order_doc.company
    if not is_mes_integration_enabled(stock_entry_data.get("company")):
        throw_mes_integration_disabled(stock_entry_data.get("company"))

    stock_entry_data["doctype"] = "Stock Entry"
    stock_entry_data["stock_entry_type"] = stock_entry_type
    stock_entry_data["purpose"] = "Material Receipt"
    mark_mes_receipt_stock_entry(stock_entry_data)
    set_mes_stock_entry_sales_order(stock_entry_data, sales_order_doc)
    receipt_no = (stock_entry_data.get("custom_stock_entry_no") or "").strip()

    # The idempotency lock covers the lookup and insert. A plain exists() check
    # is not enough because two concurrent MES retries could both see no row.
    with lock_mes_receipt_idempotency(stock_entry_data.get("company"), receipt_no):
        existing_stock_entry = get_existing_mes_receipt_stock_entry(
            stock_entry_data.get("company"),
            receipt_no,
            request_data=stock_entry_data,
            sales_order_doc=sales_order_doc,
        )
        if existing_stock_entry:
            validate_mes_receipt_identity(
                existing_stock_entry, stock_entry_data, sales_order_doc
            )
            if existing_stock_entry.docstatus == 2:
                throw_mes_receipt_identity_conflict(
                    f"MES 入库编号 {receipt_no} 已对应已取消的 ERP Stock Entry "
                    f"{existing_stock_entry.name}，不能重新创建。"
                )

            if cint(submit) and existing_stock_entry.docstatus == 0:
                existing_stock_entry.flags.mes_receipt_request = True
                existing_stock_entry.submit()
                existing_stock_entry.reload()

            return build_mes_stock_entry_response(
                existing_stock_entry, sales_order_doc, reused=True
            )

        set_mes_stock_entry_default_target_warehouses(stock_entry_data)
        stock_entry_doc = frappe.get_doc(stock_entry_data)
        set_mes_stock_entry_item_defaults(stock_entry_doc)
        validate_mes_stock_entry_data(stock_entry_doc, sales_order_doc)
        prepare_mes_stock_entry_for_submit(stock_entry_doc)
        set_allow_zero_valuation_rate_for_mes_items_without_cost(stock_entry_doc)
        # Keep an explicit marker for standard receipts created through this API.
        # It allows the submit hook to distinguish them from ordinary ERP receipts.
        stock_entry_doc.flags.mes_receipt_request = True
        stock_entry_doc.insert()
        stock_entry_doc.db_set("stock_entry_type", stock_entry_type, update_modified=False)
        stock_entry_doc.reload()

        if cint(submit):
            stock_entry_doc.flags.mes_receipt_request = True
            stock_entry_doc.submit()
            stock_entry_doc.reload()

        return build_mes_stock_entry_response(
            stock_entry_doc, sales_order_doc, reused=False
        )


@contextmanager
def lock_mes_receipt_idempotency(company, receipt_no):
    """Serialize concurrent creation attempts for one MES receipt number."""
    if (
        not company
        or not receipt_no
        or getattr(frappe.db, "db_type", None) != "mariadb"
    ):
        yield
        return

    lock_name = "mes_receipt_" + hashlib.sha256(
        f"{getattr(frappe.local, 'site', '')}|{company}|{receipt_no}".encode("utf-8")
    ).hexdigest()
    result = frappe.db.sql(
        "SELECT GET_LOCK(%s, %s)",
        (lock_name, MES_RECEIPT_IDEMPOTENCY_LOCK_TIMEOUT_SECONDS),
    )
    if not result or cint(result[0][0]) != 1:
        raise frappe.QueryDeadlockError(
            f"Timed out waiting for MES receipt idempotency lock: {receipt_no}"
        )

    try:
        yield
    finally:
        release_lock = lambda: release_mes_receipt_idempotency_lock(lock_name)
        frappe.db.after_commit.add(release_lock)
        frappe.db.after_rollback.add(release_lock)


def release_mes_receipt_idempotency_lock(lock_name):
    frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))


def get_existing_mes_receipt_stock_entry(
    company,
    receipt_no,
    request_data=None,
    sales_order_doc=None,
):
    """Return the MES receipt matching an idempotent retry.

    ``custom_stock_entry_no`` is the MES receipt identity within one company.
    Exact retries reuse the existing document; a different payload is rejected
    instead of creating another inventory movement under the same identity.
    """
    if (
        not company
        or not receipt_no
        or not frappe.db.has_column("Stock Entry", "custom_stock_entry_no")
    ):
        return None

    filters = {
        "company": company,
        "custom_stock_entry_no": receipt_no,
        "stock_entry_type": ["in", sorted(MES_RECEIPT_STOCK_ENTRY_TYPES)],
    }
    if frappe.db.has_column("Stock Entry", "custom_mes_receipt"):
        # The same production/batch number is also carried by Material Issue
        # documents. Only records created through the MES receipt API belong to
        # this idempotency scope.
        filters["custom_mes_receipt"] = 1

    names = frappe.get_all(
        "Stock Entry",
        filters=filters,
        pluck="name",
        order_by="creation asc",
        limit_page_length=0,
    )
    if len(names) > 1:
        throw_mes_receipt_identity_conflict(
            f"MES 入库编号 {receipt_no} 已对应多个 ERP Stock Entry：{', '.join(names)}。"
            "请先人工确认并清理重复单据，ERP 不会自动选择其中一张。"
        )

    if request_data is not None:
        if not names:
            return None

        stock_entry = frappe.get_doc("Stock Entry", names[0])
        mismatches = get_mes_receipt_identity_mismatches(
            stock_entry, request_data, sales_order_doc
        )
        if mismatches:
            throw_mes_receipt_identity_conflict(
                f"MES 入库编号 {receipt_no} 已对应 ERP Stock Entry {stock_entry.name}，"
                f"但本次请求字段不一致：{', '.join(mismatches)}。"
            )

        return stock_entry

    return frappe.get_doc("Stock Entry", names[0]) if names else None


def validate_mes_receipt_identity(stock_entry, request_data, sales_order_doc):
    """Reject reuse of a MES number for a different receipt payload."""
    mismatches = get_mes_receipt_identity_mismatches(
        stock_entry, request_data, sales_order_doc
    )

    if mismatches:
        throw_mes_receipt_identity_conflict(
            f"MES 入库编号 {stock_entry.get('custom_stock_entry_no')} 已存在于 "
            f"ERP Stock Entry {stock_entry.name}，但请求字段不一致：{', '.join(mismatches)}。"
        )


def get_mes_receipt_identity_mismatches(stock_entry, request_data, sales_order_doc):
    """Return fields that differ between an ERP receipt and an MES request."""
    mismatches = []
    if stock_entry.company != request_data.get("company"):
        mismatches.append("company")
    if stock_entry.stock_entry_type != request_data.get("stock_entry_type"):
        mismatches.append("stock_entry_type")

    requested_sales_order = sales_order_doc.name if sales_order_doc else None
    existing_sales_order = stock_entry.get("custom_sales_order")
    if requested_sales_order and requested_sales_order != existing_sales_order:
        mismatches.append("sales_order")

    for fieldname in ("posting_date", "posting_time"):
        requested_value = get_mes_receipt_header_identity_value(
            fieldname, request_data.get(fieldname)
        )
        if requested_value is None:
            continue

        existing_value = get_mes_receipt_header_identity_value(
            fieldname, stock_entry.get(fieldname)
        )
        if requested_value != existing_value:
            mismatches.append(fieldname)

    if request_data.get("items") is not None and not mes_receipt_items_match(
        request_data.get("items"), stock_entry.get("items")
    ):
        mismatches.append("items")

    return mismatches


def mes_receipt_items_match(request_items, existing_items):
    """Compare supplied item identity without penalizing ERP-filled defaults."""
    requested_items = get_mes_receipt_item_identity(request_items)
    existing_items = get_mes_receipt_item_identity(existing_items)
    if len(requested_items) != len(existing_items):
        return False

    optional_fields = (
        "uom",
        "stock_uom",
        "conversion_factor",
        "s_warehouse",
        "t_warehouse",
        "batch_no",
        "serial_no",
        "basic_rate",
        "valuation_rate",
        "incoming_rate",
        "allow_zero_valuation_rate",
    )
    unmatched_items = list(existing_items)
    for requested in requested_items:
        match_index = next(
            (
                index
                for index, existing in enumerate(unmatched_items)
                if requested["item_code"] == existing["item_code"]
                and requested["qty"] == existing["qty"]
                and all(
                    requested[fieldname] is None
                    or requested[fieldname] == existing[fieldname]
                    for fieldname in optional_fields
                )
            ),
            None,
        )
        if match_index is None:
            return False
        unmatched_items.pop(match_index)

    return True


def get_mes_receipt_item_identity(items):
    """Build a stable, business-level identity for receipt item comparisons."""
    identity = []
    for row in items or []:
        identity.append(
            {
                "item_code": row.get("item_code"),
                "qty": flt(row.get("qty")),
                "uom": get_mes_receipt_item_identity_value(row, "uom"),
                "stock_uom": get_mes_receipt_item_identity_value(row, "stock_uom"),
                "conversion_factor": get_mes_receipt_item_identity_value(
                    row, "conversion_factor", numeric=True
                ),
                "s_warehouse": get_mes_receipt_item_identity_value(row, "s_warehouse"),
                "t_warehouse": get_mes_receipt_item_identity_value(row, "t_warehouse"),
                "batch_no": get_mes_receipt_item_identity_value(row, "batch_no"),
                "serial_no": get_mes_receipt_item_identity_value(row, "serial_no"),
                "basic_rate": get_mes_receipt_item_identity_value(
                    row, "basic_rate", numeric=True
                ),
                "valuation_rate": get_mes_receipt_item_identity_value(
                    row, "valuation_rate", numeric=True
                ),
                "incoming_rate": get_mes_receipt_item_identity_value(
                    row, "incoming_rate", numeric=True
                ),
                "allow_zero_valuation_rate": get_mes_receipt_item_identity_value(
                    row, "allow_zero_valuation_rate", integer=True
                ),
            }
        )

    return sorted(
        identity,
        key=lambda row: (
            row["item_code"] or "",
            row["s_warehouse"] or "",
            row["t_warehouse"] or "",
            row["qty"],
        ),
    )


def get_mes_receipt_header_identity_value(fieldname, value):
    if value in (None, ""):
        return None

    if fieldname == "posting_date":
        return getdate(value).isoformat()
    if fieldname == "posting_time":
        return get_time(value).isoformat()

    return value


def get_mes_receipt_item_identity_value(row, fieldname, numeric=False, integer=False):
    value = row.get(fieldname)
    if value in (None, ""):
        return None
    if integer:
        return cint(value)
    if numeric:
        return flt(value)
    return str(value).strip()


def throw_mes_receipt_identity_conflict(message):
    """Return a stable 409 business error that MES must not solve by new IDs."""
    frappe.response["error_code"] = "ERP_STOCK_ENTRY_IDENTITY_CONFLICT"
    frappe.throw(
        f"ERP_STOCK_ENTRY_IDENTITY_CONFLICT: {message}",
        exc=MESStockEntryIdentityConflict,
        title=frappe._("MES 入库幂等编号冲突"),
    )


def build_mes_stock_entry_response(stock_entry, sales_order_doc, reused=False):
    submitted = stock_entry.docstatus == 1
    return {
        "status": "success",
        "message": (
            frappe._("已复用原入库单，未创建新单据。")
            if reused
            else (
                frappe._("入库单已创建并提交。")
                if submitted
                else frappe._("物料移动草稿已创建，可在 ERP 审核后直接提交。")
            )
        ),
        "stock_entry": stock_entry.name,
        "stock_entry_type": stock_entry.stock_entry_type,
        "stock_entry_docstatus": stock_entry.docstatus,
        "submitted": submitted,
        "idempotent_reuse": reused,
        "sales_order": sales_order_doc.name if sales_order_doc else None,
        "sales_order_crm_order_no": (
            sales_order_doc.get("custom_crm_order_no") if sales_order_doc else None
        ),
        "stock_entry_url": frappe.utils.get_url_to_form("Stock Entry", stock_entry.name),
        "timestamp": now(),
    }


@frappe.whitelist(methods=["POST"])
def create_and_submit_stock_entry_from_mes(data=None, stock_entry=None):
    """Create and submit a MES receipt Stock Entry in one transaction."""
    return create_draft_stock_entry_from_mes(
        data=data,
        stock_entry=stock_entry,
        submit=True,
    )


def get_sales_order_by_reference(payload, stock_entry_data, required=True):
    """Resolve an ERP Sales Order by ERP name or CRM order number.

    MES should send the ERP Sales Order.name in ``sales_order``.  CRM order
    numbers remain supported as a backward-compatible fallback for older
    callers, but MES does not need to know or store that number.
    """
    sales_order_reference = (
        payload.get("sales_order")
        or payload.get("sales_order_name")
        or stock_entry_data.get("sales_order")
        or stock_entry_data.get("sales_order_name")
    )

    if not sales_order_reference:
        if required:
            frappe.throw(frappe._("缺少销售订单编号 sales_order"))
        return None

    # Prefer an exact ERP document name when one exists. This keeps the old
    # contract working even if an ERP name happens to resemble a CRM number.
    if frappe.db.exists("Sales Order", sales_order_reference):
        return frappe.get_doc("Sales Order", sales_order_reference)

    if not frappe.db.has_column("Sales Order", "custom_crm_order_no"):
        frappe.throw(
            frappe._("未找到 ERP 销售订单或 CRM 销售订单号 {0}").format(
                sales_order_reference
            )
        )

    sales_order_names = frappe.get_list(
        "Sales Order",
        filters={"custom_crm_order_no": sales_order_reference, "docstatus": 1},
        pluck="name",
        limit_page_length=2,
    )

    if len(sales_order_names) > 1:
        frappe.throw(
            frappe._("CRM 销售订单号 {0} 对应多张 ERP 销售订单，无法安全关联").format(
                sales_order_reference
            )
        )

    if not sales_order_names:
        frappe.throw(
            frappe._("未找到 ERP 销售订单或 CRM 销售订单号 {0}").format(
                sales_order_reference
            )
        )

    return frappe.get_doc("Sales Order", sales_order_names[0])


def get_sales_order_by_name(payload, stock_entry_data, required=True):
    """Backward-compatible alias for callers using the old helper name."""
    return get_sales_order_by_reference(payload, stock_entry_data, required=required)


def set_mes_stock_entry_sales_order(stock_entry_data, sales_order_doc):
    """Persist the resolved ERP Sales Order on the Stock Entry when available."""
    if not sales_order_doc or not frappe.db.has_column("Stock Entry", "custom_sales_order"):
        return

    stock_entry_data["custom_sales_order"] = sales_order_doc.name


def mark_mes_receipt_stock_entry(stock_entry_data):
    """Persist the MES source for receipts that may be submitted later."""
    if frappe.db.has_column("Stock Entry", "custom_mes_receipt"):
        stock_entry_data["custom_mes_receipt"] = 1


def parse_json_if_needed(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def validate_mes_api_user():
    if not is_mes_api_user():
        frappe.throw(frappe._("当前用户没有 MES 接口调用权限"), frappe.PermissionError)


def validate_mes_stock_entry_permissions():
    if not frappe.has_permission("Stock Entry", "create"):
        frappe.throw(
            frappe._("当前用户缺少 {0} 的 {1} 权限").format("Stock Entry", "create"),
            frappe.PermissionError,
        )


def validate_mes_receipt_stock_entry_type(stock_entry_type):
    if not stock_entry_type:
        frappe.throw(frappe._("缺少物料移动类型 stock_entry_type"))

    if stock_entry_type not in MES_RECEIPT_STOCK_ENTRY_TYPES:
        frappe.throw(
            frappe._("MES 入库接口仅支持移动类型：{0}").format(
                ", ".join(sorted(MES_RECEIPT_STOCK_ENTRY_TYPES))
            )
        )


def set_mes_stock_entry_default_target_warehouses(stock_entry_data):
    company = stock_entry_data.get("company")
    if not company:
        frappe.throw(frappe._("缺少物料移动公司，无法查询物料默认仓库"))

    missing_items = []
    configured_warehouse = get_mes_company_warehouse(
        company,
        MES_RECEIPT_WAREHOUSE_FIELD,
    )

    for index, row in enumerate(stock_entry_data.get("items") or [], start=1):
        if row.get("t_warehouse"):
            continue

        item_code = row.get("item_code")
        if not item_code:
            continue

        if not frappe.db.exists("Item", item_code):
            missing_items.append(frappe._("第 {0} 行物料 {1} 不存在").format(index, item_code))
            continue

        default_warehouse = (get_item_defaults(item_code, company) or {}).get("default_warehouse")
        target_warehouse = default_warehouse or configured_warehouse
        if not target_warehouse:
            missing_items.append(
                frappe._(
                    "第 {0} 行物料 {1} 缺少目标仓库；请传 t_warehouse、设置物料默认仓库，"
                    "或配置公司 MES 默认入库仓。"
                ).format(index, item_code)
            )
            continue

        validate_mes_warehouse_company(target_warehouse, company, "入库")
        row["t_warehouse"] = target_warehouse

    if missing_items:
        frappe.throw(
            "<br>".join(missing_items),
            title=frappe._("MES 入库物料或仓库配置错误"),
        )


def get_mes_item_largest_stock_warehouse(item_code, company):
    warehouses = frappe.db.sql(
        """
        SELECT bin.warehouse
        FROM `tabBin` bin
        INNER JOIN `tabWarehouse` warehouse ON warehouse.name = bin.warehouse
        WHERE bin.item_code = %s
            AND IFNULL(bin.actual_qty, 0) > 0
            AND IFNULL(warehouse.is_group, 0) = 0
            AND IFNULL(warehouse.disabled, 0) = 0
            AND (IFNULL(warehouse.company, '') = '' OR warehouse.company = %s)
        ORDER BY bin.actual_qty DESC, bin.warehouse ASC
        LIMIT 1
        """,
        (item_code, company),
        as_dict=True,
    )

    return warehouses[0].warehouse if warehouses else None


def get_mes_company_warehouse(company, fieldname):
    if not company or not frappe.db.has_column("Company", fieldname):
        return None

    return frappe.db.get_value("Company", company, fieldname)


def validate_mes_warehouse_company(warehouse, company, operation="库存业务"):
    warehouse_doc = frappe.db.get_value(
        "Warehouse",
        warehouse,
        ["name", "company", "is_group", "disabled"],
        as_dict=True,
    )
    if not warehouse_doc:
        frappe.throw(frappe._("未找到仓库 {0}").format(warehouse))
    if warehouse_doc.is_group or warehouse_doc.disabled:
        frappe.throw(
            frappe._("仓库 {0} 不可用于{1}").format(warehouse, operation)
        )
    if warehouse_doc.company and warehouse_doc.company != company:
        frappe.throw(
            frappe._("仓库 {0} 不属于公司 {1}").format(warehouse, company)
        )


def set_mes_stock_entry_item_defaults(stock_entry):
    for row in stock_entry.get("items", []):
        if not row.get("item_code"):
            continue

        item = frappe.db.get_value(
            "Item",
            row.get("item_code"),
            ["stock_uom", "item_name", "description"],
            as_dict=True,
        )
        if not item:
            continue

        row.stock_uom = row.get("stock_uom") or item.stock_uom
        row.uom = row.get("uom") or row.stock_uom
        row.item_name = row.get("item_name") or item.item_name
        row.description = row.get("description") or item.description

        if not flt(row.get("conversion_factor")):
            row.conversion_factor = get_mes_item_conversion_factor(
                row.get("item_code"),
                row.get("uom"),
                row.get("stock_uom"),
                row.idx,
            )


def get_mes_item_conversion_factor(item_code, uom, stock_uom, row_idx):
    if not uom or not stock_uom or uom == stock_uom:
        return 1

    conversion_factor = frappe.db.get_value(
        "UOM Conversion Detail",
        {"parent": item_code, "uom": uom},
        "conversion_factor",
    )

    if not conversion_factor:
        frappe.throw(
            frappe._("第 {0} 行缺少单位换算系数，且物料 {1} 没有单位 {2} 的换算设置").format(
                row_idx,
                item_code,
                uom,
            )
        )

    return flt(conversion_factor)


def prepare_mes_stock_entry_for_submit(stock_entry):
    stock_entry.set_transfer_qty()
    stock_entry.set_actual_qty()
    stock_entry.calculate_rate_and_amount(raise_error_if_no_rate=False)


def set_allow_zero_valuation_rate_for_mes_items_without_cost(stock_entry):
    for row in stock_entry.get("items", []):
        if has_mes_stock_entry_item_valuation_cost(row):
            continue

        row.allow_zero_valuation_rate = 1


def has_mes_stock_entry_item_valuation_cost(row):
    return any(
        flt(row.get(fieldname)) > 0
        for fieldname in ("valuation_rate", "basic_rate", "incoming_rate")
    )


def validate_mes_stock_entry_data(stock_entry, sales_order=None):
    if stock_entry.doctype != "Stock Entry":
        frappe.throw(frappe._("只能通过此接口创建 Stock Entry"))

    if stock_entry.docstatus != 0:
        frappe.throw(frappe._("MES 传入的物料移动必须是草稿状态"))

    if not stock_entry.get("purpose"):
        frappe.throw(frappe._("缺少物料移动 Purpose"))

    if not stock_entry.get("company") and sales_order:
        stock_entry.company = sales_order.company

    if not stock_entry.get("company"):
        frappe.throw(frappe._("缺少物料移动公司"))

    if sales_order and stock_entry.company != sales_order.company:
        frappe.throw(frappe._("物料移动公司必须与销售订单公司一致"))

    if not stock_entry.get("items"):
        frappe.throw(frappe._("物料移动至少需要一行明细"))

    for row in stock_entry.get("items"):
        if not row.get("item_code"):
            frappe.throw(frappe._("第 {0} 行缺少物料号").format(row.idx))

        if flt(row.get("qty")) <= 0:
            frappe.throw(frappe._("第 {0} 行数量必须大于 0").format(row.idx))

        if stock_entry.purpose == "Material Receipt" and not row.get("t_warehouse"):
            frappe.throw(frappe._("第 {0} 行缺少目标仓库").format(row.idx))

        if not row.get("s_warehouse") and not row.get("t_warehouse"):
            frappe.throw(frappe._("第 {0} 行至少需要来源仓库或目标仓库").format(row.idx))

def update_material_request_transferred_qty(stock_entry, method=None):
    if not is_mes_integration_enabled(stock_entry.get("company")):
        return

    mr_item_names = {
        row.material_request_item
        for row in stock_entry.get("items", [])
        if is_material_request_issue_row(row)
    }
    if not mr_item_names:
        return

    transferred_qty_by_item = get_submitted_transferred_qty_by_material_request_item(mr_item_names)
    mr_names = set()

    for mr_item_name in mr_item_names:
        transferred_qty = transferred_qty_by_item.get(mr_item_name, 0)
        mr_name = frappe.db.get_value("Material Request Item", mr_item_name, "parent")

        frappe.db.set_value(
            "Material Request Item",
            mr_item_name,
            {
                "custom_transferred_qty": transferred_qty,
                "ordered_qty": transferred_qty,
            },
            update_modified=False,
        )

        if mr_name:
            mr_names.add(mr_name)

    for mr_name in mr_names:
        update_material_request_issue_status(mr_name)


def get_submitted_transferred_qty_by_material_request_item(mr_item_names):
    placeholders = ", ".join(["%s"] * len(mr_item_names))
    rows = frappe.db.sql(
        f"""
        SELECT
            sed.material_request_item,
            SUM(
                CASE
                    WHEN IFNULL(se.is_return, 0) = 1 THEN -IFNULL(sed.transfer_qty, 0)
                    ELSE IFNULL(sed.transfer_qty, 0)
                END
            ) AS transferred_qty
        FROM `tabStock Entry Detail` sed
        INNER JOIN `tabStock Entry` se ON se.name = sed.parent
        WHERE sed.material_request_item IN ({placeholders})
            AND sed.docstatus = 1
            AND se.docstatus = 1
            AND IFNULL(sed.s_warehouse, '') != ''
        GROUP BY sed.material_request_item
        """,
        tuple(mr_item_names),
        as_dict=True,
    )

    return {row.material_request_item: flt(row.transferred_qty) for row in rows}


def is_material_request_issue_row(row):
    return bool(
        row.get("material_request")
        and row.get("material_request_item")
        and row.get("s_warehouse")
    )


def update_material_request_issue_status(mr_name):
    mr = frappe.get_doc("Material Request", mr_name)
    if mr.docstatus != 1 or mr.status in ("Stopped", "Cancelled"):
        return None

    total_qty = 0
    issued_qty = 0

    for row in mr.get("items", []):
        total_qty += flt(row.stock_qty or row.qty)
        issued_qty += flt(row.get("custom_transferred_qty"))

    per_ordered = (issued_qty / total_qty) * 100 if total_qty else 0

    if per_ordered >= 100:
        per_ordered = 100
        status = "Issued"
    elif per_ordered > 0:
        status = "Partially Ordered"
    else:
        status = "Pending"

    frappe.db.set_value(
        "Material Request",
        mr_name,
        {
            "per_ordered": per_ordered,
            "status": status,
        },
        update_modified=False,
    )

    return {
        "issued_qty": flt(issued_qty),
        "total_qty": flt(total_qty),
        "per_ordered": flt(per_ordered),
        "status": status,
    }


def is_mes_receipt_stock_entry(stock_entry):
    stock_entry_type = stock_entry.get("stock_entry_type")
    if stock_entry_type in MES_CUSTOM_RECEIPT_STOCK_ENTRY_TYPES:
        return True

    # A standard Material Receipt is also used by ordinary ERP users.  It must
    # be identified by the MES request context or by the persisted marker on a
    # MES-created draft; the generic savedocs route is not sufficient because
    # the ERP Desk uses the same route for manual submissions.
    return stock_entry_type == STANDARD_MATERIAL_RECEIPT and (
        is_mes_api_user()
        or stock_entry.flags.get("mes_receipt_request")
        or cint(stock_entry.get("custom_mes_receipt"))
    )


DEFAULT_RECEIPT_STATUS_DOCUMENT_TYPE = "stock_entry"
MES_RECEIPT_STATUS_EVENT = "Stock Entry Receipt Status Callback"
VALID_RECEIPT_STATUS_RESPONSE_STATUSES = {"processed"}
MES_RECEIPT_STATUS_QUEUE = "short"
MES_RECEIPT_STATUS_TIMEOUT = 300


def notify_mes_stock_entry_status(stock_entry, method=None):
    """
    Stock Entry on_submit / on_cancel hook handler.

    Queue the MES status callback after the Stock Entry transaction commits.

    Capturing the event docstatus prevents a queued submit callback from being
    changed into a cancellation callback if the document is cancelled before
    the worker starts. Any queue failure is logged and does not block ERP.
    """
    if not is_mes_integration_enabled(stock_entry.get("company")):
        return

    if not is_mes_receipt_stock_entry(stock_entry):
        return

    event_docstatus = {
        "on_submit": 1,
        "on_cancel": 2,
    }.get(method, cint(stock_entry.docstatus))
    frappe.db.after_commit.add(
        lambda: enqueue_mes_stock_entry_status_callback(
            stock_entry.name, event_docstatus
        )
    )


def enqueue_mes_stock_entry_status_callback(stock_entry_name, docstatus):
    """Enqueue one receipt status event after its ERP transaction commits."""
    try:
        frappe.enqueue(
            "mes_integration.mes_integration.stock_entry.push_stock_entry_status_to_mes_job",
            queue=MES_RECEIPT_STATUS_QUEUE,
            timeout=MES_RECEIPT_STATUS_TIMEOUT,
            job_id=f"mes-stock-entry-status:{stock_entry_name}:{docstatus}",
            deduplicate=True,
            stock_entry_name=stock_entry_name,
            docstatus=docstatus,
        )
    except Exception:
        frappe.log_error(
            title="Failed to enqueue MES Stock Entry status callback",
            message=frappe.get_traceback(),
        )


def push_stock_entry_status_to_mes_job(stock_entry_name, docstatus):
    """Push a committed receipt status and retry callback failures."""
    stock_entry = frappe.get_doc("Stock Entry", stock_entry_name)
    if not is_mes_integration_enabled(stock_entry.get("company")):
        return None

    try:
        return push_stock_entry_status_to_mes(stock_entry, docstatus=docstatus)
    except frappe.RetryBackgroundJobError:
        raise
    except Exception as exc:
        # Frappe retries this exception up to its normal background-job limit.
        # Commit the integration-log failure before handing control back to RQ.
        try:
            frappe.db.commit()
        except Exception:
            pass
        raise frappe.RetryBackgroundJobError(
            f"MES Stock Entry status callback failed for {stock_entry_name}"
        ) from exc


def push_stock_entry_status_to_mes(
    stock_entry, erp_status=None, message=None, docstatus=None
):
    """
    Push a finished/semi-finished goods receipt Stock Entry status to MES.

    Raises on failure so the caller can decide whether to block or swallow.
    """
    if not is_mes_receipt_stock_entry(stock_entry):
        frappe.throw(frappe._("只有成品/半成品入库单支持 MES 状态回写"))

    payload = build_stock_entry_status_payload(
        stock_entry,
        erp_status=erp_status,
        message=message,
        docstatus=docstatus,
    )

    mes_log = create_mes_log(
        direction="Outbound",
        event=MES_RECEIPT_STATUS_EVENT,
        status="Pending",
        reference_doctype="Stock Entry",
        reference_name=stock_entry.name,
        source="ERPNext",
        request_url=None,
        request_payload=payload,
        batch_no=stock_entry.get("custom_stock_entry_no"),
    )

    try:
        request_url = get_mes_status_callback_url()
        update_mes_log(mes_log, request_url=request_url)

        frappe.logger().info(
            f"回写入库单状态 {stock_entry.name} 到 MES: {frappe.as_json(payload)}"
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
        "message": _("入库单 {0} 状态已成功回写 MES").format(stock_entry.name),
        "trace_id": response.get("traceId"),
        "timestamp": now(),
    }


@frappe.whitelist(methods=["POST"])
def retry_push_stock_entry_status_to_mes(stock_entry_name):
    """
    Manually re-trigger the MES status callback for a receipt Stock Entry.
    """
    stock_entry = frappe.get_doc("Stock Entry", stock_entry_name)
    stock_entry.check_permission("read")

    if not is_mes_integration_enabled(stock_entry.get("company")):
        throw_mes_integration_disabled(stock_entry.get("company"))

    stock_entry.flags.mes_receipt_request = True
    return push_stock_entry_status_to_mes(stock_entry)


def build_stock_entry_status_payload(
    stock_entry, erp_status=None, message=None, docstatus=None
):
    payload_docstatus = stock_entry.docstatus if docstatus is None else cint(docstatus)
    if erp_status is None or message is None:
        mapped_status, mapped_message = map_stock_entry_docstatus_to_erp_status(
            payload_docstatus, stock_entry.name
        )
        erp_status = erp_status or mapped_status
        message = message or mapped_message

    payload = {
        "documentType": DEFAULT_RECEIPT_STATUS_DOCUMENT_TYPE,
        "erpStockEntryName": stock_entry.name,
        "documentNo": stock_entry.name,
        "docstatus": payload_docstatus,
        "erpStatus": erp_status,
        "message": message,
    }

    reference_no = stock_entry.get("custom_stock_entry_no")
    if reference_no:
        payload["referenceNo"] = reference_no

    return payload


def map_stock_entry_docstatus_to_erp_status(docstatus, stock_entry_name):
    return {
        0: ("draft", f"ERP Stock Entry {stock_entry_name} 为草稿"),
        1: ("submitted", f"ERP Stock Entry {stock_entry_name} 已提交"),
        2: ("cancelled", f"ERP Stock Entry {stock_entry_name} 已取消"),
    }.get(docstatus, ("unknown", f"ERP Stock Entry {stock_entry_name} 状态未知"))


def post_stock_entry_status_to_mes(payload, url=None):
    url = url or get_mes_status_callback_url()
    timeout = flt(
        frappe.conf.get("mes_status_callback_timeout")
        or frappe.conf.get("mes_request_timeout")
        or 15
    )

    try:
        response = get_request_session().post(
            url,
            json=payload,
            headers=get_mes_push_headers(),
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except RequestException as exc:
        response = getattr(exc, "response", None)
        log_mes_push_error("MES 状态回写接口 HTTP 调用失败", payload, response)
        frappe.throw(
            get_mes_http_error_message(response)
            or frappe._("MES 状态回写接口调用失败：{0}").format(url)
        )
    except ValueError:
        log_mes_push_error("MES 状态回写接口响应不是 JSON", payload)
        frappe.throw(frappe._("MES 状态回写接口响应不是 JSON，请查看 Error Log"))


def validate_stock_entry_status_response(response, payload):
    if not isinstance(response, dict):
        log_mes_push_error("MES 状态回写接口响应格式异常", payload, response)
        frappe.throw(frappe._("MES 状态回写接口响应格式异常"))

    if not response.get("success"):
        log_mes_push_error("MES 状态回写接口返回失败", payload, response)
        frappe.throw(get_mes_error_message(response) or frappe._("MES 状态回写失败"))

    data = response.get("data") or {}
    status = data.get("status")
    if status not in VALID_RECEIPT_STATUS_RESPONSE_STATUSES:
        log_mes_push_error("MES 状态回写业务状态异常", payload, response)
        frappe.throw(
            get_mes_error_message(response)
            or frappe._("MES 状态回写业务状态异常：{0}").format(status)
        )


def get_mes_status_callback_url():
    url = frappe.conf.get("mes_status_callback_url")
    if not url:
        frappe.throw(frappe._("缺少 MES 状态回写接口配置：mes_status_callback_url"))
    return url


def get_mes_push_headers():
    return {
        "Authorization": get_mes_push_authorization(),
        "Content-Type": "application/json",
    }


def get_mes_push_authorization():
    authorization = frappe.conf.get("mes_push_authorization")
    if not authorization:
        frappe.throw(frappe._("缺少 MES 推送接口配置：mes_push_authorization"))

    return authorization
