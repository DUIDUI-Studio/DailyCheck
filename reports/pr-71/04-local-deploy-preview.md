# 报告 04/4：部署预演报告（本地预演，未上生产）

> 生成方式：`scripts/agent_framework/preview.sh 71` + 本次人工补充（真实进程启动 + 真实 HTTP 冒烟）
> 模板：[docs/agent-framework/templates/04-local-deploy-preview.md](../../../docs/agent-framework/templates/04-local-deploy-preview.md)
> 对应 Issue：#47　分支：`fix/issue-47-recipe-bom-delete-index`

## 1. 元信息

| 字段 | 值 |
|------|-----|
| PR | #71 |
| 分支 | `fix/issue-47-recipe-bom-delete-index` |
| 基线 | origin/main @ adc71c7 |
| HEAD | ed665e7 |
| compose 文件 | 未使用（本次无容器/端口/依赖变更） |
| 预演方式 | 真实进程 `flask --app app run`（127.0.0.1:8099）+ 独立库目录 `/tmp/dc-preview-71/db` |
| Dev Agent | preview.sh + 人工补充 |
| 生成时间 | 2026-10-27T22:37:30+08:00 |

## 2. 环境对齐检查

| 项 | 本机（dev 预演） | wdg_vps (prod) | 一致性 |
|---|---|---|---|
| Python | 3.13.12 | 3.12.x | ⚠️ minor（本次无版本特性依赖） |
| SQLite | 3.45 | 3.37 | ⚠️ minor（本次只用标准 SQL） |
| 系统包 | macOS | ubuntu 22.04 | ⚠️ 已知差异，本次无新增依赖 |
| 新增依赖 | 无 | 无 | ✅ |
| DB schema 变更 | 无 | 无 | ✅（本次不碰 schema，无需迁移） |
| 监听端口 | 8099（预演临时端口） | 8090/8091 | ✅ 隔离 |

**已知差异说明**：不影响本次预演结论 —— 本次改动只涉及视图层的表单解析与两条新路由，无版本特性依赖。

## 3. 启动日志摘要

真实进程启动（`DAILYCHECK_DB_DIR=/tmp/dc-preview-71/db flask --app app run --host 127.0.0.1 --port 8099`），
数据库使用独立预览库（master.db + warehouses/WH_PRE.db，与仓库内 db/ 完全隔离），启动后探活：

```
 * Serving Flask app 'app'
 * Debug mode: off
WARNING: This is a development server. Do not use it in a production deployment. Use a production WSGI server instead.
 * Running on http://127.0.0.1:8099
Press CTRL+C to quit
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET /health HTTP/1.1" 200 -
```

`GET /health` → `200`，响应体 `{"forecast_last_success_at":null,"forecast_lock_failures":0,"status":"ok"}`。
应用进程无 traceback、无 schema 迁移动作（本次不改 schema）。

## 4. 启动日志摘要（关键路径请求日志）

启动后的完整请求序列（真实 HTTP，含会话登录 → 选仓 → 打开两个研发配方编辑页 → 建配方 → 删行 → 撞重复）：

```
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET  /health                              200
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET  /login                               200
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET  /select-warehouse                    200
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET  /recipe-cost/ic-recipes/new          200
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET  /recipe-cost/recipes/new             200
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET  /recipe-cost/ic-recipes/new          200
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET  /recipe-cost/ic-recipes/new          200
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET  /recipe-cost/ic-recipes/3/edit       200
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET  /recipe-cost/ic-recipes/3/edit       200
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET  /recipe-cost/ic-recipes/3/edit       200
127.0.0.1 - - [27/Oct/2026 22:37:20] "GET  /recipe-cost/ic-recipes/3/edit       200
```

进程内无任何 traceback（无 500）。逐条响应的实测结果：

