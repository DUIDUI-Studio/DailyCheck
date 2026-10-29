# 多仓库履约（单订单跨仓自动路由）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. 本计划由 Orca coordinator 派发 worker 执行；每 Task 独立可测、TDD 先红后绿、频繁 commit（`git push --no-verify` 为既定姿势）。

**Goal:** 门店订单一单可跨多仓自动路由履约，订货/发货体验对门店与仓库人员完全无感。

**Architecture:** 仓下沉到订单明细行（`source_warehouse_code`），显式路由表 `item_routing` 确定性归仓；跨库提交用 `op_no` 幂等键 + 条件原子扣减 + 锁内聚合 + 条件状态迁移；全站可见性收敛到统一「参与仓」EXISTS 谓词。

**Tech Stack:** Flask 3.1 + SQLite（master.db + 每仓独立文件，`PRAGMA foreign_keys=ON`）+ pytest + Jinja2。

**Spec:** `docs/superpowers/specs/2026-10-29-multi-warehouse-fulfillment-design.md`（v2，决策 D1–D14）
**评审:** `docs/superpowers/reviews/2026-10-29-multi-warehouse-fulfillment-codex-review.md`（P0-01…P2-03 已消解）

## Global Constraints

- Ruff：line_length=100，规则 E/F/W/I/B/UP；Python 3.10 target。
- 触碰 `blueprints/ db/ mcp_server/ app.py config.py permissions.py templates/ static/ tests/ scripts/` 的 commit **必须同 commit 更新 `docs/ARCHITECTURE.md`** 对应章节（pre-commit 门禁，禁 bypass 当默认）。
- 修改 `mcp_server/` 必须同步 `docs/integrations/dailycheck-mcp/`。
- 不提交 `*.db` / `.venv` / 日志。
- 数量不变量：`0 ≤ fulfilled_quantity(已收) ≤ shipped_quantity(已发) ≤ quantity(订购)`，所有写路径带守卫。
- 一期仅租户 1（D13）；跨仓创建受开关 `MULTI_WAREHOUSE_FULFILLMENT`（默认关）控制（D14）。
- 调货 `dc_transfer` 业务语义零变化，既有测试断言不改（只允许因建单层适配新增）。
- DC staff 发货能力不收紧：发货路由保持 `require_role("staff")`。
- 所有状态迁移条件式（`UPDATE … WHERE status=<expected>`，`changes()==1` 才写事件）。
- PR body 必含 `## 报告 1/4` + `## Checklist` + `## 生产侧事项` 三段，禁字面「❌」。

---

### Task 1: Schema 迁移与存量回填

**Files:**
- Modify: `db/__init__.py`（新库 DDL：`store_order_items`/`store_order_deliveries` 新列、`store_order_delivery_items` 表、`item_routing` 表、`store_order_carts.dc_warehouse_code` 可空、仓内 `stock_movements.op_no`）
- Modify: `migrations/__init__.py` + 对应迁移模块（注册存量迁移）
- Test: `tests/test_migration_multi_warehouse.py`（新建）

**Interfaces:**
- Produces: 存量库执行后具备列/表：`store_order_items.source_warehouse_code TEXT`、`store_order_items.ship_op_no TEXT`、`store_order_deliveries.warehouse_code TEXT`、`store_order_delivery_items(delivery_id, order_item_id, warehouse_code, item_id, quantity, op_no TEXT UNIQUE, created_at)`、`item_routing(tenant_id, canonical_id, fulfillment_warehouse_code, updated_at, updated_by, PRIMARY KEY(tenant_id, canonical_id))`、仓库 `stock_movements.op_no TEXT`。迁移函数名 `migrate_multi_warehouse_fulfillment(master_conn, warehouse_conns) -> dict`（返回 `{backfilled_items, backfilled_deliveries, carts_rebuilt, anomalies}`）。

