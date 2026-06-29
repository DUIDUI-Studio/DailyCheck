# 配方发布 + 全站通知 — 子项目 5 spec

**版本**：v1（2026-06-29）
**状态**：实施中。引用总览 PRD §2.5。
**前置依赖**：subproject 3（`emit_event` 函数已实现）。
**目标读者**：实施 agent。

---

## 0. 引用与本 spec 自决

引用 PRD：
- §2.5.1 目标
- §2.5.2 配方发布语义（**PRD 锁定**：版本化）
- §2.5.3 发布范围（**PRD 锁定**：按门店）
- §2.5.4 全站通知（**PRD 锁定**：通用事件总线 Web 首期）
- §2.5.5 数据契约
- §2.5.6 异常表
- §2.5.7 测试要点
- §3.2/3.3/3.5/3.6
- §8 锁定项

本 spec 自决项：

1. **emit_event 失败处理**：本子项目调用 `emit_event` 必须 try/except 包裹；emit_event 失败 → 写 `publish_event_items.status='failed'` + 错误详情，**不**回滚 `product_bom_versions` 写入。
2. **target_url 拼装**：`/products/<product_id>/versions/<version_id>`。spec 0.6 of subproject 3 提到"target_url 由前端从 summary 解析"——本子项目**直接**把 URL 写入 `target_url` 字段（不改 subproject 3 的接口）。**owner review 时确认**。
3. **门店级生效版本**：`product_bom_store_versions` 表记录每门店生效版本（spec §0.5 锁定）。本子项目实现。
4. **生产录入按 current_version 取 BOM**：subproject 1 不变（生产录入由 blueprints/production.py 处理，读取 `products.current_version_id`）。
5. **老版本冻结可查**：spec §0.3 写"不允许删除老版本"。本子项目**不**实现 delete 版本 API（owner review 时确认是否需要）。
6. **配方发布失败部分成功语义**：同 subproject 4 — 部分门店成功 + 部分失败，返回 publish_event_id + 失败列表。
7. **配方发布触发 A6 通知**：spec §0.4 (subproject 3) 写"配方发布后全站通知自动出现"。本子项目是 emit_event 的**第一个真实调用方**。

---

## 1. 数据契约（与 PRD 2.5.5 一致）

### 1.1 GET /notifications（subproject 3 已实现）

不变。

### 1.2 POST /admin/publish/recipe

```
POST /admin/publish/recipe
{
    "product_id": 12,
    "bom_items": [
        {"item_id": 1, "qty_per_unit": 0.5},
        {"item_id": 2, "qty_per_unit": 1.0}
    ],
    "warehouse_codes": ["wh_001", "wh_002"],
    "summary": "经典柠檬茶 v3 已发布"
}
→ 200 { "version_id": 3, "publish_event_id": 88 }
```

实施后：
1. 调 `create_new_bom_version(product_id, bom_items)` → 写 `product_bom_versions`（新行，`version` 自动递增），`products.current_version_id` 指向新版本。
2. 对每个 warehouse_code：写 `product_bom_store_versions(product_id, warehouse_code, version_id)`。
3. 写 `publish_events` + `publish_event_items` per warehouse。
4. 调 `emit_event('recipe_published', summary, target_url, [all_user_ids])`。

---

## 2. Schema（master.db + warehouse.db 新增）

### 2.1 master.db

```sql
-- recipe 发布事件（区别于 item publish）
CREATE TABLE recipe_publish_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    bom_version_id INTEGER NOT NULL,
    started_by INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary TEXT,
    warehouse_codes_json TEXT NOT NULL
);

CREATE TABLE recipe_publish_event_warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_event_id INTEGER NOT NULL,
    warehouse_code TEXT NOT NULL,
    status TEXT NOT NULL,  -- success|failed
    error_message TEXT,
    FOREIGN KEY (publish_event_id) REFERENCES recipe_publish_events(id)
);
```

### 2.2 warehouse.db

