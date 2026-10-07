# 门店订货 v3.1 测试计划

- 日期：2026-10-06
- 关联：`feat/store-ordering` 分支
- 上游：[开发计划](./2026-10-06-store-ordering-dev-plan.md)
- 测试范围：品项可订开关（A）+ 运费规则（B）

---

## 测试矩阵

### A. 品项可订开关

| ID | 用例 | 类型 | 文件 | 覆盖点 |
|---|---|---|---|---|
| A1 | `migrate_warehouse_db_columns` 给老仓加 `is_orderable` 列，默认 1 | pure | test_store_ordering_pure | schema 幂等 |
| A2 | `list_available_dc_items` 过滤掉 `is_orderable=0` 品项 | pure | test_store_ordering_pure | catalog 过滤 |
| A3 | `set_dc_item_orderable(False)` 后 catalog 不再显示该品项 | pure | test_store_ordering_pure | toggle 生效 |
| A4 | `set_dc_item_orderable` 不存在的 canonical_id 抛 ValueError | pure | test_store_ordering_pure | 边界 |
| A5 | 提交时校验：购物车里含被下架品项被阻止 | pure | test_store_ordering_pure | 提交拦截 |
| A6 | `list_dc_items_for_management` 含可订 + 不可订（用于管理页面） | pure | test_store_ordering_pure | 管理列表 |
| A7 | DC manager 访问 `/dc/items` 200 + 看到品项列表 | e2e | test_store_ordering_e2e | 路由 + 权限 |
| A8 | DC staff 访问 `/dc/items` 403（@require_role("manager")） | e2e | test_store_ordering_e2e | 权限 |
| A9 | 门店用户访问 `/dc/items` 403（@require_warehouse_type("distribution_center")） | e2e | test_store_ordering_e2e | 权限 |
| A10 | POST 切换后页面状态 pill 翻转 + flash | e2e | test_store_ordering_e2e | toggle UI |
| A11 | nav 入口存在（DC 区） | e2e | test_store_ordering_e2e | 导航 |

### B. 运费规则

| ID | 用例 | 类型 | 文件 | 覆盖点 |
|---|---|---|---|---|
| B1 | `compute_shipping_fee(100, base=5, pct=0.01)` → 6.0 | pure | test_store_ordering_pure | 公式 |
| B2 | `compute_shipping_fee(0, base=5, pct=0)` → 5.0（base 不论金额都收） | pure | test_store_ordering_pure | 边界 |
| B3 | `compute_shipping_fee` 2dp 量化（避免 0.300000004） | pure | test_store_ordering_pure | 精度 |
| B4 | `get_active_shipping_rule` 无 active 行返回 None | pure | test_store_ordering_pure | 默认无运费 |
| B5 | `upsert_shipping_rule` 已有 active → UPDATE；否则 INSERT | pure | test_store_ordering_pure | 写入 |
| B6 | `submit_order` 写入 shipping_fee，`get_order_detail` 返回 | pure | test_store_ordering_pure | 持久化 |
| B7 | ship 后 shipping_fee 不变（锁定） | pure | test_store_ordering_pure | 锁定 |
| B8 | 修改规则后新订单用新值，老订单用旧值 | pure | test_store_ordering_pure | 规则切换 |
| B9 | `submit_order` 无 active 规则 → shipping_fee=0（向后兼容） | pure | test_store_ordering_pure | 默认行为 |
| B10 | admin 配置页 GET 200 + POST 保存生效 | e2e | test_store_ordering_e2e | 路由 |
| B11 | 非 admin 访问 `/admin/shipping` 403 | e2e | test_store_ordering_e2e | 权限 |
| B12 | submit 页面显示运费 + 总计 | e2e | test_store_ordering_e2e | UI 展示 |
| B13 | order_detail 信息卡显示运费行 | e2e | test_store_ordering_e2e | UI 展示 |
| B14 | admin_report summary 含 `total_shipping_fee`，by_store 含 `shipping_fee` | e2e | test_store_ordering_e2e | 报表扩展 |
| B15 | nav 入口存在（admin 区「运费规则」） | e2e | test_store_ordering_e2e | 导航 |

