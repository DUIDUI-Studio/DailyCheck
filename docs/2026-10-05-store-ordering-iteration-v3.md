# PRD 增量迭代 v3：门店订货

- 日期：2026-10-05（晚）
- 关联 PRD v0：`docs/2026-10-04-store-ordering-prd.md`
- 关联 PRD v1（v2 增量）：`docs/2026-10-05-store-ordering-iteration.md`
- 触发：Eric dev 试用第二批反馈

---

## A. Eric 反馈清单

| # | 问题 / 需求 | 优先级 |
|---|---|---|
| A1 | order_detail 缺 Admin 发货按钮（审批通过后无出库触发） | P0 |
| A2 | order_detail 缺门店收货按钮 | P0 |
| A3 | 支持部分发货（DC 可分多次出库，与部分收货对齐） | P0 |
| A4 | order_detail 缺订单总金额展示 | P0 |
| A5 | 门店下单时**不显示** DC 库存（cart/catalog 移除库存数字） | P0 |
| A6 | DC 审批/出库时**显示** DC 当前库存（每行明细旁标注仓库数 / 订单数） | P0 |
| A7 | 无货仍可发货（移除/减弱库存硬校验，库存不足仅 flash 警告不阻止） | P0 |
| A8 | UI 按钮样式、排版保持一致（与 inventory/restock/outbound 一致） | P0 |

---

## B. 调整后的目标流程（user journey，e2e 测试骨架）

### 门店侧（wh_002）

1. 点「门店订货」→ catalog 浏览可订品项
3. **catalog 卡片不显示库存数字**（A5：仓库数量对门店无意义，门店只关心「能订」）
4. 点卡片 → 弹窗选单位、数量 → 金额预览（qty × 单价）
5. 加入购物车
6. cart 显示购物车总金额；按钮 → 「发送」
7. 提交 → 状态 `pending`

### DC 视角（wh_000）

1. 点「门店订货」→ 自动跳 `/review`
2. 看到 pending 订单
3. 进入 order_detail
4. **每行明细旁显示「配送中心库存 / 订单数 / 已发数」**（A6）
5. **页面显示「订单总金额 ¥ XX.XX」**（A4）
6. 点「通过」→ 状态 `approved`
7. **出现「发货」按钮**（A1）→ 点发货
8. 发货表单：可填部分发货数量（如订单 5 件，仓库 5 件，但发 3 件）
9. **无货也可以发**（A7）：库存不足时 flash「库存不足」警告但允许继续
11. **支持多次发货**（A3）：3 件后再发 2 件，每次累计出货量
12. 全部发齐 → 状态 `shipped`；否则仍为 `approved` 允许继续发

### 门店收货（wh_023）

1. 进入「我的订单」→ 找到 shipped/部分发货的订单
2. 进入 order_detail
3. **出现「收货」按钮**（A2）→ 点收货
4. 收货表单：可填部分收货数量（如发了 5 件，先收 3 件）
5. 多次收货，每次累计
6. 全部收齐 → `delivered`

---

## C. 数据模型调整（最小）

### C.1 复用现有字段 + 调整语义

- `store_order_items.fulfilled_quantity`：现在仅收货累计；改为同时支持「发货累计」+「收货累计」两个维度
- **建议新增**：
    - `store_order_items.shipped_quantity REAL NOT NULL DEFAULT 0` —— 出货累计
    - 保留 `fulfilled_quantity` 字段但语义改为「**收货**累计」（改 README/注释）
- 简化命名：保留 `fulfilled_quantity` 作为收货累计，新增 `shipped_quantity` 作为发货累计

### C.2 状态机调整

- `pending → approved → shipped → delivered`
- 部分发货：`pending → approved → approved（仍可继续发）`
- 部分收货：`shipped → shipped（仍可继续收）→ delivered`
- 不引入 `partial_shipped` 中间态，与 v2 部分收货一致

---

## D. UI 改动

### D.1 order_detail.html 全面重构

布局（顶部→底部）：
1. 标题：订单号 + 状态 pill
2. 顶部信息卡：发起门店 / 配送中心 / 期望到货 / 备注 / 创建时间 / 申请人 / 审批人 / 发货人 / 收货人
3. **总金额 ¥ XX.XX**（A4）—— 显眼位置
4. 明细表（每个品项）：
   - 门店视角（无库存列）：品项 | 单位 | 单价 | 数量 | 已收数 | 本批收货表单
   - DC 视角：品项 | 单位 | 单价 | 数量 | 已发数 | 仓库库存 | 部分发货表单
