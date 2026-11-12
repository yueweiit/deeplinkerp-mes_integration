"""Check Stock Entry quantity labels across receipt and issue purposes."""

from __future__ import annotations

import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "public/js/stock_entry.js"


def test_receipt_labels_follow_purpose_and_restore_issue_labels() -> None:
	javascript = f"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const handlers = [];
global.frappe = {{ui:{{form:{{on:(doctype, events)=>{{if(doctype==='Stock Entry')handlers.push(events)}}}}}}}};
global.__ = value => value;
global.cint = value => Number(value || 0);
global.document = {{}};
global.$ = () => ({{ready(){{}},on(){{}}}});
vm.runInThisContext(fs.readFileSync({str(SCRIPT)!r}, 'utf8'));
const events = handlers.find(events => events.purpose);
const update = events.purpose;
const original = {{transfer_qty:'Issued Qty',actual_qty:'Actual Qty (at source/target)'}};
const grid = {{docfields:Object.entries(original).map(([fieldname,label])=>({{fieldname,label}})),
 update_docfield_property(fieldname,property,value){{
  assert.equal(property,'label');
  this.docfields.find(field=>field.fieldname===fieldname).label=value;
 }}}};
const frm = {{doc:{{purpose:'Material Receipt',stock_entry_type:'Other Receipt'}},
 fields_dict:{{items:{{grid}}}},_mes_integration_enabled:false}};
const labels = () => Object.fromEntries(grid.docfields.map(field=>[field.fieldname,field.label]));
update(frm);
assert.deepEqual(labels(),{{transfer_qty:'入库数量（库存单位）',actual_qty:'目标仓过账时点库存'}});
frm.doc.purpose='Material Issue';frm.doc.stock_entry_type='Material Issue';update(frm);
assert.deepEqual(labels(),original);
frm.doc.purpose='';frm.doc.stock_entry_type='Material Receipt';events.stock_entry_type(frm);
assert.deepEqual(labels(),{{transfer_qty:'入库数量（库存单位）',actual_qty:'目标仓过账时点库存'}});
frm.doc.purpose='Repack';frm.doc.stock_entry_type='Finished Goods Receipt';
frm._mes_integration_enabled=true;events.purpose(frm);
assert.deepEqual(labels(),{{transfer_qty:'入库数量（库存单位）',actual_qty:'目标仓过账时点库存'}});
frm.doc.stock_entry_type='Material Issue';events.purpose(frm);
assert.deepEqual(labels(),original);
const sharedCalls=[];
global.DeepLinkERPStockEntryQuantityLabels={{refresh:(form,isMesReceipt)=>sharedCalls.push(isMesReceipt)}};
frm.doc.stock_entry_type='Finished Goods Receipt';frm.doc.purpose='Repack';
frm._mes_integration_enabled=true;events.purpose(frm);
assert.deepEqual(sharedCalls,[true]);
frm._mes_integration_enabled=false;events.purpose(frm);
assert.deepEqual(sharedCalls,[true,false]);
"""
	result = subprocess.run(["node", "-e", javascript], text=True, capture_output=True)
	assert result.returncode == 0, result.stderr
