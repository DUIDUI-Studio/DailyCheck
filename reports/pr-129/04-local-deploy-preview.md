# 报告 04/4：本地部署预演报告（PR #129 补录快照）

> **补录说明**：本路径原有一份来自其它分支（`feat/item-loss-attribution`）的残留内容，
> 与本 PR 无关。PR #129 合并后按真实执行记录重写。
> CI 每次运行 `scripts/agent_framework/preview.sh` 会在工作区重写同名文件供四报告门禁读取，
> 但不会回提交；本文件是仓库内的归档快照。

## 1. 元信息

| 字段 | 值 |
|------|-----|
| PR | #129 |
| 分支 | `feat/multi-warehouse-env-switch` → main（merge commit `83c11be`） |
| 基线 | `origin/main` @ `b970992` |
| 变更文件 | `config.py`、`blueprints/store_ordering.py`、`docs/ARCHITECTURE.md`、`tests/test_store_ordering_dc_route.py`（+76 / −5） |
| 变更性质 | 开关取值来源（代码常量 → 环境变量）；DC 发货入口与路由权限判定对齐 |
| Dev Agent | 人工补录（基于 2026-11-02 的真实执行记录） |
| 生成时间 | 2026-11-02T14:50:00Z |

## 2. 环境对齐检查

| 项 | Mac mini（dev） | wdg_vps（prod） | 一致性 |
|----|----------------|----------------|--------|
| Python | 3.12.13 | 3.12.3（`.venv` 实测） | 同 minor |
| SQLite | 3.45 | 3.37（`journal_mode=delete`） | 仅标准 SQL，无版本特性依赖 |
| 系统包 | macOS | Ubuntu 22.04 | 本次无新增依赖 |
| 监听端口 | 本地 pytest 不占端口 | 8090（blue）/ 8091（green） | 隔离 |
| 数据库 | 临时库（`--basetemp`） | `/root/DailyCheck/db` 共享目录 | 蓝绿共库，必须同时生效 |

## 3. 启动日志摘要

生产采用 deploy-first：先部署分支提交、后合并。两次重启均为蓝绿同时进行，
`DAILYCHECK_MULTI_WAREHOUSE=1` 由 `/etc/dailycheck/app.env` 经 systemd drop-in 注入。

```
Nov 02 13:55:13 systemd[1]: Stopping dailycheck.service - DailyCheck Inventory Management...
Nov 02 13:55:13 systemd[1]: dailycheck.service: Deactivated successfully.
Nov 02 13:55:13 systemd[1]: dailycheck.service: Consumed 6.033s CPU time, 102.6M memory peak, 0B memory swap peak.
Nov 02 13:55:13 systemd[1]: Started dailycheck.service - DailyCheck Inventory Management.
```

重启后核验：

```
dailycheck        pid=2892651 state=active env=DAILYCHECK_MULTI_WAREHOUSE=1
dailycheck-green  pid=2892704 state=active env=DAILYCHECK_MULTI_WAREHOUSE=1
GET /health  → 8090=200 8091=200
journalctl --since "-90s" | grep -iE "error|traceback|exception"  → 无输出
```

本地全量相关测试（改动前后各跑一次作对照）：

```
251 passed in 37.68s
```

## 4. 关键路径冒烟

| 路径 | 方式 | 状态 | 说明 |
|------|------|------|------|
| 多仓开关取值 | 进程内 `import config` | 通过 | 带 `DAILYCHECK_MULTI_WAREHOUSE=1` 为 True，不带为 False |
| 订货相关 9 个测试文件 | pytest | 通过 | 251 passed（改动前基线 250，新增 1 条回归） |
| 管理员无绑定进入 DC 详情 | 真实模板渲染 + 临时库 | 通过 | `test_admin_without_warehouse_binding_sees_ship_form`；去掉修复即打红（缺 `shipped_items[`） |
| 跨仓订单两仓视角 | 生产代码 + 生产库只读渲染 | 通过 | wh_000 只给自己的行出输入框，wh_005 行显示「由 蓝天大厦 发货」，反之亦然 |
| 生产健康检查 | `curl /health` | 通过 | 8090 / 8091 均 200 |
| 代码一致性 | 生产 HEAD 与 main 比对 | 通过 | 合并后 ff 到 `83c11be`，文件内容与运行中进程一致，无需重启 |

## 5. 资源基线

| 指标 | 观测值 |
|------|--------|
| 进程重启耗时 | systemd 单次 stop→start 亚秒级；应用可用 < 5s（第 6 秒探活已 200） |
| 内存峰值 | 102.6 MB（journal 记录） |
| 新增依赖 | 无 |
| 新增定时任务 / 端口 | 无 |

## 6. 回滚演练（不需回滚代码）

```
# 关：删掉环境变量并重启两个实例（秒级）
sed -i '/DAILYCHECK_MULTI_WAREHOUSE/d' /etc/dailycheck/app.env
systemctl restart dailycheck dailycheck-green
# 开：写回 1 并重启，同上
```

- 关闭后行为回到单仓：目录重新要求选配送中心，明细来源写车头 DC。
- **已产生的跨仓订单不回滚**：行来源已固化在 `store_order_items.source_warehouse_code`，
  关闭开关后这些单仍按原来源仓展示与发货（与设计 D6 固化原则一致）。
- 数据侧回滚点（本次配套的数据操作，不属于代码回滚范围）：
  `/root/DailyCheck/db/backups/pre-mw-20261102-094106/`（多仓前置扇出前）、
  `/root/DailyCheck/db/backups/pre-backfill-src-20261102-141206/`（历史单来源回填前）。

## 7. 报告签名

Dev Agent: 人工补录 · 基线 `b970992` · 分支 `feat/multi-warehouse-env-switch` · 合并 `83c11be`
报告生成时间: 2026-11-02T14:50:00Z
下次预演：CI `preview.sh` 自动跑（工作区临时产出，不回提交）

---

> **门禁说明**：本 PR 为单文件配置读取方式 + 一处判定对齐，无端口、依赖、schema、部署布局变更。
> 生产已 deploy-first 验证，合并后 main 与生产代码一致。
