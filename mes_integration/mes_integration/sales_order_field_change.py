
from frappe import _

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime

import frappe
from frappe.utils import cint, getdate

from mes_integration.mes_integration.integration_log import create_mes_log
from mes_integration.mes_integration.settings import is_mes_integration_enabled
from mes_integration.mes_integration.stock_entry import validate_mes_api_user


BATCH_DOCTYPE = "MES Sales Order Field Change Batch"
OPERATION_DOCTYPE = "MES Sales Order Field Change Operation"
MAX_BATCH_ITEMS = 200
MAX_ID_LENGTH = 140
LOCK_TIMEOUT_SECONDS = 30
ALLOWED_PROCESS_STATUSES = {
	"Deposit Confirmation Processing",
	"Pending Production",
}
FIELD_SETTINGS = {
	"odtNo": {"erp_field": "custom_odt", "label": "ODT"},
	"deliveryDate": {"erp_field": "delivery_date", "label": "交期"},
}


def update_sales_order_fields_batch(data=None):
	"""Apply idempotent MES ODT and delivery-date changes to Sales Orders."""
	validate_mes_api_user()
	payload = get_batch_payload(data)
	validate_batch_payload(payload)
	validate_batch_idempotency_header(payload["requestId"])

	request_hash = hash_payload(payload)
	lock_keys = [f"batch:{payload['requestId']}"]
	lock_keys.extend(
		f"operation:{change['operationId']}"
		for item in payload["items"]
		for change in item["changes"].values()
	)

	with lock_field_change_keys(lock_keys):
		existing_batch = get_existing_batch(payload["requestId"])
		if existing_batch:
			return replay_existing_batch(existing_batch, request_hash)

		batch = create_batch_record(payload, request_hash)
		response_items = []
		operation_index = 0
		for item in payload["items"]:
			results = {}
			for field_key, change in item["changes"].items():
				operation_index += 1
				results[field_key] = process_field_change_with_savepoint(
					payload["requestId"],
					item,
					field_key,
					change,
					operation_index,
				)

			response_items.append(
				{
					"externalOrderId": item["externalOrderId"],
					"orderNo": item["orderNo"],
					"sourceVersion": item["sourceVersion"],
					"results": results,
				}
			)

		response = {
			"code": 200,
			"message": "processed",
			"data": {
				"requestId": payload["requestId"],
				"items": response_items,
			},
		}
		finish_batch_record(batch, response)
		log_batch_result(payload, response)
		return response


def get_sales_order_field_change_result(operation_id=None):
	"""Return the immutable final result stored for one MES operation."""
	validate_mes_api_user()
	operation_id = str(operation_id or "").strip()
	if not operation_id:
		return error_response(400, "INVALID_REQUEST", "缺少 operation_id。")

	record = get_operation_record(operation_id)
	if not record:
		return error_response(404, "OPERATION_NOT_FOUND", "未找到该 operationId 的处理结果。")

	if record.get("sales_order") and not frappe.has_permission(
		"Sales Order", "read", doc=record.get("sales_order")
	):
		return error_response(403, "PERMISSION_DENIED", "当前用户无权读取关联销售订单。")

	return {
		"code": 200,
		"message": "processed",
		"data": {
			"requestId": record.get("request_id"),
			"externalOrderId": record.get("external_order_id"),
			"orderNo": record.get("order_no"),
			"sourceVersion": record.get("source_version"),
			"field": record.get("field_key"),
			"result": build_result_from_record(record),
		},
	}


def get_batch_payload(data=None):
	if isinstance(data, str):
		try:
			data = json.loads(data)
		except ValueError:
			return data
	if isinstance(data, dict):
		return data

	if getattr(frappe.local, "request", None) and frappe.request.is_json:
		request_json = frappe.request.get_json(silent=True)
		if request_json is None:
			request_json = {}
		if isinstance(request_json, dict) and isinstance(request_json.get("data"), dict):
			return request_json["data"]
		return request_json

	return data


def validate_batch_payload(payload):
	if not isinstance(payload, dict):
		frappe.throw(_("请求体必须是 JSON 对象。"))

	unsupported = set(payload) - {"schemaVersion", "sourceSystem", "requestId", "items"}
	if unsupported:
		frappe.throw(_("请求体包含不支持的字段：{0}。").format(', '.join(sorted(unsupported))))
	if payload.get("schemaVersion") != "1.0":
		frappe.throw(_("schemaVersion 必须为 1.0。"))
	if payload.get("sourceSystem") != "MES":
		frappe.throw(_("sourceSystem 必须为 MES。"))
	validate_identifier(payload.get("requestId"), "requestId")

	items = payload.get("items")
	if not isinstance(items, list) or not items:
		frappe.throw(_("items 必须是非空数组。"))
	if len(items) > MAX_BATCH_ITEMS:
		frappe.throw(_("每批最多允许 {0} 张销售订单。").format(MAX_BATCH_ITEMS))

	seen_orders = set()
	seen_operations = set()
	for index, item in enumerate(items, start=1):
		validate_batch_item(item, index, seen_orders, seen_operations)


