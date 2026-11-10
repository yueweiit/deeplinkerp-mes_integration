"""Preserve the MES receipt number for marked standard Material Receipts."""

import hashlib

import frappe

from mes_integration.patches.v1_0.protect_receipt_stock_entry_no_client_script import (
    CLIENT_SCRIPT_NAME,
    get_script,
)


LEGACY_SCRIPT_SHA256 = "00e8730d2c60a2d64dc3e29ee72e322b21f2a4ba510bcf140c48d150a5a4367c"
OLD_CONDITION = "if (MES_RECEIPT_STOCK_ENTRY_TYPES.includes(frm.doc.stock_entry_type)) {"
NEW_CONDITION = """if (
        MES_RECEIPT_STOCK_ENTRY_TYPES.includes(frm.doc.stock_entry_type) ||
        (frm.doc.stock_entry_type === "Material Receipt" && Number(frm.doc.custom_mes_receipt) === 1)
    ) {"""


def updated_script():
    original = get_script()
    if original.count(OLD_CONDITION) != 1:
        raise RuntimeError("MES receipt number Client Script source has changed")
    return original.replace(OLD_CONDITION, NEW_CONDITION, 1)


def execute():
    if not frappe.db.exists("Client Script", CLIENT_SCRIPT_NAME):
        return

    script = frappe.get_doc("Client Script", CLIENT_SCRIPT_NAME)
    current = (script.script or "").replace("\r\n", "\n").strip()
    target = updated_script()
    if current == target.strip():
        return
    if hashlib.sha256(current.encode()).hexdigest() != LEGACY_SCRIPT_SHA256:
        frappe.logger().warning(
            "MES receipt number Client Script was customized; review its standard Material Receipt guard manually"
        )
        return

    script.script = target
    script.save(ignore_permissions=True)
