# DailyCheck 商业化合规差距评估

> 日期：2026-10-10
> 评估人：MVP 开发专家团（项目总监）
> 范围：通读代码 / 配置 / 部署 / 文档 后，对「如果要商业化还差什么」做盘点
> 评级：P0 阻塞 / P1 必需 / P2 锦上添花

---

## 1. 项目画像（30 秒读懂）

| 维度 | 现状 |
|------|------|
| 定位 | 多仓轻量库存管理（品类/库存/出库/盘点/补货/生产/预测/采购/通知），单租户、单组织内部工具 |
| 技术栈 | Flask 3.1.1 + Python 3.10+ + SQLite（master.db + 每仓独立 db）+ Jinja2 + 原生 CSS + PWA Service Worker |
| 体量 | 蓝图 15,375 行 / 18 个蓝图 / 152 条路由 / 13 个 MCP 工具 / 729 测试用例 / 97.5% 通过 |
| 部署 | 单 VPS（112.124.18.246:33756 root）+ GitHub Actions SSH 部署脚本（无 Dockerfile、无 HA、无 DR） |
| 鉴权 | Flask session（web）+ Bearer Token + agent_tokens 表（带仓库/路径 ACL，pbkdf2 哈希 + 加密存储） |
| MCP | stdio + HTTP/SSE 双模式，路径级 ACL，单租户身份 |
| 业务复杂度 | 高 — canonical 主数据跨仓扇出、store-ordering 6 态状态机、配方成本核算、研发配方跨仓发布 |
| 工程成熟度 | 中高 — schema 迁移幂等化、PRAGMA user_version、fcntl flock、git pre-push hook、单元 + 集成测试齐 |

**关键判断**：这不是个 demo，是 Eric 一年半反复迭代的真实业务系统（issue-reports 自 2025-07 起累计 55 份）。核心业务逻辑扎实，但**面向商业化所需的「SaaS 周围设施」基本空白**。

---

## 2. 设计亮点（不要丢的资产）

商业化时这些是真正的护城河，别为了迎合通用 SaaS 模板把它打平：

1. **多仓模型天然支持数据隔离**：`db/warehouses/<code>.db` 每仓独立文件，物理层就隔开了 — 多租户改造时这是现成的隔离边界。
2. **canonical 主数据 + 扇出机制**：R&D 研发 → 门店/DC 跨仓下发，含冲突冻结（Q4=freeze）、价格收归（Q6=canonical_managed）、库存零丢失（Q7=strict）— 这种治理深度在通用库存 SaaS 里少见。
3. **state machine 严谨**：store-ordering 6 态、`pending → approved → shipped → delivered`，含部分收货累计、运费锁定、收货上限语义单一入口 `order_item_receivable_qty()` — 是真正能跑业务的状态机，不是状态字段乱写。
4. **MCP 13 工具 + Bearer + per-token ACL**：AI Agent 访问层已经搭好且带路径/仓库白名单。库存 SaaS + AI Agent 双入口是稀缺的组合。
5. **PWA + 移动端 CSS**：Service Worker、`manifest.webmanifest`、1612 行 mobile-first CSS、`icon-192/512.png`、离线回退页 — 现场/门店场景的可用性已经超过大多数桌面优先的库存系统。
6. **Schema 迁移纪律**：每个 ALTER 都 PRAGMA 守卫 + `user_version` 门控 + 幂等。SQLite 上能做到这一步不多。
7. **测试密度**：729 测试 / 58 文件，97.5% 通过；pre-existing 18 failed 都是历史遗留，新功能零引入（owner SOP 有据）。
8. **文档厚度**：24 篇设计文档（specs/runbooks/plans）+ 39 用例的验收脚本（`test_acceptance_item_master_ordering.py`）— 说明业务方一直在参与迭代，不是闭门造车。

---

## 3. 商业化差距（按 P0/P1/P2 排序）

### P0 — 阻塞级（不上线就别卖）