def validate_batch_item(item, index, seen_orders, seen_operations):
	if not isinstance(item, dict):
		frappe.throw(_("items 第 {0} 项必须是对象。").format(index))
	unsupported = set(item) - {"externalOrderId", "orderNo", "sourceVersion", "changes"}
	if unsupported:
		frappe.throw(_("items 第 {0} 项包含不支持的字段：{1}。").format(index, ', '.join(sorted(unsupported))))

	external_order_id = validate_identifier(item.get("externalOrderId"), f"items[{index}].externalOrderId")
	validate_identifier(item.get("orderNo"), f"items[{index}].orderNo")
	if external_order_id in seen_orders:
		frappe.throw(_("items 中重复出现销售订单 {0}。").format(external_order_id))
	seen_orders.add(external_order_id)

	source_version = item.get("sourceVersion")
	if isinstance(source_version, bool) or not isinstance(source_version, int) or source_version < 1:
		frappe.throw(_("items[{0}].sourceVersion 必须是大于 0 的整数。").format(index))

	changes = item.get("changes")
	if not isinstance(changes, dict) or not changes:
		frappe.throw(_("items[{0}].changes 必须是非空对象。").format(index))
	unsupported_changes = set(changes) - set(FIELD_SETTINGS)
	if unsupported_changes:
		frappe.throw(_("items[{0}].changes 包含不支持的字段：{1}。").format(index, ', '.join(sorted(unsupported_changes))))

	for field_key, change in changes.items():
		validate_field_change(change, index, field_key, seen_operations)


def validate_field_change(change, item_index, field_key, seen_operations):
	path = f"items[{item_index}].changes.{field_key}"
	if not isinstance(change, dict):
		frappe.throw(_("{0} 必须是对象。").format(path))
	unsupported = set(change) - {"operationId", "expectedValue", "value"}
	if unsupported:
		frappe.throw(_("{0} 包含不支持的字段：{1}。").format(path, ', '.join(sorted(unsupported))))
	if "expectedValue" not in change or "value" not in change:
		frappe.throw(_("{0} 必须同时包含 expectedValue 和 value。").format(path))

	operation_id = validate_identifier(change.get("operationId"), f"{path}.operationId")
	if operation_id in seen_operations:
		frappe.throw(_("items 中重复出现 operationId {0}。").format(operation_id))
	seen_operations.add(operation_id)

	if field_key == "odtNo":
		validate_odt_value(change.get("expectedValue"), f"{path}.expectedValue", allow_empty=True)
		validate_odt_value(change.get("value"), f"{path}.value", allow_empty=False)
	else:
		validate_date_value(change.get("expectedValue"), f"{path}.expectedValue", allow_empty=True)
		validate_date_value(change.get("value"), f"{path}.value", allow_empty=False)


def validate_identifier(value, fieldname):
	if not isinstance(value, str) or not value.strip():
		frappe.throw(_("{0} 必须是非空字符串。").format(fieldname))
	if value != value.strip():
		frappe.throw(_("{0} 不能包含首尾空格。").format(fieldname))
	if len(value) > MAX_ID_LENGTH:
		frappe.throw(_("{0} 不能超过 {1} 个字符。").format(fieldname, MAX_ID_LENGTH))
	return value


def validate_odt_value(value, fieldname, allow_empty):
	if value is None and allow_empty:
		return
	if not isinstance(value, str):
		frappe.throw(_("{0} 必须是字符串。").format(fieldname))
	if not allow_empty and not value.strip():
		frappe.throw(_("{0} 不能为空。").format(fieldname))
	if len(value) > MAX_ID_LENGTH:
		frappe.throw(_("{0} 不能超过 {1} 个字符。").format(fieldname, MAX_ID_LENGTH))


def validate_date_value(value, fieldname, allow_empty):
	if value in (None, "") and allow_empty:
		return
	if not isinstance(value, str):
		frappe.throw(_("{0} 必须是 YYYY-MM-DD 格式的字符串。").format(fieldname))
	try:
		parsed = datetime.strptime(value, "%Y-%m-%d").date()
	except ValueError:
		frappe.throw(_("{0} 必须是有效的 YYYY-MM-DD 日期。").format(fieldname))
	if parsed.isoformat() != value:
		frappe.throw(_("{0} 必须是 YYYY-MM-DD 格式。").format(fieldname))