- [ ] **Step 1 写失败测试**：`tests/test_migration_multi_warehouse.py` 覆盖——(a) 迁移后新列存在且存量行 `source_warehouse_code` = 头仓；(b) **幂等**：先迁移→手工把某行来源改成 B→再迁移→该行仍为 B（只回填 NULL）；(c) carts 表重建后 `dc_warehouse_code` 可空、旧数据保留；(d) 违反 `0≤已收≤已发≤订购` 的存量行进 `anomalies` 列表且**不被修改**；(e) 重跑返回一致（幂等）。
- [ ] **Step 2** `pytest tests/test_migration_multi_warehouse.py -v` → FAIL（函数未定义）。
- [ ] **Step 3 实现**：DDL 进 `db/__init__.py`；迁移注册进 `migrations/`；carts 用标准重建表模式（`CREATE TABLE new … dc_warehouse_code TEXT`、copy、drop、rename，FK ON 下先 `PRAGMA foreign_keys=OFF` 于事务内重建后恢复 ON，或直接重建保留 FK 指向）。回填 SQL 必须带 `WHERE source_warehouse_code IS NULL`。
- [ ] **Step 4** 测试全绿；`pytest tests/test_migration_runner.py -v` 回归绿。
- [ ] **Step 5 Commit**（同 commit 更新 `docs/ARCHITECTURE.md` 数据模型章节 + `## 生产侧事项` 记迁移执行）：`feat(db): 多仓履约 schema 迁移——行级来源/批次仓/op_no 幂等键/item_routing`。

### Task 2: 路由播种 + admin 配置页

**Files:**
- Create: `blueprints/item_routing_pure.py`（播种 + CRUD pure 层）
- Modify: `blueprints/ops.py` 或新蓝图注册路由（依现有 admin 页惯例，实现时先看 `app.py` 蓝图注册处选归属）
- Create: `templates/item_routing/admin.html`
- Modify: `migrations/`（播种作为独立迁移步骤，可重跑）
- Test: `tests/test_item_routing_pure.py`、`tests/test_item_routing_route.py`

**Interfaces:**
- Produces:
  - `seed_routing(master_conn, wh_db_map: dict[str, sqlite3.Connection]) -> dict`：返回 `{enabled: [(canonical_id, dc_code)], ambiguous: [canonical_id], missing: [canonical_id], skipped_exclusive: [canonical_id]}`。候选仅同租户(=1) `warehouse_type='distribution_center'` 仓；有效绑定=`items.is_active=1 AND is_alias=0` 且 canonical active；唯一合格 DC→自动启用；多 DC/零 DC→ambiguous/missing；专属品 skip（D10）。已存在路由的 canonical 不覆盖。
  - `list_routing(master_conn) -> list[dict]`、`set_routing(master_conn, canonical_id, dc_code, *, actor_id) -> None`（校验 dc 为 DC 类型且同租户，否则 ValueError）、`delete_routing(master_conn, canonical_id, *, actor_id)`、`get_routing(master_conn, canonical_id) -> str | None`。
  - `routing_pending(master_conn) -> dict`：ambiguous+missing 清单供页面高亮。

- [ ] **Step 1 失败测试**：seed——研发仓镜像不参与候选；跨租户仓不参与；唯一 DC 自动启用且不覆盖既有路由；双 DC 镜像→ambiguous；专属品→skipped；`set_routing` 对门店仓/他租户仓抛 ValueError；重跑 seed 幂等。
- [ ] **Step 2** 跑测试 FAIL。
- [ ] **Step 3 实现** pure 层 + 播种迁移 + admin 页（列表/设置/删除/pending 高亮；守卫 `@require_platform_admin` 或现有 admin 权限点惯例——实现时对照 `blueprints/ops.py` 既有 admin 路由）。
- [ ] **Step 4** 测试绿 + `ruff check .`。
- [ ] **Step 5 Commit**（ARCHITECTURE 主数据章节同步）：`feat(routing): item_routing 播种迁移与 admin 配置页`。

### Task 3: 履约行解析器

**Files:**
- Create: `blueprints/fulfillment_pure.py`
- Test: `tests/test_fulfillment_pure.py`

