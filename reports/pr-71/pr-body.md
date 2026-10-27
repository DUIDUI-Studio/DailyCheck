Closes #47

## 报告 1/4：需求对齐

| ID | 需求 | 状态 |
|----|------|------|
| R-001 | 研发配方编辑页的原料行删除改为独立路由、自带主键；路由内校验该行属于该配方，跨配方删行被拒绝 | ✅ 已实现 |
| R-002 | 「保存配方」表单不再提交 `bom_delete[]`，只负责新增与改用量 | ✅ 已实现 |
| R-003 | 保存撞唯一约束时整批 `rollback()`，明确指出第几行重复，`flash(..., "error")` 走 business_rule 409 语义 | ✅ 已实现 |
| R-004 | 两个研发配方页（`ic_recipe_edit.html` / `recipe_edit.html`）都覆盖回归；门店生产配方页不动 | ✅ 已实现 |

**根因**（两条，都是 PR #42 在门店生产配方页已修、研发这两页被明确排除的同型缺陷）：

1. **原料行删除索引错位（静默数据损坏，比 500 更危险）**
   `blueprints/recipe_cost.py` 的 `_save_ic_recipe()` / `_save_recipe()` 用 `request.form.getlist("bom_delete")` 与 `bom_row_id[]` **平行数组按索引配对**：`if i < len(deletes) and deletes[i] == "1"`。
   checkbox **未勾选就不提交** → `deletes` 长度必然短于 `bom_row_id` → 按索引整体错位：**勾第 3 行实际删掉第 1 行**。BOM 行与用量静默对不上，**全程不报错**。

2. **未捕获 IntegrityError → 500 + 半保存**
   `_save_ic_recipe()` 的 `except sqlite3.IntegrityError` 只 `flash(...)`、**不 `rollback()`**；且新建配方时配方行已 `commit()`、BOM 行随后才写 → 失败时留下「有配方、没 BOM」的半保存记录。
   同一表单两行指向同一原料 → `UNIQUE(ic_recipe_id, item_id)` 冲突 → 用户只看到笼统的「保存失败」，且不知道是哪一行、也不知道改动有没有落库。

**设计取舍**：

- **删除意图自带主键**（照搬 PR #42 已验证的做法）：新增两条路由，删除意图自带 `bom_id`，与任何数组顺序无关；路由内用 `WHERE id = ? AND <外键> = ?` 校验归属 → 不能借 A 配方的 URL 删 B 配方的行。不匹配时 `flash(..., "error")` → AJAX 侧是 409 `business_rule`（沿用 `blueprints/_helpers.py` 的 PR #42 语义）。
- **「保存配方」表单只负责新增与改用量**：行尾 checkbox 换成「删除」按钮，用 HTML5 `form` 属性关联到表格外的 `<form>`（外层已是 `bom-form`，不能嵌套 form，HTML 会截断 DOM）；未保存的新增行删除只摘 DOM（`static/recipe_cost.js` 的 `[data-remove-row]` 委托）。这样后端**不再需要任何 `bom_delete[]` 数组**，索引错位在结构上不可能再发生。
- **配方行与 BOM 行同生共死**：新建配方时配方 `INSERT` 不再提前 `commit()`（`audit()` 内部自带 commit，故 create 审计移到最终 commit 之后写）；任何一步失败 → 一次 `rollback()` 把配方行与 BOM 行一起撤销，不留半保存。
- **出品配方页的重复判定放在视图层**：`recipe_items` 是多态表（`item_id` / `ic_recipe_id` 二选一），**没有 UNIQUE 约束**，撞不出 IntegrityError。所以按 `(source_type, target_id)` 在本次提交内判重 —— 否则同一原料被静默记两行、用量与成本口径一起失真（与缺陷 1 同属静默损坏）。两页的失败语义因此一致：都报「第几行重复」。

**改动清单**（7 个文件，+约 260 / -约 90）：

| 文件 | 改动 |
|---|---|
| `blueprints/recipe_cost.py` | 新增 `ic_recipe_bom_row_delete` / `recipe_bom_row_delete` 两条路由；`_save_ic_recipe` / `_save_recipe` 去掉 `bom_delete[]`、改整批回滚 + 指明行号 + 配方行延迟 commit |
| `templates/recipe_cost/ic_recipe_edit.html` | 行尾 checkbox → 「删除」按钮（`form` 属性）+ 表格外每行一个删除表单；文案说明 |
| `templates/recipe_cost/recipe_edit.html` | 同上 |
| `static/recipe_cost.js` | 新增行的删除控件改为摘 DOM 的按钮（`[data-remove-row]`）；去掉 `bom_delete` checkbox 的合计逻辑 |
| `permissions_manifest.py` | 两条新路由登记为平台超管（`ROUTE_MATRIX` 双向校验用） |
| `tests/test_recipe_bom_delete.py` | **新增**：20 例回归（冰激凌配方页 10 + 出品配方页 9 + 跨页权限 1） |
| `docs/ARCHITECTURE.md` / `docs/ARCHITECTURE_MAP.md` | 新增 §13.13（研发配方页的删除与回滚契约）、§5 模块图标注、§12 测试登记、§14 风险登记；架构地图用 `scripts/arch_map.py --write` 重生成 |

**回归与变异验证**（参照 `tests/test_production_recipe_archive.py` 的 9 例模式，两个页面各覆盖）：

