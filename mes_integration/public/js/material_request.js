const INJECTION_MOLDING_PURPOSE = "Injection Molding Issuance";
const INJECTION_MOLDING_WEIGHT_FIELDS = [
	"custom_new_material_weight",
	"custom_recycled_material_weight"
];
const CUSTOM_ISSUE_MATERIAL_REQUEST_TYPES = [
	"Material Issue",
	"Material Transfer for Manufacture",
	INJECTION_MOLDING_PURPOSE
];
const SUBMIT_AND_ISSUE_MATERIAL_REQUEST_TYPES = [
	"Material Issue",
	INJECTION_MOLDING_PURPOSE
];

frappe.ui.form.on("Material Request", {
	setup: function(frm) {
		set_item_detail_queries(frm);
	},

	onload: function(frm) {
		return load_mes_material_request_integration_enabled(frm);
	},

	onload_post_render: function(frm) {
		return load_and_refresh_mes_material_request_ui(frm);
	},

	refresh: function(frm) {
		return load_and_refresh_mes_material_request_ui(frm);
	},

	company: function(frm) {
		frm._mes_integration_company = null;
		frm._mes_integration_enabled = false;
		frm._mes_integration_enabled_promise = null;
		return load_and_refresh_mes_material_request_ui(frm);
	},

	on_submit: function(frm) {
		if (frm._mes_submit_and_issue || !is_mes_material_request_integration_enabled(frm)) {
			return;
		}

		show_issue_stock_entry_prompt(frm);
	},

	material_request_type: function(frm) {
		refresh_mes_material_request_ui(frm);
	}
});

frappe.ui.form.on("Material Request Item", {
	items_add: function(frm) {
		toggle_injection_molding_weight_fields(frm);
	},

	custom_material_request_item_detail_button: function(frm, cdt, cdn) {
		show_material_request_item_details(frm, cdt, cdn);
	}
});

frappe.ui.form.on("MES Material Request Item Detail", {
	material_request_item_idx: function(frm, cdt, cdn) {
		set_item_detail_from_item_row(frm, cdt, cdn);
	}
});


function load_and_refresh_mes_material_request_ui(frm) {
	return load_mes_material_request_integration_enabled(frm).then(function() {
		refresh_mes_material_request_ui(frm);
	});
}

function load_mes_material_request_integration_enabled(frm) {
	if (!frm || !frm.doc || !frm.doc.company) {
		if (frm) {
			frm._mes_integration_company = null;
			frm._mes_integration_enabled = false;
			frm._mes_integration_enabled_promise = null;
		}
		return Promise.resolve(false);
	}

	if (frm._mes_integration_company === frm.doc.company) {
		if (frm._mes_integration_enabled_promise) {
			return frm._mes_integration_enabled_promise;
		}
		return Promise.resolve(Boolean(frm._mes_integration_enabled));
	}

	const company = frm.doc.company;
	frm._mes_integration_company = company;
	frm._mes_integration_enabled = false;

	const enabled_promise = Promise.resolve(frappe.db
		.get_value("Company", company, "custom_enable_mes_integration"))
		.then(function(r) {
			const value = r && r.message ? r.message.custom_enable_mes_integration : 0;
			const enabled = cint(value) === 1;
			if (frm.doc && frm.doc.company === company && frm._mes_integration_company === company) {
				frm._mes_integration_enabled = enabled;
			}
			return enabled;
		})
		.catch(function() {
			if (frm.doc && frm.doc.company === company && frm._mes_integration_company === company) {
				frm._mes_integration_enabled = false;
			}
			return false;
		})
		.finally(function() {
			if (frm._mes_integration_enabled_promise === enabled_promise) {
				frm._mes_integration_enabled_promise = null;
			}
		});

	frm._mes_integration_enabled_promise = enabled_promise;
	return enabled_promise;
}

function is_mes_material_request_integration_enabled(frm) {
	return Boolean(frm && frm._mes_integration_enabled);
}

function refresh_mes_material_request_ui(frm) {
	if (!is_mes_material_request_integration_enabled(frm)) {
		frm.remove_custom_button(__("提交并发料"));
		frm.remove_custom_button(__("发料并推送至DLM"));
		frm.remove_custom_button(__("生成物料移动"));
		frm.remove_custom_button(__("发料"), __("Create"));
	}

	toggle_injection_molding_weight_fields(frm);
	apply_injection_molding_material_issue_warehouse_labels(frm);
	add_custom_issue_stock_entry_button(frm);
}

function set_item_detail_queries(frm) {
	frm.set_query("item_code", "custom_item_details", function(doc) {
		const item_codes = (doc.items || [])
			.map(function(row) {
				return row.item_code;
			})
			.filter(Boolean);

		return {
			filters: {
				name: ["in", item_codes.length ? item_codes : [""]]
			}
		};
	});
}

