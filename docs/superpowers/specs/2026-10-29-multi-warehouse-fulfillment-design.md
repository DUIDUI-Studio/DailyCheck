# 多仓库履约设计：单订单跨仓发货（自动路由）

- 日期：2026-10-29
- 分支：dailycheck-dev/merganser
- 状态：**待评审**（Eric 要求先交 codex 审核，通过后出实施计划）
- 口径来源：本 session 逐节确认的决策，编号 D1–D9

## 1. 问题与目标

现状：一张门店订单（`store_orders`）硬绑**单一** `dc_warehouse_code`，订货先选配送中心、
发货/审批/超发校验/库存扣减全部以订单头 DC 为准。多仓库并存时，门店订货和仓库发货被
「先选仓」绑架，跨仓需求只能拆成多张单。

目标：**多仓库不影响订货和发货体验** ——

1. 门店端无感：不选仓、不感知路由，一单一提交。
2. 仓库端无感：发货人员不选发货仓，系统按品项自动确定来源仓。
3. 一张订单可由多个仓共同履约，各仓并行发货。

## 2. 业务语义与约束（已拍板）

| # | 决策 |
|---|------|
| D1 | 路由语义：**同一品项固定归属唯一发货仓**，确定性映射，不做择仓/拆量/库存优先回退 |
| D2 | 落地结构：**一单多仓批次**（方案 B）——不拆单，仓下沉到明细行，订单仍是一张 |
| D3 | 路由来源：**显式路由配置表 `item_routing`**（方案 A）——与库存/绑定存在性解耦；因 canonical 扇出本来就允许一品项多仓持有行，「全局唯一绑定」不可依赖 |
| D4 | 审批视角：DC 审批人员看**整张订单**（含他仓行，只读带仓标签）；发货视角：发货人员只见**本仓来源且有待发量**的行 |
| D5 | 审批权限：**全仓权限才可审**（方案 A）——审批人须在订单**所有来源仓**均持有 `order.approve`；`is_admin` 绕过；无权限者可见单但批准按钮置灰并注明缺哪些仓权限 |
| D6 | 归属异常处理：提交时逐行查路由，任一行无配置 → **整单退回**，逐行列出「未配置发货仓」品项；不做部分提交 |
| D7 | 仓库订单页面加**仓库筛选**：admin 生效；非管理员硬锁当前仓、参数忽略（沿用 `?dc=` 既有守卫先例） |
| D8 | 头表兼容字段 `dc_warehouse_code` **保留不删**：单仓单填该仓；跨仓单填**主仓**（行数最多的仓，同点取创建时间早的仓，作排序 tie-break 规则写死）。所有新查询逻辑不得依赖它，仅报表/MCP 旧口径过渡兜底 |
| D9 | 明确不做：库存预留/锁定（沿用「不预留，发货时才扣」）、自动择仓求解、改固定品类、改 canonical 扇出架构 |

## 3. 数据模型（加法式迁移，无破坏）

```sql
-- master.db
ALTER TABLE store_order_items ADD COLUMN source_warehouse_code TEXT;   -- 行来源仓，建单时按路由回填
ALTER TABLE store_order_deliveries ADD COLUMN warehouse_code TEXT;      -- 发货批次归属仓

CREATE TABLE IF NOT EXISTS item_routing (
    tenant_id INTEGER NOT NULL,
    canonical_id INTEGER NOT NULL,
    fulfillment_warehouse_code TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER,
    PRIMARY KEY (tenant_id, canonical_id),
    FOREIGN KEY (canonical_id) REFERENCES canonical_items(id),
    FOREIGN KEY (fulfillment_warehouse_code) REFERENCES warehouses(code)
);
```

- **存量回填**：`UPDATE store_order_items SET source_warehouse_code =
  (SELECT dc_warehouse_code FROM store_orders WHERE id = order_id)`；历史批次同理回填头仓。
  回填后单仓语义与原状恒等 → 存量数据零行为变化。
- **播种 `item_routing`**：迁移脚本扫描各仓 `items` 中 `canonical_id` 绑定行（
  `binding_active` 有效）：唯一持有 → 自动写入配置；多仓持有或门店专属 → 进「待配置」清单
  （admin 页面高亮），**不猜**。
- **调货单 `dc_transfer`**：建单时行级 `source_warehouse_code` = 头表 `dc_warehouse_code`，
  批次同仓 → 调货模块逻辑与页面**零改动**，以回归测试锁死。
- 购物车：`store_order_carts.dc_warehouse_code` 列保留（兼容）但不再作为约束依据，
  一店一车语义不变；session 键 `store_ordering_dc` 废弃。

## 4. 路由与自动匹配

1. 提交订单时逐行解析 `canonical_id → item_routing.fulfillment_warehouse_code`。
2. 任一行缺失 → 整单退回 + 明确错误列表（D6）；路由表在提交后变更**不影响**已建订单
   （行级来源已固化）。