5. 状态历史（按时间倒序）
6. 收货历史 / 发货历史（如有）

按钮统一：
- 「审批通过 / 拒绝」表单（DC manager+）
- 「发货」按钮（DC staff+，状态 approved）
- 「收货」按钮（storefront manager+，状态 shipped）
- 按钮风格用 `btn-sm` 类（与 review.html 一致）

### D.2 catalog.html 移除库存数字

文件：`templates/store_ordering/catalog.html`

- 卡片头部：`.status-pill ok` 显示「库存 XX」（移除）
- 或改为 `.status-pill` 显示「可订」（更中性）
- 弹窗内「当前库存: XX 件」（移除）
- 用户体验：门店不感知 DC 库存

### D.3 按钮样式统一

- 所有按钮 `class="btn-sm"` 或 `class="btn"`
- 与 inventory/restock/outbound 已有的视觉一致
- flash 错误用 `flash-list` + `flash danger`

---

## E. 数据模型 / 纯逻辑改动

### E.1 `store_order_items` 加 `shipped_quantity`

新增列：`shipped_quantity REAL NOT NULL DEFAULT 0`

`db/__init__.py` `MASTER_SCHEMA` ALTER TABLE（幂等）：
```sql
ALTER TABLE store_order_items ADD COLUMN shipped_quantity REAL NOT NULL DEFAULT 0;
```

### E.2 `ship_order` 改为部分发货

`blueprints/store_ordering_pure.py`：

```python
def ship_order(
    master_conn,
    order_id,
    shipped_quantity_map,  # {order_item_id: qty}  一次发一批
    shipped_by,
    tracking_note=None,
):
    """
    部分/全部发货：
      - 校验订单 status == 'approved'
      - 对每个 (order_item_id, qty)：
        - 校验 qty > 0 且 (qty + shipped_quantity) <= quantity
        - DC 仓 items.quantity -= qty
        - 写 stock_movements（action='门店订货出库', delta=-qty, note 含订单号）
        - UPDATE store_order_items.shipped_quantity += qty
        - UPDATE store_order_items.status = 'partial' if shipped<quantity else 'fulfilled'
      - 全部明细 shipped == quantity → 改 store_orders.status='shipped'
      - 写 store_order_deliveries + store_order_status_history
      - emit_event store_order_shipped
    """
```

**库存不足不再抛异常**（A7）：仅 flash 警告即可

### E.3 路由调整

`blueprints/store_ordering.py`：

```python
@bp.route("/orders/<int:order_id>/ship", methods=["POST"])
@require_warehouse_type("distribution_center")
@require_role("staff")
def ship_order_route(order_id):
    """
    接 POST 参数 shipped_items[] = order_item_id + qty
    兼容旧版：qty 总量在 'qty' 字段一次性发货（兼容）
    """
```

UI 发货表单：每行明细一个 qty input，提交时收集到 `shipped_items[id]=X` 一并 POST。

### E.4 receive_order_item 维持

不变；继续累计 `fulfilled_quantity`。

### E.5 `validate_cart_for_submit` 调整

不再强校验 DC 库存（A7：门店不感知库存）。保留 canonical_id 绑定校验。

---

## F. 待确认问题

| # | 问题 | 选项 | 倾向 |
|---|---|---|---|
| F1 | 部分发货的库存校验：完全不校验 vs 警告但不阻止？ | A) 完全不校验（Eric 默认要求）；B) 校验但允许继续 | A |
| F2 | catalog 卡片是否完全移除库存数字？ | A) 移除；B) 仅显示「可订/不可订」 | A |
| F3 | 部分发货路由是否一次发多品项，还是每个品项单独 POST？ | A) 一张表单多品项批量提交；B) 每品项独立表单 | A（一个表单批量更省操作） |
| F4 | 「已发数 / 仓库库存」是否对 DC 始终可见，还是订单状态变化后才显示？ | A) 始终显示；B) 仅 approved 后显示 | B（pending 时审批人也想看库存） |

