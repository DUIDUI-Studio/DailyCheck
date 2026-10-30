# Issue #80 · Bot MCP PR2 验证记录

日期：2026-10-30。开发范围是 [#80](http://hub01:3000/eric/dailycheck/issues/80)，批准依据为 [#78 comment 714](http://hub01:3000/eric/dailycheck/issues/78#issuecomment-714)、[635](http://hub01:3000/eric/dailycheck/issues/78#issuecomment-635)、[634](http://hub01:3000/eric/dailycheck/issues/78#issuecomment-634)。此记录是本地验收，不代表 bot 已人工同步或生产启写；#88 契约文档由另一 writer 所有，本 PR 不修改它。

## 隔离可行性

在独立 `/tmp/dc80-bot-py313` venv 安装 `bot/requirements.txt` 与 pytest/ruff；没有向主站 venv 安装任何包，也没有连接生产 BotID。Python 3.13.12、MCP 1.30.0、Werkzeug 3.1.9、Flask 3.1.3、jsonschema 4.26.0 的 import 成功。系统 Python 3.9 的初次安装找不到兼容 MCP，切换项目现用的 Python 3.13 解释器创建独立 venv 后成功。未限定版本会安装 MCP 2.3.0，其 `Tool` 没有当前代码使用的 `inputSchema`；因此 bot 与项目依赖均约束 `mcp>=1.2.0,<2`。

测试 `test_approved_schema_dispatch_real_result_without_context` 对 22 项逐名断言 `has_app_context()==False`、`has_request_context()==False`，在原生 `init_master_db/init_warehouse_db` 初始化的临时库实调。所有调用显式传自己的 `AuthContext`，即使遗留 ContextVar 和进程 MCP token 存在，也不使用它们。不存在请求上下文依赖阻断；发现的两个数据库副作用使用薄适配处理：

- 采购原 `_store_procurement_json` 经 `_resolve_warehouse/_get_config/_read_cache/_write_cache` 初始化和写缓存。bot 只读分支复用 `compute_store_procurement` 与 `aggregate_hub`，显式传现有配置值与只读连接，跳过缓存/初始化，不重写公式。
- canonical 详情的 `collect_bindings` 原先会迁移仓库列。显式只读模式不迁移、连接 `mode=ro`；初始化/迁移仍属于应用生命周期。缺少已初始化的库时明确失败，不创建空库。

## 逐工具对账

schema 来自原 MCP `get_tools()`；`bot/tool_contract.py` 的固定批准池限制 schema/dispatch，`bot/mcp_tools.py` 显式调用对应模块的 `<name>_impl(args, ctx=...)`。原 14 和额外八读全部接入；不能把 14/14 冒充本轮覆盖。临时库种子：两个仓 wh_a/wh_b，各一个面粉 F1，库存 100；每仓七笔出库各 2；动作流水出库 20；采购最小安全库存 200；两订单、两冲突、一个含两仓/两项的事件。

| 工具名 | handler 域 | 正常默认 | 临时库实际结果/范围 |
|---|---|---|---|
| warehouse_list | inventory | on | 返回 wh_a、wh_b；受限上下文只列授权仓 |
| items_list | inventory | on | wh_a/F1，current_stock=100 |
| items_detail | inventory | on | item_id=1，name=面粉，current_stock=100 |
| movements_list | inventory | on | 七笔业务出库 + 一笔动作流水，共八行 |
| restock_create | inbound | on（仍需三写护栏） | id=1、quantity=3、真实 movement_id；库存 100→103 |
| restock_list | inbound | on | 空库 total=0；真实创建后 total=1、quantity=3 |
| item_forecast | forecast | on | daily_avg=2、horizon_days=14、forecast_total=28；非补货看板预测 |
| procurement_store | procurement | on | 单仓 suggested_qty=100；不写缓存 |
| procurement_hub | procurement | on | 全授权两仓 total_suggested_qty=200；限 wh_a 时聚合只含该仓 |
| outbound_create | outbound | on（仍需三写护栏） | id=8、quantity=4；库存扣减四；失效已有采购缓存为原业务副作用 |
| outbound_list | outbound | on | total=7、每行 quantity=2；保留分页形状 |
| outbound_rollback | outbound | on（仍需三写护栏） | 回滚种子单 id=1、quantity=2；成功链回滚新出库后库存恢复；重复回滚拒绝 |
| warehouse_consumption | consumption | on | consume_qty=14；保留 warehouse_turnover 元数据 |
| item_consumption | consumption | on | consume_7d.qty=14；monthly.window_days=28；生产单据/镜像去重另验 |
| canonical_list | canonical | off，显式启用 | total=1、canonical_sku=C1、categories/item_count；需全局授权 |
| canonical_detail | canonical | off，显式启用 | canonical + bindings；bound_warehouse_count=2；需全局授权 |
| canonical_categories | canonical | off，显式启用 | 未归品类 item_count=1；全局数据 |
| canonical_fanout_targets | canonical | off，显式启用 | 两个 storefront 候选仓；不授予 fanout 权限 |
| canonical_conflicts | canonical | off，显式启用 | 两条 open 冲突及 canonical_name=面粉；全局数据 |
| canonical_fanout_event | canonical | off，显式启用 | event.id=1、两仓、两项；不伪称按仓裁切 |
| store_order_list | store_order | off，显式启用 | 两订单；限仓后 rows/total 均为一；保留分页/去重、门店或 DC 命中规则 |
| store_order_detail | store_order | off，显式启用 | order/items/deliveries/receipts/status_history 五段；范围外与不存在同义 |

额外八读均有真实请求构造验证：逐项 on 时出现、下一消息 off 时消失，执行入口同步拒绝。六个 canonical 读只在受信策略 `global_read=true` 且 `warehouses=null` 时允许，路径仍需匹配；`global_read` 单独为 true 或仅不限仓均不足，受限仓绝不被提升为全局。

五自有 `search_items/list_low_stock/list_store_overview/list_orders/get_project_help` 保留。四旧名的参数/输出不替换成 MCP，独立子开关偏好和计数保留，父 off 同时屏蔽子名。旧价格 null/主数据跟随/停用过滤沿用原测试；订单旧名的行、状态汇总和链接均按当前授权范围。旧消费保留动作 `SUM(-delta)`（不添加 delta<0）、日期 cutoff 和 1–365 days；新测试验证 90 天、门店订货、总仓调货、生产及镜像、回滚、午夜 cutoff、正 delta 的原差异。

Owner D3 明确排除七写：`canonical_create/canonical_update/canonical_claim/canonical_unclaim/canonical_fanout/canonical_resolve_conflict/canonical_set_binding_active`。即使 on、owner、全权限进程 token 都无法获得 bot schema/adapter，逐名伪造调用拒绝且数据库字节不变；`get_price` 持续隐藏且 unknown_tool。注入未来新注册项只扩大动态展示，不扩大执行池。

## 三写安全与生产前置

- `tool.restock_create/tool.outbound_create/tool.outbound_rollback` 正常配置默认 on；既有显式 off 保持 off。关闭 restock 的 aiohttp 实际请求构造不含该名，伪造执行仍 unknown_tool。
- frame 的 `body.from.userid` 由 serve 显式传到 agent；模型 args 经 schema 拒绝额外 userid/ctx，不能伪装发送者。未知/缺失 userid、非白名单、switch off、跨路径/仓库均给明确结构化拒绝，两个仓库与主库的快照不变。
- 默认仅历史 owner 值 `LinJi`；协调者确认尚无真实 userid 证据，故额外身份核实门槛默认拒写。只有 Owner/Prod 核实后设置单个 `BOT_OWNER_USERID`，白名单缺省才使用该确切值。它是运维核实声明，不能由聊天或模型设置。现有显式白名单键优先，空名单拒绝任何人。
- PR1 配置表不能表达路径/仓范围；`BOT_AUTH_POLICY` 是受信部署策略文件，每消息加载，按真实 userid 键控。例如结构为 `{"<verified-userid>":{"read_paths":["*"],"write_paths":["/api/v1/restock"],"warehouses":["wh_a"],"global_read":false}}`。这只是结构示例，不是生产 userid 或授权结论。
- 写策略缺失=空写权限；指定文件缺失/损坏或 userid 无记录=全部范围拒绝。`warehouses` 缺省空列表，null 才表示不限仓；仍仅在通过路径/仓校验后构造仅本路径/单仓的最小写上下文。未配置文件保留旧库存读，不自动授权 canonical 全局读。
- 所有工具记录 admin_audit 的 actor + detail(JSON who/tool/args/result/duration_ms)，包括拒绝和服务失败；写前 attempt 不可落盘则不执行。写后结果日志故障保留真实回执并向服务日志告警，需管理员按 attempt 核对，不能谎报数据库回滚。
- 回执由真实工具结果直接追加到最终回复；即使模型只答“已完成”也保留 id/quantity。并发两 userid 不串写白名单或读仓范围；同一消息快照不可变，白名单/策略编辑下一消息生效。
- 配置读失败/缺表/坏值仍固定旧九只读 + medium、空写白名单；不从 31 名 schema/dispatch 推导回退集。sender ACL 继续生效。

## 执行证据

- 隔离 venv bot 定向测试：`/tmp/dc80-bot-py313/bin/python -m pytest tests/bot -q`，包含全部真实临时库实调；243 passed；随后新增 BLOB 坏配置回退测试，配置定向套件另跑 19 passed。
- SQLite 误写注入真实触发 readonly 拒绝；缺库查询不创建数据库。19 个读工具逐项路径拒绝、六全局读双条件、三写 18 种拒绝组合均验证数据不变。
- 四个变异分别移除 userid/warehouse/path 检查或将 read_only 改为 false，对应测试全部真实 FAIL；恢复后定向套件通过。
- 本机主站测试 venv 实际已有 MCP 2.3.0，与本 PR 明示依赖范围不符；一次全量启动因 schema API 不兼容失败，已停止该次运行而未修改该 venv。最终全量在另建 `/tmp/dc80-test-venv` 按项目 requirements 安装后执行；新增 BLOB 白名单坏配置回退另跑定向验证。
- 开工 CI 原生基线：`pytest --ignore=tests/mcp_server -q` = 2619 passed / 14 failed，全部属于 known_failures；未扩张基线。提交前隔离测试 venv 的 CI 原生全量为 **2708 passed / 14 failed（247.40s）**；失败集合与开工基线完全相等，`scripts/check_known_failures.py` exit 0（零新增）。改动 Python Ruff 零告警，`arch_map.py --check` 与 `git diff --check` 通过。
- MCP/服务/管理台专项 279 项中 277 passed，两个既有失败：已知采购 staff 权限断言，以及被原 CI 排除的入库 service 将 data 层 `{id,movement_id}` 嵌套为 id 的断言。后者已通过加载 `origin/main` 原 service 源码运行同一测试再次复现，本 PR 不改原 MCP 返回形状，仅在 bot adapter 展开真实 id/movement_id。

## 生产交接

主站/MCP 代码与依赖按标准候选部署流程验证。bot 不在蓝绿流程内，生产侧需人工同步 `/opt/dailycheck-bot` 的 bot/MCP/业务复用模块与独立 venv 依赖，主站部署成功不能证明 bot 生效。Owner 真实 userid、受信范围策略与审计日志写权限是启写前置；未核实时保持拒写。八读由管理员逐项显式启用，六个 canonical 读须核定全局信息授权；观察写回执/审计、白名单热加载、mode=ro、依赖导入与单 BotID 单连接。补货看板预测不在本 PR，归 #81。
