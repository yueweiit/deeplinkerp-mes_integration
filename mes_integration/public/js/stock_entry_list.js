frappe.listview_settings["Stock Entry"] = frappe.listview_settings["Stock Entry"] || {};

const MES_DLM_STOCK_ENTRY_TYPES = [
	"Material Issue",
	"Material Transfer for Manufacture",
	"Injection Molding Issuance",
];

frappe.listview_settings["Stock Entry"].add_fields = [
	...(frappe.listview_settings["Stock Entry"].add_fields || []),
	"`tabStock Entry`.`stock_entry_type`",
	"`tabStock Entry`.`custom_mes_status`",
];

frappe.listview_settings["Stock Entry"].formatters = {
	...(frappe.listview_settings["Stock Entry"].formatters || {}),
	stock_entry_type: function(value) {
		if (!value) return "";
		return `<a class="filterable ellipsis" data-filter="stock_entry_type,=,${frappe.utils.escape_html(value)}">${frappe.utils.escape_html(__(value))}</a>`;
	},
	custom_mes_status: function(value, df, doc) {
		if (!doc || !MES_DLM_STOCK_ENTRY_TYPES.includes(doc.stock_entry_type)) {
			return "";
		}

		const status = value || "Unpushed";
		const color = status === "Pushed" ? "green" : "red";

		return `<span class="indicator-pill ${color} no-indicator-dot">${__(status)}</span>`;
	},
};