**Interfaces:**
- Consumes: `item_routing_pure.get_routing`。
- Produces:
  - `resolve_fulfillment_item(master_conn, wh_conn, *, canonical_id: int, warehouse_code: str) -> dict`：返回 `{item_id, unit, unit_cost, order_multiple, available}`。规则：仓内 `canonical_id=? AND is_active=1 AND is_alias=0` 行**必须唯一**；多行→`FulfillmentAmbiguousError`；零行→`FulfillmentMissingError`；单位与 canonical 不可比→`FulfillmentConflictError`（异常均带 canonical_id/warehouse_code 上下文）。**禁止 fetchone 首行**。
  - `parse_price_and_multiple(item_row) -> tuple[float, float]`：与现有目录价格/倍数逻辑同源（实现时对照 `store_ordering_pure.py:316/:333` 抽取复用，不改行为）。

- [ ] **Step 1 失败测试**：唯一有效行→正确解析；别名行不计入；停用行不计入；双绑定→Ambiguous；无行→Missing；单位冲突→Conflict；跨仓同 item_id 不串（用两个独立内存库验证）。
- [ ] **Step 2–4** 红→实现→绿。
- [ ] **Step 5 Commit**：`feat(fulfillment): 履约行解析器——唯一有效行/失败关闭/禁首行`。

### Task 4: 参与仓谓词 + 权限目标仓解析

**Files:**
- Modify: `blueprints/store_ordering_pure.py`（新增谓词工具）
- Modify: `permissions.py`
- Test: `tests/test_order_participation_pure.py`、`tests/test_permissions_target_warehouse.py`

**Interfaces:**
- Produces:
  - `order_participation_sql(alias: str = "so") -> str`：返回片段
    `EXISTS (SELECT 1 FROM store_order_items soi WHERE soi.order_id={alias}.id AND COALESCE(soi.source_warehouse_code, {alias}.dc_warehouse_code) = ?)`（参数为仓 code；头仓兜底仅历史 NULL 行）。
  - `has_permission(user_id: int, perm: str, *, warehouse_code: str | None = None) -> bool`（`permissions.py`）：缺省当前仓；目标仓按同规则解析内置/租户角色；缓存键 `(user_id, warehouse_code, tenant_id)`；解析失败→False（失败关闭）。
  - `order_participants(master_conn, order_id: int) -> list[str]`：参与仓集合（行来源仓 ∪ 历史兜底头仓），去重排序。
  - `can_act_on_order(user_id: int, order_id: int, *, perm: str = "order.approve") -> tuple[bool, list[str]]`：返回 `(可否, 缺失权限仓列表)`；`is_admin` 恒 True。

- [ ] **Step 1 失败测试**：谓词——单仓行命中头仓；跨仓行 A/B 各命中一次且不翻倍（COUNT 验证）；历史 NULL 行回落头仓。权限——同用户在 A 仓 manager、B 仓 staff：`has_permission(uid,'order.approve',warehouse_code='A')` True / `'B'` False；缓存分键不串；无角色仓 False。`can_act_on_order` 缺 B 权限→`(False, ['B'])`；is_admin→`(True, [])`。
- [ ] **Step 2–4** 红→实现→绿；`pytest tests/test_route_permission_matrix.py -v` 回归（不改断言）。
- [ ] **Step 5 Commit**（ARCHITECTURE 鉴权章节同步）：`feat(perm): 订单参与仓谓词与目标仓权限解析`。

### Task 5: 发货跨库提交协议（P0-01 核心）

**Files:**
- Modify: `blueprints/store_ordering_pure.py`（`_ship_order_partial_impl`、`ship_order_full`、`_generate_delivery_no`）
- Modify: `blueprints/store_ordering.py`（`ship_order_route`：op_no 下发/回传、操作仓=会话仓、旧 qty/空表单入口限定本仓）
- Test: `tests/test_ship_op_protocol.py`、扩展 `tests/test_store_ordering_dc_route.py`

