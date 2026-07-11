# 品项发布 — 子项目 4 spec

**版本**：v1（2026-06-29）
**状态**：实施中。引用总览 PRD §2.4。
**前置依赖**：无（独立子项目；subproject 1+2 不强依赖；subproject 5 会消费 publish_events 表）。
**目标读者**：实施 agent。

---

## 0. 引用与本 spec 自决

引用 PRD：
- §2.4.1 目标
- §2.4.2 模板身份（**PRD 锁定**：品项集合，不是门店型）
- §2.4.3 发布动作流程
- §2.4.4 数据契约
- §2.4.5 事件追溯
- §2.4.6 异常表
- §2.4.7 必须自动化（一次发布 = 多个门店同时收到）
- §2.4.8 测试要点
- §3.2/3.3/3.5/3.6
- §8 锁定项

本 spec 自决项：

1. **冲突解决默认值**（preview → confirm 之间）：admin 可指定 `default_action`：`keep_store` | `overwrite` | `merge`。未指定时默认 `merge`（**最保守**）。
2. **`publish_event_items` 关联的 item_id 来源**：`items.id`（主仓 spec 锁定）。门店库在 publish 时**新建 item 行**（不是覆盖），由 `items.created_by_publish_event_id` 反向引用。**覆盖**语义：旧 item 不删，新 item 行用 `published_at = now`；store 端查询时按 `is_latest=1` 过滤。**为简化**：本子项目不实现"旧 item 软删除"——`overwrite` 直接 `UPDATE items SET ...`。**owner review 时拍板**。
3. **`/admin/publish/items/preview` + `/confirm` 路径**：PRD §2.4.4 写两条。**本子项目实现**两条独立路由。
4. **template_id 版本化**：每次保存 template → 递增 `version`（spec §0 锁定）。本子项目 `template_versions` 表存历史。
5. **失败部分成功语义**（PRD §2.4.6）：confirm 后写 publish_event 时，**per-warehouse** 包裹 try/except。失败的 warehouse 写 `publish_event_items.status='failed'`，成功的 'success'。**本子项目实现**。
6. **template 创建 UI**：admin 创建/编辑 template，**不**在本子项目 spec 范围（属于 admin template management）。**本子项目只做发布侧**（preview + confirm）。template 创建可调 `POST /admin/publish/items/templates` 接受 JSON。
7. **target 仓库列表**：admin 显式传 `warehouse_codes` 列表（不是从某"默认门店集合"读）。

---

## 1. 数据契约（与 PRD 2.4.4 一致）

### 1.1 POST /admin/publish/items/preview

```
POST /admin/publish/items/preview
{
    "template_id": 7,
    "template_version": 3,
    "warehouse_codes": ["wh_001", "wh_002"]
}
→ 200 {
    "template_id": 7,
    "template_version": 3,
    "warehouses": [
        {
            "warehouse_code": "wh_001",
            "items": [
                {
                    "template_item_idx": 0,
                    "item_name": "木樨子油",
                    "status": "add|skip|conflict",
                    "existing_item_id": 123,      // 仅 conflict
                    "diff_fields": ["unit_cost"]  // 仅 conflict
                }
            ]
        }
    ]
}
```

### 1.2 POST /admin/publish/items/confirm

```
POST /admin/publish/items/confirm
{
    "template_id": 7,
    "template_version": 3,
    "warehouse_codes": ["wh_001"],
    "resolutions": [
        {"template_item_idx": 0, "warehouse_code": "wh_001",
         "action": "keep_store|overwrite|merge"}
    ]
}
→ 200 { "publish_event_id": 42 }
```

---

## 2. Schema（master.db 新增）