function set_item_detail_from_item_row(frm, cdt, cdn) {
	const detail = locals[cdt][cdn];
	const item_row = (frm.doc.items || []).find(function(row) {
		return cint(row.idx) === cint(detail.material_request_item_idx);
	});

	if (!item_row) {
		frappe.model.set_value(cdt, cdn, "material_request_item", "");
		return;
	}

	frappe.model.set_value(cdt, cdn, {
		item_code: item_row.item_code,
		item_name: item_row.item_name,
		uom: item_row.uom || item_row.stock_uom,
		material_request_item: item_row.name
	});
}



function show_material_request_item_details(frm, cdt, cdn) {
	const item_row = locals[cdt][cdn];

	if (!item_row || !item_row.item_code) {
		frappe.msgprint(__("请先选择物料编码"));
		return;
	}

	const details = get_material_request_item_details(frm, item_row);
	const dialog = new frappe.ui.Dialog({
		title: __("物料具体明细 - {0}", [item_row.item_code]),
		size: "large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "details_html"
			}
		]
	});

	dialog.fields_dict.details_html.$wrapper.html(
		get_material_request_item_details_html(item_row, details)
	);
	dialog.show();
	dialog.$wrapper.addClass("mes-item-detail-dialog");
}

function get_material_request_item_details(frm, item_row) {
	return (frm.doc.custom_item_details || []).filter(function(detail) {
		return detail.item_code === item_row.item_code;
	});
}

function get_material_request_item_details_html(item_row, details) {
	const total_order_qty = details.reduce(function(total, detail) {
		return total + flt(detail.order_qty);
	}, 0);
	const total_issue_qty = details.reduce(function(total, detail) {
		return total + flt(detail.issue_qty);
	}, 0);

	if (!details.length) {
		return `
			<div class="text-muted">
				${__("当前物料在物料具体明细中没有记录")}
			</div>
		`;
	}

	const rows = details.map(function(detail) {
		return `
			<tr>
				<td class="text-muted">${mes_escape_html(detail.material_request_item_idx || "")}</td>
				<td>
					<div class="mes-detail-primary">${mes_escape_html(detail.item_code || "")}</div>
					<div class="mes-detail-secondary">${mes_escape_html(detail.item_name || item_row.item_name || "")}</div>
				</td>
				<td>
					<div class="mes-detail-primary">${mes_escape_html(detail.model || "")}</div>
					<div class="mes-detail-secondary">${mes_escape_html(detail.model_code || "")}</div>
				</td>
				<td>
					<div class="mes-detail-primary">${mes_escape_html(detail.color || "")}</div>
					<div class="mes-detail-secondary">${mes_escape_html(detail.color_code || "")}</div>
				</td>
				<td>${mes_escape_html(detail.article_code || "")}</td>
				<td>${mes_escape_html(detail.batch_no || "")}</td>
				<td class="text-right mes-detail-number">${mes_format_detail_qty(detail.order_qty)}</td>
				<td class="text-right mes-detail-number">${mes_format_detail_qty(detail.issue_qty)}</td>
				<td>${mes_escape_html(detail.uom || item_row.uom || item_row.stock_uom || "")}</td>
				<td>${mes_escape_html(detail.remarks || "")}</td>
			</tr>
		`;
	}).join("");

	return `
		<style>
			.mes-item-detail-dialog .modal-dialog {
				max-width: min(1320px, calc(100vw - 48px));
				width: min(1320px, calc(100vw - 48px));
			}
			.mes-item-detail-dialog .modal-body {
				padding: 20px 24px 24px;
			}
			.mes-item-detail-summary {
				margin-bottom: 16px;
			}
			.mes-item-detail-summary .item-code {
				font-size: 16px;
				font-weight: 600;
			}
			.mes-item-detail-table-wrap {
				max-height: 62vh;
				overflow: auto;
				border: 1px solid var(--border-color);
				border-radius: 6px;
			}
			.mes-item-detail-table {
				min-width: 970px;
				margin-bottom: 0;
				font-size: 13px;
			}
			.mes-item-detail-table th {
				position: sticky;
				top: 0;
				z-index: 1;
				background: var(--fg-color);
				white-space: nowrap;
				vertical-align: middle;
			}
			.mes-item-detail-table td {
				vertical-align: top;
				word-break: normal;
			}
			.mes-detail-primary {
				font-weight: 500;
				line-height: 1.35;
			}
			.mes-detail-secondary {
				margin-top: 2px;
				color: var(--text-muted);
				font-size: 12px;
				line-height: 1.3;
			}
			.mes-detail-number {
				white-space: nowrap;
				font-variant-numeric: tabular-nums;
			}
		</style>
		<div class="mes-item-detail-summary">
			<div class="item-code">${mes_escape_html(item_row.item_code || "")}</div>
			<div class="text-muted">${mes_escape_html(item_row.item_name || "")}</div>
		</div>
		<div class="mes-item-detail-table-wrap">
			<table class="table table-bordered table-hover mes-item-detail-table">
				<thead>
					<tr>
						<th style="width: 64px;">${__("明细行号")}</th>
						<th style="width: 160px;">${__("物料")}</th>
						<th style="width: 190px;">${__("型号")}</th>
						<th style="width: 140px;">${__("颜色")}</th>
						<th style="width: 150px;">${__("成品货号")}</th>
						<th style="width: 170px;">${__("生产批次号")}</th>
						<th class="text-right" style="width: 110px;">${__("订单数量")}</th>
						<th class="text-right" style="width: 110px;">${__("需求量")}</th>
						<th style="width: 80px;">${__("单位")}</th>
						<th style="width: 180px;">${__("备注")}</th>
					</tr>
				</thead>
				<tbody>
					${rows}
				</tbody>
				<tfoot>
					<tr>
						<th colspan="6" class="text-right">${__("合计")}</th>
						<th class="text-right mes-detail-number">${mes_format_detail_qty(total_order_qty)}</th>
						<th class="text-right mes-detail-number">${mes_format_detail_qty(total_issue_qty)}</th>
						<th></th>
						<th></th>
					</tr>
				</tfoot>
			</table>
		</div>
	`;
}

