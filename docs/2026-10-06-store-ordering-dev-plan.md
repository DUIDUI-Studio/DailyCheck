# 门店订货 v3.1 开发计划

- 日期：2026-10-06
- 关联：`feat/store-ordering` 分支
- 上游：[用户操作流程](./2026-10-06-store-ordering-user-flows.md)
- 下游：[测试计划](./2026-10-06-store-ordering-test-plan.md)

---

## 阶段 0: 准备（不写代码）

读懂现有实现（避免重复造轮子）：
- `db/__init__.py:migrate_warehouse_db_columns`（幂等 ALTER 模式：`PRAGMA table_info` 检查 + ALTER）
- `db/__init__.py:init_master_db` + `migrate_master_db_columns`（master.db schema 演进）
- `blueprints/store_ordering_pure.py:list_available_dc_items`（catalog 数据源）
- `blueprints/store_ordering_pure.py:submit_order` + `get_order_detail`（订单写入/读取）
- `blueprints/store_ordering.py` 装饰器惯例：`@require_login` / `@require_role("manager")` / `@require_warehouse_type("distribution_center")`

---

## 阶段 1: 品项可订开关（独立，估 1-2 小时）

### 1.1 schema 迁移（`db/__init__.py:migrate_warehouse_db_columns`）
```sql
-- 追加到 migrate_warehouse_db_columns 函数末尾，PRAGMA 守卫
ALTER TABLE items ADD COLUMN is_orderable INTEGER NOT NULL DEFAULT 1
```
- 老仓数据自动 `DEFAULT 1`（保持当前"全部可订"行为，零回归）
- 走 `if "is_orderable" not in item_cols:` 守卫，幂等

### 1.2 pure 函数

**修改 `list_available_dc_items`**（store_ordering_pure.py:309）：
- 在 DC 视角的 WHERE 子句加 `i.is_orderable = 1`

**新增 `set_dc_item_orderable`**：
```python
def set_dc_item_orderable(
    master_conn: sqlite3.Connection,
    dc_warehouse_code: str,
    canonical_id: int,
    is_orderable: bool,
) -> bool:
    """Return True iff a row was updated."""
```
- `open_warehouse_db(dc_warehouse_code)` → UPDATE → commit
- 找不到 dc_items 行返回 False（异常路径用 ValueError）

**新增 `list_dc_items_for_management`**：
- 返回所有 DC items（含不可订），用于 DC 品项管理页面
- 按品类分组排序

### 1.3 路由（`blueprints/store_ordering.py`）

```python
@bp.route("/dc/items", methods=["GET", "POST"])
@require_login
@require_warehouse_type("distribution_center")
@require_role("manager")
def manage_dc_items() -> str:
    """DC 品项可订开关管理（DC manager + admin）。"""
```

- GET：调 `list_dc_items_for_management` 渲染模板
- POST：取 `canonical_id` + `is_orderable` form，调 `set_dc_item_orderable`，flash 结果

### 1.4 模板（`templates/store_ordering/dc_items.html`）
- 表头：品项 / 单位 / 库存 / 状态 / 操作
- 每行：品项名 + 当前库存 + 状态 pill + 切换按钮（POST form）
- 切换按钮：`<button>` 触发 form submit，POST 到同 URL

### 1.5 nav（`templates/base.html`）
- DC 区加「品项管理」链接（`is_admin or is_distribution_center`，已存在分支内追加）

### 1.6 提交时校验
- `store_ordering.py:submit_order_route`：调 `validate_cart_for_submit` 后再加一道 `is_orderable` 校验
- `store_ordering_pure.py:validate_cart_for_submit` 加新返回字段 `disabled_items: list[str]`（被下架品项的 canonical_name 列表）
- 模板 `submit.html` 显示该字段，flash 提示「请移除」

---

## 阶段 2: 运费规则 schema + pure（估 2-3 小时）

### 2.1 schema（`db/__init__.py`）

**新表 `shipping_rules`**（master.db）：
```sql
CREATE TABLE shipping_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    base_fee REAL NOT NULL DEFAULT 0,
    pct_fee REAL NOT NULL DEFAULT 0,   -- 小数：0.01 = 1%
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```
- 注册到 `init_master_db` 的 `executescript(SCHEMA)` 块
- 在 `migrate_master_db_columns` 里补 CREATE TABLE IF NOT EXISTS 守卫（幂等）

**新列 `store_orders.shipping_fee`**：
```sql
ALTER TABLE store_orders ADD COLUMN shipping_fee REAL NOT NULL DEFAULT 0
```
- 幂等守卫同上
- 老订单默认 0，不影响历史数据

### 2.2 pure 函数

**`compute_shipping_fee`**：
```python
def compute_shipping_fee(subtotal: float, rule: dict) -> float:
    """fee = base_fee + subtotal × pct_fee, 2dp quantize."""
```
- 输入 subtotal = 订单金额合计（基础单位 × 单价）
- 输出 round 到 0.01

