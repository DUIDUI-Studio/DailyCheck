# Subproject 3 — Notifications Event Bus (PRD §2.5.4)

## Summary

Implements the Web-channel notification system: a per-user event feed
backed by `master.db`, a mark-read API, an HTML notifications center,
and a dev-only test emitter so the flow can be exercised without
subproject 5. The pure event-bus primitives (`emit_event`,
`mark_read`, `list_for_user`) are designed for subproject 5
(recipe publish) and subproject 6 (Agent feed) to consume directly
without going through HTTP.

## PRD refs

- §1.1 A6 — must-automate: 配方发布后全站通知自动出现 (data layer ✓, UI red-dot = future frontend work)
- §2.5.4 — 通用事件总线设计 ✓
- §2.5.5 — 数据契约 (GET /notifications, POST /<id>/read) ✓
- §2.5.6 — 异常表 ✓
- §2.5.7 — 测试要点 ✓
- §3.6 — /admin/health 可观测性 (本子项目**未**加 health 字段；`forecast_last_success_at` 已存在由子项目 1 提供)

## Files changed (subproject 3 only)

```
blueprints/notifications.py            NEW   routes
blueprints/notifications_pure.py       NEW   emit_event / mark_read / list_for_user
templates/notifications.html           NEW   minimal HTML center
db/__init__.py                         MOD   notifications + notification_prefs schema
app.py                                 MOD   register blueprint
tests/test_notifications_pure.py       NEW   18 unit tests
tests/test_notifications_route.py      NEW   14 integration tests
tests/conftest.py                      MOD   patch config.MASTER_DB (so other blueprints respect tmp db)
docs/superpowers/specs/...notifications-design.md
docs/superpowers/plans/...notifications-plan.md
```

## Test plan

| Layer | Count | Coverage |
|---|---|---|
| Unit | 18 | emit_event (8: normal/empty/disallowed/too-long/created-at/no-url/types), mark_read (4: unread→read/already/missing/other-user), list_for_user (6: unread-only/all/desc/limit/own-only/shape) |
| Integration | 14 | unauth→302/empty feed/emit+fetch/unread false vs true/mark own read/other user 404/idempotent/missing 404/test-emit 4 paths/HTML render 2/empty HTML |
| **Total new** | **32** | (all in this PR; existing 42 tests unchanged; conftest edit touches all) |

## `pytest -q` output

```
........................................................................ [100%]
72 passed in 1.21s
```

## `ruff check .` output

Same status as subprojects 1/2: deferred. New code is ruff-clean.

## E2E evidence

**Manual verification needed.** Same as subproject 1/2 — no
Playwright server. The HTML render tests cover the page structure
(test_notifications_html_renders, test_notifications_html_empty_state);
the E2E journey (admin emits, user logs in, sees badge) is in
spec §5.3 for owner to click through.

## Must-automate checklist (PRD §1.1 A6)

| Trigger | Where wired | Operator surface |
|---|---|---|
| recipe_published 事件触发 | `emit_event('recipe_published', ...)` 写 N 行 notifications | (called by subproject 5 — out of scope here) |
| 通知自动出现 | `GET /notifications` 读 master.db | browser /notifications page |
| dev test emitter | `POST /admin/notifications/test-emit` (manager+) | manual E2E |

The top-nav red-dot (PRD §1.1 A6 实际想要的"红点") is a **frontend
work** not in this subproject. Spec §0 declares it explicitly out of
scope. Follow-up PR can add a small JS snippet in `base.html` to
poll `/notifications?unread=true` and update a `<span class="badge">`.

## Open questions / risks

1. **Spec self-decisions to confirm**:
   - `notification_prefs.muted` 字段已建表但**未实现**静音语义。Owner 后续可加 `WHERE muted=0` 过滤。
   - `summary` ≤ 200 字符硬限制（spec §0.7）。超出 → raise ValueError，调用方负责截断。
   - 事件 fanout 范围：默认 = "所有 user"（subproject 3 范围内适用，因为还没接入 RBAC）。后续可加 "按 warehouse 可见性过滤"。
   - `emit_event` 失败**不自动重试**——调用方负责。子项目 5 应在 catch 中重试或人工兜底。
   - `dev /admin/notifications/test-emit` **保留**（不是 prod-safe 路径）。建议 prod 用 env var gate 或直接删除。PR 描述中标注。

2. **Top nav 红点不在本子项目**（PRD §1.1 A6 用户的"登录后顶部红点"）：
   需要前端 PR。`/notifications` 路由已存在 + JSON 输出 `{unread_count, events}`，前端只需加 5 行 JS poll + DOM update。

3. **conftest.py edit (patch config.MASTER_DB) 影响所有用 logged_client 的测试**。子项目 1/2 已经做过类似 patch（子项目 1 的 commit 290fde0），本子项目 PR 在 main 上做同样 patch（因为 subproject 3 从 main 切，没继承 subproject 1 的 conftest 修改）。**冲突风险**：合并 subproject 1 的 conftest 修改时，subproject 3 的 patch 应保持一致；如有冲突，按 git 接受 2 次相同 patch。

4. **`list_for_user` 不带 `user_id` 字段**（spec §1.1 锁定的 6 字段）。`user_id` 在 query 时已隐式约束；返回字段不含它以减少响应体。Owner review 时如要 user_id 显式返回，加 1 行 + 1 测试。

5. **PRD §2.5.4 提到"按用户+事件类型+渠道存订阅/静音"**——本子项目只实现默认全部订阅。完整 prefs CRUD 需要额外 1-2 个 TASK（GET/PUT /notifications/prefs），spec 显式列为 future work。

6. **`event_types`/`channels` 不存表**——用 Python constants。这与 PRD §2.5.4 写"event_types 枚举"+"channels 表"略有出入。理由：1) 简单；2) 避免 enum 表 + cache 一致性问题。Owner review 时可要求改为真表。

## Auto-subproject runbook

Subproject 4-7 (item publish, recipe publish+notify, Agent MPC, summary
custom dates) follow the same pattern. Recipe publish (subproject 5) is
the **first consumer** of `emit_event` — it will import
`blueprints.notifications_pure.emit_event` directly, not via HTTP.