function mes_escape_html(value) {
	return frappe.utils.escape_html(cstr(value));
}

function mes_format_detail_qty(value) {
	return format_number(flt(value), null, frappe.defaults.get_default("float_precision"));
}

function add_custom_issue_stock_entry_button(frm) {
	frm.remove_custom_button(__("提交并发料"));
	frm.remove_custom_button(__("发料并推送至DLM"));
	frm.remove_custom_button(__("生成物料移动"));
	frm.remove_custom_button(__("发料"), __("Create"));

	if (can_submit_and_issue_material_request(frm)) {
		frm.add_custom_button(__("提交并发料"), function() {
			submit_and_issue_material_request(frm);
		});
	}

	if (can_submit_issue_and_push_to_dlm(frm)) {
		frm.add_custom_button(__("发料并推送至DLM"), function() {
			submit_issue_and_push_to_dlm(frm);
		});
	}

	if (can_submit_and_issue_material_request(frm) || can_submit_issue_and_push_to_dlm(frm)) {
		style_submit_and_issue_button(frm);
		return;
	}

	if (!can_create_material_movement(frm)) {
		return;
	}

	frm.add_custom_button(__("生成物料移动"), function() {
		open_issue_stock_entry(frm);
	});
	style_material_movement_button(frm);
}

function can_submit_and_issue_material_request(frm) {

	if (!is_mes_material_request_integration_enabled(frm)) {
		return false;
	}
	return (
		frm &&
		frm.doc &&
		frm.doc.docstatus === 0 &&
		!frm.is_new() &&
		is_mes_material_request(frm) &&
		SUBMIT_AND_ISSUE_MATERIAL_REQUEST_TYPES.includes(frm.doc.material_request_type)
	);
}

function submit_and_issue_material_request(frm) {
	frappe.confirm(
		__("确认提交此物料需求并立即发料？"),
		function() {
			frm._mes_submit_and_issue = true;
			frm.save("Submit")
				.then(function() {
					frm._mes_submit_and_issue = false;
					setTimeout(function() {
						open_issue_stock_entry(frm);
					}, 300);
				})
				.catch(function() {
					frm._mes_submit_and_issue = false;
				});
		}
	);
}

function can_submit_issue_and_push_to_dlm(frm) {
	return can_create_issue_stock_entry(frm);
}

function is_mes_material_request(frm) {
	const source = cstr(frm && frm.doc && frm.doc.custom_request_source).trim();
	if (source) {
		return source === "MES";
	}

	return cstr(frm && frm.doc && frm.doc.name).startsWith("MAT-MR-MES-");
}

function submit_issue_and_push_to_dlm(frm) {
	const rows = get_issue_and_push_dialog_rows(frm);
	if (!rows.length) {
		frappe.msgprint({
			title: __("提示"),
			indicator: "orange",
			message: __("没有可发料的明细")
		});
		return;
	}

	frappe.call({
		method: "mes_integration.mes_integration.material_request.get_issue_dialog_default_uoms",
		args: {
			item_codes: rows.map(function(row) {
				return row.item_code;
			})
		},
		callback: function(r) {
			const result = r.message || {};
			apply_issue_dialog_default_uoms(rows, result.default_uoms || {});
			show_issue_and_push_dialog(frm, rows);
			show_issue_dialog_default_uom_warnings(result.warnings || []);
		},
		error: function() {
			show_issue_and_push_dialog(frm, rows);
		}
	});
}

function apply_issue_dialog_default_uoms(rows, default_uoms) {
	(rows || []).forEach(function(row) {
		const default_uom = default_uoms[row.item_code];
		if (!default_uom || !default_uom.uom || !flt(default_uom.conversion_factor)) {
			return;
		}

		row.uom = default_uom.uom;
		set_issue_dialog_row_conversion_factor(row, default_uom.conversion_factor);
	});
}