#### P0-1 无多租户隔离 / 租户概念
- **证据**：`config.py` 只有一个 `MASTER_DB` 常量，`db/warehouses/` 是**单组织的仓集合**。新客户 = 整库迁移，物理上无法隔离。
- **后果**：租户 A 的 admin 能直接读租户 B 的所有库存；备份/恢复只能整库；性能隔离无解。
- **判据**：单 SQLite 文件承载 N 个 tenant 会撞 write-lock；50+ tenant 必崩。
- **改造**：
  - 轻：`master.db` 加 `tenants` 表，每个 tenant 自带 master + warehouses 子集，session 带 tenant_id
  - 重：迁移到 PostgreSQL（行级 RLS 做隔离，路径比 SQLite-per-file 干净 10 倍）
- **预估**：2–4 周（轻）/ 6–8 周（重）

#### P0-2 无计费 / 支付 / 订阅
- **证据**：grep `Stripe|weixin|payment|charge|invoice|billing|subscription` 在源码里 0 命中（除 venv 许可证字段噪声）。
- **后果**：无法上线商业产品。定价、订单、续费、对账、发票全部缺。
- **改造**：先决定商业模式（订阅？按仓？按 SKU？）— 计费是商业模式的下游。
- **预估**：4–8 周（含对账、税务、退款）

#### P0-3 无 CSRF 保护
- **证据**：`grep "CSRF|csrf_protect|WTF_CSRF"` 在 `app.py` / `blueprints/auth.py` 0 命中。Flask 不会默认开启 CSRF。
- **后果**：用户登录态下，第三方页面 POST 到 `/items/<id>/delete` 等就能删库。SameSite=Lax 兜了一部分但非全部。
- **改造**：装 `Flask-WTF` + `CSRFProtect(app)`；所有 form 加 `{{ csrf_token() }}`；MCP HTTP/SSE 不受影响。
- **预估**：3–5 天（含全量回归）

#### P0-4 无 rate limiting / 暴力破解防护
- **证据**：grep `rate.?limit|throttle` 在 blueprints 0 命中。`/login` 没有失败计数、没有 backoff、没有 captcha。
- **后果**：账号爆破、库存数据被脚本薅、API 被滥用。AI Agent MCP 入口也没限速。
- **改造**：`Flask-Limiter` + IP/token 双维度；MCP 入口按 token 配额；登录失败计数 + 锁定。
- **预估**：1 周

#### P0-5 无 CI 测试/质量门禁
- **证据**：`.github/workflows/` 只有 `deploy.yml`；没有 pytest、ruff、type-check 的 PR gate。pre-existing 18 failed 没人拦。
- **后果**：合并就上 prod。owner SOP 写得好但不能取代机械门禁。
- **改造**：加 `.github/workflows/ci.yml`：lint → pytest → type-check → docker build（如果引入 Dockerfile）。pre-existing failed 单独建 issue tracker，必须 0 → 0 推进。
- **预估**：3 天

#### P0-6 部署无回滚 / 无迁移工具
- **证据**：`deploy.yml` 是 `pkill gunicorn → tar 解压 → pip install → gunicorn -D`。schema 变更靠启动时 `init_master_db()` 的 PRAGMA 守卫跑 ALTER，没有 alembic 等价物。
- **后果**：发版失败 = 旧进程被杀、新版起不来 = 全站宕机。schema 变更跑一半失败 = 数据库不一致。
- **改造**：
  - 加 Alembic（或自写 migration runner）— schema 变更独立于进程启动
  - 部署脚本：起新 gunicorn 在另一端口、health-check 通过 → kill 旧 → 失败保留旧
  - 数据库备份在每次 schema 变更前自动做
- **预估**：2–3 周

#### P0-7 备份策略非自动化 / 无 DR
- **证据**：`backups/` 在 `.gitignore`，但生产里是手工 ops 备份；`/health` 只报 forecast lock counter，没有磁盘/db 完整性检查；没有异地备份。
- **后果**：VPS 失联 = 数据全失；RPO/RTO 没有承诺。
- **改造**：
  - 自动 cron：`sqlite3 .backup` 到 /backups + 加密上传对象存储
  - 每日校验 backup 可还原（起一个临时实例跑 health）
  - 文档化 RTO/RPO，runbook 写明故障切换