**Interfaces:**
- Consumes: Task 1 列/表、Task 3 解析器、Task 4 `order_participants`。
- Produces:
  - `ship_partial(master_conn, wh_conn_factory, *, order_id, operator_dc_code, op_no: str, line_qty: dict[int, float], shipped_by) -> dict`：协议——① 校验 line_qty 全部行的 `source_warehouse_code == operator_dc_code`，否则 ValueError；② 仓库库 `BEGIN IMMEDIATE` + 条件原子扣减 `UPDATE items SET quantity=quantity-? WHERE id=? AND quantity>=?`（`changes()==0`→库存不足 ValueError）+ `stock_movements(op_no=?)`；③ master `BEGIN IMMEDIATE`：INSERT 批次头+`store_order_delivery_items(op_no)`，`op_no` UNIQUE 冲突且同 op_no→幂等返回既有批次；④ 行累计 `UPDATE store_order_items SET shipped_quantity=shipped_quantity+?, ship_op_no=? WHERE id=? AND (ship_op_no IS NULL OR ship_op_no<>?)` + 上限守卫 `shipped_quantity+? <= quantity`；⑤ **锁内重读全部行**聚合：全部 `shipped>=quantity` → 条件迁移 `UPDATE store_orders SET status='shipped' WHERE id=? AND status='approved'`，`changes()==1` 才写 history+通知。
  - `generate_op_no() -> str`（uuid4 hex）。
  - 孤儿对账 `reconcile_orphan_shipments(master_conn, wh_conn_factory) -> list[dict]`：仓库存量 `op_no` 流水无对应批次 → 生成红字冲销 movement + 返回清单（告警载体）。

- [ ] **Step 1 失败测试**（关键场景逐条）：(a) 正常单仓发货扣减/批次/累计正确；(b) **同 op_no 重试只扣一次**（第二次返回同批次、库存不再减）；(c) 他仓行 ID 混入→拒绝且不扣任何库存；(d) master 批次 INSERT 前故障注入（monkeypatch 抛错）→仓库库已提交、`reconcile_orphan_shipments` 检出孤儿并红字冲销、库存恢复；(e) 同仓两单竞争最后一件库存→恰一单成功（条件扣减验证）；(f) 两仓并发各自发齐最后一行（顺序交错调用）→shipped 恰一次、通知恰一条；(g) 旧入口：空表单全发/旧 qty 字段仅作用本仓未发行；(h) 平台管理员未显式操作仓→拒绝。
- [ ] **Step 2** FAIL。
- [ ] **Step 3 实现**（替换 `:1831-1856` 读-算-写绝对值路径；`:1885` 快照聚合改锁内重读；全发 helper `:1708` 加 `operator_dc_code` 参数）。
- [ ] **Step 4** 全绿 + 既有发货测试回归（`test_store_ordering_dc_route.py` 允许因 op_no 参数新增而适配 fixture，不改语义断言）。
- [ ] **Step 5 Commit**（ARCHITECTURE 请求生命周期/订货章节同步）：`feat(ship): 跨库提交协议——op_no 幂等/条件原子扣减/锁内聚合`。

### Task 6: 收货镜像协议 + 取消竞争修复

**Files:**
- Modify: `blueprints/store_ordering_pure.py`（收货 `:2087-2242` 路径、`cancel_order` `:2298-2321`）
- Test: `tests/test_receipt_op_protocol.py`、扩展 `tests/test_store_ordering_store_route.py`

**Interfaces:**
- Produces:
  - `receive_partial(master_conn, store_conn_factory, *, order_id, op_no, item_qty: dict[int, float], received_by) -> dict`：master 先 INSERT receipt（op_no UNIQUE，冲突同 op_no→幂等返回）→ 门店库条件原子加库（`stock_movements.op_no`）+ 行累计 `fulfilled_quantity=fulfilled_quantity+?` 守卫 `+? <= shipped_quantity` → 回写 receipt 应用标记；全收齐且 `status='shipped'` → 条件迁移 delivered 一次。
  - `cancel_order(...)` 改造：同事务 `UPDATE store_orders SET status='cancelled', … WHERE id=? AND status IN ('pending','approved')`，`changes()==0` → 抛「状态已变更」；已发货不可取消守卫不变；取消需 `can_act_on_order`（D12）。

