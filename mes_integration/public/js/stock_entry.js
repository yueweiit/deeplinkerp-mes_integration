(function() {
"use strict";

frappe.ui.form.on("Stock Entry", {
	onload: function(frm) {
		load_mes_integration_enabled(frm).then(function() {
			refresh_mes_stock_entry_ui(frm);
		});
	},

	refresh: function(frm) {
		load_mes_integration_enabled(frm).then(function() {
			refresh_mes_stock_entry_ui(frm);
		});
	},

	company: function(frm) {
		frm._mes_integration_company = null;
		frm._mes_integration_enabled = false;
		frm._mes_manufacturing_warehouse = null;
		frm._mes_integration_enabled_promise = null;
		load_mes_integration_enabled(frm).then(function() {
			refresh_mes_stock_entry_ui(frm);
		});
	},

	stock_entry_type: function(frm) {
		refresh_mes_receipt_quantity_labels(frm);
		if (!is_mes_integration_enabled(frm)) {
			return;
		}
		clear_manual_mes_receipt_stock_entry_no(frm);
		sync_mes_stock_entry_fields(frm);
		load_mes_stock_entry_source(frm).then(function() {
			refresh_mes_stock_entry_actions(frm);
		});
	},

	validate: function(frm) {
		if (is_mes_integration_enabled(frm)) {
			sync_mes_stock_entry_fields(frm, { immediate: true });
		}
	},

	items_add: function(frm, cdt, cdn) {
		if (is_mes_integration_enabled(frm)) {
			set_manufacturing_warehouse(frm, cdt, cdn);
			load_mes_stock_entry_source(frm).then(function() {
				refresh_mes_stock_entry_actions(frm);
			});
		}
	},

	on_submit: function(frm) {
		if (frm._mes_prompt_after_save_submit || !is_mes_integration_enabled(frm)) {
			return;
		}

		show_push_to_dlm_prompt(frm);
	},

	after_save: function(frm) {
		if (is_mes_integration_enabled(frm) && frm.doc.custom_mes_status === "Pushed") {
			frappe.call({
				method: "mes_integration.mes_integration.stock_entry.reset_mes_status",
				args: {
					stock_entry_name: frm.doc.name
				},
				freeze: false,
				callback: function(r) {
					if (r.message && r.message.status === "success") {
						frm.reload_doc();
					}
				}
			});
		}
	}
});


function load_mes_integration_enabled(frm) {
	if (!frm || !frm.doc || !frm.doc.company) {
		if (frm) {
			frm._mes_integration_company = null;
			frm._mes_integration_enabled = false;
			frm._mes_manufacturing_warehouse = null;
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
	frm._mes_manufacturing_warehouse = null;

	const enabledPromise = Promise.resolve(frappe.db
		.get_value("Company", company, [
			"custom_enable_mes_integration",
			"custom_mes_manufacturing_warehouse"
		]))
		.then(function(r) {
			const value = r && r.message ? r.message.custom_enable_mes_integration : 0;
			const manufacturingWarehouse = r && r.message
				? r.message.custom_mes_manufacturing_warehouse || null
				: null;
			const enabled = cint(value) === 1;
			if (frm.doc && frm.doc.company === company && frm._mes_integration_company === company) {
				frm._mes_integration_enabled = enabled;
				frm._mes_manufacturing_warehouse = manufacturingWarehouse;
			}
			return enabled;
		})
		.catch(function() {
			if (frm.doc && frm.doc.company === company && frm._mes_integration_company === company) {
				frm._mes_integration_enabled = false;
				frm._mes_manufacturing_warehouse = null;
			}
			return false;
		})
		.finally(function() {
			if (frm._mes_integration_enabled_promise === enabledPromise) {
				frm._mes_integration_enabled_promise = null;
			}
		});

	frm._mes_integration_enabled_promise = enabledPromise;
	return enabledPromise;
}

function is_mes_integration_enabled(frm) {
	return Boolean(frm && frm._mes_integration_enabled);
}

function refresh_mes_stock_entry_ui(frm) {
	refresh_mes_receipt_quantity_labels(frm);
	if (!is_mes_integration_enabled(frm)) {
		frm.remove_custom_button(__("保存并提交"));
		frm.remove_custom_button(__("推送至DLM"));
		frm.remove_custom_button(__("推送至MES"), __("MES操作"));
		remove_mes_status_badge(frm);
		return;
	}

	remember_mes_receipt_stock_entry_no(frm);
	show_mes_receipt_stock_entry_no(frm);
	sync_mes_stock_entry_fields(frm);
	frm.remove_custom_button(__("推送至DLM"));
	remove_mes_status_badge(frm);
	load_mes_stock_entry_source(frm).then(function() {
		refresh_mes_stock_entry_actions(frm);
	});
}

function refresh_mes_stock_entry_actions(frm) {
	add_stock_entry_save_and_submit_button(frm);
	add_push_to_mes_button(frm);
	schedule_mes_status_display(frm);
}

const MES_RECEIPT_STOCK_ENTRY_TYPES = [
	"Semi Finished Goods Receipt",
	"Finished Goods Receipt"
];
const STANDARD_MATERIAL_RECEIPT = "Material Receipt";
const MES_DLM_STOCK_ENTRY_TYPES = [
	"Material Issue",
	"Material Transfer for Manufacture",
	"Injection Molding Issuance"
];

function is_mes_receipt_stock_entry(frm) {
	return frm && frm.doc && MES_RECEIPT_STOCK_ENTRY_TYPES.includes(frm.doc.stock_entry_type);
}

function is_mes_receipt_quantity_stock_entry(frm) {
	if (!frm || !frm.doc) {
		return false;
	}

	return (
		MES_RECEIPT_STOCK_ENTRY_TYPES.includes(frm.doc.stock_entry_type) ||
		(
			frm.doc.stock_entry_type === STANDARD_MATERIAL_RECEIPT &&
			cint(frm.doc.custom_mes_receipt) === 1
		)
	);
}

function refresh_mes_receipt_quantity_labels(frm) {
	const grid = frm && frm.fields_dict && frm.fields_dict.items
		? frm.fields_dict.items.grid
		: null;
	if (!grid) {
		return;
	}

	if (!frm._mes_original_stock_entry_quantity_labels) {
		const transferQty = grid.docfields.find(function(df) {
			return df.fieldname === "transfer_qty";
		});
		const actualQty = grid.docfields.find(function(df) {
			return df.fieldname === "actual_qty";
		});
		frm._mes_original_stock_entry_quantity_labels = {
			transfer_qty: transferQty && transferQty.label,
			actual_qty: actualQty && actualQty.label
		};
	}

	const originalLabels = frm._mes_original_stock_entry_quantity_labels;
	const useReceiptLabels = (
		is_mes_integration_enabled(frm) && is_mes_receipt_quantity_stock_entry(frm)
	);
	grid.update_docfield_property(
		"transfer_qty",
		"label",
		useReceiptLabels ? __("入库数量（库存单位）") : originalLabels.transfer_qty
	);
	grid.update_docfield_property(
		"actual_qty",
		"label",
		useReceiptLabels ? __("目标仓当前库存") : originalLabels.actual_qty
	);
}

function is_dlm_issue_stock_entry(frm) {
	return frm && frm.doc && MES_DLM_STOCK_ENTRY_TYPES.includes(frm.doc.stock_entry_type);
}

function get_stock_entry_material_request_names(frm) {
	return [...new Set((frm && frm.doc && frm.doc.items || [])
		.map(function(row) {
			return row && row.material_request;
		})
		.filter(Boolean))];
}

function load_mes_stock_entry_source(frm) {
	const materialRequestNames = get_stock_entry_material_request_names(frm);
	const cacheKey = materialRequestNames.join("\u0001");

	if (
		frm &&
		frm._mes_stock_entry_source_cache_key === cacheKey &&
		frm._mes_stock_entry_source_loaded
	) {
		return Promise.resolve(Boolean(frm._mes_source_stock_entry));
	}

	if (!frm) {
		return Promise.resolve(false);
	}

	frm._mes_stock_entry_source_cache_key = cacheKey;
	frm._mes_stock_entry_source_loaded = false;
	frm._mes_source_stock_entry = false;

	if (!materialRequestNames.length) {
		frm._mes_stock_entry_source_loaded = true;
		return Promise.resolve(false);
	}

	return frappe.db.get_list("Material Request", {
		filters: {
			name: ["in", materialRequestNames]
		},
		fields: ["name", "custom_request_source"],
		limit_page_length: materialRequestNames.length
	}).then(function(rows) {
		const materialRequests = {};
		(rows || []).forEach(function(row) {
			materialRequests[row.name] = row;
		});

		frm._mes_source_stock_entry = materialRequestNames.some(function(name) {
			const source = materialRequests[name] && materialRequests[name].custom_request_source;
			return source ? source === "MES" : String(name).startsWith("MAT-MR-MES-");
		});
		frm._mes_stock_entry_source_loaded = true;
		return frm._mes_source_stock_entry;
	}).catch(function() {
		frm._mes_source_stock_entry = false;
		frm._mes_stock_entry_source_loaded = true;
		return false;
	});
}

function is_mes_source_stock_entry(frm) {
	return Boolean(frm && frm._mes_stock_entry_source_loaded && frm._mes_source_stock_entry);
}

function can_push_stock_entry_to_dlm(frm) {

	if (!is_mes_integration_enabled(frm)) {
		return false;
	}
	if (!is_mes_source_stock_entry(frm)) {
		return false;
	}
	return (
		frm &&
		frm.doc &&
		frm.doc.docstatus === 1 &&
		is_dlm_issue_stock_entry(frm) &&
		frm.doc.custom_mes_status !== "Pushed"
	);
}

function add_stock_entry_save_and_submit_button(frm) {

	if (!is_mes_integration_enabled(frm)) {
		return;
	}
	frm.remove_custom_button(__("保存并提交"));
	frm.remove_custom_button(__("推送至DLM"));

	if (!frm || !frm.doc || frm.doc.docstatus !== 0 || has_stock_entry_submit_button(frm)) {
		return;
	}

	if (can_save_submit_and_push_stock_entry_to_dlm(frm)) {
		frm._mes_save_submit_button = frm.add_custom_button(__("推送至DLM"), function() {
			save_submit_and_push_stock_entry_to_dlm(frm);
		});
		apply_stock_entry_save_and_submit_button_style(frm);
		return;
	}

	frm._mes_save_submit_button = frm.add_custom_button(__("保存并提交"), function() {
		save_and_submit_stock_entry(frm);
	});
	apply_stock_entry_save_and_submit_button_style(frm);
}

function has_stock_entry_submit_button(frm) {
	const labels = [...new Set(["Submit", __("Submit"), "提交", __("提交")])];
	return labels.some(function(label) {
		return $(frm.page.wrapper)
			.find(`.page-actions button[data-label="${encodeURIComponent(label)}"]`)
			.length > 0;
	});
}

function save_and_submit_stock_entry(frm) {
	frappe.confirm(
		__("确认保存并提交此物料移动？"),
		function() {
			frm._mes_prompt_after_save_submit = true;
			frm.save("Submit")
				.then(function() {
					frm._mes_prompt_after_save_submit = false;
					setTimeout(function() {
						show_push_to_dlm_prompt(frm);
					}, 300);
				})
				.catch(function() {
					frm._mes_prompt_after_save_submit = false;
				});
		}
	);
}

function can_save_submit_and_push_stock_entry_to_dlm(frm) {

	if (!is_mes_integration_enabled(frm)) {
		return false;
	}
	if (!is_mes_source_stock_entry(frm)) {
		return false;
	}
	return (
		frm &&
		frm.doc &&
		frm.doc.docstatus === 0 &&
		["Material Issue", "Injection Molding Issuance"].includes(frm.doc.stock_entry_type) &&
		frm.doc.custom_mes_status !== "Pushed"
	);
}

function save_submit_and_push_stock_entry_to_dlm(frm) {
	frappe.confirm(
		__("确认保存、提交并推送此物料移动至 DLM？"),
		function() {
			frm._mes_prompt_after_save_submit = true;
			frm.save("Submit")
				.then(function() {
					frm._mes_prompt_after_save_submit = false;
					push_stock_entry_to_mes_after_save(frm);
				})
				.catch(function() {
					frm._mes_prompt_after_save_submit = false;
				});
		}
	);
}


function push_stock_entry_to_mes_after_save(frm) {
	wait_for_saved_stock_entry_name(frm)
		.then(function(stock_entry_name) {
			push_stock_entry_to_mes(frm, stock_entry_name);
		})
		.catch(function() {
			frappe.msgprint({
				title: __("提示"),
				indicator: "orange",
				message: __("物料移动已提交，但尚未获取正式单号。请刷新后再推送至 DLM。")
			});
		});
}

function wait_for_saved_stock_entry_name(frm, attempt) {
	attempt = attempt || 0;

	return new Promise(function(resolve, reject) {
		const stock_entry_name = get_saved_stock_entry_name(frm);

		if (stock_entry_name) {
			resolve(stock_entry_name);
			return;
		}

		if (attempt >= 10) {
			reject();
			return;
		}

		setTimeout(function() {
			wait_for_saved_stock_entry_name(frm, attempt + 1).then(resolve).catch(reject);
		}, 300);
	});
}

function get_saved_stock_entry_name(frm) {
	if (!frm || !frm.doc || !frm.doc.name || frm.is_new()) {
		return null;
	}

	if (String(frm.doc.name).startsWith("new-")) {
		return null;
	}

	return frm.doc.name;
}

function apply_stock_entry_save_and_submit_button_style(frm) {
	requestAnimationFrame(function() {
		const labels = [...new Set(["保存并提交", __("保存并提交"), "推送至DLM", __("推送至DLM")])];
		const selector = labels
			.map(function(label) {
				return `.page-actions button[data-label="${encodeURIComponent(label)}"]`;
			})
			.join(", ");

		$(frm.page.wrapper)
			.find(selector)
			.removeClass("btn-default btn-secondary btn-xs")
			.addClass("btn-primary btn-sm mes-save-submit-button");
	});
}

function remember_mes_receipt_stock_entry_no(frm) {
	if (!is_mes_receipt_stock_entry(frm) || frm.is_new() || !frm.doc.custom_stock_entry_no) {
		return;
	}

	frm._mes_receipt_stock_entry_no = frm.doc.custom_stock_entry_no;
}

function clear_manual_mes_receipt_stock_entry_no(frm) {
	if (!is_mes_receipt_stock_entry(frm) || !frm.is_new()) {
		return;
	}

	frm._mes_receipt_stock_entry_no = "";
	if (frm.doc.custom_stock_entry_no) {
		frappe.model.set_value(frm.doctype, frm.docname, "custom_stock_entry_no", "");
	}
}

function restore_mes_receipt_stock_entry_no(frm, immediate) {
	if (!is_mes_receipt_stock_entry(frm) || !frm._mes_receipt_stock_entry_no) {
		return;
	}

	const restore = function() {
		if (!frm.doc.custom_stock_entry_no) {
			frappe.model.set_value(
				frm.doctype,
				frm.docname,
				"custom_stock_entry_no",
				frm._mes_receipt_stock_entry_no
			);
		}
	};

	if (immediate) {
		restore();
		return;
	}

	setTimeout(restore, 100);
}

function show_mes_receipt_stock_entry_no(frm) {
	if (!is_mes_receipt_stock_entry(frm)) {
		return;
	}

	frm.toggle_display("custom_stock_entry_no", true);
	frm.set_df_property("custom_stock_entry_no", "hidden", 0);
	frm.refresh_field("custom_stock_entry_no");
}

frappe.ui.form.on("Stock Entry Detail", {
	material_request: function(frm, cdt, cdn) {
		set_manufacturing_warehouse(frm, cdt, cdn);
		sync_mes_stock_entry_fields(frm);
		load_mes_stock_entry_source(frm).then(function() {
			refresh_mes_stock_entry_actions(frm);
		});
	},

	material_request_item: function(frm, cdt, cdn) {
		set_manufacturing_warehouse(frm, cdt, cdn);
		sync_mes_stock_entry_fields(frm);
		load_mes_stock_entry_source(frm).then(function() {
			refresh_mes_stock_entry_actions(frm);
		});
	},

	custom_material_request_no: function(frm) {
		sync_material_request_no(frm);
	}
});

function sync_mes_stock_entry_fields(frm, options) {

	if (!is_mes_integration_enabled(frm)) {
		return;
	}
	options = options || {};
	clear_manual_mes_receipt_stock_entry_no(frm);
	remember_mes_receipt_stock_entry_no(frm);
	show_mes_receipt_stock_entry_no(frm);
	restore_mes_receipt_stock_entry_no(frm, options.immediate);
	sync_material_request_no(frm);
	sync_stock_entry_no_from_material_request(frm);
	schedule_all_manufacturing_warehouses(frm);
}

function sync_material_request_no(frm) {
	if (is_mes_receipt_stock_entry(frm)) {
		return;
	}

	const firstRow = get_first_stock_entry_item(frm);
	const materialRequestNo = firstRow ? firstRow.custom_material_request_no || "" : "";
	set_form_value_if_changed(frm, "custom_material_request_no", materialRequestNo);
}

function sync_stock_entry_no_from_material_request(frm) {
	if (is_mes_receipt_stock_entry(frm)) {
		return;
	}

	const firstRow = get_first_stock_entry_item(frm);
	const materialRequest = firstRow && firstRow.material_request;

	if (!materialRequest) {
		set_form_value_if_changed(frm, "custom_stock_entry_no", "");
		return;
	}

	frappe.db.get_value("Material Request", materialRequest, "custom_stock_entry_no").then(function(r) {
		const stockEntryNo = r && r.message ? r.message.custom_stock_entry_no || "" : "";
		set_form_value_if_changed(frm, "custom_stock_entry_no", stockEntryNo);
	});
}

function get_first_stock_entry_item(frm) {
	return frm.doc.items && frm.doc.items.length ? frm.doc.items[0] : null;
}

function set_form_value_if_changed(frm, fieldname, value) {
	if (frm.doc[fieldname] === value) {
		return;
	}

	frm.set_value(fieldname, value);
}

function set_manufacturing_warehouse(frm, cdt, cdn) {

	if (!is_mes_integration_enabled(frm)) {
		return;
	}
	const row = locals[cdt] && locals[cdt][cdn];
	const targetWarehouse = frm._mes_manufacturing_warehouse;

	if (!row || !targetWarehouse || frm.doc.purpose !== "Material Transfer for Manufacture") {
		return;
	}

	if ((row.material_request || row.material_request_item) && row.t_warehouse !== targetWarehouse) {
		frappe.model.set_value(cdt, cdn, "t_warehouse", targetWarehouse);
	}
}

function schedule_all_manufacturing_warehouses(frm) {
	if (frm.doc.purpose !== "Material Transfer for Manufacture") {
		return;
	}

	setTimeout(function() {
		set_all_manufacturing_warehouses(frm);
	}, 300);
}

function set_all_manufacturing_warehouses(frm) {
	const targetWarehouse = frm._mes_manufacturing_warehouse;
	if (!targetWarehouse || frm.doc.purpose !== "Material Transfer for Manufacture") {
		return;
	}

	(frm.doc.items || []).forEach(function(row) {
		if ((row.material_request || row.material_request_item) && row.t_warehouse !== targetWarehouse) {
			frappe.model.set_value(row.doctype, row.name, "t_warehouse", targetWarehouse);
		}
	});
}

$(document).ready(function() {
	remove_mes_status_badge(window.cur_frm);
});

$(document).on('page-change', function() {
	remove_mes_status_badge(window.cur_frm);
	if (window.cur_frm && window.cur_frm.doctype === 'Stock Entry') {
		schedule_mes_status_display(window.cur_frm);
	}
});

function schedule_mes_status_display(frm) {
	[0, 150, 350, 700, 1200].forEach(function(delay) {
		setTimeout(function() {
			display_mes_status(frm, 0);
		}, delay);
	});
}

function display_mes_status(frm, attempt) {

	if (!is_mes_integration_enabled(frm)) {
		return;
	}
	attempt = attempt || 0;
	var currentUrl = window.location.pathname;

	if (!currentUrl || currentUrl === '/desk/stock-entry') {
		remove_mes_status_badge(frm);
		return;
	}

	if (!frm || frm.doctype !== 'Stock Entry' || !frm.doc || !frm.doc.name || !frm.page) {
		retry_mes_status_display(frm, attempt);
		return;
	}

	if (!is_dlm_issue_stock_entry(frm)) {
		remove_mes_status_badge(frm);
		return;
	}

	if (!is_mes_source_stock_entry(frm)) {
		remove_mes_status_badge(frm);
		return;
	}

	var $titleArea = frm.page.$title_area;
	if (!$titleArea || !$titleArea.length) {
		retry_mes_status_display(frm, attempt);
		return;
	}

	var mesStatus = frm.doc.custom_mes_status || "Unpushed";
	var statusHtml = mesStatus === "Pushed"
		? '<span class="mes-status-badge indicator-pill no-indicator-dot whitespace-nowrap blue"><span>' + get_dlm_status_label(true) + '</span></span>'
		: '<span class="mes-status-badge indicator-pill no-indicator-dot whitespace-nowrap red"><span>' + get_dlm_status_label(false) + '</span></span>';

	remove_mes_status_badge(frm);

	var $statusIndicator = frm.page.indicator && frm.page.indicator.length
		? frm.page.indicator
		: $titleArea.find('.indicator-pill').not('.mes-status-badge').last();

	if ($statusIndicator.length) {
		$statusIndicator.after(statusHtml);
	} else {
		$titleArea.append(statusHtml);
	}
}

function remove_mes_status_badge(frm) {
	if (frm && frm.page && frm.page.wrapper) {
		frm.page.wrapper.find('.mes-status-badge').remove();
		return;
	}

	$('.mes-status-badge').remove();
}

function retry_mes_status_display(frm, attempt) {
	if (attempt >= 8) {
		return;
	}

	setTimeout(function() {
		display_mes_status(frm, attempt + 1);
	}, 150);
}

function get_dlm_status_label(is_pushed) {
	const lang = (frappe.boot && frappe.boot.lang) || frappe.lang || '';
	if (lang.toLowerCase().startsWith('es')) {
		return is_pushed ? 'DLM: Enviado' : 'DLM: No enviado';
	}

	return is_pushed ? __('DLM: 已推送') : __('DLM: 未推送');
}

function add_push_to_mes_button(frm) {

	if (!is_mes_integration_enabled(frm)) {
		return;
	}
	frm.remove_custom_button(__("推送至MES"), __("MES操作"));

	if (!frm || !frm.doc || frm.doc.docstatus !== 1) {
		return;
	}

	frm.remove_custom_button(__("推送至DLM"));

	if (!can_push_stock_entry_to_dlm(frm)) {
		return;
	}

	if (frm.doc.docstatus === 1) {
		frm._mes_push_button = frm.add_custom_button(__("推送至DLM"), function() {
			push_stock_entry_to_mes(frm);
		});

		apply_mes_button_style(frm);
	}
}

function show_push_to_dlm_prompt(frm) {

	if (!is_mes_integration_enabled(frm)) {
		return;
	}
	if (!can_push_stock_entry_to_dlm(frm)) {
		return;
	}

	const dialog = new frappe.ui.Dialog({
		title: __("是否立即推送至DLM？"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "message",
				options: `<p class="text-muted">${__("物料移动已提交。是否立即推送发料结果至 DLM？")}</p>`
			}
		],
		primary_action_label: __("推送至DLM"),
		primary_action: function() {
			dialog.hide();
			push_stock_entry_to_mes(frm);
		}
	});

	dialog.show();
}

function apply_mes_button_style(frm) {
	requestAnimationFrame(function() {
		const $btn = frm && frm._mes_push_button ? frm._mes_push_button : $('[data-label="' + __("推送至DLM") + '"]');
		$btn.addClass('mes-push-dlm-button btn-primary')
			.removeClass('btn-default btn-light');
	});
}

function push_stock_entry_to_mes(frm, stock_entry_name) {
	stock_entry_name = stock_entry_name || get_saved_stock_entry_name(frm);

	if (!stock_entry_name) {
		frappe.msgprint({
			title: __("提示"),
			indicator: "orange",
			message: __("请先保存物料移动并获取正式单号后再推送至 DLM。")
		});
		return;
	}

	if (frm._mes_push_in_progress) {
		return;
	}

	frm._mes_push_in_progress = true;
	frm._mes_push_error_shown = false;

	const $btn = frm._mes_push_button || $('.mes-push-dlm-button');
	$btn.prop("disabled", true);

	frappe.call({
		method: "mes_integration.mes_integration.stock_entry.push_to_mes",
		args: {
			stock_entry_name: stock_entry_name
		},
		freeze: true,
		freeze_message: __("正在推送至 DLM，请稍候..."),
		callback: function(r) {
			$btn.prop("disabled", false);
			frm._mes_push_in_progress = false;

			if (r.exc) {
				show_mes_push_error(frm, r);
				return;
			}

			if (r.message && r.message.status === "success") {
				frappe.msgprint({
					title: __("成功"),
					indicator: "green",
					message: r.message.message
				});

				frm.reload_doc();
				frm.refresh_fields();
				return;
			}

			frappe.msgprint({
				title: __("提示"),
				indicator: "orange",
				message: __("DLM 推送已返回，但没有收到成功结果，请查看 Error Log 或浏览器控制台。")
			});
		},
		error: function(r) {
			$btn.prop("disabled", false);
			frm._mes_push_in_progress = false;
			show_mes_push_error(frm, r);
		}
	});
}

function show_mes_push_error(frm, r) {
	if (frm && frm._mes_push_error_shown) {
		return;
	}

	if (frm) {
		frm._mes_push_error_shown = true;
	}

	let message = __("推送至 DLM 失败，请查看 Error Log。");

	if (r && r._server_messages) {
		try {
			const serverMessages = JSON.parse(r._server_messages);
			const messages = serverMessages.map(function(item) {
				return JSON.parse(item).message;
			}).filter(Boolean);

			message = dedupe_mes_push_messages(messages).join("<br>");
		} catch (e) {
			message = r._server_messages;
		}
	} else if (r && r.message) {
		message = r.message;
	} else if (r && r.responseJSON && r.responseJSON.message) {
		message = r.responseJSON.message;
	}

	frappe.msgprint({
		title: __("错误"),
		indicator: "red",
		message: message
	});
}

function dedupe_mes_push_messages(messages) {
	return [...new Set(messages.map(function(message) {
		return String(message || "").trim();
	}).filter(Boolean))];
}

const MES_MATERIAL_REQUEST_TYPES_FOR_STOCK_ENTRY = [
	"Material Transfer",
	"Material Issue",
	"Customer Provided",
	"Material Transfer for Manufacture",
	"Injection Molding Issuance"
];

frappe.ui.form.on("Stock Entry", {
	refresh: function(frm) {
		replace_material_request_button(frm);
	}
});

function replace_material_request_button(frm) {
	if (frm.doc.docstatus !== 0 || frm.doc.subcontracting_inward_order) {
		return;
	}

	frm.remove_custom_button(__("Material Request"), __("Get Items From"));
	frm.add_custom_button(__("Material Request"), function() {
		open_material_request_mapper(frm);
	}, __("Get Items From"));
}

function open_material_request_mapper(frm) {
	const depends_on_condition = "eval:doc.material_request_type==='Customer Provided'";
	const d = erpnext.utils.map_current_doc({
		method: "mes_integration.mes_integration.material_request.make_stock_entry_from_material_request",
		source_doctype: "Material Request",
		target: frm,
		date_field: "schedule_date",
		setters: [
			{
				fieldtype: "Select",
				label: __("Purpose"),
				options: MES_MATERIAL_REQUEST_TYPES_FOR_STOCK_ENTRY.join("\n"),
				fieldname: "material_request_type",
				default: frm.doc.purpose === "Material Issue" ? "Material Issue" : "Material Transfer",
				mandatory: 1,
				change() {
					if (this.value === "Customer Provided") {
						d.dialog.get_field("customer").set_focus();
					}
				},
			},
			{
				fieldtype: "Link",
				label: __("Customer"),
				options: "Customer",
				fieldname: "customer",
				depends_on: depends_on_condition,
				mandatory_depends_on: depends_on_condition,
			},
		],
		get_query_filters: {
			docstatus: 1,
			material_request_type: ["in", MES_MATERIAL_REQUEST_TYPES_FOR_STOCK_ENTRY],
			status: ["not in", ["Transferred", "Issued", "Cancelled", "Stopped"]],
		},
	});
}

})();