def validate_batch_idempotency_header(request_id):
	if not getattr(frappe.local, "request", None):
		return
	header = (frappe.get_request_header("X-Idempotency-Key") or "").strip()
	if not header:
		frappe.throw(_("缺少 X-Idempotency-Key 请求头。"))
	if header != request_id:
		frappe.throw(_("X-Idempotency-Key 必须与 requestId 一致。"))


def process_field_change_with_savepoint(request_id, item, field_key, change, index):
	savepoint = f"mes_so_field_change_{index}"
	frappe.db.savepoint(savepoint)
	try:
		result = process_field_change(request_id, item, field_key, change)
		frappe.db.release_savepoint(savepoint)
		return result
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			title="MES Sales Order field change failed",
			message=frappe.get_traceback(),
		)
		result = build_result(
			change["operationId"],
			status="failed",
			error_code="APPLY_FAILED",
			message="ERP 处理字段修改时发生错误。",
			retryable=True,
		)
		create_operation_record(request_id, item, field_key, change, result)
		return result


def process_field_change(request_id, item, field_key, change):
	request_hash = hash_operation(item, field_key, change)
	existing = get_operation_record(change["operationId"])
	if existing:
		if existing.get("request_hash") != request_hash:
			return build_result(
				change["operationId"],
				status="conflict",
				error_code="IDEMPOTENCY_CONTENT_MISMATCH",
				message="operationId 已用于另一份字段修改内容。",
				retryable=False,
			)
		return build_result_from_record(existing)

	sales_order = get_locked_sales_order(item["externalOrderId"])
	if not sales_order:
		return persist_result(
			request_id,
			item,
			field_key,
			change,
			build_result(
				change["operationId"],
				status="failed",
				error_code="SALES_ORDER_NOT_FOUND",
				message="未找到 ERP 销售订单。",
				retryable=False,
			),
		)

	validation_result = validate_sales_order_for_change(sales_order, item, field_key, change)
	if validation_result:
		return persist_result(request_id, item, field_key, change, validation_result, sales_order)

	latest_version = get_latest_source_version(sales_order.name)
	if latest_version is not None and item["sourceVersion"] < latest_version:
		result = build_result(
			change["operationId"],
			status="conflict",
			current_value=get_current_value(sales_order, field_key),
			error_code="STALE_SOURCE_VERSION",
			message=f"sourceVersion {item['sourceVersion']} 小于 ERP 已处理版本 {latest_version}。",
			retryable=False,
		)
		return persist_result(request_id, item, field_key, change, result, sales_order)

	current_value = get_current_value(sales_order, field_key)
	expected_value = normalize_field_value(field_key, change.get("expectedValue"))
	if current_value != expected_value:
		result = build_result(
			change["operationId"],
			status="conflict",
			current_value=current_value,
			error_code="EXPECTED_VALUE_MISMATCH",
			message=f"当前{FIELD_SETTINGS[field_key]['label']}与 MES 预期原值不一致。",
			retryable=False,
		)
		return persist_result(request_id, item, field_key, change, result, sales_order)

	requested_value = normalize_field_value(field_key, change["value"])
	if field_key == "deliveryDate" and getdate(requested_value) < getdate(
		sales_order.get("transaction_date")
	):
		result = build_result(
			change["operationId"],
			status="conflict",
			current_value=current_value,
			error_code="INVALID_DELIVERY_DATE",
			message="交期不能早于销售订单日期。",
			retryable=False,
		)
		return persist_result(request_id, item, field_key, change, result, sales_order)

	apply_field_change(sales_order, field_key, requested_value)
	result = build_result(
		change["operationId"],
		status="success",
		applied_value=requested_value,
		message="success",
		retryable=False,
	)
	return persist_result(request_id, item, field_key, change, result, sales_order)