3. 门店目录合并：`/catalog` 去掉 DC 选择器，直接列可订品项。
   - **Assumption（待 codex/产品确认）**：门店目录 = 全部**已配置路由**的 canonical 品项
     ∪ 本店专属品（`is_store_exclusive`）。未配置路由的全局品项对门店不可见，避免
     「能加购、提交才报错」的挫败路径。若产品希望「可见但提交时集中报错」，只改过滤条件，
     不动结构。

## 5. 订货流程（门店端）

- 浏览目录（不选仓）→ 加购（一店一车）→ 提交（付款凭证、期望到货日、备注均不变）。
- 提交时按 §4 归仓、写行级来源、按 D8 填兼容字段；订单号规则不变。
- 门店详情页不显示仓；发货批次列表自然呈现多批次，批注**来源仓名称**（物流事实，非路由泄露）。

## 6. 审批（DC 端，整单视角）

- 订单板/详情页对审批人展示全部行（含他仓行，只读 + 仓标签）。
- 批准动作校验 D5：操作者须对**每个来源仓**持 `order.approve`（或 `is_admin`）。
- 拒绝/取消：维持整单粒度与现有守卫（已发货不可取消不变）。
- 审批通过 = 整单进 `approved`，各仓随即并行可发货。
- 通知 fan-out：审批事件收件人从「头 DC 审批人」扩为「**各来源仓**审批人」去重集合。

## 7. 发货与收货（DC 端，本仓视角）

- 发货入口：仅列 `source_warehouse_code = 当前仓` 且有剩余可发量的行；表单沿用
  `shipped_items[N]=qty` 多值结构，行 ID 天然限定本仓（越仓行 ID 提交 → 服务端拒绝）。
- 库存校验/扣减发生在**行来源仓**的独立库文件；`dc_item_id` 回填逻辑不变（仓从行取）。
- 每次发货生成 `store_order_deliveries` 批次，写 `warehouse_code`。
- 超发校验：`fulfilled_quantity` 按行累计（一行一仓），公式不变。
- 状态聚合：主状态机不变。分批发货期间（含跨仓部分发）停 `approved`；
  **全部行发完 → `shipped`**（此时发一条 `store_order_shipped` 通知，与现有「仅全发齐才通知」
  语义一致）；全部收完 → `delivered`。
- 收货：`store_order_receipts` 不变（行级收货、累计逻辑不动）。

## 8. 仓库筛选（订单页面）

- `/review` 与 `/admin/orders`（同一 `order_board.html` 模板）加「仓库」下拉。
- 权限口径与现 `?dc=` 守卫完全一致：`is_admin` → 全量可筛；非管理员 → 硬锁当前仓、忽略参数。
- 跨仓单在多仓筛选结果中均出现；命中他仓时行明细标注来源仓 + 进度徽标
  （「本仓 3/5 待发 · 全单 8/12 待发」）。
- 数据范围 SQL：订单可见集 = 头 DC 命中 ∪ **存在本仓来源行**（新主判据）。

## 9. 模块影响矩阵

**必改**：`store_ordering_pure.py`（54 处头 DC 依赖 → 行级来源 + 路由解析）、
`store_ordering.py`（catalog/cart 去 DC 化、审批/发货守卫）、`order_board.html` + 详情页模板、
购物车表语义。

**轻改**：`/outbound` DC 发货流水（仓归属取批次 `warehouse_code`）、
`compute_store_order_report`（按仓筛选改行级）、MCP `store_order_list/detail`（可见性 OR
加行来源仓；detail 行加 `source_warehouse_code`；**同步 docs/integrations/dailycheck-mcp/**）、
通知 fan-out、`store_order_submitted` webhook payload（单仓不变，跨仓加行字段）。

**无影响（显式确认）**：调货 `dc_transfer`、盘点/库存/入库回滚/生产 BOM/消耗分析/预测/
采购建议、付款凭证、权限点定义本身、门店 PWA 交互。

**生产侧事项**：master.db 加列 + 存量回填 + 路由播种（一次性脚本，随蓝绿部署执行）；
「待配置」清单需各仓经理补配后跨仓单才能提交（单仓存量单不受影响）。

## 10. 测试计划（TDD，先 RED 后实现）

1. 路由解析纯函数：唯一/缺失/歧义 → 整单退回错误列表。
2. 建单归仓：跨仓订单行级来源正确、兼容字段主仓规则正确。
3. 审批权限矩阵：全仓权限可审 / 缺仓权限置灰 / `is_admin` 绕过。
4. 发货隔离：A 仓 staff 对 B 仓行 ID 提交 → 拒绝；本仓行正常发。
5. 状态聚合：跨仓部分发货停 approved；全发齐才 shipped + 单条通知。
6. 订单板筛选：admin 跨仓筛 / DC 锁本仓 / 参数越权无效。
7. **调货回归锁**：现有 `dc_transfer` 测试全绿不改断言。
8. 迁移幂等：加列/回填/播种脚本重跑无副作用。
9. MCP 可见性：行来源仓命中的单对 token 可见；旧口径单仓单返回形状不变。

## 11. 未决项

- [ ] 门店目录可见性口径（§4 Assumption，待 codex 评审 + Eric 确认）
- [ ] codex 审核意见处理
