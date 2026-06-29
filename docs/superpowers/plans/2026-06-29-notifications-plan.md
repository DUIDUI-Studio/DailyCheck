# 通知事件总线 — 子项目 3 plan

**版本**：v1（2026-06-29）。引用 spec `2026-06-29-notifications-design.md`。
**目标读者**：实施 agent。

---

## 0. 执行约定

- 一任务 = 一 commit。任务顺序 = 提交顺序。
- 每任务：RED → GREEN（看测试 fail 才写实现）。
- ruff config 已存在（子项目 1 引入）。

---

## 1. 任务清单

### TASK 1 — 纯函数：`emit_event` / `mark_read` / `list_for_user`（RED → GREEN）

**目标**：`blueprints/notifications_pure.py` 3 个纯函数（纯 = 输入输出，不读写 HTTP）。**注意**：这些函数**需要**接受 db connection 作为参数（不能 module-level）以保持可测性。

**RED**：写 `tests/test_notifications_pure.py`：
- `emit_event`:
  - 正常：传 1 个 event + 3 个 user_id → DB 写 3 行
  - 空 user_ids → raise ValueError
  - event_type 不在 {'recipe_published'} → raise ValueError
  - summary > 200 字符 → raise ValueError
- `mark_read`:
  - 存在且未读 → set read_at, return True
  - 不存在 → return False
  - 已读 → return False (idempotent)
- `list_for_user`:
  - unread_only=True → 只未读
  - unread_only=False → 全部
  - 倒序 + limit 100

**GREEN**：实现 3 个函数。`emit_event` 接受 `db_conn` (sqlite3.Connection)；其余接受 (db_conn, user_id, ...)。理由：**保持纯函数性质 = 不依赖 g / current_app**。

**commit 序列**：
1. `test(notifications): RED emit_event + mark_read + list_for_user`
2. `feat(notifications): implement event bus pure fns`

---

### TASK 2 — schema：notifications + notification_prefs（RED → GREEN）

**目标**：master.db 加 2 张表。

**RED**：集成测试
- `notifications` 表存在，列：id, user_id, event_type, summary, target_url, created_at, read_at
- `notification_prefs` 表存在，列：user_id, event_type, channel, muted
- 写一行 notifications → 读回一致

**GREEN**：在 `db/__init__.py` 的 `MASTER_SCHEMA` 追加。

**commit**：`feat(notifications): notifications + notification_prefs schema`

---

### TASK 3 — 路由：GET /notifications + POST .../read（RED → GREEN）

**目标**：spec §1.1 + §1.2。

**RED**：写 `tests/test_notifications_route.py`：
- 未登录 GET → 302 /login
- 登录但无通知 → 200 + unread_count=0 + events=[]
- 登录有通知 → 200 + 包含
- POST .../read 自己的 → 200 + DB read_at set
- POST .../read 别人的 → 404
- 重复 POST .../read → 200（幂等）

**GREEN**：
- `blueprints/notifications.py::notifications_feed` (GET) → 调 `list_for_user(g.user.id, unread_only=...)` → 渲染 HTML 或 JSON (Accept 头)
- `blueprints/notifications.py::mark_notification_read` (POST) → 调 `mark_read(g.user.id, event_id)` → 200 或 404

**commit 序列**：
1. `test(notifications): RED feed + mark_read routes`
2. `feat(notifications): GET /notifications + POST /<id>/read routes`

---

### TASK 4 — 通知中心 HTML 页（让 E2E 有目标）

**目标**：`templates/notifications.html` 列出 events + 标记已读按钮。

**RED**：test_client GET /notifications (Accept: text/html) → 200 + 至少一个 event row 渲染

**GREEN**：简单 Jinja 模板 + 路由按 Accept 渲染。

**commit**：`feat(notifications): HTML page`

---

### TASK 5 — fanout 集成：test-emit 路由（dev-only）

**目标**：让 E2E 验证 fanout 工作（不接子项目 5）。

**RED**：test emit 后 → 当前用户的 GET /notifications 看到新通知。

**GREEN**：`POST /admin/notifications/test-emit` 接受 `{event_type, summary, target_url, user_ids?}` → 调 `emit_event(...)`。user_ids 缺省 = 所有用户。

**commit**：`feat(notifications): dev-only test-emit route`

---

### TASK 6 — 集成验证 + PR 描述

**目标**：跑全套 pytest + 写 PR 描述 + push draft PR。

---

## 2. 任务依赖图

```
TASK 1 ──► TASK 2 ──► TASK 3 ──► TASK 4 ──► TASK 5 ──► TASK 6
                ▲
                └── (TASK 2 可在 TASK 1 后并行)
```

---

## 3. 验证门

- `pytest -q` → 0 失败（预期 ≥ 145 passed）
- 新代码 ruff clean
- 草稿 PR body 含：
  - 引用 PRD §2.5.4
  - 文件清单
  - pytest 末尾
  - 7 个 spec 自决项显式声明
  - Open questions：顶 nav 红点（前端 future work）、`notification_prefs.muted` 未实现（future work）

---

## 4. 风险

- **顶 nav 红点不在本子项目**——spec §0 显式声明。如果 owner 要求"全链路含前端"，需要 1 个前端 PR，不在本子项目范围。
- **`notification_prefs.muted` 字段建表但未读**——预留 future work。
- **`emit_event` 失败语义**：本子项目**不**自动重试，调用方负责。如果子项目 5 配方发布成功但 fanout 失败 → 用户看不到通知，需手动重发。**后续 spec 可加 retry 队列**。