```sql
-- 配方版本快照（per-product，跨店共享的事实来源）
CREATE TABLE product_bom_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    bom_json TEXT NOT NULL,   -- JSON: [{item_id, qty_per_unit}, ...]
    created_at TEXT NOT NULL,
    UNIQUE(product_id, version),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

-- 每门店生效版本
CREATE TABLE product_bom_store_versions (
    product_id INTEGER NOT NULL,
    warehouse_code TEXT NOT NULL,
    bom_version_id INTEGER NOT NULL,
    effective_at TEXT NOT NULL,
    PRIMARY KEY (product_id, warehouse_code),
    FOREIGN KEY (bom_version_id) REFERENCES product_bom_versions(id)
);
```

`products` 表加列 `current_version_id INTEGER`。

---

## 3. 异常

| 场景 | 行为 |
|---|---|
| product 不存在 | 404 + `{"error": "product_not_found"}` |
| bom_items 为空 | 400 + `{"error": "empty_bom"}` |
| 仓库不存在 | 400 + `{"error": "warehouse_not_found"}` |
| 系统故障导致部分门店未生效 | 返回成功 + 失败列表（**部分成功语义**） |
| 用户无登录态访问 /notifications | 302 /login（subproject 3 行为不变） |

---

## 4. 流程

1. 校验 product / bom / warehouses
2. 写 `product_bom_versions`（新 version 号 = `MAX(version) + 1` for product_id）
3. UPDATE `products.current_version_id`
4. per warehouse：写 `product_bom_store_versions`（per-store try/except）
5. 写 `recipe_publish_events` + `recipe_publish_event_warehouses`
6. 调 `emit_event('recipe_published', summary, f'/products/{pid}/versions/{vid}', [all_user_ids])` —— emit_event 失败 **不**回滚前 5 步
7. 写 access.log + audit

---

## 5. 必须自动化（PRD §1.1 A6）

发布成功后**全站通知**自动出现 = step 6 调 emit_event。

---

## 6. 错误处理

- 不可逆动作：`product_bom_versions` INSERT 不可删。spec §0.3 锁定。
- 可回滚动作：无。
- 失败兜底：per-store try/except；emit_event 失败 → log warning，**不** throw。

---

## 7. 测试矩阵

### 7.1 单元

- `create_new_bom_version(product_id, bom_items) -> int`:
  - 新产品（无现存 version）→ version=1
  - 已存 version=2 → 返回 version=3
  - version 号不重用（spec §0.3 锁定）

### 7.2 集成

- POST /admin/publish/recipe product 不存在 → 404
- POST /admin/publish/recipe bom_items 为空 → 400
- POST /admin/publish/recipe 全成功 → 200 + product_bom_versions 新行 + 2 店 product_bom_store_versions
- POST /admin/publish/recipe 1 店成功 + 1 店失败 → 200 + 部分成功语义
- POST /admin/publish/recipe 触发 emit_event → /notifications 看到 1 条

### 7.3 E2E

- admin 发布 → 任意用户登录 → /notifications 看到通知红点 +1

---

## 8. 文件清单

- 新增 `blueprints/publish_recipe.py`（routes）
- 新增 `blueprints/publish_recipe_pure.py`（create_new_bom_version 纯函数）
- 修改 `db/__init__.py`（2 张 master.db 表 + 2 张 warehouse.db 表 + products 加列）
- 修改 `app.py`
- 新增 `templates/admin_publish_recipe.html`（最小 UI）
- 新增 `tests/test_publish_recipe_pure.py`
- 新增 `tests/test_publish_recipe_route.py`
- 修改 `templates/base.html`（admin nav 加"配方发布"）
- 修改 `templates/land.html`（admin 看到"配方发布"卡）

---

## 9. 验收门

1. `pytest -q` 全绿
2. emit_event 调用成功 → /notifications 真实看到通知
3. PRD §2.5.2 锁定项"不允许删除老版本"满足（不实现 delete API）