**合计**：16 个纯函数测试 + 11 个 e2e 测试 = **27 个新测试**（之前 94）

---

## 测试覆盖关键决策

### 决策 1：运费公式 = 加和
- **B1** 直接断言 `base + pct × subtotal`
- **B2** 边界：subtotal=0 时仍收 base_fee

### 决策 2：取消订单 = 运费作废
- 当前实现：cancel_order 不动 shipping_fee 字段（订单标 cancelled，但字段保留）
- 测试不专门覆盖（保持字段语义简单）
- 若未来需要退款/作废逻辑，单独加测试

### 决策 3：报表含运费
- **B14** 验证 KPI 第 5 张卡 + by_store 列
- 与 PRD P2-4「实付金额」对齐（订单金额 + 运费）

### 决策 4：DC manager 可管理自己 DC 的品项
- **A7-A9** 三个角色权限测试

---

## 已知陷阱（写测试时要避开）

| 陷阱 | 应对 |
|---|---|
| 老仓无 `is_orderable` 列导致 migrate 报错 | PRAGMA table_info 守卫，幂等 |
| `shipping_fee` 老订单默认 0 时报表 SUM 不爆 | DEFAULT 0，老数据安全 |
| `compute_shipping_fee` 浮点精度 | 用 Decimal 量化到 0.01（与货币一致） |
| `submit_order` 写入 shipping_fee 但 e2e 看不到（无模板字段） | 模板字段先加，否则 e2e 看不到，但 DB 已写入 |
| `validate_cart_for_submit` 返回结构变了影响其他测试 | 字段加而非删，向后兼容 |
| e2e `manage_shipping_rule` POST 后 `get_active_shipping_rule` 应返回新规则 | 注意 active=1 的唯一约束（多行 active 不会被选中） |
| `compute_store_order_report` SQL 加 SUM(so.shipping_fee)，但 store_orders 必须有该列 | 老仓迁移后才有，但 e2e 走 fresh db，无问题 |

---

## 测试运行顺序

1. **单元测试先跑**（fast feedback）：
   ```bash
   pytest tests/test_store_ordering_pure.py -q
   ```
2. **路由测试再跑**：
   ```bash
   pytest tests/test_store_ordering_e2e.py -q
   ```
3. **全套回归**（确认未破其他测试）：
   ```bash
   pytest tests/test_store_ordering_pure.py \
          tests/test_store_ordering_dc_route.py \
          tests/test_store_ordering_store_route.py \
          tests/test_store_ordering_integration.py \
          tests/test_store_ordering_receive.py \
          tests/test_store_ordering_e2e.py -q
   ```
4. **lint 复检**（仅修改文件）：
   ```bash
   ruff check blueprints/store_ordering.py blueprints/store_ordering_pure.py
   ```

---

## 测试数据准备

### pure fixture（`ordering_env`）
现有 fixture 已覆盖 master + dc + store + 基础品项/订单结构。
- A 类测试：在 fixture 里直接 INSERT 一个 dc item with `is_orderable=0` 即可
- B 类测试：在 fixture 里 INSERT 一行 shipping_rules 即可

### e2e fixture（`e2e_env`）
现有 fixture 含两个 DC + 一个门店。
- A 类测试：登录 dc_mgr (user 3) 在 dc1_test，调切换
- B 类测试：登录 admin (user 4)，调配置 + 验证 submit/detail/report

---

## 完成定义（DoD）

测试部分单独 DoD：
- [ ] 27 个新测试全部通过
- [ ] 之前 94 个测试 0 回归
- [ ] 修改文件 ruff 0 错
- [ ] 测试运行时间 < 10s（pure）/ < 5s（e2e）
- [ ] 所有测试有清晰 docstring 说明覆盖点