// The compact list is supplied by deeplinkerp_branding on sites that install it.
// Keep the native list (and its MES issue formatter above) on other sites.
(function (root) {
	const engine = root.DeepLinkERPCompactList;
	if (!engine) return;
	const pending = "mes_pending_receipt";
	const submitted = "mes_submitted_receipt";
	const receiptCallbackField = "dlp_mes_receipt_callback";
	const receiptCallbackLabels = {Success:"已回传",Failed:"回传失败",Pending:"回传待确认","No Record":"无回传记录","Awaiting submission":"待提交",Loading:"加载中","Load failed":"加载失败"};
	const views = [pending, submitted, "all"];
	const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"})[char]);
	const labels = {[pending]:"待核对入库", [submitted]:"已入库", all:"全部"};
	const columns = [
		["name","ERP入库单号",230], ["stock_entry_type","移动类型",175],
		["docstatus","状态",100], ["custom_stock_entry_no","MES入库编号",230],
		["custom_sales_order","销售订单",190], ["posting_date","过账日期",120],
		["company","公司",170], ["custom_mes_status","MES发料回传",130],
		[receiptCallbackField,"MES入库回传",140],
	].map(([fieldname,label,width]) => ({fieldname,label,width}));
	const defaultColumns = columns.map(col => col.fieldname);
	const canShowReceipts = c => ["custom_mes_receipt","docstatus"].every(field => c.allowed.has(field));
	const visibleColumn = (c,field) => (c.seView === "all" || field !== "custom_mes_status") && (field !== receiptCallbackField || canShowReceipts(c));
	function viewQuery(args, c) {
		if (c.seView === "all") return args;
		return {...args, filters:[...(args.filters || []),
			["Stock Entry","custom_mes_receipt","=",1],
			["Stock Entry","docstatus","=",c.seView === pending ? 0 : 1]]};
	}
	function renderCounts(c) {
		c.$seViews?.find("[data-se-count]").each(function () {
			const count = c.seCounts?.[this.dataset.seCount];
			c.root.$(this).text(typeof count === "number" ? count.toLocaleString() : count ?? "…");
		});
	}
	function refreshCounts(c) {
		if (!c.root.frappe.call || !c.originals?.get_args) return;
		let base;
		try { base = grid.buildQuery(c.originals.get_args.call(c.list), c.quick, c.allowed); }
		catch (_) { return; }
		const version = ++c.seCountVersion;
		c.seCounts = {[pending]:null,[submitted]:null,all:null};
		if (!canShowReceipts(c)) c.seCounts[pending] = c.seCounts[submitted] = "—";
		renderCounts(c);
		for (const view of views) {
			if (view === c.seView || (view !== "all" && !canShowReceipts(c))) continue;
			const args = grid.buildRequests(viewQuery(base, {...c,seView:view}), [], c.allowed).count;
			Promise.resolve().then(() => c.root.frappe.call({method:"frappe.desk.reportview.get_count",args,silent:true})).then(response => {
				if (version !== c.seCountVersion) return;
				c.seCounts[view] = Number(response.message || 0);
				renderCounts(c);
			}).catch(() => {
				if (version !== c.seCountVersion) return;
				c.seCounts[view] = "—";
				renderCounts(c);
			});
		}
	}
	function mountViews(c) {
		if (!c.$filters) return;
		const label = value => escape(c.translate(value));
		c.$seViews = c.root.$(`<div class="dlp-se-view-strip"><div class="btn-group btn-group-sm" role="group" aria-label="${label("物料移动视图")}">
			${views.map(view => `<button type="button" class="btn btn-sm ${view === c.seView ? "btn-primary" : "btn-default"}" data-se-view="${view}" aria-pressed="${view === c.seView}" ${view !== "all" && !canShowReceipts(c) ? "disabled" : ""}>${label(labels[view])} <span class="dlp-se-view-count" data-se-count="${view}">…</span></button>`).join("")}
		</div></div>`).insertBefore(c.$filters);
		c.$seViews.on("click.dlpSEViews", "[data-se-view]", event => {
			const view = event.currentTarget.dataset.seView;
			if (!views.includes(view) || view === c.seView || (view !== "all" && !canShowReceipts(c))) return;
			c.list.clear_checked_items?.();
			c.seCallbackVersion++;
			c.seView = view;
			c.$seViews.find("[data-se-view]").each(function () {
				const active = this.dataset.seView === view;
				c.root.$(this).attr("aria-pressed", String(active)).toggleClass("btn-primary", active).toggleClass("btn-default", !active);
			});
			c.setPage(0);
			c.refresh();
		});
		renderCounts(c);
	}
	const clamp = n => Math.max(80, Math.min(800, Math.round(n)));
	function widthKey(c) {
		return `${grid.preferenceKey(c.root.frappe.boot?.sitename || c.root.location?.host, c.root.frappe.session?.user)}:stock-entry-widths`;
	}
	function resizeColumns(c) {
		try {
			const saved = JSON.parse(c.root.localStorage?.getItem(widthKey(c)) || "null");
			if (saved && typeof saved === "object" && !Array.isArray(saved)) for (const col of columns) {
				if (Number.isFinite(saved[col.fieldname])) col.width = clamp(saved[col.fieldname]);
			}
		} catch (_) { /* Browser storage is optional. */ }
		const host = c.list.$result?.[0];
		if (!host?.querySelectorAll || !c.root.document?.addEventListener) return;
		const update = (field,width) => {
			const col = columns.find(entry => entry.fieldname === field);
			if (!col || col.width === width) return false;
			col.width = width;
			const visible = c.preferences.columns.filter(name => visibleColumn(c,name))
				.map(name => columns.find(entry => entry.fieldname === name)).filter(Boolean);
			const tracks = `36px 40px ${visible.map(entry => `${entry.width}px`).join(" ")}`;
			for (const node of host.querySelectorAll(".dlp-po-grid-header, .dlp-po-grid-row")) node.style.setProperty("--dlp-po-columns", tracks);
			for (const node of host.querySelectorAll(`.dlp-po-grid-cell[data-fieldname="${field}"]`)) node.style.width = `${width}px`;
			for (const node of host.querySelectorAll(`[data-se-resize="${field}"]`)) node.setAttribute("aria-valuenow",String(width));
			return true;
		};
		const finish = field => {
			try { c.root.localStorage?.setItem(widthKey(c), JSON.stringify(Object.fromEntries(columns.map(col => [col.fieldname,col.width])))); }
			catch (_) { /* Widths still work for this session. */ }
			c.list.render_list();
			c.list.set_rows_as_checked?.();
			if (field) c.list.$result.find(`[data-se-resize="${field}"]`)[0]?.focus?.();
		};
		c.list.$result.on("click.dlpSEResize", "[data-se-resize]", event => { event.preventDefault(); event.stopPropagation(); });
		c.list.$result.on("keydown.dlpSEResize", "[data-se-resize]", event => {
			if (!["ArrowLeft","ArrowRight"].includes(event.key)) return;
			event.preventDefault(); event.stopPropagation();
			const field = event.currentTarget.dataset.seResize;
			const col = columns.find(entry => entry.fieldname === field);
			if (col && update(field, clamp(col.width + (event.key === "ArrowRight" ? 1 : -1) * (event.shiftKey ? 20 : 10)))) finish(field);
		});
		c.list.$result.on("pointerdown.dlpSEResize", "[data-se-resize]", event => {
			if (event.button !== 0) return;
			const field = event.currentTarget.dataset.seResize;
			const col = columns.find(entry => entry.fieldname === field);
			if (!col) return;
			event.preventDefault(); event.stopPropagation();
			const startX = event.originalEvent?.clientX ?? event.clientX;
			const pointerId = event.originalEvent?.pointerId ?? event.pointerId;
			const original = col.width;
			let changed = false;
			const move = next => {
				if (next.pointerId !== pointerId) return;
				changed = update(field,clamp(original + next.clientX - startX)) || changed;
			};
			const stop = next => {
				if (next.pointerId !== pointerId) return;
				c.root.document.removeEventListener("pointermove",move);
				c.root.document.removeEventListener("pointerup",stop);
				c.root.document.removeEventListener("pointercancel",stop);
				if (changed) finish();
			};
			c.root.document.addEventListener("pointermove",move);
			c.root.document.addEventListener("pointerup",stop);
			c.root.document.addEventListener("pointercancel",stop);
		});
	}
	function readableFields(c) {
		return new Set((c.root.frappe.get_meta("Stock Entry Detail")?.fields || []).filter(df => !df.is_virtual && c.root.frappe.perm.has_perm("Stock Entry",df.permlevel || 0,"read")).map(df => df.fieldname));
	}
	async function loadDetails(c,doc) {
		const frappe = c.root.frappe;
		await frappe.model.with_doctype("Stock Entry Detail");
		const native = (await frappe.call({method:"frappe.client.get",args:{doctype:"Stock Entry",name:doc.name},silent:true})).message;
		if (!native) throw new Error(c.translate("入库明细读取失败，请重试。"));
		const readable = readableFields(c);
		const fields = [["item_code","物料编码"],["item_name","物料名称"],["qty","数量"],["uom","单位"],["transfer_qty","库存数量"],["stock_uom","库存单位"],["t_warehouse","入库仓库"],["s_warehouse","发出仓库"]].filter(([field]) => readable.has(field));
		return {columns:fields,items:(native.items || []).map(row => Object.fromEntries(fields.map(([field]) => [field,row[field]])))};
	}
	function quantity(value, root) {
		if (value === null || value === undefined || value === "") return "—";
		const number = Number(value);
		if (!Number.isFinite(number)) return escape(value);
		const numeric = /^[+-]?\d+(?:\.(\d+))?(?:e([+-]?\d+))?$/i;
		const parts = String(value).trim().match(numeric) || String(number).match(numeric);
		const decimals = parts ? Math.max(0, (parts[1] || "").replace(/0+$/, "").length - Number(parts[2] || 0)) : 0;
		return escape(root.format_number ? root.format_number(number, null, Math.min(decimals, 9)) : number.toLocaleString(undefined, {maximumFractionDigits:9}));
	}
	function rowExtra(c,doc) {
		if (!c.expandedDetails?.has(doc.name)) return "";
		const detail = c.detailCache.get(doc.name);
		let content = detail?.error ? escape(detail.error) : escape(c.translate("正在读取入库明细…"));
		if (detail && !detail.error) content = detail.items.length && detail.columns.length
			? engine.detailTable({columns:detail.columns,items:detail.items,escape,translate:c.translate,wrapperClass:"dlp-purchase-detail-scroll",tableClass:"dlp-purchase-detail-table",textFields:["item_name","t_warehouse","s_warehouse"],textClass:"dlp-purchase-detail-text",format:(field,row) => ["qty","transfer_qty"].includes(field) ? quantity(row[field],c.root) : escape(row[field] ?? "—")})
			: escape(c.translate("暂无可读入库明细"));
		return `<div class="dlp-purchase-expanded" role="region" aria-label="${escape(c.translate("入库明细"))}">${content}</div>`;
	}
	function loadReceiptCallbackStatuses(c) {
		const version = ++c.seCallbackVersion;
		const rows = c.list.data || [];
		const names = [];
		for (const doc of rows) {
			if (!doc.custom_mes_receipt) continue;
			if (doc.docstatus === 0) {
				doc[receiptCallbackField] = "Awaiting submission";
			} else {
				doc[receiptCallbackField] = "Loading";
				names.push(doc.name);
			}
		}
		if (!names.length) return;
		const requested = new Set(names);
		if (!c.root.frappe.call) {
			for (const doc of rows) if (requested.has(doc.name)) doc[receiptCallbackField] = "Load failed";
			return;
		}
		Promise.resolve().then(() => c.root.frappe.call({
			method:"mes_integration.mes_integration.stock_entry.get_mes_receipt_callback_statuses",
			args:{stock_entry_names:names},silent:true,
		})).then(response => {
			if (version !== c.seCallbackVersion) return;
			for (const doc of rows) if (requested.has(doc.name)) {
				doc[receiptCallbackField] = response.message?.[doc.name] || "Load failed";
			}
			c.list.render_list();
			c.list.set_rows_as_checked?.();
		}).catch(() => {
			if (version !== c.seCallbackVersion) return;
			for (const doc of rows) if (requested.has(doc.name)) doc[receiptCallbackField] = "Load failed";
			c.list.render_list();
			c.list.set_rows_as_checked?.();
		});
	}
	const grid = engine.create({
		doctype:"Stock Entry",columns,defaultColumns,preferenceVersion:2,
		migratePreferences:value => value?.version === 2 ? value : {...value,columns:[...new Set([...(Array.isArray(value?.columns) ? value.columns : defaultColumns),receiptCallbackField])]},
		controllerKey:"dlpStockEntryGrid",routeClass:"dlp-stock-entry-grid-active",
		dateField:"posting_date",dates:["posting_date"],searchFields:["name","custom_stock_entry_no"],
		computedFields:[receiptCallbackField],
		extraFields:["custom_mes_receipt","modified"],
		onMount:c => {
			c.seView = canShowReceipts(c) ? pending : "all";
			c.seCounts = {[pending]:null,[submitted]:null,all:null};
			c.seCountVersion = 0;
			c.seCallbackVersion = 0;
			resizeColumns(c);
			engine.mountRowDetails(c,{rows:() => c.list.data,load:doc => loadDetails(c,doc),canExpand:() => c.allowed.has("items"),showExpandAll:false,active:() => {const route = c.root.frappe.get_route?.() || [];return String(route[0]).toLowerCase() === "list" && route[1] === "Stock Entry";}});
		},
		onRequestStart:c => {c.seCountVersion++;c.seCallbackVersion++;c.seCounts = {[pending]:null,[submitted]:null,all:null};renderCounts(c);},
		onRows:c => {c.invalidateRowDetails?.();refreshCounts(c);loadReceiptCallbackStatuses(c);},
		onCount:c => {if (c.seCounts) {c.seCounts[c.seView] = c.total;renderCounts(c);}},
		mountControls:mountViews,transformQuery:viewQuery,
		visibleColumns:visibleColumn,
		exportCurrent:async (c,args,visible) => {
			if (!root.DeepLinkERPPurchaseOrderExport?.exportExcel) await root.frappe.require("/assets/deeplinkerp_branding/js/purchase_order_export.js");
			const exporter = root.DeepLinkERPPurchaseOrderExport;
			if (!exporter?.exportExcel) throw new Error(c.translate("Excel 导出组件加载失败，请刷新页面后重试。"));
			if (!visible.includes(receiptCallbackField)) return exporter.exportExcel(root,args);
			const position = visible.slice(0,visible.indexOf(receiptCallbackField)).filter(field => c.allowed.has(field)).length;
			const native = await exporter.fetchNativeWorkbook(root,{...args,receipt_callback_position:position},"mes_integration.mes_integration.stock_entry_export.export_stock_entries_with_receipt_status");
			const bytes = await exporter.removeNativeOwner(native,{expectedColumns:args.fields.length+2,ownerLabels:["Owner","Created By","创建人",root.__("Owner"),root.__("Created By")]});
			exporter.downloadWorkbook(root,bytes,root.__("物料移动"));
		},
		headerCellExtra:(c,col) => `<span class="dlp-se-resize" data-se-resize="${escape(col.fieldname)}" role="separator" tabindex="0" aria-orientation="vertical" aria-label="${escape(c.translate(col.label) + "：" + c.translate("拖动或按方向键调整列宽"))}" aria-valuemin="80" aria-valuemax="800" aria-valuenow="${col.width}"></span>`,
		rowExtra,
		renderSequence:(c,doc) => c.allowed.has("items") ? `<button type="button" class="dlp-purchase-expand" data-name="${escape(doc.name)}" aria-expanded="${Boolean(c.expandedDetails?.has(doc.name))}" aria-label="${escape(c.translate("展开 / 收起入库明细"))}">${c.expandedDetails?.has(doc.name) ? "⌄" : "›"}</button>` : "",
		controls:[
			{fieldname:"stock_entry_type",fieldtype:"Link",options:"Stock Entry Type",label:root.__("移动类型")},
			{fieldname:"search",fieldtype:"Data",label:root.__("ERP / MES入库编号")},
			{fieldname:"from_date",fieldtype:"Date",label:root.__("入库日期起"),permission_field:"posting_date"},
			{fieldname:"to_date",fieldtype:"Date",label:root.__("入库日期止"),permission_field:"posting_date"},
			{fieldname:"company",fieldtype:"Link",options:"Company",label:root.__("公司")},
		],
		renderValue:(field,doc,formatters,escapeHTML) => {
			if (field === "stock_entry_type") return escapeHTML(formatters.translate?.(doc.stock_entry_type) || doc.stock_entry_type || "—");
			if (field === "docstatus") return `<span class="indicator-pill ${doc.docstatus === 0 ? "orange" : doc.docstatus === 1 ? "green" : "red"} no-indicator-dot">${escapeHTML(formatters.translate?.(doc.docstatus === 0 ? "Draft" : doc.docstatus === 1 ? "Submitted" : "Cancelled") || "")}</span>`;
			if (field === "custom_mes_status") return MES_DLM_STOCK_ENTRY_TYPES.includes(doc.stock_entry_type) ? escapeHTML(formatters.translate?.(doc.custom_mes_status || "Unpushed") || doc.custom_mes_status || "Unpushed") : "";
			if (field === receiptCallbackField) {
				if (!doc.custom_mes_receipt) return "";
				const status = doc[receiptCallbackField] || "Loading";
				const color = status === "Success" ? "green" : status === "Failed" ? "red" : status === "Pending" || status === "No Record" ? "orange" : "gray";
				return `<span class="indicator-pill ${color} no-indicator-dot">${escapeHTML(formatters.translate?.(receiptCallbackLabels[status] || status) || receiptCallbackLabels[status] || status)}</span>`;
			}
		},
	});
	root.DeepLinkERPStockEntryGrid = grid;
	grid.install(root);
})(typeof globalThis !== "undefined" ? globalThis : this);