function show_issue_dialog_default_uom_warnings(warnings) {
	warnings = (warnings || []).filter(Boolean);
	if (!warnings.length) {
		return;
	}

	frappe.msgprint({
		title: __("默认发料单位提示"),
		indicator: "orange",
		message: warnings.join("<br>")
	});
}

function show_issue_and_push_dialog(frm, rows) {
	const is_transfer = frm.doc.material_request_type === "Material Transfer for Manufacture";
	const dialog = new frappe.ui.Dialog({
		title: __("发料并推送至DLM"),
		size: "extra-large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "message",
				options: `<p class="text-muted">${__("请确认本次发料数量和发料仓，确认后将自动创建并提交物料移动，然后推送至 DLM。")}</p>`
			},
			{
				fieldtype: "Table",
				fieldname: "items",
				label: __("物料"),
				cannot_add_rows: true,
				in_place_edit: true,
				data: rows,
				fields: get_issue_and_push_dialog_fields(frm, is_transfer)
			}
		],
		primary_action_label: __("确定"),
		primary_action: function(values) {
			const issue_rows = get_validated_issue_and_push_rows(values.items || [], is_transfer);
			if (!issue_rows) {
				return;
			}

			dialog.hide();
			call_issue_and_push_to_dlm(frm, issue_rows);
		}
	});

	dialog.show();
	setup_issue_and_push_dialog_warehouse_queries(frm, dialog);
	setup_issue_dialog_actual_qty_refresh(dialog);
	refresh_issue_dialog_actual_qty(dialog);
	dialog.$wrapper.addClass("mes-issue-push-dialog");
	dialog.$wrapper.find(".modal-dialog").css({
		"width": "min(1200px, 92vw)",
		"max-width": "92vw"
	});
}

function get_issue_and_push_dialog_rows(frm) {
	return (frm.doc.items || [])
		.map(function(row) {
			const original_conversion_factor = flt(row.conversion_factor) || 1;
			const remaining_qty = get_material_request_item_remaining_qty(row);
			const remaining_stock_qty = remaining_qty * original_conversion_factor;
			const max_issue_stock_qty = get_issue_dialog_max_issue_stock_qty(remaining_stock_qty, original_conversion_factor);
			return {
				material_request_item: row.name,
				item_code: row.item_code,
				item_name: row.item_name,
				uom: row.uom || row.stock_uom,
				request_uom: row.uom || row.stock_uom,
				stock_uom: row.stock_uom || row.uom,
				original_conversion_factor: original_conversion_factor,
				conversion_factor: original_conversion_factor,
				remaining_stock_qty: remaining_stock_qty,
				max_issue_stock_qty: max_issue_stock_qty,
				actual_stock_qty: 0,
				max_issue_qty: max_issue_stock_qty / original_conversion_factor,
				requested_qty: flt(row.qty),
				issued_qty: get_material_request_item_issued_qty(row),
				remaining_qty: remaining_qty,
				qty: remaining_qty,
				s_warehouse: get_default_issue_source_warehouse(frm, row),
				actual_qty: 0,
				projected_remaining_qty: 0,
				projected_remaining_stock_qty: 0,
				t_warehouse: get_default_issue_target_warehouse(frm, row)
			};
		})
		.filter(function(row) {
			return row.item_code && flt(row.remaining_qty) > 0;
		});
}

function get_issue_and_push_dialog_fields(frm, is_transfer) {
	const warehouse_query = function() {
		return get_material_request_issue_warehouse_query(frm, this && this.doc);
	};
	const update_actual_qty = function() {
		update_issue_dialog_row_actual_qty(this && this.doc);
	};
	const update_uom = function() {
		update_issue_dialog_row_uom(this && this.doc);
	};
	const update_projected_remaining_qty = function() {
		update_issue_dialog_row_projected_remaining_qty(this && this.doc);
	};

	return [
		{ fieldtype: "Data", fieldname: "material_request_item", label: __("Material Request Item"), hidden: 1, read_only: 1 },
		{ fieldtype: "Link", fieldname: "item_code", label: __("物料编码"), options: "Item", in_list_view: 1, read_only: 1, columns: 1 },
		{ fieldtype: "Data", fieldname: "item_name", label: __("物料名称"), hidden: 1, read_only: 1 },
		{ fieldtype: "Link", fieldname: "uom", label: __("单位"), options: "UOM", in_list_view: 1, reqd: 1, columns: 1, onchange: update_uom },
		{ fieldtype: "Float", fieldname: "qty", label: __("本次发料数量"), in_list_view: 1, reqd: 1, columns: 1, onchange: update_projected_remaining_qty },
		{ fieldtype: "Float", fieldname: "original_conversion_factor", hidden: 1, read_only: 1 },
		{ fieldtype: "Float", fieldname: "conversion_factor", hidden: 1, read_only: 1 },
		{ fieldtype: "Float", fieldname: "remaining_stock_qty", hidden: 1, read_only: 1 },
		{ fieldtype: "Float", fieldname: "max_issue_stock_qty", hidden: 1, read_only: 1 },
		{ fieldtype: "Float", fieldname: "max_issue_qty", hidden: 1, read_only: 1 },
		{ fieldtype: "Float", fieldname: "actual_qty", hidden: 1, read_only: 1 },
		{ fieldtype: "Float", fieldname: "projected_remaining_qty", hidden: 1, read_only: 1 },
		{ fieldtype: "Float", fieldname: "requested_qty", label: __("需求数量"), in_list_view: 1, read_only: 1, columns: 1 },
		{ fieldtype: "Float", fieldname: "issued_qty", label: __("已发料数量"), hidden: 1, read_only: 1 },
		{ fieldtype: "Float", fieldname: "remaining_qty", label: __("剩余数量"), in_list_view: 1, read_only: 1, columns: 1 },
		{ fieldtype: "Data", fieldname: "request_uom", label: __("需求单位"), in_list_view: 1, read_only: 1, columns: 1 },
		{ fieldtype: "Link", fieldname: "s_warehouse", label: __("发料仓"), options: "Warehouse", in_list_view: 1, reqd: 1, columns: 2, get_query: warehouse_query, onchange: update_actual_qty },
		{ fieldtype: "Float", fieldname: "actual_stock_qty", label: __("实际库存"), in_list_view: 1, read_only: 1, columns: 1 },
		{ fieldtype: "Float", fieldname: "projected_remaining_stock_qty", label: __("预计剩余"), in_list_view: 1, read_only: 1, columns: 1 },
		{ fieldtype: "Link", fieldname: "t_warehouse", label: __("目标仓库"), options: "Warehouse", in_list_view: is_transfer ? 1 : 0, hidden: is_transfer ? 0 : 1, reqd: is_transfer ? 1 : 0, columns: 2, get_query: warehouse_query }
	];
}

function setup_issue_and_push_dialog_warehouse_queries(frm, dialog, fieldname = "items") {
	const grid = dialog.fields_dict[fieldname] && dialog.fields_dict[fieldname].grid;
	if (!grid) {
		return;
	}

	["s_warehouse", "t_warehouse"].forEach(function(fieldname) {
		const field = grid.get_field(fieldname);
		if (!field) {
			return;
		}

		field.get_query = function(doc, cdt, cdn) {
			const row = get_issue_dialog_grid_row_doc(grid, cdt, cdn) || (this && this.doc);
			return get_material_request_issue_warehouse_query(frm, row);
		};
	});
}

function setup_issue_dialog_actual_qty_refresh(dialog, fieldname = "items") {
	const grid = dialog.fields_dict[fieldname] && dialog.fields_dict[fieldname].grid;
	if (!grid) {
		return;
	}

	grid.wrapper.on("change", '[data-fieldname="s_warehouse"] input', function() {
		setTimeout(function() {
			refresh_issue_dialog_actual_qty(dialog);
		}, 100);
	});

	grid.wrapper.on("change", '[data-fieldname="uom"] input', function() {
		setTimeout(function() {
			const rows = dialog.get_value(fieldname) || [];
			Promise.all(rows.map(update_issue_dialog_row_uom)).then(function() {
				grid.refresh();
			});
		}, 100);
	});

	grid.wrapper.on("change", '[data-fieldname="qty"] input', function() {
		setTimeout(function() {
			const rows = dialog.get_value(fieldname) || [];
			rows.forEach(update_issue_dialog_row_projected_remaining_qty);
			grid.refresh();
		}, 100);
	});
}

function refresh_issue_dialog_actual_qty(dialog, fieldname = "items") {
	const grid = dialog.fields_dict[fieldname] && dialog.fields_dict[fieldname].grid;
	const rows = dialog.get_value(fieldname) || [];
	const promises = rows.map(function(row) {
		return update_issue_dialog_row_actual_qty(row);
	});

	Promise.all(promises).then(function() {
		if (grid) {
			grid.refresh();
		}
	});
}

function update_issue_dialog_row_actual_qty(row) {
	if (!row || !row.item_code || !row.s_warehouse) {
		set_issue_dialog_row_actual_qty(row, 0);
		return Promise.resolve();
	}

	return frappe.call({
		method: "mes_integration.mes_integration.material_request.get_item_warehouse_actual_qty",
		args: {
			item_code: row.item_code,
			warehouse: row.s_warehouse
		},
		callback: function(r) {
			set_issue_dialog_row_actual_qty(row, flt(r.message));
		}
	}); 
}

function update_issue_dialog_row_uom(row) {
	if (!row || !row.item_code || !row.uom) {
		return Promise.resolve();
	}

	return frappe.call({
		method: "erpnext.stock.get_item_details.get_conversion_factor",
		args: {
			item_code: row.item_code,
			uom: row.uom
		},
		callback: function(r) {
			const conversion_factor = flt(r.message && r.message.conversion_factor) || 1;
			set_issue_dialog_row_conversion_factor(row, conversion_factor);
		}
	});
}

