### Mes Integration

connect to mes

独立仓库为 [yueweiit/deeplinkerp-mes_integration](https://github.com/yueweiit/deeplinkerp-mes_integration)，开发主线和 PR 目标均为 `main`。Frappe App 名保持 `mes_integration`。

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app --branch main mes_integration https://github.com/yueweiit/deeplinkerp-mes_integration.git
bench install-app mes_integration
```

### Material Request 接口压测

压测脚本位于 `scripts/benchmark_material_request_api.py`。建议在 ERP/Frappe 主机上直接访问本机地址，并先使用单并发建立不受网络和并发影响的基线。脚本只使用标准库，不会调用 MES/DLM 外部接口，但会创建并提交真实的 Material Request，请使用专用测试公司、物料和仓库。

```bash
export MES_API_AUTHORIZATION='token <api_key>:<api_secret>'

python3 scripts/benchmark_material_request_api.py \
  --base-url http://127.0.0.1:8000 \
  --host-header dev.localhost \
  --company 'Test Company' \
  --item-code 'TEST-ITEM' \
  --warehouse 'Stores - TC' \
  --uom Nos \
  --detail-count 5000 \
  --requests 5 \
  --workers 1 \
  --warmup 1 \
  --confirm
```

输出包括成功率、吞吐量、平均耗时、P50/P95/P99 和最大耗时。`--workers 1` 用于单并发基线，确认基线后再逐步增加 `--workers` 观察并发下的变化。可先加 `--dry-run` 检查请求体大小而不调用接口；认证信息不会写入结果文件。

脚本默认只发送接口必需字段，重点观察大明细落库耗时。如果需要复现生产请求体大小，可准备一条明细 JSON 样例并传入 `--detail-template-file`；脚本会复制其中的业务字段到每条明细，并自动覆盖 `idx`、`parent` 等服务端字段。

接口内部使用不依赖 `tabSeries` 的 MES 单号。MES 请求先进入 long 队列，后台创建并提交 Material Request；`tabBin.indented_qty` 在建单事务之后由 short 队列任务重新汇总。任务按物料和仓库重新汇总所有已提交需求，因此重复执行和失败重试不会重复累加。任务执行时仍会锁定相同“物料＋仓库”的 Bin，并与标准 ERPNext 更新保持一致；队列不可用时任务会进入 Failed，MES 可使用同一个幂等号重试。任务完成前 Bin 汇总可能存在短暂延迟。

MES 重试请求时应传递稳定的 `X-Idempotency-Key`，或在请求数据中提供 `custom_material_request_no`、`request_id` 或 `idempotency_key`，接口会复用同一个异步任务，不会重复建单。

完整异步创建任务投递到 Frappe long 队列，Bin 汇总和审计日志使用 short 队列；部署时建议至少运行 2 个 long worker，并单独运行 short/default worker，避免大明细建单任务互相排队。

当前开发 bench 的进程配置为：

    bench worker --queue long
    bench worker --queue long
    bench worker --queue short,default

系统每 5 分钟会自动恢复没有实际队列任务的 Queued 任务，以及超过 30 分钟的 Processing 任务；恢复执行仍使用原任务提交用户的权限。

### MES 入库接口

MES 系统页面地址通过站点配置 `mes_portal_url` 提供，配置后可从制造工作区打开 MES 页面。

MES 可调用以下接口创建入库 Stock Entry：

    POST /api/method/mes_integration.api.create_stock_entry

该接口默认创建并提交入库单。需要明确保留草稿用于人工审核时，才在请求顶层传 `submit=0`。兼容入口如下，同样固定创建并提交：

    POST /api/method/mes_integration.api.create_and_submit_stock_entry

`create_stock_entry` 的 `submit` 必须放在请求顶层，放在 `data` 内不会生效。请求数据应至少包含 `company`、`stock_entry_type` 和 `items`，其中 `stock_entry_type` 支持 `Material Receipt`、`Semi Finished Goods Receipt` 和 `Finished Goods Receipt`。每行入库明细需要 `item_code`、正数 `qty` 和 `t_warehouse`；未传目标仓库时 ERP 会按物料默认仓库或配置的兜底仓库补齐。

入库接口必须传 `sales_order`，新调用应填写 ERP Sales Order 内部号（`Sales Order.name`）。系统会读取该订单并写入 Stock Entry 的 `custom_sales_order` Link 字段；CRM 销售订单号由 ERP 销售订单的 `custom_crm_order_no` 保存，MES 不需要提供。为兼容旧调用，服务端仍支持用 CRM 订单号反查 ERP 销售订单。创建并提交成功的响应会返回 ERP 销售订单号 `sales_order` 和 CRM 订单号 `sales_order_crm_order_no`，以及 `stock_entry_docstatus: 1`、`submitted: true`。

MES 重试入库请求时必须继续使用原来的 `custom_stock_entry_no`，ERP 会按“公司 + MES 入库编号”复用原 Stock Entry，并在响应中返回 `idempotent_reuse: true`；不会再次创建 ERP 单据。相同编号但公司、销售订单或明细不一致时，ERP 返回 HTTP 409 和 `ERP_STOCK_ENTRY_IDENTITY_CONFLICT`，MES 不应生成新编号绕过该错误。

提交入库后，系统会向 `mes_status_callback_url` 回调 Stock Entry 状态；MES 接口需要返回 HTTP 2xx 的 JSON，并且 `success: true`、`data.status: "processed"`。

入库单在 `docstatus=1` 后才生成库存流水并增加 `t_warehouse` 的库存；`docstatus=0` 的草稿不影响库存。服务端统一使用 `Material Receipt` purpose，入库明细不保留来源仓，因此不会扣减 ERP 库存。

异步接口返回的是“已接收并返回任务号”，压测脚本统计的是 MES 到 ERP 的接收耗时，不是后台建单耗时。

MES 创建物料需求后，响应会同时放在 Frappe 标准的 `message` 和兼容字段 `data` 中。应保存返回的 `task_id`（`message.task_id` 或 `data.task_id`），并轮询：

    GET /api/method/mes_integration.api.get_material_request_task_status?task_id=<task_id>

状态为 queued、processing、success 或 failed。响应中的 `request_id` 对应请求的 `custom_material_request_no`。只有 success 表示 Material Request 已创建并提交；失败时可使用同一个幂等号重新调用创建接口。任务成功后会自动清空暂存的原始大 payload，只保留状态和单据号。

### 直接履约产品的回调例外

具有 CRM 订单号、按公司产品名单完成直接履约财务放行的订单，不向 MES 下发销售订单，也不回调尾款和 Delivery Note 提交/取消状态。管理员在公司资料的“无需 MES 生产的产品编码前缀”维护三位产品编码（默认 NSF 钢化膜），同产品多型号/颜色随整单处理。`settings.requires_sales_order_mes_sync` 复用 CRM `production_routing.py`，优先读取销售订单的持久 `custom_crm_fulfillment_route`，成功财务审计及 MES 成功记录仅兼容未补存的旧单；旧待生产状态保持 MES，已完成/取消但路径不明的订单为 UNKNOWN，阻止 HTTP 并要求核对，不按现行名单重新分类历史订单。名单调整和日志清理不会改变已保存的路径。

发送前返回 `status=skipped, reason=NO_MES_PRODUCTION`，不创建 MES 投递日志或调用外部 HTTP。出库单关联多个销售订单时仅跳过直接履约订单，其他订单仍分别发送。人工补推与已排队任务执行相同检查。原生非 CRM 订单、未配置产品和空明细沿用原行为；异常混单由 CRM 财务放行前拦截。CRM App 未安装或未提供该策略模块时保持原逻辑，不把内部依赖损坏静默当作跳过。需要同时更新 CRM 与 MES 两个 App，并按 CRM 的安装/迁移流程同步 Company 配置字段、Sales Order 路径字段及历史证据快照。回归入口为 `test_direct_fulfillment.py` 及原有销售订单、出库回调测试。

### MES 销售订单 ODT/交期回传

MES 收到 ERP 下发的销售订单后，可批量回传订单头 ODT 和交期：

    POST /api/method/mes_integration.api.update_sales_order_fields_batch

请求使用顶层 JSON，必须携带与 `requestId` 相同的 `X-Idempotency-Key`。每批最多 200 张订单；`externalOrderId` 固定为 ERP `Sales Order.name`，每个字段分别提供稳定的 `operationId`、递增的 `sourceVersion`、`expectedValue` 和新 `value`。ERP 仅在订单流程状态为 `Deposit Confirmation Processing` 或 `Pending Production` 时接收，修改 ODT/交期不会改变流程状态。交期写入订单头时也会同步现有订单明细的交期。

接口支持批次部分成功，HTTP 200 后仍需逐字段检查 `status`。相同 `requestId` 和相同内容会重放原批次响应；相同 `operationId` 和相同内容会返回首次字段结果。内容变化、旧版本、预期原值不一致或订单状态不允许时会返回明确的冲突码。结果不确定时按 `operationId` 查询：

    GET /api/method/mes_integration.api.get_sales_order_field_change_result?operation_id=<operationId>

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/mes_integration
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade
### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit
