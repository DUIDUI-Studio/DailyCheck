# 多仓库履约设计：单订单跨仓发货（自动路由）— v2

- 日期：2026-10-29（v2 修订同日晚于 codex 评审）
- 分支：dailycheck-dev/llm-deepseek-account-model-5（worktree 双仓库功能逻辑）
- 状态：**v2 已消解评审全部 P0/P1/P2 + Eric 三项产品拍板，待 Eric 过目后进实施计划**
- 评审：`docs/superpowers/reviews/2026-10-29-multi-warehouse-fulfillment-codex-review.md`
  （1 P0 / 10 P1 / 3 P2，结论「需修订后实施」）
- v1→v2 变更：D5/D6 扩充、新增 D10–D14；P0-01、P1-01/02/03/04/05/06/07/08/09/10、
  P2-01/02/03 逐项消解（映射见 §11）；修正 v1 两处事实错误（`shipped_quantity` 语义、
  通知方向）。

## 1. 问题与目标（同 v1）

一张门店订单硬绑单一 `dc_warehouse_code`，多仓库并存时订货/发货被「先选仓」绑架。
目标：**多仓库不影响订货和发货体验** —— 门店无感（不选仓）、仓库人员无感（不选发货仓，
系统按品项路由）、一单多仓并行履约。

## 2. 决策集

| # | 决策 |
|---|------|
| D1 | 同一品项固定归属唯一发货仓，确定性映射；不做择仓/拆量/库存回退 |
| D2 | 一单多仓批次：不拆单，仓下沉到明细行 |
| D3 | 路由来源：显式配置表 `item_routing`，与绑定存在性解耦 |
| D4 | 审批视角整单（他仓行只读+仓标签）；发货视角仅本仓来源行 |
| D5 | 批准：操作者须对**全部参与仓**持 `order.approve`（`is_admin` 绕过）；缺权显示所需仓清单 |
| D6 | 提交时任一行路由缺失 → 整单退回，逐行列明；路由提交后固化，改配置不影响已建单 |
| D7 | 订单板加仓库筛选：admin 生效；非管理员锁本仓、参数忽略 |
| D8 | 头字段 `dc_warehouse_code` 保留为兼容/展示字段：单仓填该仓；跨仓填主仓（行数最多；平局取**仓 code 字典序小**——v2 修正，消 P2-03 稳定键缺口）。新查询不得依赖 |
| D9 | 不做：库存预留/锁定、自动择仓求解、改固定品类、改扇出架构。**短事务写锁不在禁止范围** |
| D10 | **专属品不进 DC 订单**（Eric 拍板）：`is_store_exclusive` 品项不进 DC 目录、不参与提交、不播种路由 |
| D11 | **运费仅全单展示**（Eric 拍板）：`shipping_fee` 头级事实，报表展示全单值，**禁止按仓求和**；不摊分 |
| D12 | **拒绝/取消同审批**（Eric 拍板）：整单拒绝、审批后取消均需全参与仓 `order.approve` 权限 |
| D13 | 一期显式**仅支持租户 1**：路由管理/提交/发货接口校验并拒绝其他租户（失败关闭，不装作支持） |
| D14 | 灰度与回滚策略见 §6（跨仓创建开关后置于旧写者全部退场） |

## 3. 数据模型（全部走版本化迁移注册表，消 P1-02）

新增 schema 一律进 `migrations/` 注册表（架构铁律 `ARCHITECTURE.md:1255`），
一次性脚本仅指生产执行该迁移的操作载体；新仓初始 DDL 同步更新。

**master.db：**
```sql
ALTER TABLE store_order_items ADD COLUMN source_warehouse_code TEXT;      -- 行来源仓（建单必填）
ALTER TABLE store_order_items ADD COLUMN ship_op_no TEXT;                 -- 最近一次发货操作号（幂等守卫）
ALTER TABLE store_order_deliveries ADD COLUMN warehouse_code TEXT;        -- 批次归属仓
CREATE TABLE store_order_delivery_items (…);                              -- 新表：批次行明细（DDL 见 §7）
CREATE TABLE item_routing (
    tenant_id INTEGER NOT NULL,
    canonical_id INTEGER NOT NULL,
    fulfillment_warehouse_code TEXT NOT NULL,
    updated_at TEXT NOT NULL, updated_by INTEGER,
    PRIMARY KEY (tenant_id, canonical_id), …
);
```

**仓库 db（每仓文件）：**
```sql
ALTER TABLE stock_movements ADD COLUMN op_no TEXT;   -- 外部操作号，订单发货/收货回链
```

- **回填仅填空值**（v2 修正，消 P1-02）：
  `UPDATE store_order_items SET source_warehouse_code = (SELECT dc_warehouse_code FROM store_orders o WHERE o.id=order_id) WHERE source_warehouse_code IS NULL`；批次同理。
  重跑不覆盖已固化来源。验收含「迁移→建跨仓单→再迁移」顺序测试。
- **调货最小适配**（消 P1-02「零改动」矛盾）：调货建单走统一建单层显式填
  `source_warehouse_code =` 头仓、批次同仓；业务页面/断言语义不变，回归全绿锁死。
- **购物车**：`PRAGMA foreign_keys=ON` 已强制（`db/__init__.py:39/:59`）→ 哨兵值不可行。
  迁移以标准 SQLite 重建表模式将 `store_order_carts.dc_warehouse_code` 改为**可空**
  （表行数小，重建成本可忽略），新代码写 NULL；该列**不再参与任何查询/路由/展示**，
  历史非空值仅保留原样作展示。车语义维持**每用户每门店一车**
  （现 UNIQUE(user_id, store_warehouse_code)，v1 误写「一店一车」，v2 更正）。
- 历史异常数据门：回填前校验「`shipped_quantity`/`fulfilled_quantity` 违反
  `0 ≤ 已收 ≤ 已发 ≤ 订购`」的存量行，列入迁移报告人工处置，不静默修数。

## 4. 路由播种与履约行解析（消 P1-04、P1-05）

**播种（一次性迁移内，可审计清单）**：
- 候选仅：同租户（一期=租户 1）、`warehouse_type='distribution_center'` 的仓。
  研发/门店仓镜像**不得**成为路由目标（`store_ordering.py:1066` 发货入口仅 DC）。
- 绑定有效 = `items.is_active=1 AND is_alias=0`（真实列；v1 的 `binding_active` 不存在，更正），
  且 `canonical_items` 为 active。
- 唯一合格 DC 持有 → **自动启用** + 生成「播种审计清单」（启用/歧义/缺失三段，缺失与歧义进
  admin 待配置页）；多 DC 持有 → 不猜，进待配置。
- 播种后**新扇出/claim 不自动改路由**：路由仅管理接口可变更（D6 固化原则的镜像面）。
- 门店专属品不播种（D10）。

**履约行解析器（新增 pure 函数，禁用首行 fetchone）**：
在指定来源仓内按 `canonical_id` 解析扣库行：有效绑定行必须**唯一**；多候选、单位不可比、
存在阻断冲突 → 返回明确错误（提交/发货时失败关闭）。价格、起订倍数、`dc_item_id` 回填、
详情展示**共用同一解析结果**。已建单来源固化；本地行解绑/停用 → 该单发货报错并可诊断，
**禁止静默换仓/换行**，修复走路由管理页 + 受审计处置。

## 5. 目录与提交（消 P1-06 + D10）

目录 = 既有四道门禁（主数据 active、来源仓 `is_orderable=1`、本店启停、起订倍数）**∩ 已配置路由**。
- 未路由品项不进目录（默认不显示，避免「能加购、提交才报错」）。
- 专属品不进 DC 目录（D10）。
- 提交时一次性解析并**固化快照**：路由、履约行 ID、单位/倍数、价格同刻解析；购物车阶段
  快照仅用于展示，提交重解析；路由变更导致价格/倍数变化 → 提交返回差异确认契约（门店
  重确认后才建单）。本店绑定门禁维持现状口径。

## 6. 发布、灰度与回滚（消 P1-03，D14）

分三段：
1. **T1 兼容发布**：新代码全量上线（含 MCP、bot、后台脚本、订单板），跨仓创建开关
   （config 项 `MULTI_WAREHOUSE_FULFILLMENT`，默认关）关闭 → 行为与存量恒等（全部单仓）。
2. **T2 开跨仓**：确认蓝绿双实例同为 T1 或更高、无旧写者承流后，开跨仓创建。首张跨仓单
   产生后**回滚目标版本必须仍是 T1+**（能识别行级来源）；严禁回滚到 T1 前版本。
3. **T3 收敛**：兼容字段 `dc_warehouse_code` 降级为纯展示；报表/MCP/健康报表谓词切换完成。

灰度期新旧同池（`switch.sh` 5%/50%）时**绝不开启跨仓创建**——旧实例按头仓扣所有行的风险
（P1-03）由此关闭。本设计不改部署流水线（红线不动）。

## 7. 跨库提交协议：发货/收货幂等与恢复（消 P0-01，本 v2 最重条目）

**发货（单来源仓一次一批，平台管理员也须显式定操作仓，一单可多批）**：
1. 服务端以**当前 DC 上下文仓**为唯一操作仓；表单行 ID 非本仓来源 → 拒绝。
2. 生成 `op_no`（uuid）。
3. 仓库 db：`BEGIN IMMEDIATE` → **条件原子扣减**
   `UPDATE items SET quantity=quantity-? WHERE id=? AND quantity>=?`（替代 v1/现状的读-算-写绝对值，
   防并发覆盖）→ 写 `stock_movements(op_no)` → 校验本仓行累计不超发（重读行，锁内）→ COMMIT。
4. master db：`BEGIN IMMEDIATE` → **先 INSERT 批次头+批次行明细**。
   **勘误（2026-10-29，Task 5 实施裁决）**：`store_order_delivery_items.op_no` 为单列 UNIQUE
   （Task 1 已合入并有测试锁死），与「多行共享一个请求级 op_no」矛盾。实施采用**行级派生键**
   `line_op_no = f'{op_no}:{order_item_id}'`（uuid4 hex 无冒号，首 ':' 分割无歧义），同时写入
   `store_order_delivery_items.op_no` 与仓内 `stock_movements.op_no`；请求级 op_no 以
   `split(':',1)[0]` 还原。幂等路径：行明细 INSERT 撞 IntegrityError → 按 line_op_no 精确查回
   既有 delivery_id → 校验同订单且前缀=本请求 op_no → 幂等返回既有批次（不重复扣减）。
   → `UPDATE store_order_items SET shipped_quantity=shipped_quantity+?, ship_op_no=? WHERE id=? AND ship_op_no IS DISTINCT FROM 本 op_no`（累计式，带守卫）
   → §9 锁内重聚合定状态 → COMMIT。
5. 崩溃恢复：仓库 db 有 `line_op_no` 流水但 master 无对应明细行 → 孤儿，**库存不动**，对账作业
   按 line_op_no 精确等值匹配，以红字冲销流水（生成反向 movement + 待人工确认告警）；
   客户端重试**必须复用原请求级 op_no**（发货表单携带幂等键，Web 表单由服务端在页面生成、
   提交回传）——派生键确定性保证重试命中同一 UNIQUE 键。孤儿对账是兜底，不是主路径。
6. D9 澄清：不预留、不锁定库存不变；上述短事务锁不是预留。

**收货（镜像协议）**：门店 db 条件原子加库 + `op_no` 流水；master 先写 receipt 记录（op_no UNIQUE）
再提交门店库改为——**先 master INSERT receipt（幂等键）→ 门店库原子加 → 回写 receipt 应用标记**；
中间崩溃 → 门店库未加、receipt 未应用，重放安全；「已应用未回写」由对账作业补标。
`fulfilled_quantity` 改为累计式条件 UPDATE（`fulfilled_quantity=fulfilled_quantity+?` + 上限守卫
`fulfilled_quantity+? <= shipped_quantity`，v2 修正：上限是**已发量**而非订购量）。

**数量不变量**：`0 ≤ fulfilled(已收) ≤ shipped(已发) ≤ quantity(订购)`，全部写入路径带守卫。

**批次明细表（消 P2-01）**：
```sql
CREATE TABLE store_order_delivery_items (
    delivery_id INTEGER NOT NULL, order_item_id INTEGER NOT NULL,
    warehouse_code TEXT NOT NULL, item_id INTEGER NOT NULL,      -- 仓内实扣行
    quantity REAL NOT NULL, op_no TEXT NOT NULL UNIQUE,   -- 存行级派生键 line_op_no（见上文勘误）
    created_at TEXT NOT NULL, …
);
```
批次 `delivered_at` 维持整单派生语义（现状 `store_ordering_pure.py:2233`），不宣称批次级签收。
收货协议（Task 6）沿用同一派生键约定：receipt 幂等键 = `f'{op_no}:{order_item_id}'`。

## 8. 并发状态聚合（消 P1-01）

- 每次发货/收货在 **master `BEGIN IMMEDIATE` 事务内重读全部行**后聚合判定状态，
  不再用函数入口旧快照（现状 `:1885`）。
- 状态迁移全部条件式：`UPDATE store_orders SET status='shipped' WHERE id=? AND status='approved'`，
  `changes()=1` 才写事件与通知 → **两仓并发发最后一行，恰好一次转 shipped、恰好一条通知**。
- v1 事实更正：防超发累计列是 **`shipped_quantity`**（`db/__init__.py:1094`），
  `fulfilled_quantity` 是已收量。v1 §7 写错，v2 全篇更正。
- 「shipped 且无已发数回落订购量」的历史 v2 兼容仅作用于可识别历史数据（迁移门 §3），
  新数据异常不得被当作已发。
- **维持既有能力**：approved 期间可按行收实发量（`:1940/:1968/:2079`），不加「整单 shipped 才可收」门禁。
- 取消/拒绝（D12）：同事务锁内校验「未发货」+ 条件式 `WHERE status IN ('pending','approved')`，
  消除检查后竞争。

## 9. 权限（消 P1-07）

- `permissions.py` 新增**目标仓解析**能力：`has_permission(user, perm, *, warehouse_code=None)`，
  缺省当前仓；缓存按 (user, warehouse, tenant) 分键；解析失败关闭。内置/租户角色规则与现状同源。
- 审批/拒绝/取消（D5/D12）：对订单**全部参与仓**逐一解析 `order.approve`，任一缺失 → 动作禁用 +
  UI 显示缺失仓清单；`is_admin` 绕过。
- 展示门禁 `can_review`、看板守卫（`permissions_manifest.py:351`）与模板同步改权限点语义，
  但**不收紧 DC staff 发货能力**：发货路由保持 `require_role("staff")` 现状（既有非白名单行为
  不变更），仅增加「操作仓=会话仓、行 ID 属本仓」的服务器端约束。
- 旧发货兼容入口一并收口：无明细字段自动全发（`store_ordering.py:1133`）与旧 `qty` 应用所有
  未发行（`:1117`）→ 改为**仅作用于操作仓的本仓未发行**；全发 helper（`pure:1708`）接收来源仓参数。
- 动作矩阵（谁/哪仓/什么状态/哪些入口）写入实施计划，测试覆盖：伪造他仓行 ID、空表单、
  旧 qty 字段、平台管理员未定仓、仅部分仓授权用户批准。

## 10. 查询、报表与外部消费者（消 P1-08、P1-09、P1-10）

**统一「订单参与仓」谓词**（新 pure 工具，全站复用，EXISTS 防 JOIN 翻倍）：
```sql
EXISTS (SELECT 1 FROM store_order_items soi WHERE soi.order_id=so.id
        AND COALESCE(soi.source_warehouse_code, so.dc_warehouse_code) = :wh)
```
头仓仅作历史 NULL 兜底与展示标签，不再宣称可替代参与仓。
- 同步改造清单：订单板列表/页签计数（`pure:1498`）/深链守卫/详情可见性/付款凭证附件下载
  （`store_ordering.py:738/:744`）、`/outbound` 订单回链（现读当前仓 `stock_movements`，
  物理天然隔离——**不改驱动方式**，仅验证非主仓回链不 403）、`reports_health_pure.py:388/:393`
  （积压/在途/履约周期按参与仓）、MCP `store_order.py` 三处独立可见性（`:130/:160/:167/:186`）
  收敛到同一谓词、`list_orders` 计数/分页/total/truncated 一致性、bot `tools.py` 查询口径验证。
- **MCP ACL 语义扩展（显式声明）**：token 命中任一参与仓 → 可读**全单**（含他仓行、金额、
  收货历史）。这是扩权，须写进工具 description + 集成文档并测试（B-only token 读全单成立）。
- **订货报表粒度**（D11）：明细数量按 `source_warehouse_code` 分组；订单数 = 去重订单集
  （全局一次、参与仓维度各计一次）；运费 = 头级展示值，**禁止跨仓求和**（仓维度列显示「—」
  或全单值+脚注，实施计划定 UI 细节）。实现分「过滤后明细集合」与「去重订单集合」两查询。
- **补货/采购纳入影响面（v1 误判「无影响」，更正）**：门店需求只拉动**路由来源仓**；
  可订开关从来源仓解析（`replenishment_data.py:695/:732` 的 `dc_orderable[canonical_id]`
  多 DC 覆盖 bug 一并修复）；采购邮件按路由去重 DC 行（`procurement_digest_pure.py:110`）。
  非来源仓镜像库存保留自身水位逻辑，不承接门店需求。验收：双 DC 镜像同 canonical、
  路由=B → 采购拉动仅 B 出现一次。

## 11. 通知与前端（消 P2-02、P2-03）

**逐事件收件矩阵**（v1 方向写反，更正；收件人按**有效权限**解析，非角色名）：