```sql
CREATE TABLE publish_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    created_by INTEGER
);

CREATE TABLE template_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    items_json TEXT NOT NULL,    -- JSON: [{name, category, unit, unit_cost, gram_per_unit, safety_stock}, ...]
    created_at TEXT NOT NULL,
    UNIQUE(template_id, version),
    FOREIGN KEY (template_id) REFERENCES publish_templates(id)
);

CREATE TABLE publish_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    template_version INTEGER NOT NULL,
    started_by INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    warehouse_codes_json TEXT NOT NULL,  -- JSON array
    resolutions_json TEXT NOT NULL       -- JSON array
);

CREATE TABLE publish_event_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_event_id INTEGER NOT NULL,
    template_item_idx INTEGER NOT NULL,
    warehouse_code TEXT NOT NULL,
    item_id INTEGER,                     -- post-publish item id (NULL if action=keep_store or failed)
    action TEXT NOT NULL,                -- keep_store|overwrite|merge|add
    status TEXT NOT NULL,                -- success|failed
    error_message TEXT,
    FOREIGN KEY (publish_event_id) REFERENCES publish_events(id)
);

-- warehouse items 表加列
ALTER TABLE items ADD COLUMN created_by_publish_event_id INTEGER;
```

`items.created_by_publish_event_id` 是 spec §0.2 提到的反向引用。

---

## 3. 异常

| 场景 | 行为 |
|---|---|
| 模板无该 version | 404 + `{"error": "template_version_not_found"}` |
| 门店不存在 | 400 + `{"error": "warehouse_not_found"}` |
| confirm 时 resolutions 缺失 | 400 + `{"error": "missing_resolutions"}` |
| confirm 时仍存在 conflict (无 action) | 400 + `{"error": "unresolved_conflicts"}` |
| 某门店写 items 失败 | 200 + `publish_event_items.status='failed'` + 错误详情（**不**回滚其他成功） |

---

## 4. 流程（PRD §2.4.3）

1. preview：调 `compute_publish_diff(template_version, warehouse_codes)` → 写 `add/skip/conflict`。
2. confirm：调 `apply_publish(template_version, warehouse_codes, resolutions)` → 写 `publish_events` + per-store `publish_event_items` + 实际写 items 表。
3. 写 `items.created_by_publish_event_id` 反向引用。
4. 写 access.log + audit（通过 `auth.audit`）。

---

## 5. 必须自动化（PRD §1.1 A4）

- 一次发布 = 多个门店同时收到。**已实现**（loop over warehouse_codes）。
- **不在范围**：admin template CRUD UI。本子项目只做发布侧。

---

## 6. 错误处理

- 不可逆动作：items 表 UPDATE / INSERT 实际发生。**不**提供 undo（PRD 没要求）。
- 可回滚动作：无。
- 失败兜底：per-store try/except → 写 status='failed'。partial success 写 `publish_event_items` + 返回成功的 warehouse_codes 给客户端。

---

## 7. 测试矩阵

### 7.1 单元

- `compute_publish_diff(template_items, store_items)`:
  - template item 不在 store → 'add'
  - template item 存在且全等 → 'skip'
  - template item 存在但字段差异 → 'conflict' + diff_fields
  - empty store_items → all 'add'

### 7.2 集成

- POST preview 未知 template_version → 404
- POST preview 已知 template_version + 1 warehouse + 1 item 不存在 → add
- POST preview 已知 template_version + 1 item 全等 → skip
- POST preview 已知 template_version + 1 item 字段差异 → conflict + diff_fields
- POST confirm 缺 resolutions → 400
- POST confirm 仍有未处理 conflict → 400
- POST confirm keep_store → 不写 items
- POST confirm overwrite → UPDATE items
- POST confirm merge → 字段补齐
- POST confirm 1 店成功 + 1 店失败 → 200 + 部分成功语义

### 7.3 E2E

- admin 发布 → 门店 staff 登录 → 在 /items 看到新品项

---

## 8. 文件清单

- 新增 `blueprints/publish_items.py`（blueprint + routes）
- 新增 `blueprints/publish_items_pure.py`（diff 纯函数）
- 修改 `db/__init__.py`（4 张新表 + items 加列）
- 修改 `app.py`（注册 blueprint）
- 修改 `templates/admin_publish_items.html`（最小 UI：preview + confirm）
- 新增 `tests/test_publish_items_pure.py`
- 新增 `tests/test_publish_items_route.py`
- 修改 `templates/base.html`（admin nav 加"发布"入口）
- 修改 `templates/land.html`（admin 看到发布卡）

---

## 9. 验收门

1. `pytest -q` 全绿
2. 新代码 ruff clean
3. partial success 路径：故意让 1 店失败（mock 写错误），验证其他店成功
4. PR 描述引用 PRD §2.4