- **预估**：1–2 周

---

### P1 — 必需级（不补就上不去规模）

#### P1-1 无用户自助注册 / 密码重置 / 邮件验证
- **证据**：用户创建仅 `flask create-user <username> <password>` CLI；没有 `/signup`、没有 forgot-password、no email 集成。
- **改造**：邮箱验证 + 密码重置 token + 强密码策略 + 二次验证（可选）。
- **预估**：1–2 周

#### P1-2 无可观测性栈
- **证据**：仅 gunicorn access.log/error.log + `audit_log` 表 + 单文件 `admin_audit.log`。没有 prometheus/otel/Sentry。
- **后果**：上线后无法回答「为什么慢了」「哪个功能报错最多」「转化率在哪一步掉」。
- **改造**：`prometheus-flask-exporter` + `/metrics`；OpenTelemetry trace；Sentry 错误上报。
- **预估**：1 周

#### P1-3 无 SLA / 隐私 / ToS / 安全披露文档
- **证据**：`find` 顶层无 `LICENSE` / `CHANGELOG` / `CONTRIBUTING` / `PRIVACY` / `TERMS` / `SECURITY.md`（除 node_modules noise）。
- **改造**：补 6 份文档。商业产品的合规底线。
- **预估**：3 天（写内容）+ 后续维护

#### P1-4 无 onboarding / 引导
- **证据**：登录后直接进 dashboard，没有 first-run 向导、没有 demo 数据生成、没有「这是干嘛的」指引。
- **改造**：基于 `is_first_login` 标志的 step-by-step 引导（创建第一个仓 → 建第一个品 → 入库演示）。
- **预估**：1 周

#### P1-5 无客户支持入口
- **证据**：UI 里没有任何 help / feedback / 客服入口。报错只能看 access.log。
- **改造**：站内 help 页（markdown 维护）+ 反馈按钮 → 入 `feedback` 表 → 邮件通知 admin。
- **预估**：3–5 天

#### P1-6 无对外 REST API
- **证据**：MCP 是唯一的程序化接口，且定位是 AI Agent 内部。客户系统集成只能走 MCP。
- **改造**：开 REST API（OpenAPI 3.0 文档）— 跟 MCP 复用 service 层，复用 auth 层。
- **预估**：2–3 周

#### P1-7 i18n / 多语言
- **证据**：grep `i18n|locale|translation|gettext` 在源码 1 命中（且非翻译），UI 100% 中文。
- **改造**：`Flask-Babel` + 提取中文字符串到 `zh_CN.po`；预留 `en_US.po`。
- **预估**：1 周（中文抽取）+ 翻译工作是翻译团队的事

#### P1-8 数据导出 / 用户数据访问权（GDPR/个保法准备）
- **证据**：用户没有「下载我的数据」按钮；admin 没有「按用户导出」工具。
- **改造**：自助导出（JSON/CSV）+ 删除请求的 soft-delete 工作流。
- **预估**：1 周

---

### P2 — 锦上添花（不影响首单）

- **P2-1 缺 LICENSE 文件** — 用别人代码的潜在合规风险
- **P2-2 SQLite write-lock 天花板** — 50+ 仓并发写必崩，过渡方案：postgresql 适配层（不破现有架构）
- **P2-3 无 PWA push notifications** — 现场场景的差异化卖点，但开发量大
- **P2-4 无遥测** — 不知道哪个功能被用、哪里有阻力
- **P2-5 无 SDK / 客户端库** — Python/JS SDK 给集成方用
- **P2-6 无多端** — 没有 iOS/Android 原生 app（虽然 PWA 已经覆盖，但客户会问）
- **P2-7 无 A/B 框架 / feature flag** — 实验能力为零
- **P2-8 配方成本模块 qty 语义不一致 pre-existing bug** — 商业化前必须修，否则财务对账会被挑战
- **P2-9 audit_log 写入覆盖不全** — 现在只在 recipe_cost 写，库存/出库/盘点变更都该审计