---

## G. 端到端测试流（与 user journey §B 一一对应）

每一步用一个测试用例或一个断言覆盖：

1. **门店 catalog 无库存数字**：访问 `/catalog`，断言所有 `.inv-card` 不含「库存」字样
3. **发金额预览**：弹窗输入数量，金额 = qty × 单价
5. **cart 总金额** + 「发送」按钮
7. **提交订单 pending**
8. **DC review 列表显示 pending 订单**
9. **order_detail 显示：**
   - 总金额
   - 每行 DC 库存 / 订单数
   - 「通过 / 拒绝」表单
10. **审批通过 → approved + 出现「发货」按钮**
11. **部分发货 1/3 数量：DC 库存 -1，订单状态仍 approved**
12. **继续发货 1/3：累计 shipped_quantity=2**
13. **最后一次发货 1/3：累计 shipped_quantity=3，状态 shippe状态 sent）
14. **无货场景：DC 库存=0 时发剩余，仍允许（不抛错）**
16. **门店 orders 看到 shipped**
17. **order_detail 出现「收货」按钮**
18. **部分收货 2/3：store 库存 +2**
19. **继续收货 1/3：累计 fulfilled_quantity=3，状态 delivered**

总计 19 步。

---

## H. 开发计划

### T10 数据模型 + 纯逻辑层（**必须先做**）

- `db/__init__.py` MASTER_SCHEMA ALTER 加 `shipped_quantity` 列
- `blueprints/store_ordering_pure.py`：
  - 新增 `ship_order_partial(master, order_id, shipped_items_map, actor_id, note=None)`
  - 保留旧 `ship_order` 兼容路径（一键串号）—— 调用新函数变 partial
  - 调整 `validate_cart_for_submit`：保留 canonical_id 绑定检查，移除库存检查
  - 调整 `mark_order_shipped` / `mark_order_delivered`：自动调用新 partial 函数

### T11 路由 + 模板重做（依赖 T10）

- `blueprints/store_ordering.py`：
  - `ship_order_route` 改为接 `shipped_items[id]=qty` 数组，部分发货
  - order_detail 路由传 `dc_items_info`（DC 当前库存，每行明细） + `total_amount`
- `templates/store_ordering/catalog.html`：
  - 卡片移除 `.status-pill ok` 库存数字；改为「可订」pill
  - 弹窗内移除库存数字
- `templates/store_ordering/order_detail.html`：完整重做
  - 顶部信息卡
  - 总金额
  - 明细表（DC 视角含 DC 库存；storefront 视角不含）
  - 按钮（审批 / 发货 / 收货）
  - 状态历史
  - 发货历史 / 收货历史

### T12 测试（依赖 T11）

- `tests/test_store_ordering_partial.py` 新增（部分发货、部分收货场景）
- `tests/test_store_ordering_e2e.py` 补充 19 步验证
- 跑全量回归 `tests/test_store_ordering_*.py` 61 + 新增 ≥ 8 = 69+

### T13 lint + dev 冒烟

- `ruff check .` 在改动文件无新增错误
- dev 服务重启 + curl 关键页面 + 写最小 E2E HTTP 验证脚本

---

## I. 风险与边界

- `shipped_quantity` 列新增需要 ALTER，dev master.db ALTER 后老订单该列=0（无影响）
- 「无货可发」会让 inventory 出现负值；是否接受？建议接受（业务允许欠货出库）
- 部分发货 + 部分收货同时推进时，`store_order_items.status` 字段怎么写？建议保持 `pending/partial/fulfilled/cancelled`，shipped 时初始化 partial，fulfilled 在 `shipped_quantity==quantity` 时设； fulfilled 在 `fulfilled_quantity==quantity` 时设
- 发货历史表是否复用 store_order_deliveries？建议复用（一次发货写一行 record）

---

## J. 工作流（按 Eric 要求）

1. ✅ 分析需求（已完成）
2. ✅ 写用户体验（§B）
3. ✅ 写测试流（§G）
4. ✅ 制定开发计划（§H）
5. ⏸ **等 Eric 拍板 §F 待确认问题**（特别是 F1 库存校验是否完全移除）
6. 派架构师出 v3 设计
7. 工程师 T10-T13 实施
8. QA 验证 19 步 E2E