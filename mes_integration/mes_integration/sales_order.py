import hashlib

import frappe
from frappe import _
from frappe.utils import now

from mes_integration.mes_integration.integration_log import create_mes_log, update_mes_log
from mes_integration.mes_integration.settings import (
    is_mes_integration_enabled,
    throw_mes_integration_disabled,
)
from mes_integration.mes_integration.stock_entry import (
    get_mes_status_callback_url,
    post_stock_entry_status_to_mes,
    validate_stock_entry_status_response,
)


SALES_ORDER_STATUS_EVENT = "Sales Order Status Callback"
SALES_ORDER_STATUS_DOCUMENT_TYPE = "sales_order"
MES_SALES_ORDER_STATUS_QUEUE = "short"
MES_SALES_ORDER_STATUS_TIMEOUT = 300


def enqueue_sales_order_status_callback(
    sales_order_name, triggered_status=None, trigger_event=None
):
    try:
        frappe.enqueue(
            "mes_integration.mes_integration.sales_order.push_sales_order_status_to_mes_job",
            queue=MES_SALES_ORDER_STATUS_QUEUE,
            timeout=MES_SALES_ORDER_STATUS_TIMEOUT,
            enqueue_after_commit=True,
            job_id=get_sales_order_status_job_id(
                sales_order_name,
                triggered_status,
                trigger_event,
            ),
            deduplicate=True,
            sales_order_name=sales_order_name,
            triggered_status=triggered_status,
            trigger_event=trigger_event,
        )
    except Exception:
        frappe.log_error(
            title="Failed to enqueue MES Sales Order status callback",
            message=frappe.get_traceback(),
        )


def get_sales_order_status_job_id(
    sales_order_name,
    triggered_status=None,
    trigger_event=None,
):
    event_key = hashlib.sha256(
        f"{triggered_status or ''}\0{trigger_event or ''}".encode("utf-8")
    ).hexdigest()[:16]
    return f"mes-sales-order-status:{sales_order_name}:{event_key}"


def push_sales_order_status_to_mes_job(
    sales_order_name,
    triggered_status=None,
    trigger_event=None,
):
    """Push one callback and let Frappe retry transient failures."""
    try:
        return push_sales_order_status_to_mes(
            sales_order_name,
            triggered_status=triggered_status,
            trigger_event=trigger_event,
        )
    except frappe.RetryBackgroundJobError:
        raise
    except Exception as exc:
        try:
            frappe.db.commit()
        except Exception:
            pass
        raise frappe.RetryBackgroundJobError(
            f"MES Sales Order status callback failed for {sales_order_name}"
        ) from exc


def push_sales_order_status_to_mes(
    sales_order_name, triggered_status=None, trigger_event=None
):
    sales_order = frappe.get_doc("Sales Order", sales_order_name)
    if not is_mes_integration_enabled(sales_order.get("company")):
        return None

    return push_sales_order_status_doc_to_mes(
        sales_order, triggered_status=triggered_status, trigger_event=trigger_event
    )


def push_sales_order_status_doc_to_mes(
    sales_order, triggered_status=None, trigger_event=None
):
    payload = build_sales_order_status_payload(
        sales_order, triggered_status=triggered_status, trigger_event=trigger_event
    )
    mes_log = create_mes_log(
        direction="Outbound",
        event=SALES_ORDER_STATUS_EVENT,
        status="Pending",
        reference_doctype="Sales Order",
        reference_name=sales_order.name,
        source="DeeplinkERP",
        request_payload=payload,
    )

    try:
        request_url = get_mes_status_callback_url()
        update_mes_log(mes_log, request_url=request_url)

        frappe.logger().info(
            f"回写销售订单 {sales_order.name} 状态到 MES: {frappe.as_json(payload)}"
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
        "sales_order": sales_order.name,
        "trace_id": response.get("traceId"),
        "timestamp": now(),
    }


@frappe.whitelist(methods=["POST"])
def retry_push_sales_order_status_to_mes(sales_order_name):
    sales_order = frappe.get_doc("Sales Order", sales_order_name)
    sales_order.check_permission("read")
    if not is_mes_integration_enabled(sales_order.get("company")):
        throw_mes_integration_disabled(sales_order.get("company"))

    return push_sales_order_status_doc_to_mes(sales_order)


def build_sales_order_status_payload(
    sales_order, triggered_status=None, trigger_event=None
):
    sales_order_status = triggered_status or sales_order.get("custom_process_status")

    payload = {
        "documentType": SALES_ORDER_STATUS_DOCUMENT_TYPE,
        "erpSalesOrderNo": sales_order.name,
        "sales_order": sales_order.name,
        "documentNo": sales_order.name,
        "referenceNo": sales_order.name,
        "docstatus": sales_order.docstatus,
        "salesOrderStatus": sales_order_status,
        "erpSalesOrderStatus": sales_order_status,
        "message": (
            _("ERP Sales Order {0} 状态：{1}").format(sales_order.name, sales_order_status or '')
        ),
    }

    if trigger_event:
        payload["triggerEvent"] = trigger_event
        payload["trigger_event"] = trigger_event

    return payload