function set_issue_dialog_row_conversion_factor(row, conversion_factor) {
	if (!row) {
		return;
	}

	row.conversion_factor = flt(conversion_factor) || 1;
	row.max_issue_stock_qty = get_issue_dialog_max_issue_stock_qty(row.remaining_stock_qty, row.conversion_factor);
	row.max_issue_qty = flt(row.max_issue_stock_qty) / row.conversion_factor;
	row.actual_qty = flt(row.actual_stock_qty) / row.conversion_factor;
	row.qty = row.max_issue_qty;
	update_issue_dialog_row_projected_remaining_qty(row);
	if (row.doctype && row.name && locals[row.doctype] && locals[row.doctype][row.name]) {
		Object.assign(locals[row.doctype][row.name], {
			conversion_factor: row.conversion_factor,
			max_issue_stock_qty: row.max_issue_stock_qty,
			max_issue_qty: row.max_issue_qty,
			actual_qty: row.actual_qty,
			qty: row.qty,
			projected_remaining_qty: row.projected_remaining_qty,
			projected_remaining_stock_qty: row.projected_remaining_stock_qty
		});
	}
}

function set_issue_dialog_row_actual_qty(row, actual_qty) {
	if (!row) {
		return;
	}

	row.actual_stock_qty = flt(actual_qty);
	row.actual_qty = row.actual_stock_qty / (flt(row.conversion_factor) || 1);
	update_issue_dialog_row_projected_remaining_qty(row);
	if (row.doctype && row.name && locals[row.doctype] && locals[row.doctype][row.name]) {
		locals[row.doctype][row.name].actual_stock_qty = row.actual_stock_qty;
		locals[row.doctype][row.name].actual_qty = row.actual_qty;
		locals[row.doctype][row.name].projected_remaining_qty = row.projected_remaining_qty;
		locals[row.doctype][row.name].projected_remaining_stock_qty = row.projected_remaining_stock_qty;
	}
}

function update_issue_dialog_row_projected_remaining_qty(row) {
	if (!row) {
		return;
	}

	row.projected_remaining_stock_qty = flt(row.actual_stock_qty) - flt(row.qty) * (flt(row.conversion_factor) || 1);
	row.projected_remaining_qty = row.projected_remaining_stock_qty / (flt(row.conversion_factor) || 1);
	if (row.doctype && row.name && locals[row.doctype] && locals[row.doctype][row.name]) {
		locals[row.doctype][row.name].projected_remaining_qty = row.projected_remaining_qty;
		locals[row.doctype][row.name].projected_remaining_stock_qty = row.projected_remaining_stock_qty;
	}
}


function get_issue_dialog_max_issue_stock_qty(remaining_stock_qty, conversion_factor) {
	remaining_stock_qty = flt(remaining_stock_qty);
	if (remaining_stock_qty <= 0) {
		return 0;
	}

	// Changing the display UOM must not increase the remaining stock quantity.
	return remaining_stock_qty;
}

function get_issue_dialog_grid_row_doc(grid, cdt, cdn) {
	if (cdn && grid.grid_rows_by_docname && grid.grid_rows_by_docname[cdn]) {
		return grid.grid_rows_by_docname[cdn].doc;
	}

	if (cdt && cdn && locals[cdt] && locals[cdt][cdn]) {
		return locals[cdt][cdn];
	}

	return null;
}

function get_material_request_issue_warehouse_query(frm, row) {
	return {
		query: "mes_integration.mes_integration.material_request.issue_warehouse_query",
		filters: {
			company: frm.doc.company,
			item_code: row && row.item_code ? row.item_code : ""
		}
	};
}

function get_validated_issue_and_push_rows(rows, is_transfer) {
	const issue_rows = [];
	for (const [index, row] of rows.entries()) {
		const qty = flt(row.qty);
		if (qty <= 0) {
			continue;
		}

		if (qty > flt(row.max_issue_qty || row.remaining_qty)) {
			frappe.msgprint({
				title: __("操作失败"),
				indicator: "red",
				message: __("第 {0} 行本次发料数量不能超过剩余数量", [index + 1])
			});
			return null;
		}

		if (!row.uom) {
			frappe.msgprint({
				title: __("操作失败"),
				indicator: "red",
				message: __("第 {0} 行缺少单位", [index + 1])
			});
			return null;
		}

		if (!row.s_warehouse) {
			frappe.msgprint({
				title: __("操作失败"),
				indicator: "red",
				message: __("第 {0} 行缺少发料仓", [index + 1])
			});
			return null;
		}

		if (is_transfer && !row.t_warehouse) {
			frappe.msgprint({
				title: __("操作失败"),
				indicator: "red",
				message: __("第 {0} 行缺少目标仓库", [index + 1])
			});
			return null;
		}

		issue_rows.push({
			material_request_item: row.material_request_item,
			item_code: row.item_code,
			qty: qty,
			uom: row.uom,
			s_warehouse: row.s_warehouse,
			t_warehouse: row.t_warehouse
		});
	}

	if (!issue_rows.length) {
		frappe.msgprint({
			title: __("提示"),
			indicator: "orange",
			message: __("没有可发料的明细")
		});
		return null;
	}

	return issue_rows;
}

