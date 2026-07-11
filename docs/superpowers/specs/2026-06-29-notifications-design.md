# 通知事件总线 — 子项目 3 spec

**版本**：v1（2026-06-29）
**状态**：实施中。引用总览 PRD §2.5.4。
**前置依赖**：无（独立子项目）。
**下游消费者**：子项目 5（配方发布 + 通知）、子项目 6（Agent MPC feed 预留）、子项目 4（品项发布未来对接）。
**目标读者**：实施 agent。

---

## 0. 引用与本 spec 自决

引用 PRD：
- §2.5.4 — 通用事件总线设计
- §2.5.5 — 数据契约（GET /notifications、POST .../read、POST /admin/publish/recipe）
- §2.5.6 — 异常表
- §2.5.7 — 测试要点
- §1.1 A6 — 登录后顶部红点自动显示（**本子项目只做数据层 + API；前端 nav 红点属于前端工作，留 future**）
- §3.6 — /admin/health 可观测性

**本子项目范围**（PRD §2.5.4 显式定义）：

- **事件源枚举**：`event_types` 枚举（recipe_published / item_published / system_alert）——**本子项目首期只实现 `recipe_published`**（PRD §2.5.4）。其他两个 schema 预留但**不实现 fanout**。
- **渠道**：`channels` 表（web / email / im）——首期只实现 `web`。
- **用户偏好**：`notification_prefs` 表（按 user × event_type × channel）。
- **Web 体验**：通知中心 `/notifications` + 标记已读 API。

**不在范围**（PRD §2.5.4 末段）：
- toast 弹窗
- 邮件 / IM / 推送
- 顶 nav 红点（前端）

本 spec 自决项：

1. **数据存储位置**：`notifications` 和 `notification_prefs` 在 **master.db**（跨店共享）。`channels` 和 `event_types` 是**枚举**，不存表，存 Python constants（更简单，避免 enum 表 + cache 一致性问题）。
2. **web 渠道语义**：每条 event 为**所有有 warehouse 访问权限的用户**生成一条 notification（"fanout at write"）。理由：web 渠道订阅不需要细化（PRD §2.5.4 写"按用户+事件类型+渠道存订阅/静音"——本子项目**只实现默认全部订阅**，静音未实现，PR 描述里声明 future work）。
3. **fanout 实现**：`recipe_published` 事件触发时，`publish_event` 函数直接 INSERT 一行 `notifications` (per user)。
4. **已读语义**：`notifications` 表加 `read_at` 列（NULL = 未读）。`POST /notifications/<id>/read` → set read_at=now。
5. **未读数查询**：`GET /notifications?unread=true` 返回 `{unread_count, events: [...]}`。
6. **target_url**：从 `summary` 字段 + `event_type` 拼装。`recipe_published` → `/products/<id>/versions/<version_id>`。本子项目**只把 product_id + version_id 存在 summary 字段**（如 `"经典柠檬茶 v3 已发布"`），**target_url 由前端从 summary 解析**（本子项目不在 summary 里塞 URL 字符串）。
7. **summary 长度限制**：≤ 200 字符（数据库 NOT VALID 但代码层强制）。

---

## 1. 数据契约

### 1.1 GET /notifications

```
GET /notifications?unread=true
→ 200 {
    "unread_count": 3,
    "events": [
        {
            "event_id": 88,
            "event_type": "recipe_published",
            "summary": "经典柠檬茶 v3 已发布",
            "created_at": "2026-06-29T...",
            "target_url": "/products/12/versions/3",
            "read": false
        }
    ]
}
```

**本子项目实现补充**：
- `unread=true` 过滤未读；缺省（`unread=false` 或缺失）→ 全部。
- 倒序按 `created_at` DESC。
- 最多 100 条（避免一次拉太多）。

### 1.2 POST /notifications/<event_id>/read

```
POST /notifications/88/read
→ 200 { "ok": true }
```