| 请求 | 结果 |
|---|---|
| `POST /login` | 302 → 登录成功 |
| `POST /select-warehouse/1` | 302 → 选仓成功 |
| `GET /recipe-cost/ic-recipes/new` | 200（冰激凌配方新建页渲染） |
| `GET /recipe-cost/recipes/new` | 200（出品配方新建页渲染） |
| `POST /recipe-cost/ic-recipes/new`（3 行原料） | 302 → 配方创建成功，跳转编辑页 |
| `POST /recipe-cost/ic-recipes/3/bom/6/delete`（删中间那行） | 302 → 剩余行 `[(5, 1, 500.0), (7, 3, 10.0)]`，**只删目标行** |
| `POST /recipe-cost/ic-recipes/3/bom/<B配方行>/delete`（跨配方，AJAX） | **409** `{"ok":false,"reason":"business_rule","message":"该配方行不存在或不属于此冰激凌配方"}`，B 配方 3 行全留 |
| `POST /recipe-cost/ic-recipes/3/edit`（两行同一原料） | 302 + 页面提示「保存失败：第 2 行的原料与配方中其它行重复，本次修改已全部撤销」，**回滚后剩余行原样保留** |

## 5. 关键路径冒烟

| 路径 | 方式 | 状态 | 说明 |
|---|---|---|---|
| 进程启动 + `/health` | 真实进程 + curl | 通过 | 见 §3 |
| 两个研发配方编辑页渲染 | 真实 HTTP | 通过 | `ic_recipe_edit` / `recipe_edit` 均 200 |
| 编辑页表单契约 | 真实 HTTP | 通过 | 页面出现 `name="bom_delete"` 次数 **0**；行尾是 3 条自带 `bom_id` 的删除路由 |
| 原料行删除（只删目标行） | 真实 HTTP | 通过 | 删中间行 → 前后两行 id / 原料 / 用量原封不动 |
| 跨配方删行被拒 | 真实 HTTP（AJAX） | 通过 | 409 `business_rule`，对方配方数据未动 |
| 保存撞重复原料 | 真实 HTTP | 通过 | 整批回滚 + 明确报「第 2 行」；原数据保留 |
| 定向回归用例 | pytest | 通过 | `tests/test_recipe_bom_delete.py` 20 例全绿 |
| 相关模块回归 | pytest | 通过 | `test_recipe_cost_route` / `test_recipe_cost_pure` / `test_route_permission_matrix` / `test_production_recipe_archive` / `test_ajax_form` 等 148 例通过（1 例 pre-existing 失败，见 §6） |
| 全量 pytest | pytest | 通过 | `14 failed, 2494 passed`；14 条全部在 `tests/known_failures.txt` 内，`check_known_failures.py` exit 0（零新增失败） |
| T11 门店订货端到端 | `scripts/dev_smoke_t11_e2e.py` | **pre-existing 失败** | 见 §6 |

## 6. 阻塞与已知问题

**T11 冒烟失败为本仓库既有问题，不属本次改动范围。**
对照证据：把本次改动涉及的 6 个文件全部 checkout 回 `origin/main` 版本后**重跑同一脚本**，仍在同一断言处失败：

```
[1] storefront 加购 OK
  File "scripts/dev_smoke_t11_e2e.py", line 110, in e2e_smoke
    assert "提交成功" in body
AssertionError
```

该脚本走的是门店订货链路，与本次改动的两个研发配方页无交集。

**回滚演练（清理干净）**：预演进程已终止，预演库位于 `/tmp/dc-preview-71/`（独立目录，与仓库内 `db/` 完全隔离），删除该目录即可清干净；仓库工作树 `git status` 仅剩本 PR 的改动文件，无运行时文件（`*.db` / `*.log` / 截图）混入。

**代码回滚**：`git revert ed665e7`（单提交，可独立回滚）。**schema 回滚**：本次无 schema 变更，无需降级脚本。

## 7. 报告签名

```
Dev Agent: Codex（Orca dispatched worker · task_42b7a0d66a16）· ed665e7
报告生成时间: 2026-10-27T22:37:30+08:00
下次预演：可重复执行，结果幂等（预演库独立、进程即起即停）
```

> **门禁**：冒烟任一失败 / 资源异常 / 清理失败 → 阻塞 PR。
> **状态**：v0.1 半自动（T11 路径由 `preview.sh` 自动跑；配方页关键路径为本次人工补充的真实 HTTP 冒烟）。