function call_issue_and_push_to_dlm(frm, issue_rows) {
	frappe.call({
		method: "mes_integration.mes_integration.material_request.issue_and_push_to_dlm_from_dialog",
		args: {
			material_request_name: frm.doc.name,
			items: issue_rows
		},
		freeze: true,
		freeze_message: __("正在创建物料移动并推送至 DLM，请稍候..."),
		callback: function(r) {
			if (r.exc) {
				return;
			}

			const result = r.message || {};
			if (result.status === "success") {
				frappe.msgprint({
					title: __("成功"),
					indicator: "green",
					message: result.message
				});
			} else if (result.status === "partial") {
				frappe.msgprint({
					title: __("部分完成"),
					indicator: "orange",
					message: result.message
				});
			}

			frm.reload_doc();
		},
		error: function() {
			frappe.msgprint({
				title: __("操作失败"),
				indicator: "red",
				message: __("发料或推送过程中发生错误，请查看错误日志或联系管理员。")
			});
		}
	});
}

function get_material_request_item_remaining_qty(row) {
	const conversion_factor = flt(row.conversion_factor) || 1;
	const requested_stock_qty = flt(row.stock_qty) || flt(row.qty) * conversion_factor;
	const issued_stock_qty = flt(row.custom_transferred_qty || row.ordered_qty);
	return flt(Math.max(requested_stock_qty - issued_stock_qty, 0) / conversion_factor);
}

function get_material_request_item_issued_qty(row) {
	const conversion_factor = flt(row.conversion_factor) || 1;
	return flt(row.custom_transferred_qty || row.ordered_qty) / conversion_factor;
}

function get_default_issue_source_warehouse(frm, row) {
	if (frm.doc.material_request_type === "Material Transfer for Manufacture") {
		return row.from_warehouse || frm.doc.set_from_warehouse || "";
	}

	return row.warehouse || row.from_warehouse || frm.doc.set_warehouse || frm.doc.set_from_warehouse || "";
}

function get_default_issue_target_warehouse(frm, row) {
	if (frm.doc.material_request_type !== "Material Transfer for Manufacture") {
		return "";
	}

	return row.warehouse || frm.doc.set_warehouse || "";
}

function style_submit_and_issue_button(frm) {
	requestAnimationFrame(function() {
		const labels = [...new Set([
			"提交并发料", __("提交并发料"),
			"发料并推送至DLM", __("发料并推送至DLM")
		])];
		const selector = labels
			.map(function(label) {
				return `.page-actions button[data-label="${encodeURIComponent(label)}"]`;
			})
			.join(", ");

		$(frm.page.wrapper)
			.find(selector)
			.removeClass("btn-default btn-secondary btn-xs")
			.addClass("btn-primary btn-sm mes-submit-issue-button");
	});
}

function show_issue_stock_entry_prompt(frm) {

	if (!is_mes_material_request_integration_enabled(frm)) {
		return;
	}
	if (!can_create_issue_stock_entry(frm)) {
		return;
	}

	const dialog = new frappe.ui.Dialog({
		title: __("是否立即发料？"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "message",
				options: `<p class="text-muted">${__("物料需求已提交。是否立即创建物料移动并发料？")}</p>`
			}
		],
		primary_action_label: __("发料"),
		primary_action: function() {
			dialog.hide();
			open_issue_stock_entry(frm);
		}
	});

	dialog.show();
}

function can_create_issue_stock_entry(frm) {
	if (!is_mes_material_request_integration_enabled(frm) || !is_mes_material_request(frm)) {
		return false;
	}

	return can_create_material_movement(frm);
}

function can_create_material_movement(frm) {
	if (
		frm.doc.docstatus !== 1 ||
		frm.doc.status === "Stopped" ||
		!CUSTOM_ISSUE_MATERIAL_REQUEST_TYPES.includes(frm.doc.material_request_type)
	) {
		return false;
	}

	const precision = frappe.defaults.get_default("float_precision");
	return flt(frm.doc.per_ordered, precision) < 100;
}

function style_material_movement_button(frm) {
	requestAnimationFrame(function() {
		$(frm.page.wrapper)
			.find('.page-actions button[data-label="%E7%94%9F%E6%88%90%E7%89%A9%E6%96%99%E7%A7%BB%E5%8A%A8"]')
			.removeClass("btn-default btn-secondary btn-xs")
			.addClass("btn-primary btn-sm");
	});
}

function open_issue_stock_entry(frm) {
	frappe.model.open_mapped_doc({
		method: "mes_integration.mes_integration.material_request.make_issue_stock_entry",
		frm: frm
	});
}

