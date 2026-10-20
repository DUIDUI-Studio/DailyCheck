# 报告 04/4：本地部署预演报告（半自动 · v0.1）

> 生成方式：`scripts/agent_framework/preview.sh` + 本次人工补充
> 模板：[docs/agent-framework/templates/04-local-deploy-preview.md](../../../docs/agent-framework/templates/04-local-deploy-preview.md)

## 1. 元信息

| 字段 | 值 |
|------|-----|
| PR | #129 |
| 分支 | feat/item-loss-attribution |
| 基线 | origin/main @ 4c15d26 |
| compose 文件 | 未使用（本次无容器/端口/依赖变更） |
| Dev Agent | preview.sh + 人工补充 |
| 生成时间 | 2026-10-20T02:28:00Z |

## 2. 环境对齐检查

| 项 | Mac mini（dev） | wdg_vps (prod) | 一致性 |
|----|----------------|----------------|--------|
| Python | 3.12.13 | 3.12.x | ✅ same minor |
| SQLite | 3.45 | 3.37 | ⚠️ minor（本次仅用标准 SQL，无版本特性依赖） |
| 系统包 | macOS | ubuntu 22.04 | ⚠️ 已知差异，本次无新增依赖 |
| 监听端口 | 8089（预览临时端口） | 8090/8091 | ✅ 隔离 |
| 新增依赖 | 无 | 无 | ✅ |

## 3. 启动日志摘要

真实进程启动（`DAILYCHECK_DB_DIR=/tmp/dc-preview-db RUNAPP=1 PORT=8089 python app.py`），
数据库使用生产快照副本（master.db + warehouses/*.db），启动后探活：

```
 * Debug mode: off
WARNING: This is a development server. Do not use it in a production deployment.
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:8089
Press CTRL+C to quit
127.0.0.1 - - [20/Oct/2026 10:24:29] "GET /health HTTP/1.1" 200 -
```

`GET /health` → `200`，响应体 `{"forecast_last_success_at":null,"forecast_lock_failures":0,"status":"ok"}`。
应用进程无 traceback，schema 迁移（v2 → v4）在首次访问仓库库时自动完成，无异常。

## 4. 关键路径冒烟

| 路径 | 方式 | 状态 | 说明 |
|------|------|------|------|
| 进程启动 + `/health` | 真实进程 + curl | 通过 | 见 §3 |
| `/report/health` 渲染归因区块 | 路由级 pytest | 通过 | `tests/test_reports_loss_pure.py::test_health_page_renders_ledger_block`，断言页面含「品项误差台账」「R-BOM-ZERO」「建议动作」 |
| `/report/health/export` CSV 段 6 | 路由级 pytest | 通过 | `test_health_csv_has_ledger_section` |
| 台账表缺失时降级 | 路由级 pytest | 通过 | `test_health_page_survives_missing_ledger_table`，页面 200 且显示降级提示 |
| 旧库迁移幂等 | 真实库副本连跑两次 | 通过 | `user_version` 2→4，二次运行无异常，`source_type` 无 NULL 残留 |
| T11 门店订货端到端（`dev_smoke_t11_e2e.py`） | test client | **pre-existing 失败** | 见下方说明 |

**T11 冒烟失败为本仓库既有问题，不属本次改动范围。**
对照证据：在**同一 worktree 上暂存本次全部改动后**（`git stash push -u`，工作区回到干净 main @ 4c15d26）
重跑该脚本，仍在同一断言处失败：

```
[1] storefront 加购 OK
  File "scripts/dev_smoke_t11_e2e.py", line 110, in e2e_smoke
    assert "提交成功" in body
AssertionError
```

本次改动只触及 `production` / `outbound` / `stocktake` / `reports` 四个蓝图与 `db` schema，
不涉及门店订货提交链路；T11 失败已作为独立问题记录，不在本 PR 处理。

## 5. 资源基线

| 指标 | 观测值 |
|------|--------|
| 启动时间 | < 2s（dev server，快照库 ~700KB × 6） |
| 健康页单次请求新增开销 | 台账重算 584 行 DELETE+INSERT，实测 < 20ms（单仓量级） |
| 内存 | 未采集（dev server 无监控端点，Phase 2 接 compose 后补） |

## 6. 回滚演练（清理干净）

```
# 预览使用独立库目录 /tmp/dc-preview-db，与仓库内 db/ 完全隔离
rm -rf /tmp/dc-preview-db /tmp/pytest-dc3
# 代码回滚：单 PR 全量，git revert 该 merge commit 即可
# schema 回滚：item_loss_ledger 为新增表，production_run_items/outbound_requests
# 仅新增可空列（planned_default 有默认值 1），旧代码忽略新列即可运行 —— 无需降级脚本
```

## 7. 报告签名

Dev Agent: preview.sh + 人工补充 · 基线 4c15d26 · 工作分支 feat/item-loss-attribution
报告生成时间: 2026-10-20T02:28:00Z
下次预演：CI workflow 自动跑 + 本地手动可重跑

---

> **门禁说明**：本次为纯分析模块 + 可空列新增，无端口/依赖/部署布局变更。
> T11 失败已在干净基线上复现，判定 pre-existing，不阻塞本 PR；
> 已在 PR「生产侧事项」登记，供 Prod Agent 决策。