| 事件 | 方向/收件人 |
|---|---|
| 提交（pending） | 各**参与仓**审批权限持有人（知会+可审一体），并额外推企微群（现状不变） |
| 批准/拒绝 | 门店侧（现状方向维持） |
| 部分收货 | 门店 + 该批来源仓（原「DC」改「参与仓中相关者」） |
| 全发齐 shipped | 门店；由 §8 唯一迁移触发，恰好一条 |
| 取消 | 参与仓审批人 + 门店（方向按现状语义映射参与仓） |

- **无全权审批人告警**：跨仓单产生时若不存在任何持全部参与仓 `order.approve` 的在职用户
  → 给平台管理员一条站内告警（订单板徽标），防「有单无人可批」死角。
- 调货事件方向维持现状。
- **草稿键 v2**：localStorage 键从「用户:DC」升版「用户+门店仓」；catalog 与 cart 消费清理
  同步改（`catalog.html:185/:186`、`cart.html:79-81`）；旧 DC 键草稿**丢弃并提示**；恢复时
  校验品项仍有效（失效品项出提示清单）。bfcache/pageshow 联动维持既有契约。
  门店订单列表（`orders.html:95`）跨仓单显示参与仓摘要。
- 门店详情页出现多批次时列批次来源仓名（物流事实）；目录/购物车全面去 DC 选择器。

## 12. 影响矩阵 v2

**必改**：`store_ordering_pure.py`（路由解析、跨库协议、锁内聚合、谓词收敛）、
`store_ordering.py`（catalog/cart 去 DC、审批/拒绝/取消/发货守卫、幂等键下发）、
`permissions.py`（目标仓解析）、`migrations/`（注册条目+历史异常门）、
订单板/目录/购物车/订单列表模板、`reports_health_pure.py`、订货报表、
`replenishment_data.py`、`procurement_digest_pure.py`、`dc_transfer_pure.py`（建单适配）、
MCP `service/store_order.py` + 工具描述 + `docs/integrations/dailycheck-mcp/`、
路由 admin 配置页、`docs/ARCHITECTURE.md`（同 commit 铁律）。

**验证性改动（不假设必改）**：`/outbound` 回链 403、bot `tools.py` 口径、
`store_ordering_pure.py:846` 在途量查询不重复计跨仓在途。

**无影响（显式确认）**：盘点/库存/入库回滚/生产 BOM/消耗分析/预测、付款凭证**存储**（附件
权限归入验证项）、权限点名称定义本身（新增的是解析能力）、固定品类、扇出架构、企微模板。

## 13. 验收矩阵（评审 §建议补入 全部采纳，进实施计划逐条落测试）

| 领域 | 必须可观察 |
|---|---|
| 数量与事务 | 任意失败点后按 op_no 可恢复；复用 op_no 重试只扣一次；同仓两单竞争不超扣；取消与发货竞争不产生已取消在途单 |
| 并发状态 | 两仓同发最后一行 → shipped 一次、通知一次；A 发/收时 B 未发 → 单停 approved 且 A 可收；收齐 delivered 一次 |
| 路由 | 研发镜像/跨租户/专属/无本地行/重复绑定/单位冲突各有明确失败关闭结果；改路由不改历史单 |
| 可见与动作 | B-only 用户/token 见参与单且 list/detail/count/附件一致；缺权不能批准；越仓行/空表单/旧 qty 不越权 |
| 迁移与发布 | 跨仓单建成后重跑迁移保留 A/B；调货写来源；旧版本承流/回滚无法误发跨仓单 |
| 报表与补货 | 多仓筛选数量正确、订单不重复、运费不跨仓求和、非主仓健康指标不漏；采购拉动仅路由仓 |
| 前端与通知 | 双门店草稿隔离、回退不重复加购；收件方向矩阵逐事件成立；无全权审批人可诊断 |

测试基线扩展自：`test_store_ordering_{pure,dc_route,store_route,board}`、`test_dc_transfer`、
`test_store_order_mcp`、`tests/mcp_server/test_service/test_store_order`、
`test_route_permission_matrix`、`test_migration_runner`、补货/健康报表测试。

## 14. 遗留（实施计划阶段处理，非设计缺口）

- 运费仓维度展示细节（「—」或全单值+脚注）—— UI 文案级，实施时定。
- 对账作业的运行载体（CLI 命令 vs 调度器挂载）与孤儿红字模板 —— 实施计划定。
- 生产 `master.db` 迁移执行窗口与备份流程 —— 走 workflow.md Prod Agent 蓝绿程序。
