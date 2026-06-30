# Subproject 2 — Procurement Suggestions (PRD §2.2)

## Summary

Implements per-store and hub-level procurement suggestions: derive a
safety stock from the forecast's daily-average, compute suggested
replenishment quantity, expose JSON + HTML views, and accept suggestions
into a 5-column CSV. Auto-invalidates the cache when outbounds /
restocks / stocktakes / adjustments complete, satisfying PRD §1.1 A2
("库存变动后自动重算").

**Stacked PR**: this branch (`feat/procurement`) is based on
`feat/forecast` (subproject 1) because the procurement safety-stock
formula consumes the same `daily_avg` that subproject 1's
`/forecast/item/<id>` computes. Subproject 1 is in PR #1 (DRAFT,
awaiting merge). When subproject 1 merges to `main`, this branch will
rebase cleanly on top; until then, the diff against `main` shows
both subprojects together. Reviewer should look at the **6 subproject-2
commits only** (`docs(procurement): spec + plan` … `feat(procurement):
HTML page`) — the earlier commits are subproject 1 already covered in
PR #1.

## PRD refs

- §1.1 A2 — must-automate: 库存变动后自动重算 ✓
- §1.1 A3 — must-automate: 补货预警推送 (本子项目**只算不算推**；推送 = 子项目 3 通知总线)
- §2.1 — 依赖预测 daily_avg 口径 ✓
- §2.2.2 — 数据契约 ✓
- §2.2.3 — 安全库存公式 (PRD 锁定) ✓
- §2.2.4 — 采纳仅生成 CSV (PRD 锁定) ✓
- §2.2.5 — 重算时机 (invalidation-based 满足 5s 窗口) ✓
- §2.2.6 — 异常表 ✓
- §2.2.7 — 测试要点 ✓
- §3.2/3.3/3.5/3.6 — TDD + 三层测试 + 验证门

## Files changed (subproject 2 only)

```
blueprints/procurement.py            NEW   routes + cache helpers
blueprints/procurement_pure.py       NEW   compute_safety_stock / suggested_qty / aggregate_hub
blueprints/outbound.py               MOD   mark_procurement_invalid() in submit (1 line)
blueprints/restock.py                MOD   mark_procurement_invalid() in submit (1 line)
blueprints/stocktake.py              MOD   mark_procurement_invalid() in approve (1 line)
blueprints/adjustment.py             MOD   mark_procurement_invalid() in submit (1 line)
db/__init__.py                       MOD   procurement_config + procurement_cache schema + seed
templates/procurement_store.html     NEW   minimal page
tests/test_procurement_pure.py       NEW   16 unit tests
tests/test_procurement_route.py      NEW   15 integration tests
docs/superpowers/specs/...procurement-design.md
docs/superpowers/plans/...procurement-plan.md
```

## Test plan

| Layer | Count | Coverage |
|---|---|---|
| Unit | 16 | safety_stock (5: zero/basic/max/min/high), suggested_qty (6: zero/ceil/over/frac-up/negative/int), aggregate_hub (5: empty/single/two/zero-filter/sort) |
| Integration | 15 | 404 unknown, warm appears, cold excluded, oversupplied excluded, config respected, response shape, hub aggregates 2 wh, hub empty, CSV 4-col + BOM, empty 400, download ok, traversal rejected, cache write, mark invalid, staff own wh |
| **Total new** | **31** | (all in this PR; existing 98 tests unchanged) |

## `pytest -q` output

```
........................................................................ [ 55%]
.........................................................                [100%]
129 passed in 2.35s
```

## `ruff check .` output

Same status as subproject 1: deferred. New code is ruff-clean.

## E2E evidence

**Manual verification needed.** Same reason as subproject 1 — no
Playwright server reachable in dev environment. The 5 user actions
(manager opens /procurement/store, sees suggestions, clicks 采纳, gets
CSV download; staff checks own warehouse only) are exercised by
integration tests; the visual flow is in spec §7.3 for owner to click
through.

## Must-automate checklist (PRD §1.1 A2)

| Trigger | Where wired | Operator surface |
|---|---|---|
| 出库提交 | `blueprints/outbound.py::outbound_submit` 末尾 → `mark_procurement_invalid(item_id)` | next GET /procurement/store 命中 invalid → 重算 |
| 入库提交 | `blueprints/restock.py::restock_submit` 末尾 → 同 | 同 |
| 盘点 approve | `blueprints/stocktake.py::approve` 末尾 → 同 | 同 |
| 调整单提交 | `blueprints/adjustment.py::adjustment_submit` 末尾 → 同 | 同 |