def validate_sales_order_for_change(sales_order, item, field_key, change):
	operation_id = change["operationId"]
	erp_field = FIELD_SETTINGS[field_key]["erp_field"]
	missing_fields = [
		fieldname
		for fieldname in ("custom_process_status", erp_field)
		if not frappe.db.has_column("Sales Order", fieldname)
	]
	if missing_fields:
		return build_result(
			operation_id,
			status="failed",
			error_code="ERP_SCHEMA_NOT_READY",
			message=f"ERP 销售订单缺少字段 {', '.join(missing_fields)}。",
			retryable=False,
		)

	valid_order_numbers = {
		str(value)
		for value in (sales_order.name, sales_order.get("custom_crm_order_no"))
		if value
	}
	if item["orderNo"] not in valid_order_numbers:
		return build_result(
			operation_id,
			status="conflict",
			current_value=get_current_value(sales_order, field_key),
			error_code="ORDER_NO_MISMATCH",
			message="orderNo 与 ERP 销售订单身份不一致。",
			retryable=False,
		)

	if not is_mes_integration_enabled(sales_order.get("company")):
		return build_result(
			operation_id,
			status="failed",
			error_code="MES_INTEGRATION_DISABLED",
			message="销售订单所属公司未启用 MES 集成。",
			retryable=False,
		)

	if not frappe.has_permission("Sales Order", "write", doc=sales_order):
		return build_result(
			operation_id,
			status="failed",
			error_code="PERMISSION_DENIED",
			message="当前用户无权修改该销售订单。",
			retryable=False,
		)

	if sales_order.docstatus != 1 or sales_order.get("status") == "Closed":
		return build_order_state_conflict(operation_id, sales_order, field_key)
	if sales_order.get("custom_process_status") not in ALLOWED_PROCESS_STATUSES:
		return build_order_state_conflict(operation_id, sales_order, field_key)


def build_order_state_conflict(operation_id, sales_order, field_key):
	process_status = sales_order.get("custom_process_status") or sales_order.get("status") or "Unknown"
	return build_result(
		operation_id,
		status="conflict",
		current_value=get_current_value(sales_order, field_key),
		error_code="ORDER_STATE_NOT_ALLOWED",
		message=f"销售订单当前状态 {process_status} 不允许 MES 修改 ODT 或交期。",
		retryable=False,
	)


def get_locked_sales_order(sales_order_name):
	rows = frappe.db.sql(
		"SELECT name FROM `tabSales Order` WHERE name = %s FOR UPDATE",
		(sales_order_name,),
	)
	if not rows:
		return None
	return frappe.get_doc("Sales Order", sales_order_name)


def get_latest_source_version(sales_order_name):
	row = frappe.get_all(
		OPERATION_DOCTYPE,
		filters={"sales_order": sales_order_name},
		fields=["source_version"],
		order_by="source_version desc",
		limit_page_length=1,
	)
	return cint(row[0].source_version) if row else None


def get_current_value(sales_order, field_key):
	return normalize_field_value(field_key, sales_order.get(FIELD_SETTINGS[field_key]["erp_field"]))


def normalize_field_value(field_key, value):
	if value in (None, ""):
		return None
	if field_key == "deliveryDate":
		return getdate(value).isoformat()
	return str(value)


def apply_field_change(sales_order, field_key, value):
	if field_key == "deliveryDate":
		for row in sales_order.get("items") or []:
			frappe.db.set_value(
				"Sales Order Item",
				row.name,
				"delivery_date",
				value,
				update_modified=False,
			)

	frappe.db.set_value(
		"Sales Order",
		sales_order.name,
		FIELD_SETTINGS[field_key]["erp_field"],
		value,
		update_modified=True,
	)
	sales_order.set(FIELD_SETTINGS[field_key]["erp_field"], value)
	sales_order.notify_update()


def persist_result(request_id, item, field_key, change, result, sales_order=None):
	create_operation_record(
		request_id,
		item,
		field_key,
		change,
		result,
		sales_order=sales_order,
	)
	return result


def create_operation_record(
	request_id,
	item,
	field_key,
	change,
	result,
	sales_order=None,
):
	frappe.get_doc(
		{
			"doctype": OPERATION_DOCTYPE,
			"operation_id": change["operationId"],
			"request_id": request_id,
			"request_hash": hash_operation(item, field_key, change),
			"sales_order": sales_order.name if sales_order else None,
			"external_order_id": item["externalOrderId"],
			"order_no": item["orderNo"],
			"source_version": item["sourceVersion"],
			"field_key": field_key,
			"expected_value": normalize_field_value(field_key, change.get("expectedValue")),
			"requested_value": normalize_field_value(field_key, change.get("value")),
			"status": result["status"],
			"applied_value": result.get("appliedValue"),
			"current_value": result.get("currentValue"),
			"error_code": result.get("errorCode"),
			"message": result.get("message"),
			"retryable": cint(result.get("retryable")),
			"processed_by": frappe.session.user,
		}
	).insert(ignore_permissions=True)


def get_operation_record(operation_id):
	name = frappe.db.get_value(OPERATION_DOCTYPE, {"operation_id": operation_id}, "name")
	return frappe.get_doc(OPERATION_DOCTYPE, name) if name else None