**本子项目实现补充**：
- 必须是当前用户的 notification（`user_id = g.user.id`）→ 否则 404。
- 幂等：已读再调也返回 ok。
- 用户无登录态 → 302 /login（由 `require_login` 处理）。

### 1.3 POST /admin/publish/recipe

**这是子项目 5 的工作**。本子项目**只**提供底层 fanout 函数 `emit_event(event_type, summary, target_url)`。子项目 5 在发布成功后调它。**本子项目不暴露 HTTP 触发器**——避免被错误调用。

实际上为方便 E2E 测试，**本子项目提供一个 dev-only `POST /admin/notifications/test-emit` 路由**（仅 admin），但 PR 描述里标注"测试用，prod 应禁用"。

---

## 2. 异常

| 场景 | 行为 |
|---|---|
| 用户无登录态访问 /notifications | 302 /login |
| 读别人的 notification | 404 |
| emit_event 传非法 event_type | ValueError（raise 给调用方，**不**写 DB） |
| summary > 200 字符 | raise ValueError |
| user_ids 列表为空 | raise ValueError |
| 写 notifications 失败 | access.log + 返回 500（API 调用方处理） |

---

## 3. 必须自动化（PRD §1.1 A6）

- 配方发布后全站通知自动出现（**实现 = fanout 写 notifications 表**）。
- 本子项目**不**实现前端的红点（属于前端工作）。**通知中心的"未读"列表本身**就是 A6 的"通知自动出现"产品证据。

---

## 4. 错误处理与可恢复性

- **不可逆动作**：写入 `notifications` 表。**不可**"取消已读"或"删除通知"（保留历史）。
- **可回滚动作**：无。
- **失败兜底**：`emit_event` 失败 → 写 `access.log` 含 `notification_emit_fail` → 业务层 catch 决定是否重试。**本子项目**的 `emit_event` 函数**不**自动重试，**不**写 access.log（由调用方决定）。

---

## 5. 测试矩阵

### 5.1 单元

- `emit_event(event_type, summary, target_url, user_ids)`:
  - 正常 → 写 N 条 notifications（N = len(user_ids)）
  - 空 user_ids → ValueError
  - 非法 event_type → ValueError
  - summary > 200 → ValueError
- `mark_read(user_id, event_id)`:
  - 存在且未读 → set read_at, return True
  - 不存在或已读 → return False（幂等）
- `list_for_user(user_id, unread_only)`:
  - unread_only=True → 只返回未读
  - unread_only=False → 全部
  - 按 created_at DESC
  - limit 100

### 5.2 集成

- `GET /notifications`（未登录）→ 302 /login
- `GET /notifications`（登录，unread=true，无通知）→ 200 + unread_count=0 + events=[]
- `GET /notifications`（登录，有通知）→ 200 + 包含 events
- `POST /notifications/<own_id>/read` → 200 + DB read_at 已设
- `POST /notifications/<other_user_id>/read` → 404
- 重复 POST .../read → 仍 200（幂等）
- 路由 dispatch `emit_event` 后 GET /notifications 看到新通知

### 5.3 E2E

- admin 触发 fanout（test-emit）→ 任意用户登录 → /notifications 看到红点（**前端红点不在本子项目**，但消息中心能看到事件）

---

## 6. 文件清单

- 新增 `blueprints/notifications.py`（blueprint + routes）
- 新增 `blueprints/notifications_pure.py`（emit_event / mark_read / list_for_user）
- 修改 `db/__init__.py`（`notifications` + `notification_prefs` schema）
- 修改 `app.py`（注册 blueprint）
- 新增 `tests/test_notifications_pure.py`
- 新增 `tests/test_notifications_route.py`
- 新增 `templates/notifications.html`（最小通知中心）

---

## 7. 验收门

1. `pytest -q` 全绿
2. 新代码 ruff clean
3. PRD §2.5.4 锁定项（通用事件总线 / Web 渠道首期 / 顶层红点由前端实现）显式保留
4. 子项目 5（配方发布）能调 `emit_event("recipe_published", ...)` 写通知
5. PR 描述引用 PRD §2.5.4