function toggle_injection_molding_weight_fields(frm) {
	const grid = frm.fields_dict.items && frm.fields_dict.items.grid;

	if (!grid) {
		return;
	}

	ensure_material_request_item_grid_defaults(grid);

	if (frm.doc.material_request_type === INJECTION_MOLDING_PURPOSE) {
		apply_injection_molding_item_grid(grid);
	} else {
		restore_material_request_item_grid(grid);
		hide_injection_molding_weight_fields(grid);
	}

	rebuild_material_request_item_grid(grid);
}

function apply_injection_molding_material_issue_warehouse_labels(frm) {
	if (frm.doc.material_request_type !== INJECTION_MOLDING_PURPOSE) {
		return;
	}

	const grid = frm.fields_dict.items && frm.fields_dict.items.grid;

	if (grid) {
		grid.update_docfield_property("warehouse", "label", __("From Warehouse"));
	}

	frm.set_df_property("set_warehouse", "label", __("Set From Warehouse"));
}

function apply_injection_molding_item_grid(grid) {
	const injection_columns = [
		["item_code", 2],
		["schedule_date", 2],
		["qty", 1],
		["uom", 1],
		["custom_transferred_qty", 1],
		["warehouse", 3],
		["actual_qty", 2],
		["custom_new_material_weight", 2],
		["custom_recycled_material_weight", 2],
		["custom_material_request_item_detail_button", 1]
	];
	const injection_fieldnames = injection_columns.map(function(column) {
		return column[0];
	});

	grid.docfields.forEach(function(df) {
		if (!df.fieldname || !injection_fieldnames.includes(df.fieldname)) {
			return;
		}

		set_material_request_item_docfield_property(grid, df.fieldname, "hidden", 0);
		set_material_request_item_docfield_property(grid, df.fieldname, "in_list_view", 1);
	});

	grid.visible_columns = injection_columns
		.map(function(column) {
			const df = grid.docfields.find(function(docfield) {
				return docfield.fieldname === column[0] && !docfield.hidden;
			});

			if (!df) {
				return null;
			}

			set_material_request_item_docfield_property(grid, df.fieldname, "columns", column[1]);
			set_material_request_item_docfield_property(grid, df.fieldname, "colsize", column[1]);

			return [df, column[1]];
		})
		.filter(Boolean);
	grid.user_defined_columns = [];
}

function restore_material_request_item_grid(grid) {
	grid.docfields.forEach(function(df) {
		const defaults = grid.__mes_default_docfield_properties[df.fieldname];

		if (!defaults) {
			return;
		}

		set_material_request_item_docfield_property(grid, df.fieldname, "hidden", defaults.hidden);
		set_material_request_item_docfield_property(grid, df.fieldname, "in_list_view", defaults.in_list_view);
		set_material_request_item_docfield_property(grid, df.fieldname, "columns", defaults.columns);
		set_material_request_item_docfield_property(grid, df.fieldname, "colsize", defaults.colsize);
	});

	grid.visible_columns = [];
	grid.user_defined_columns = [];
}

function hide_injection_molding_weight_fields(grid) {
	INJECTION_MOLDING_WEIGHT_FIELDS.forEach(function(fieldname) {
		set_material_request_item_docfield_property(grid, fieldname, "hidden", 1);
		set_material_request_item_docfield_property(grid, fieldname, "in_list_view", 0);
		set_material_request_item_docfield_property(grid, fieldname, "columns", undefined);
		set_material_request_item_docfield_property(grid, fieldname, "colsize", undefined);
	});
}

function ensure_material_request_item_grid_defaults(grid) {
	if (grid.__mes_default_docfield_properties) {
		return;
	}

	grid.__mes_default_docfield_properties = {};

	grid.docfields.forEach(function(df) {
		if (!df.fieldname) {
			return;
		}

		grid.__mes_default_docfield_properties[df.fieldname] = {
			hidden: df.hidden,
			in_list_view: df.in_list_view,
			columns: df.columns,
			colsize: df.colsize
		};
	});
}

function set_material_request_item_docfield_property(grid, fieldname, property, value) {
	const grid_docfield = grid.docfields.find(function(df) {
		return df.fieldname === fieldname;
	});

	if (grid_docfield) {
		grid_docfield[property] = value;
	}

	const meta_docfield = frappe.meta.get_docfield("Material Request Item", fieldname);

	if (meta_docfield) {
		meta_docfield[property] = value;
	}

	(grid.grid_rows || []).forEach(function(row) {
		const row_docfield = row.docfields && row.docfields.find(function(df) {
			return df.fieldname === fieldname;
		});

		if (row_docfield) {
			row_docfield[property] = value;
		}
	});
}

function rebuild_material_request_item_grid(grid) {
	if (grid.grid_rows) {
		grid.grid_rows.forEach(function(row) {
			if (row && row.get_open_form && row.get_open_form()) {
				row.hide_form();
			}
		});
	}

	grid.grid_rows = [];
	grid.grid_rows_by_docname = {};
	grid.wrapper.find(".grid-body .rows .grid-row").remove();
	grid.refresh();
}