- [ ] **Step 1 失败测试**：(a) 正常收货累计/入库正确；(b) 同 op_no 重试只入库一次；(c) master receipt 落库后门店库故障→重放安全（receipt 未应用标记，重试完成入库且标记）；(d) 收超发量→拒绝；(e) approved 期按行收实发量仍可行（`:1940` 能力不破坏）；(f) 取消与发货竞争：先发货提交再取消→取消失败订单仍 approved；(g) 收齐→delivered 恰一次。
- [ ] **Step 2–4** 红→实现→绿。
- [ ] **Step 5 Commit**（ARCHITECTURE 同步）：`fix(order): 收货幂等协议与取消锁内条件迁移`。

### Task 7: 建单/目录/购物车去 DC 化 + 路由固化

**Files:**
- Modify: `blueprints/store_ordering_pure.py`（`get_or_create_cart`、目录查询 `:537-575`、`submit`）、`blueprints/store_ordering.py`（`/catalog` 去选择器、提交路由）
- Modify: `config.py`（`MULTI_WAREHOUSE_FULFILLMENT = False` 开关）
- Modify: `templates/store_ordering/catalog.html`（去 DC 选择器 + 草稿键 v2「用户+门店」）、`templates/store_ordering/cart.html`（消费清理同步改键）
- Test: `tests/test_catalog_multi_dc.py`、`tests/test_submit_routing.py`、扩展 `tests/test_store_ordering_store_route.py`

**Interfaces:**
- Consumes: Task 2 `get_routing`、Task 3 解析器。
- Produces:
  - 目录 = 既有四门禁 ∩ 已配置路由 ∩ 非专属品（D10）；价格/倍数按**路由来源仓**解析（替代车头仓 `:316/:333`）。
  - `submit_cart(...)` 改造：开关关时行为与现状恒等（单仓）；开关开时逐行 `get_routing`，缺失→整单退回 ValueError（列出品项清单）；提交时刻重解析路由+履约行+价格+倍数并**固化**到行（`source_warehouse_code`、`dc_item_id`）；头字段按 D8 规则填（主仓=行数最多、平局 code 字典序小）；购物车 `dc_warehouse_code` 写 NULL。
  - 价格/倍数与购物车快照不一致 → 返回差异清单要求门店确认（契约字段 `price_changed: [{canonical_id, old, new}]`，前端确认后带 `confirm_price_change=1` 重提交）。

- [ ] **Step 1 失败测试**：(a) 开关关：目录/提交行为与现状逐字节一致（复用既有 store_route 测试全绿）；(b) 开关开：双 DC 品项同现一个目录；未路由品项不可见；专属品不可见；(c) 提交跨仓单：行来源正确、头字段=主仓规则正确（含平局）；(d) 一行缺路由→整单退回+品项清单；(e) 提交后改路由→已建单来源不变（D6 固化）；(f) 价格变化差异确认契约；(g) 购物车 NULL 兼容列、每用户每门店一车语义保持。
- [ ] **Step 2–4** 红→实现→绿；前端草稿键手测项写入 PR Checklist（同用户切两门店草稿隔离、回退不重复加购、旧 DC 键草稿丢弃提示）。
- [ ] **Step 5 Commit**（ARCHITECTURE 订货章节同步）：`feat(order): 目录去 DC 化与提交路由固化（开关控制）`。

### Task 8: 审批/拒绝/取消守卫 + 订单板视角分离与仓库筛选

**Files:**
- Modify: `blueprints/store_ordering.py`（`review_order_route`、`_render_order_board`、取消路由、`_assert_order_belongs_to_current_dc`→参与仓判定）
- Modify: `blueprints/store_ordering_pure.py`（`list_orders`/`count_orders_by_status`/详情：审批整单、发货本仓行集）
- Modify: `templates/store_ordering/order_board.html`、订单详情模板（他仓行只读+仓标签、缺权提示、参与仓摘要、进度徽标）
- Test: `tests/test_store_ordering_board.py`（扩展）、`tests/test_approve_multi_dc.py`（新建）

**Interfaces:**
- Consumes: Task 4 `can_act_on_order`、`order_participants`、参与仓谓词。
- Produces:
  - 批准/拒绝/取消路由：`can_act_on_order` 不通过→403 + 页面显示缺失仓；通过→整单一次迁移（条件式）。
  - 订单板数据范围：admin 全量+`?warehouse=` 生效；非 admin 锁本仓、参数忽略（沿用 `?dc=` 守卫测试模式）；可见集=参与仓谓词。
  - 审批视角详情：全部行+仓标签；发货视角：`source_warehouse_code=会话仓` 且有剩余可发量的行。
  - 无全权审批人→平台管理员站内告警（复用 `notifications` 既有写入 pure 函数）+ 订单板徽标。

- [ ] **Step 1 失败测试**：(a) A 仓 manager 无 B 权限→批准 403+缺失仓提示；(b) 双权限→整单批准一次；(c) is_admin 绕过；(d) 拒绝/取消同守卫（D12）；(e) admin `?warehouse=B` 过滤正确、DC 用户参数被忽略；(f) 跨仓单在 A、B 两 DC 板上都出现且各只见本仓发货操作面；(g) 页签计数用参与仓口径不翻倍；(h) 无全权审批人→告警恰一条（重复建单不重复告警）；(i) 既有 board 测试断言全部保持。
- [ ] **Step 2–4** 红→实现→绿。
- [ ] **Step 5 Commit**（ARCHITECTURE 订单板章节同步）：`feat(board): 审批整单/发货本仓视角分离与仓库筛选`。

### Task 9: 报表、健康报表与补货/采购路由化

**Files:**
- Modify: `blueprints/store_ordering_pure.py`（`compute_store_order_report`：明细数量按行来源、订单数去重集、运费头级展示禁跨仓求和 D11）
- Modify: `reports_health_pure.py`（`:388/:393` 改参与仓谓词）
- Modify: `blueprints/replenishment_data.py`（`:650-769`：门店需求只拉动路由来源仓；修 `dc_orderable[canonical_id]` 多 DC 覆盖 bug；可订开关从来源仓解析）
- Modify: `blueprints/procurement_digest_pure.py`（`:110-138` 按路由去重 DC 行）
- Test: `tests/test_report_multi_dc.py`、`tests/test_replenishment_routing.py`、扩展 `tests/test_summary_*.py` 相关

**Interfaces:**
- Consumes: Task 2 `get_routing`、Task 4 谓词。
- Produces: `compute_store_order_report` 返回结构不变（新增仓维度时运费列按 D11 显示全单值，禁止 SUM 跨仓）。

- [ ] **Step 1 失败测试**：(a) 同单 A 两行+B 一行+运费 10：全局订单数=1、全局运费=10、A/B 数量各自正确；(b) 仓维度不出现运费求和；(c) 健康报表：跨仓单在 B 仓积压/在途不再为 0；(d) 双 DC 镜像同 canonical、路由=B→采购拉动仅 B 一次、`dc_orderable` 不被覆盖；(e) 分类+日期+仓组合过滤一致；(f) 既有 summary 测试断言不改全绿。
- [ ] **Step 2–4** 红→实现→绿。
- [ ] **Step 5 Commit**（ARCHITECTURE 报表/采购章节同步）：`fix(report): 跨仓报表粒度与健康/补货/采购路由化`。

### Task 10: MCP、通知矩阵、调货适配与验证项收口

**Files:**
- Modify: `mcp_server/service/store_order.py`（三处可见性收敛到共享谓词；detail 行加 `source_warehouse_code`；批次加 `warehouse_code`）、`mcp_server/protocol/tools/store_order.py` + `tools/__init__.py`（description 同步 ACL 扩权语义）
- Modify: `docs/integrations/dailycheck-mcp/`（同步，铁律）
- Modify: `blueprints/store_ordering_pure.py`（`notify_order_event` 收件改参与仓+有效权限解析，逐事件方向按 spec §11 矩阵；企微模板不动）
- Modify: `blueprints/dc_transfer_pure.py`（`:202` 建单 INSERT 补 `source_warehouse_code=头仓`）
- Verify-only: `blueprints/outbound.py`（非主仓回链不 403）、`bot/tools.py`（口径）、`store_ordering_pure.py:846`（在途不重复计）
- Test: 扩展 `tests/test_store_order_mcp.py`、`tests/mcp_server/test_service/test_store_order.py`、`tests/test_dc_transfer.py`、`tests/test_notifications_route.py`