def build_result_from_record(record):
	return build_result(
		record.get("operation_id"),
		status=record.get("status"),
		applied_value=record.get("applied_value"),
		current_value=record.get("current_value"),
		error_code=record.get("error_code"),
		message=record.get("message"),
		retryable=bool(record.get("retryable")),
	)


def build_result(
	operation_id,
	status,
	applied_value=None,
	current_value=None,
	error_code=None,
	message=None,
	retryable=False,
):
	return {
		"operationId": operation_id,
		"status": status,
		"appliedValue": applied_value,
		"currentValue": current_value,
		"errorCode": error_code,
		"message": message,
		"retryable": bool(retryable),
	}


def create_batch_record(payload, request_hash):
	return frappe.get_doc(
		{
			"doctype": BATCH_DOCTYPE,
			"request_id": payload["requestId"],
			"request_hash": request_hash,
			"status": "Processing",
			"source_system": payload["sourceSystem"],
			"schema_version": payload["schemaVersion"],
			"submitted_by": frappe.session.user,
			"item_count": len(payload["items"]),
			"request_payload": as_json(payload),
		}
	).insert(ignore_permissions=True)


def finish_batch_record(batch, response):
	batch.db_set(
		{
			"status": "Processed",
			"response_payload": as_json(response),
		},
		update_modified=True,
	)


def get_existing_batch(request_id):
	name = frappe.db.get_value(BATCH_DOCTYPE, {"request_id": request_id}, "name")
	return frappe.get_doc(BATCH_DOCTYPE, name) if name else None


def replay_existing_batch(batch, request_hash):
	if batch.get("request_hash") != request_hash:
		return error_response(
			409,
			"IDEMPOTENCY_CONTENT_MISMATCH",
			"requestId 已用于另一份批量修改内容。",
			request_id=batch.get("request_id"),
		)
	if batch.get("status") != "Processed" or not batch.get("response_payload"):
		return error_response(
			202,
			"REQUEST_PROCESSING",
			"该批次仍在处理中，请按 operationId 查询结果。",
			request_id=batch.get("request_id"),
		)
	return json.loads(batch.response_payload)


def error_response(http_status, error_code, message, request_id=None):
	set_http_status(http_status)
	response = {
		"code": http_status,
		"message": "failed" if http_status != 202 else "processing",
		"errorCode": error_code,
		"errorMessage": message,
	}
	if request_id:
		response["data"] = {"requestId": request_id}
	return response


def set_http_status(http_status):
	if getattr(frappe.local, "response", None) is not None:
		frappe.local.response["http_status_code"] = http_status


def hash_payload(payload):
	return hashlib.sha256(as_json(payload).encode("utf-8")).hexdigest()


def hash_operation(item, field_key, change):
	return hash_payload(
		{
			"externalOrderId": item["externalOrderId"],
			"orderNo": item["orderNo"],
			"sourceVersion": item["sourceVersion"],
			"field": field_key,
			"expectedValue": change.get("expectedValue"),
			"value": change.get("value"),
		}
	)


def as_json(value):
	return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def log_batch_result(payload, response):
	statuses = [
		result["status"]
		for item in response["data"]["items"]
		for result in item["results"].values()
	]
	create_mes_log(
		direction="Inbound",
		event="Sales Order Field Change Batch",
		status="Success" if statuses and all(status == "success" for status in statuses) else "Failed",
		source="MES",
		user=frappe.session.user,
		request_payload=payload,
		response_payload=response,
		trace_id=payload["requestId"],
		processed=len(statuses),
		http_status_code=200,
		batch_no=payload["requestId"],
	)


@contextmanager
def lock_field_change_keys(keys):
	if getattr(frappe.db, "db_type", None) != "mariadb":
		yield
		return

	lock_names = sorted(
		{
			"mes_so_change_"
			+ hashlib.sha256(
				f"{getattr(frappe.local, 'site', '')}|{key}".encode("utf-8")
			).hexdigest()
			for key in keys
		}
	)
	acquired = []
	for lock_name in lock_names:
		result = frappe.db.sql(
			"SELECT GET_LOCK(%s, %s)",
			(lock_name, LOCK_TIMEOUT_SECONDS),
		)
		if not result or cint(result[0][0]) != 1:
			for lock_name in reversed(acquired):
				frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
			raise frappe.QueryDeadlockError(_("等待 MES 销售订单修改幂等锁超时。"))
		acquired.append(lock_name)

	def release_locks():
		for lock_name in reversed(acquired):
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))

	frappe.db.after_commit.add(release_locks)
	frappe.db.after_rollback.add(release_locks)
	yield
