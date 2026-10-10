# DailyCheck 商业化 P0 收敛 — 开发计划 & 测试计划

> 日期：2026-10-10
> 依据：`docs/2026-10-10-commercialization-gap-assessment.md`（P0×7）
> 目标：把「内部工具」推进到「可安全对外售卖」的工程底线，P0 全部闭环。
> 约束：任何 `git push` 须经 Eric 确认；触及架构子系统的 commit 必同步 `docs/ARCHITECTURE.md`（pre-commit hook 强制）。
>
> **决策记录（2026-10-10，Eric 拍板）**：
> 1. **计费模块（P0-2）暂不排期**——商业模式/支付通道未定，从本计划移除，待独立立项。
> 2. **多租户走轻方案**（tenants 表 + 租户目录隔离），不引入 PostgreSQL/RLS。

---

## 1. 总体排期（按依赖顺序）

| 里程碑 | 任务 | 分支 | 预估 | 依赖 |
|--------|------|------|------|------|
| M1 | P0-5 CI 门禁 | `feat/ci-gate` | 3 天 | 无 |
| M2 | P0-3 CSRF | `feat/csrf` | 3–5 天 | M1（有门禁才安全合并） |
| M3 | P0-4 Rate limit | `feat/rate-limit` | 1 周 | M1 |
| M4 | P0-6 部署回滚 + 迁移工具 | `feat/deploy-rollback` | 2–3 周 | M1 |
| M5 | P1-3 合规文档（穿插） | `docs/compliance` | 3 天 | 无，可与 M2–M4 并行 |
| M6 | P0-1 多租户（先 PoC 后全力） | `feat/multi-tenant-poc` → `feat/multi-tenant` | 2–4 周（轻方案） | M4（迁移工具先就位） |
| M7 | P0-7 备份自动化 + DR | `feat/backup-dr` | 1–2 周 | M4（回滚机制复用） |
| ~~M8~~ | ~~P0-2 计费~~ | — | **暂不排期**（决策 #1） | 待商业模式拍板后独立立项 |

**前置雷（M6 之前必须排掉，均为已知 pre-existing）：**
1. 生产时区 bug：prod unit 未设 TZ，进程 `now()`=CST 与 SQLite `datetime('now')`=UTC 差 8h。修法：systemd unit 加 `Environment=TZ=Asia/Shanghai` + 代码统一时间源。
2. recipe_cost qty 语义不一致（克数 vs 份数）：修法须 Eric 拍板后单独立项。
3. 18 个 pre-existing failed 测试：商业化前归零（不阻塞 M1–M5，M6 前必须有计划）。

---

## 2. 开发计划（逐任务）

### M1 — P0-5 CI 门禁（3 天）
- **改造**：新增 `.github/workflows/ci.yml`，PR + push to main 触发：
  1. `ruff check`（仅增量：diff 出改动文件）
  2. `pytest --basetemp` 全量，`KNOWN_FAILURES` 清单（当前 18 个）做零新增断言——FAILED 集 − KNOWN_FAILURES 非空即 fail
  3. `deploy.yml` 增加 `needs: ci`，CI 不过不部署
- **注意**：runner 上无容器，`tests/mcp_server` 标记 `@pytest.mark.skipif(not os.environ.get("MCP_IN_CONTAINER"))` 或在 CI 里起 service container（二选一，倾向后者，保覆盖）。
- **同步**：更新 `docs/ARCHITECTURE.md` 测试章节 + `.github/workflows` 属门禁目录。

### M2 — P0-3 CSRF（3–5 天）
- **改造**：
  - 加依赖 `Flask-WTF`；`app.py` 挂 `CSRFProtect(app)`。
  - 全部 form 模板加 `{{ csrf_token() }}`（65 个模板批量过一遍，写脚本扫描 `<form` 缺 token 的）。
  - 豁免清单：MCP HTTP/SSE 路由（`/api/mcp/`，Bearer 鉴权不受 cookie CSRF 影响）进 `CSRF_EXEMPT`。
  - `SessionCookie` 确认 `SameSite=Lax` 兜底保留。