---

## 4. 三条商业化路径（含成本估算）

### 路径 A：垂直 SaaS（推荐）
**目标客户**：连锁餐饮/零售/加盟体系的小型企业（5-50 仓规模）
**卖点**：多仓 + canonical 主数据治理 + MCP AI Agent 入口（PWA 移动端）
**必备工作**：P0 全部 + P1-1/2/3/5/6/8 = 约 **4-6 个月**（2 人团队）
**风险**：CSRF/rate limit 是基础合规，缺一不可；多租户是天花板
**单子估值**：年付 ¥3-8 万/客户，5-10 客户可覆盖成本

### 路径 B：白标私有部署（快钱）
**目标客户**：单一大型连锁企业，整体买断 + 定制
**跳过**：计费（P0-2）/ 多租户（P0-1 部分）/ SLA 自托管
**必备工作**：P0-3/4/5/6/7 + P1-3 = 约 **2-3 个月**
**单子估值**：¥30-100 万一次性 + ¥5-15 万/年维护
**风险**：一次性的天花板低，但现金流来得快

### 路径 C：开源 + 商业支持
**目标**：代码公开（GitHub），卖托管 / SLA / 集成咨询
**必备工作**：P0 全部 + P1-3 + LICENSE + SECURITY.md + CONTRIBUTING.md + CI 强门禁
**周期**：约 **6-9 个月**
**风险**：开源治理负担重，但品牌效应和 PR 价值高
**适合路径**：如果 Eric 想要个人 IP + 长期生态

---

## 5. 第一阶段建议（如果要动，顺序是什么）

按依赖关系排，不是按重要性：

1. **P0-5 CI 门禁（3 天）** — 没有这一步后面所有改动都没法安全合并
2. **P0-3 CSRF（3-5 天）** — 改动小、立竿见影、防止明天就被打
3. **P0-4 rate limit（1 周）** — 同上
4. **P0-6 部署回滚 + 迁移工具（2-3 周）** — 引入 alembic，开始把 schema 变更从启动路径里挪走
5. **P1-3 合规文档（3 天）** — 6 份文件批量出
6. **P0-1 多租户（2-4 周轻量方案）** — 这是最大的改动，建议先做 PoC 验证可行性再全力推进
7. **P0-2 计费（4-8 周）** — 必须基于已确定的多租户模型
8. **P0-7 备份自动化 + DR runbook（1-2 周）** — 客户签约前必须有

并行可以做：
- **P1-2 可观测性** — 不阻塞主线
- **P1-4 onboarding** — 用户体验先做
- **P1-5 支持入口** — 客户体验先做
- **P2-8 pre-existing 修** — 任何对外场景前必须修

---

## 6. 风险提示

- **pre-existing 18 failed 测试**：商业化前必须归零，否则客户拿这个当拒收理由（owner SOP 已知）。
- **生产时区 bug**：unit 没设 TZ，进程 `datetime.now()`=CST，SQLite `datetime('now')`=UTC。时间类 bug 在 dev(UTC) 不可复现、生产必现。多租户改造前必修。
- **配方成本 qty 语义不一致**（pre-existing，2026-09 起已知）：财务对账会出事。
- **PWA 图标缓存 `static/icons/icon-192.tmp` 在 .gitignore 里**：部署时若残留 tmp 会导致浏览器显示旧图标。
- **git pre-push hook 是本地保护** — 商业化时建议 push 触发 pre-receive 检查（hooks 已是 SaaS 标配）。

---

## 7. 总结一句话

**DailyCheck 是一个真实业务跑了一年半的库存管理系统，工程深度够，但「商业 SaaS 周围设施」基本空白。从「内部工具」走到「可售卖产品」最少要 4-6 个月 2 人月的工作量，关键卡点是：多租户、计费、CSRF/rate limit、部署回滚、合规文档。**
