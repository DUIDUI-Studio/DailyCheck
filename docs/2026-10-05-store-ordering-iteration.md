# PRD 增量迭代：门店订货

- 日期：2026-10-05
- 关联 PRD：`docs/2026-10-04-store-ordering-prd.md`
- 关联设计：`docs/2026-10-04-store-ordering-design.md`
- 触发：Eric 在 dev 试用反馈 6 类问题

---

## A. 反馈清单（来自 Eric，2026-10-05 试用）

| # | 问题 | 决策 |
|---|---|---|
| A1 | 卡片排版/颜色错位 | catalog 重做，沿用 `inventory.html` `.inv-card` 视觉；移除/替换原 `.grid .cols-3` 内嵌表单 |
| A2 | 品类显示英文（PACKAGING 等） | catalog 与 cart 展示中文品类名（用 `CATEGORY_CODE_MAP` 反查） |
| A3 | 点卡片没弹窗 | 改为 `restock_session.html` 同款：点 `.entry-row` → `.modal-overlay` 弹窗；pill 选单位（base/aux），输入数量，显示金额 |
| A4 | 购物车没总金额 | cart 页每行展示「单价 + 数量 + 小计」，页底加购物车总金额 |
| A5 | 提交按钮改名 | 「提交」→「发送」（与 Eric 描述的门店用户视角一致） |
| A6 | P0 自动入库 + 部分收货 | 提升为 P0：门店收货时门店仓 `items.quantity += qty`、写 `stock_movements`；支持多次部分收货，`fulfilled_quantity` 累计，全部收齐才 `delivered` |

---

## B. 调整后的目标流程（端到端 user journey，作为 e2e 测试的骨架）

1. **门店用户**：点「门店订货」 → catalog 选配送中心
2. **浏览 catalog**：可订品项按 inventory 卡片样式展示，品类中文、单价可见、库存可见
3. **点卡片**：弹窗出现，含品项名、库存、单位 pill（base/aux）、数量输入、实时金额
4. **填数量 + 选单位 → 确认**：卡片显示「已填 X」，并不立即加购物车
5. **页底「加入购物车」按钮**：把本页所有「已填」品项批量提交到购物车；未填不参与
7. **进入购物车**：每行展示品项、单位、数量、单价、小计；页底购物车总金额；可单条删除/清空
8. **点「发送」**：进入提交订单页（期望到货日期 + 备注）→ 校验库存/未绑定 → 订单状态 `pending`，购物车清空，通知 DC 审批人
9. **DC 审批人**：review 页看到 pending → 通过 → `approved`
10. **DC 出库员**：shipments 页看到 approved → 一次性出库 → DC 仓库存扣减，`stock_movements.action='门店订货出库'`，通知门店 → `shipped`
11. **门店收货员**：订单详情页 `shipped` 后显示「收货」入口 → 填本批实收数量（可分多次，部分收货）→ 门店仓 `items.quantity += qty`，写 `stock_movements.action='门店订货入库'`，`fulfilled_quantity` 累计 → 全部收齐后状态 `delivered`，通知门店

---

## C. UI/交互细节

### C.1 catalog 页面

- 顶部：配送中心选择器（已选时显示 + 「切换」链接）
- 搜索 + 品类 chips（沿用现有）
- **卡片**：使用 `.inv-card` + `.inv-card-head`（品类中文 + 品项名 + 状态 pill 库存）+ `.inv-card-body`（库存数 / 单价 / 「点击卡片选数量」提示）
- **弹窗**：复用 `restock_session.html` 的 `.modal-overlay` / `.modal` / `.pill-group` 结构，加「实时金额」展示
- **页底**：「加入购物车」按钮（遍历 `[data-saved-qty]` 不为空的所有卡片，POST 批量）

### C.2 cart 页面

- 列表：品项 | 单位 | 数量（可改）| 单价 | 小计 | 删除
- 页底：购物车总金额（¥ XX.XX）+ 「继续购物」 + 「发送」

### C.3 订单详情（门店收货）

- 状态 `shipped`：每行 store_order_items 显示「本批收货」表单（实收数量 + 备注）→ POST `/store-ordering/orders/<id>/receive`
- 已收货的明细展示收货历史（store_order_receipts）：时间、数量、收货人

---

## D. 数据模型调整

### D.1 新增 `store_order_receipts` 表

```sql
CREATE TABLE IF NOT EXISTS store_order_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    order_item_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    received_by INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES store_orders(id) ON DELETE CASCADE,
    FOREIGN KEY (order_item_id) REFERENCES store_order_items(id) ON DELETE CASCADE,
    FOREIGN KEY (received_by) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_store_order_receipts_order
    ON store_order_receipts(order_id, created_at DESC);
```

### D.2 状态机调整

- 增加 `partial_received` 状态：`shipped → partial_received → delivered`（多次收货中间态）
- 或者保持 `shipped` + 通过 `store_order_items.fulfilled_quantity` 与 `quantity` 比较判断是否全部收齐（无需新状态）

**决策**：不引入 `partial_received` 中间态。订单主表状态保持 `shipped`，所有明细的 `fulfilled_quantity == quantity` 时才 `delivered`。简化状态机。

### D.3 门店仓库存自动入库

- 收货时调用 `store` 仓 `open_warehouse_db()` → 校验/创建 store 仓 `items`（如果门店未绑定 canonical_id 对应本地 items，自动创建？或阻止？）→ 决定方案见 E

---

## E. 待确认

| 问题 | 选项 | 倾向 |
|---|---|---|
| 门店仓未绑定 canonical_id 时如何收货？ | A) 收货时自动在门店仓创建 items（quantity=0）后加库存；B) 阻止并要求先建立绑定 | 倾向 A（确保订单流程不被绑定流程阻塞） |
| 收货人权限 | A) 门店 manager+ 可收货；C) staff 也可收货 | 倾向 A |
| DC 端可部分出库吗？ | A) 不允许一次性全部；B) 允许多次 | 维持「一次性全部」（P0） |

---

## F. e2e 测试路径（与 B 一一对应，每一步断言通过）

1. 选 DC → 验证 catalog 展示品类中文（无 PACKAGING/CONSUMABLE 字样）
2. 验证卡片排版：每张卡片有 inv-card / inv-cat（中文）/ inv-name / status-pill / 单价 / 库存
3. 点卡片 → 弹窗出现；填数量 → 看到金额
4. 关闭弹窗 → 卡片显示「已填 X」
5. 点页底「加入购物车」→ 重定向到 cart
6. cart 页面有总金额、每行小计、单条删除 / 清空按钮
7. 点「发送」 → 提交订单页 → 填日期 + 备注 → 提交 → 跳转到 order detail
8. 切到 DC admin → review 页看到订单
9. 审批通过 → 状态 approved
10. 出库 → DC 库存 -N、status shipped
11. 切回门店 manager → order detail → 输入 1 件实收 → 门店库存 +1，fulfilled_quantity = 1
12. 再次收货 → 全部收齐 → status delivered
13. 验证 stock_movements：DC 端有 `门店订货出库`，store 端有 `门店订货入库`
14. 验证通知：submitted / approved / shipped / delivered 都写到 notifications 表