| 用例族 | 覆盖点 |
|---|---|
| 只删目标行 | 删中间那行，前后的行 id / 原料 / 用量原封不动 |
| 跨配方行拒绝 | 借 A 配方的 URL 删 B 配方的行 → 拒绝且对方数据不变 |
| 删除往返 | 删完再打开编辑页，该行不再出现、另一行还在 |
| AJAX 拒绝返 ok:false | 业务拒绝 → 409 `business_rule`；正常删除 → 200 `ok:true` |
| 重复原料回滚 | 两行指向同一原料 → 整批回滚 + 报「第 2 行」+ 原数据不变 |
| 正常保存不误伤 | 改用量的正常保存路径不受回滚逻辑影响 |
| 表单契约 | 保存表单里不再有 `bom_delete`，行尾按钮指向自带 `bom_id` 的删除路由 |
| 新建不留半保存 | 新建配方撞重复 → 回滚后配方行也不存在 |

变异验证（把修复临时还原成 bug 版，确认新测试真实 FAIL）：

| 变异 | 还原成什么 | 结果 |
|---|---|---|
| M1 | 整包还原成 `origin/main` 的 bug 版（blueprint + 模板 + JS） | **20 例中 17 例 FAIL** |
| M2 | 只还原缺陷 2（旧 `except IntegrityError: flash(...)` 不 rollback） | 3 例 FAIL（重复原料回滚 ×2 + 新建半保存 ×1） |
| M3 | 只还原缺陷 1（把 `bom_delete[]` checkbox 放回保存表单） | 1 例 FAIL（表单契约） |
| M4 | 删除路由无视自带 `bom_id`、恒删第一行 | 1 例 FAIL（只删目标行） |
| M5 | 删除路由去掉「该行属于该配方」校验 | 2 例 FAIL（跨配方拒绝 + AJAX ok:false） |

**不在范围（明确排除）**：

- **门店生产配方页不动**（`blueprints/production.py` + `templates/production/*`）—— PR #42 已修且已合并，本 PR 不碰。
- **审核中发现的另一处同族缺陷，本次不修**（已记入 `docs/ARCHITECTURE.md` §14 风险清单）：`recipe_edit.html` 的多态行每行只渲染「当前类型」的那一个 select（`bom_item_id` 或 `bom_ic_recipe_id`），浏览器提交时这两个数组**各自按类型计数**，与 `bom_row_id[]` **不按位对齐**；后端按 `item_ids[i]` / `ic_ids[i]` 取值 → **混合来源**的出品配方保存时部分行被静默跳过、甚至写错行（行序为 [ic_recipe, item, item] 时第 2 行可能被写成第 3 行的原料）。根因在**模板渲染 ≠ 表单契约**，与本单的两条缺陷不同源，按 workflow.md D2「顺手发现的其他 bug 另开 issue」不在本 PR 修。**提醒：改混合来源的出品配方前先核对行数与用量。**
- 不改 DB schema、不改 `tests/known_failures.txt`、不改 CI workflow。

## Checklist

- [x] 报告 01 的 R 条目全部消解（表格内无未消解标记）
- [x] ruff 改动文件零告警（`blueprints/recipe_cost.py` / `permissions_manifest.py` / `tests/test_recipe_bom_delete.py`）
- [x] pytest 零新增失败：`14 failed, 2494 passed`，`scripts/check_known_failures.py` exit 0（14 条全部在 `tests/known_failures.txt` 内；基线为 `14 failed, 2474 passed`，新增 20 例全绿）
- [x] `tests/mcp_server` 单独跑 `2 failed, 104 passed`，与 `origin/main` 同结果（该目录 CI 不跑，本地失败项全在已知清单内）
- [x] 关键断言做了变异验证（M1–M5，见上表）
- [x] 触及架构子系统已同步 `docs/ARCHITECTURE.md`（§5 / §12 / §13.13 / §14），`docs/ARCHITECTURE_MAP.md` 已用 `scripts/arch_map.py --write` 重生成
- [x] 未提交运行时文件（db/、logs/、.venv/ 等）

## 生产侧事项

**变更类别**：代码逻辑（无 schema 变更、无新增依赖、无 CI/部署配置变更）。

**影响子系统**：研发中心配方域 —— `recipe_cost` 蓝图的冰激凌配方编辑页（`/recipe-cost/ic-recipes/<id>/edit`）与出品配方编辑页（`/recipe-cost/recipes/<id>/edit`）的**保存语义**与**原料行删除入口**；新增两条平台超管路由。门店生产配方页与门店日常路径不受影响。

**部署参考**：标准蓝绿流程，无需额外前置、无需 schema 迁移。部署时应套用流程的哪些环节：候选部署的验证项里，配方页的冒烟要覆盖「打开配方编辑页 → 删一行原料 → 改一行用量并保存 → 故意提交两行同一原料」这三条路径。

**观察窗重点看**：切换后配方编辑页是否 200；原料行删除后页面刷新是否仍显示该行（若删除路由未生效，会看到行还在）；保存失败时前端是否弹「操作失败」并整页刷新而不是静默成功（`flash(..., "error")` → 409 `business_rule`）；是否出现新的 traceback（`IntegrityError` 不应再冒成 500）。

**风险声明**：低-中。行为变化有三处：(1) 配方行删除从「勾选 + 保存」变成「点行尾按钮立即生效」，属于交互变化，用户需重新适应；(2) 出品配方页现在**拒绝**两行指向同一原料并报错行号（此前静默记两行），存量若有重复行的配方，保存时会提示第几行重复、需先删掉一行；(3) 保存失败时整批回滚，改动全部丢失（此前是半保存），用户需重新提交一次。以上均为「把静默损坏改成显式拒绝」，方向上是修复而非回退。

---

**Dev Agent: Codex（Orca dispatched worker · task_42b7a0d66a16）· 2026-10-27 CST**