- **测试兼容**：`app.config["TESTING"]=True` 时 `WTF_CSRF_ENABLED=False`（不炸现有 729 用例）；另建独立测试模块在 enabled 状态下专测。
- **风险**：`fetch/XHR` POST 路由（如有）需带 `X-CSRFToken` header，grep 排查。

### M3 — P0-4 Rate limit（1 周）
- **改造**：
  - 加 `Flask-Limiter`，storage 先用内存（单机双 worker 够用），key_func = IP + session user 双维度。
  - `/login`：失败计数 5 次/15 分钟 → 锁定 30 分钟；锁定状态固定延迟响应防侧信道。
  - MCP 入口：按 token 维度配额（走现有 agent_tokens 表加 `rate_limit` 列，默认值兜底）。
  - 全局默认：如 300/分钟，超出返回 429。
- **注意**：gunicorn 2 worker × gthread，内存 storage 各自独立 → 限流阈值按 worker 均摊计算，或后续切 Redis（商业化阶段再定）。

### M4 — P0-6 部署回滚 + 迁移工具（2–3 周）
- **改造**：
  - **迁移 runner**：自写轻量 `migrations/` 目录 + `flask migrate up/down`（SQLite 上 Alembic 收益低且与现有 PRAGMA 守卫幂等体系冲突，自写更贴现有约定）。`user_version` 已在用，复用为迁移版本号。
  - **部署脚本重写 `deploy.yml`**：新版本解压到 `/root/DailyCheck-releases/<ts>/` → 原子切软链 → gunicorn 起新端口 → `/health` 探活通过 → kill 旧 → 失败则软链切回旧版重启。
  - **schema 变更前自动备份**：migration runner 执行前强制 `sqlite3 .backup` 到带版本号的路径，失败拒绝迁移。
  - 保留现有 `get_warehouse_db()` 每请求幂等 migrate 作为兜底，但新列一律走迁移文件。

### M5 — P1-3 合规文档（3 天，穿插）
- LICENSE（先定开源/闭源，影响路径 A/C 选型）、SECURITY.md、PRIVACY、TERMS、SLA、CHANGELOG。

### M6 — P0-1 多租户（2–4 周轻方案，已拍板）
- **先 PoC（1 周）**：`master.db` 加 `tenants` 表；每租户 `db/tenants/<tid>/master.db + warehouses/`；session/`g` 注入 `tenant_id`；所有 `get_warehouse_db()` / master 查询加租户路由。
- **PoC 验收点**：租户 A admin 无法访问租户 B 任何路由/数据；备份/恢复按租户粒度可行；50 租户写并发 smoke 过。
- **PoC 通过 → 全力改造**（认证、ACL、canonical 扇出、store-ordering、MCP token 全链路加租户维度）。
- **明确不做**（本期）：PostgreSQL + RLS 重方案——轻方案天花板内先跑，等付费租户逼近 50 再评估。
- **风险**：改动面最大，`auth.py` import 期绑定路径的习惯（`from config import BASE_DIR`）必须重构成函数级动态解析，否则测试副本套路全碎。

### M7 — P0-7 备份 + DR（1–2 周）
- **改造**：
  - cron：每日 `sqlite3 .backup`（master + 每仓）→ 打包加密 → 上传对象存储（阿里云 OSS）。
  - 每周自动还原演练：备份 → 临时目录还原 → 起临时实例打 `/health` → 校验通过后清理。
  - `docs/runbook-dr.md`：RPO=24h / RTO=1h，故障切换步骤化。

### ~~M8 — P0-2 计费~~（暂不排期，决策 #1）
- 商业模式（订阅 vs 按仓 vs 按 SKU）与支付通道未定。**待独立立项**，届时先出技术设计文档评审再动手。租户模型（M6 轻方案）已为其预留扩展点。

---

## 3. 测试计划

### 3.1 全局基线（每个任务开工前/提交前必跑）

