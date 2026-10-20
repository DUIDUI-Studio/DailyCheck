## 报告 1/4

### 目标

库存健康表新增「品项级生产误差归因」：把每一层损耗误差**落到唯一品项标识**（`items.sku`），
并给出可复核的成因判定 —— 区分人为操作 / 配方问题 / 流程设计，附判定依据与置信度。

### 做了什么

| 层 | 内容 |
|----|------|
| Schema | 新表 `item_loss_ledger`；`production_run_items.planned_default` / `outbound_req_id`；`outbound_requests.source_type`（+ 存量回填）；`WAREHOUSE_SCHEMA_VERSION` 3→4 |
| 归因引擎 | 新增 `reports_loss_pure.py`：L1 配方偏差 / L2 旁路消耗 / L3 账实损耗，14 个原因码，含**盘盈盘亏冲销对**识别 |
| 写入侧 | `production.py` 记 `planned_default`（表单留空=1）与 `outbound_req_id`；三处出库写入补 `source_type` |
| 展示 | `/report/health` 新增归因区块（汇总三卡 + 成因堆叠条 + 台账主表 + 自愈区 + 品项风险排行）；CSV 新增段 6 |
| 测试 | `tests/test_reports_loss_pure.py` 30 条，含 3 条路由级与 2 处变异验证 |

### 三个关键设计决策

**1. 「配料消耗 vs 出库记录」对比在原架构下恒为 0，因此改为四层归因**

`production.py` 生产提交时对每行配料双写 `production_run_items` + `outbound_requests`，
出库是消耗的副本。4 仓全量验证：823/823、890/890、376/376、75/75，0 差异 0 孤儿。
按原需求直接做会产出永远显示 0 的死指标。真正的损耗沉在盘点：盘亏可达生产消耗的 1.89 倍。

**2. 机制证据优先于统计证据**

`confidence >= 0.8` 的判定必须含至少一条机制证据（BOM 系数为 0 / 出库原因为空 / 品项不在配方 /
`unit_cost=0`）。仅靠数值接近、方向一致这类统计信号时封顶 0.65，进人工复核队列。

起因是实测：PP091 出库 2208.18 与盘亏 2208.06 相差 0.005%，按数值匹配会判成「重复计量」；
拉时间线发现是先盘盈 +2208.35、次日盘亏 −2208.06 的冲销对，净影响仅 +0.29 桶。
**数值巧合不能当因果。**

**3. 冲销对识别必须排除盘点自身产生的流水**

盘盈写 `action='库存调整'`、盘亏写 `action='出库'`（note 均以「盘点审核」开头）。
不排除则每对盘盈盘亏之间都「存在流水」，冲销对永远配不上 —— 216 万会一直挂在损耗榜首。

### 实测结果（wh_003 副本，回看 180 天）

台账 584 行：L1 9 / L2 324 / L3 251。

| 原因码 | 条数 | 说明 |
|--------|------|------|
| `H-REASON-BLANK` | 324 | 出库未填原因，记录完整性问题，不计损耗率 |
| `P-NO-CHANNEL` | 152 | 不在任何配方却有盘亏（生产消耗品/成品），无日常登记渠道 |
| `unattributed` | 70 | 规则覆盖不到，进人工队列（置信度 0，不硬猜） |
| `P-MULTI-PRODUCT` | 25 | 多产品共用，置信度 0.65 待复核 |
| `R-BOM-ZERO` | 9 | PP072 海盐 7 + PP116 稳定剂 2，修 BOM 即归零 |
| `H-REVERSAL-PAIR` | 4 | 已自愈，不计入损耗率 |

冲销对识别到 4 对（规则有泛化能力，非只命中 PP091）：PP091 ¥2,163,898.80 / PP109 ¥430.00 /
PP045 ¥20.50 / PP046 ¥17.80。

**若不做冲销对，首页损耗额会是 ¥2,206,244（虚高 52 倍）。** 可计价误差实际为 ¥42,345.57，
另有 29 条无法计价（`unit_cost=0`，显示「—」而非 0 元）。