**Interfaces:**
- Consumes: Task 4 谓词（MCP 侧以 SQL 片段复用，不 import Flask 上下文——对照 `mcp_server/service/store_order.py` 现有纯 SQL 风格）。

- [ ] **Step 1 失败测试**：(a) B-only token 见参与单且 list/detail/count/total/truncated 一致（B 读全单含 A 行——ACL 扩权显式断言）；(b) 通知方向矩阵逐事件：提交→参与仓审批权人+企微一条；批准/拒绝→门店；全发齐→门店恰一条；部分收货→门店+相关来源仓；取消→参与仓+门店；收件人按权限解析非角色名；(c) 调货建单行来源=头仓、既有 `test_dc_transfer.py` 断言零修改全绿；(d) outbound 非主仓回链 200；(e) bot 列表跨仓单状态口径正确；(f) `tools/list` 注册一致性测试更新 description 快照。
- [ ] **Step 2–4** 红→实现→绿。
- [ ] **Step 5 Commit**（ARCHITECTURE MCP/通知章节 + `docs/integrations/dailycheck-mcp/` 同步）：`feat(mcp): 参与仓可见性收敛与跨仓通知矩阵`。

### Task 11: 验收矩阵汇总测试 + 发布开关演练 + 文档收口

**Files:**
- Create: `tests/test_multi_warehouse_acceptance.py`（spec §13 验收矩阵端到端：建跨仓单→双仓并行发货→交错收货→delivered；迁移重跑保来源；灰度开关关=存量恒等）
- Modify: `docs/ARCHITECTURE.md`（全量章节收口：数据模型/订货/鉴权/MCP/报表/部署 T1-T3）
- Modify: `docs/superpowers/specs/2026-10-29-multi-warehouse-fulfillment-design.md`（状态改「已实施」）
- Test: 全量 `pytest`、`ruff check .`

- [ ] **Step 1** 写验收矩阵端到端测试（红——依赖前序 Task 全部合入后转绿）。
- [ ] **Step 2** `pytest` 全量 + `ruff check .` 全绿；`tests/known_failures.txt` 不得扩张（红线）。
- [ ] **Step 3** 关键页面手测（`/`、`/catalog`、`/review`、`/admin/orders`、`/outbound`、路由 admin 页）记录进 PR `## 报告 1/4`。
- [ ] **Step 4 Commit**：`test(mw): 多仓履约验收矩阵与文档收口`。
- [ ] **Step 5** 提 PR（三段式 body；`## 生产侧事项` 写明：T1 发布→迁移执行→播种核对 pending 清单→确认双实例 T1+→开 `MULTI_WAREHOUSE_FULFILLMENT`；回滚目标版本必须 T1+）。

---

## 依赖与派发波次（Orca coordinator）

```
Wave 1: Task 1（schema）
Wave 2: Task 2（播种）∥ Task 3（解析器）∥ Task 4（谓词+权限）   [互不依赖]
Wave 3: Task 5（发货协议）→ Task 6（收货/取消）                  [6 依赖 5 的 op_no 惯例]
Wave 4: Task 7（建单/目录）∥ Task 8（审批/订单板）               [均依赖 2/3/4；7、8 文件重叠少可并行，store_ordering.py 冲突由后合入者 rebase]
Wave 5: Task 9（报表/补货）∥ Task 10（MCP/通知/调货）
Wave 6: Task 11（验收收口）
```

每 worker 交付：测试全绿证据 + `worker_done --outcome`。coordinator 逐 Task 结算后才放行下一波；`store_ordering_pure.py` 为共享热点文件，同波内最多一个 worker 持有写权（Wave 3 串行、Wave 4 中 Task 8 若需改 pure 层则等 Task 7 合入）。