A3 (补货预警推送总仓待办) 留子项目 3 通知事件总线完成。

## Open questions / risks

1. **Spec self-decisions to confirm**:
   - `min_absolute` 全局默认 = 0；品项级覆盖未实现（PRD 写"可在品项级别覆盖"，本子项目只到全局层）。
   - `in_transit_qty` = `restock_requests` 中 `status NOT IN ('已到货', '已取消')` 的 `requested_quantity` 之和（restock_requests 无 rolled_back 列）。
   - CSV 列：5 列 `item_id, item_name, suggested_qty, unit, note`（PRD §2.2.4 写"四列"，spec 加 unit + note 5 列；owner review 时可减回 4 列）。
   - CSV 临时文件 24h 清理**未实现**——记录在 spec 8 风险 + 本 PR 末尾。Owner 如要求本子项目做，需加 1 TASK。
   - 5s TTL vs invalidation：spec 选 invalidation（更精确）。如 owner 倾向 TTL，改 `_store_procurement_json` 一行。
   - `procurement_cache` 不存 `item_name`（节省空间），读时回查 warehouse db。两次读路径有微小不一致窗口（cache 写完 → 读时再查 name），对调用方无可见影响。

2. **Stacked PR caveat**: 本分支基于 `feat/forecast`（未合并）。Owner
   评审时应只看后 6 个 commit（subproject 2 范围）。当 #1 merge to
   `main` 后，本分支 rebase 会自动快进；如有冲突（4 个 hook
   blueprint 的 import 顺序），按 git 提示解决。

3. **`/procurement/store` HTML 路由的 Accept-header 探测**：用
   `Accept` 头判 JSON vs HTML（spec §0.4）。如果 owner 倾向用
   `?format=html` 显式参数 + 默认 JSON，本分支可在 review 时改。

4. **A3 推送补货预警未实现**：spec §0.6 显式声明本子项目不实现
   推送（属于子项目 3 通知事件总线）。如果 owner 要求本子项目做，
   需要在子项目 3 之前补一个 TASK（约 30 行代码 + 1 个集成测试）。

5. **`g.user["is_admin"]` 在 sqlite3.Row 上是索引访问**：本子项目
   重蹈子项目 1 的覆辙——`g.user` 是 Row 而非 dict。已用 `["is_admin"]`
   而非 `.get()` 避免 AttributeError。代码注释里说明。

6. **per-warehouse 访问检查绕过 admin**：`_user_can_access_warehouse`
   在 `g.user["is_admin"]` 为真时直接返回 True。**这是 spec
   决定**：platform admin 在所有仓库上有全集权限。Owner review 时
   可收紧（要求 admin 也必须有 binding）。

7. **测试间状态隔离依赖 tmp_path + monkeypatch**：与子项目 1 相同
   模式（conftest.py 的 `logged_client` fixture）。本子项目新增
   `staff_client` 已存在。**没有**新增 master.db 全局 fixture。
   这意味着单测必须按 fixture 隔离——`test_mark_procurement_invalid_sets_flag`
   直接写 master.db 后再 mark，必须在 `with app_context()` 内。
   已验证可重复运行。

## Auto-subproject runbook

Subproject 3 (notifications event bus) is the next branch off `main`
(it doesn't depend on subproject 2's procurement code; only on the
shared `g.warehouse`/session model that already exists). The runbook
is unchanged.

---

## Post-merge cherry-pick

After this PR was opened, subproject 2's `/procurement/store` was
updated to use the same "consumption" source as `/inventory` and
`/forecast` (preview-phase1 commits 1e7f2ba + 3fbb1dd):

1. `_weighted_daily_avg` and `_outbound_30d_sum` switched from
   outbound-only to outbound + production union.
2. The cold-start counter (used to filter items with n<7 records in
   30 days) was also switched to the union source, so it no longer
   hides items that have plenty of production-side consumption.

The updated branch includes these fixes as a follow-up commit:
`fix(procurement): align consumption source with /inventory
(cherry-pick from preview-phase1)`. The shared `blueprints/consumption.py`
module is the source of truth for this SQL.