### 验证

- `ruff check` 改动文件全集：All checks passed
- `pytest tests/ --ignore=tests/mcp_server`：**14 failed / 1731 passed**，失败集与
  `tests/known_failures.txt` 完全一致（`check_known_failures.py` exit 0，零净增）
- 迁移幂等：库副本连跑两次，`user_version` 稳定在 4，`source_type` 无 NULL 残留
- 变异验证 2 处：删 note 过滤 → 冲销对用例变红判成 `P-NO-CHANNEL`；
  删 `exposure += inspections` → 2 条概率用例变红
- 真实进程启动 + `/health` 200，详见报告 04

### 影响面

- 无新路由、无新权限点（沿用 `/report/health` 的 manager+）
- 无新增第三方依赖
- 对既有指标的影响：`compute_production().overuse_pct` **本次未改口径**（仍在用全量行），
  仅新增独立区块；口径修正留在后续 PR，避免同一 PR 里改既有数字

## 报告 2/4

由 CI 自动产出（`reports/ci-summary.json`），详见 CI 运行结果。

## 报告 3/4

由 CI 自动产出，详见 CI 运行结果。

## 报告 4/4

见 `reports/pr-<num>/04-local-deploy-preview.md`（开 PR 后重命名目录并补充提交）：
真实进程启动日志 + `/health` 200 + 健康页归因区块渲染断言 + T11 冒烟的 pre-existing 判定证据。

## Checklist

- [x] 设计与实现一致（偏差 4 处已在 `docs/2026-10-20-production-loss-design.md` 附录 A 逐条说明理由）
- [x] 新增测试覆盖三层归因全分支 + 金额盲区 + 概率口径 + 幂等落库 + 三态降级
- [x] 关键修复做变异验证（回滚即变红）
- [x] `check_known_failures.py` exit 0（失败集零净增）
- [x] `ruff` 改动文件全集通过
- [x] 迁移幂等（连跑两次无异常、无 NULL 残留）
- [x] 同步 `docs/ARCHITECTURE.md`（§13.11 归因口径 + 架构图）与 `docs/ARCHITECTURE_MAP.md`（脚本重生成）
- [x] 无新增依赖、无新路由、无权限变更
- [x] 三态原则：无数据显示「—」不显示 0；`unit_cost=0` 不冒充 0 元
- [x] 未改动既有指标口径（`overuse_pct` 保持原样，口径修正另开 PR）

## 生产侧事项

**部署影响**：低。纯新增表 + 可空列，旧代码忽略新列即可运行，回滚无需降级脚本。

**迁移行为**：`WAREHOUSE_SCHEMA_VERSION` 3→4，首次访问各仓库时自动迁移。三条回填 UPDATE
（`source_type`）带 `WHERE source_type IS NULL`，可重复执行。生产库量级（wh_003：1475 条出库）
预计 < 1s，不阻塞请求。

**需 Prod Agent 关注**：

1. **健康页首访会触发一次台账重算**：DELETE + INSERT（wh_003 量级 584 行）。
   若首访 RT 明显上升属预期，第二次访问即为稳定态。
2. **数值预期变化**：`/report/health` 新增区块，页面总高度增加。首页 5 张 KPI 卡字段未变。
3. **T11 冒烟（`dev_smoke_t11_e2e.py`）在 main 基线即失败**，已在报告 04 附对照实验证据
   （干净基线 stash 后复现同一断言）。判定为 pre-existing，不阻塞本 PR，建议单开 issue 跟进。
4. **未做历史 `outbound_req_id` 回填**：查询侧用 `reason` 解析兜底，功能等价但多一次字符串匹配。
   若后续要收敛 `LIKE '生产领料(run=#%'` 的 4 处存量判断，需先补一次性回填脚本。

**未包含（后续 PR）**：口味消耗速度（P0 另一半）、`production_loss_events` 损耗事件表（P2）、
盘亏人工归因入口（P3）、`source_type` 替换既有 4 处 `LIKE` 判断。