```bash
# 单测（本地）
~/.local/bin/python3.12 -m pytest --basetemp=/tmp/pytest-dc -q
# mcp_server（容器内）
docker exec wdg-systemd /opt/dailycheck/.venv/bin/python -m pytest tests/mcp_server
# lint（仅改动文件）
/Users/ericmr/.workbuddy/binaries/python/versions/3.13.12/bin/ruff check <files>
```

- **红线**：FAILED 集 = 18 个已知 pre-existing，**零新增**（用 `comm -13` 对比法）。
- 每任务独立分支，`docker exec wdg-systemd` 容器实时 bind 验证手工冒烟（Flask :8080 有 reloader）。

### 3.2 分任务测试策略

| 任务 | 新增测试 | 关键用例 |
|------|---------|---------|
| M1 CI | `ci.yml` 自身 + KNOWN_FAILURES 机制自检 | ① 人为注入一个新 fail → CI 红 ② 18 known fails 不挡 ③ ci fail 时 deploy 不跑 |
| M2 CSRF | `tests/test_csrf.py` | ① 无 token POST `/items/<id>/delete` → 400 ② 带 token → 200/302 ③ `/api/mcp/` 豁免仍通 ④ fetch/XHR 路由 header 校验 ⑤ **存量 729 用例在 TESTING 模式全绿**（回归主验证） |
| M3 Rate limit | `tests/test_rate_limit.py` | ① /login 连错 5 次锁 30min ② 锁定期间正确密码也 429/锁定页 ③ MCP token 超配额 429 ④ 正常流量不误伤（729 回归） |
| M4 部署回滚 | `tests/test_migration_runner.py` + 演练脚本 | ① 迁移 up 幂等（跑两遍结果一致）② down 可回滚 ③ 迁移前自动备份存在且可打开 ④ 部署脚本在 dev 容器/临时 VPS 演练：健康检查失败 → 旧版自动接管（**必须真实演练一次，不写 mock**） |
| M6 多租户 | `tests/test_tenant_isolation.py`（PoC 起随做） | ① 租户 A admin 访问 B 的仓/路由 → 404/403 ② tenant 路由：同 warehouse code 不同租户互不可见 ③ canonical 扇出不跨租户 ④ MCP token 绑租户生效 ⑤ 备份还原按租户粒度可行 ⑥ 50 租户并发写 smoke |
| M7 备份 DR | 还原演练脚本（非 pytest，runbook 化） | ① 备份→还原→`/health` 200 ② 篡改备份文件 → 校验拒绝 ③ RTO 实测 < 1h |

### 3.3 回归与验收门

1. **每任务合入前**：全量 pytest 零新增 fail + ruff 零新增 + 关键页面 GET 200/302 + `test_acceptance_item_master_ordering.py` 39 用例全绿（防业务逻辑被误伤）。
2. **M2/M3 这类全局性改动**：额外跑容器内 Flask 冒烟——admin/xsj/xtd/hgj/sgl 五账号逐个登录，走一遍 品项→订货→收货 核心链路。
3. **M4/M7 上线动作**：生产操作前先在 dev 容器全流程演练，演练记录落 `docs/`。
4. **收尾验收（P0 闭环定义，计费除外）**：CI 全绿门禁生效 / CSRF+限速上线 / 部署演练含一次真实回滚 / 每日备份可还原 / 多租户隔离用例全绿。计费按决策 #1 移出本计划，不作为闭环条件。

### 3.4 已知风险 → 测试侧对策

- **时区**：容器 UTC vs 生产 CST，时间相关断言统一用 `datetime('now')` 同源或注入时钟，避免 dev 过 prod 挂。
- **auth.py import 期绑定路径**：涉及 M6 的测试沿用 dev 副本套路（copytree + import 前覆盖 config），M6 重构后该约束应消除，回归重点。
- **18 pre-existing**：M1 的 KNOWN_FAILURES 清单要随归零进度同步收缩，防止名单变成垃圾桶。