**`get_active_shipping_rule`**：
- SELECT * FROM shipping_rules WHERE active=1 LIMIT 1
- 找不到返回 None（视为 0 运费，向后兼容）

**`upsert_shipping_rule`**：
- 若已有 active 行 → UPDATE
- 否则 INSERT（name="默认"）
- 返回 rule_id

**`submit_order` 修改**：
- 调 `get_active_shipping_rule(master_conn)` 取当前规则
- 算 subtotal（已有逻辑，加 total_amount = subtotal + shipping_fee）
- INSERT store_orders 时带 shipping_fee
- 返回 dict 加 `shipping_fee` 字段

**`get_order_detail` 修改**：
- SELECT 加 `shipping_fee` 列
- 返回 dict 加 `shipping_fee` 字段

### 2.3 不改的部分
- `ship_order*` 不动 —— 运费在 submit 时锁定，ship 时不变
- `cancel_order` 不动 —— 取消时直接 cancel（shipping_fee 字段保留，但订单状态=cancelled 后业务上不再计费）
- `receive_order_item` 不动

---

## 阶段 3: 运费规则 UI（估 2 小时）

### 3.1 admin 配置路由
```python
@bp.route("/admin/shipping", methods=["GET", "POST"])
@require_login
@require_role("admin")
def manage_shipping_rule() -> str:
    """admin 配置运费规则。"""
```
- GET：调 `get_active_shipping_rule`，渲染表单（含实时示例计算）
- POST：调 `upsert_shipping_rule(base_fee, pct_fee, active)`，flash

### 3.2 nav（base.html）
- admin 区加「运费规则」链接（已存在的 `if g.user and g.user['is_admin']` 块内追加）

### 3.3 submit 页面（`templates/store_ordering/submit.html`）
- 底部新增运费预估 + 总计行
- 数据来源：sop 调 `get_active_shipping_rule` + `compute_shipping_fee` 算预览

### 3.4 order_detail 页面（`templates/store_ordering/order_detail.html`）
- 信息卡加运费行（`order.shipping_fee`）
- 加订单总计行（`subtotal + shipping_fee`）

### 3.5 admin_report 扩展（`blueprints/store_ordering.py:admin_report`）
- 模板 `templates/store_ordering/report.html`：
  - 顶部 KPI 加第 5 张卡「总运费」
  - by_store / by_dc 表新增「运费」列
  - by_category / by_canonical 不变（运费按订单计，不分品类/品项）

**`compute_store_order_report` 扩展**：
- summary 加 `total_shipping_fee`
- by_store / by_dc 加 `shipping_fee`
- SQL JOIN 拿 shipping_fee（已存在 store_orders 表里）
- 已有 SQL 加 `SUM(so.shipping_fee) AS shipping_fee`，按 store/dc GROUP BY

---

## 阶段 4: lint + commit + push（按 owner SOP）

### 提交前验证
1. `ruff check`（仅修改文件，零错）
2. `pytest tests/test_store_ordering_*.py`（全套通过）
3. dev 容器 hot-reload 后手动验证关键路径（catalog 过滤 / 运费表单 / submit 显示）

### 提交粒度（3 个 commit）
1. `feat(store-ordering): 品项可订开关（DC manager 管理）`
2. `feat(store-ordering): 运费规则（admin 配置 + submit/detail/report 集成）`
3. （如有）`docs(store-ordering): 三份 v3.1 文档`

### push
- 按 memory 强制规则：commit 后**先问 Eric**再 push
- 任何 push 必须经 Eric 明确同意

---

## 完成定义（DoD）

- [ ] 阶段 1-3 全部代码落地
- [ ] 测试 100% 通过（详见 [测试计划](./2026-10-06-store-ordering-test-plan.md)）
- [ ] 修改的 Python 文件 ruff 0 错
- [ ] dev 服务器手动验证：开关生效 / 运费计算正确 / 报表显示
- [ ] 3 个 commit（按 SOP 提交前验证）
- [ ] push 经 Eric 拍板

---

## 风险点

| 风险 | 缓解 |
|---|---|
| 老仓 ALTER TABLE 加列失败 | 走 PRAGMA table_info 守卫，幂等 |
| `shipping_fee` 老订单默认 0 | DEFAULT 0，老数据无回归 |
| 修改运费规则影响已 ship 订单 | 不改：shipping_fee 在 submit 时已持久化 |
| 部分取消（如 cancel approved）时 shipping_fee 状态 | 维持现状，shipping_fee 字段保留但订单 cancelled 不再计费 |
| 报表 SUM 包含 cancelled 订单 | 按当前实现，cancelled 订单 items.quantity 仍计入。沿用现有口径不动 |
| is_orderable=0 影响 `compute_suggested_order_qty` | 不影响（建议订货量是门店仓数据，不看 DC items.is_orderable） |
