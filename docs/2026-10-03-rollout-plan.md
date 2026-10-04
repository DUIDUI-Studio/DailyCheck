# 上线路径

5 仓品项主数据治理，在 DEV 跑测试。你不用做事。

---

## 总览（一段话）

工程师按 `docs/2026-10-03-canonical-item-design.md` 在 DEV 上写代码、跑测试。写完会告诉你「T1 写完」「T2 写完」。你在最后一刻**一次性**看完整结果。

dev 用 `db/warehouses/` 里你已有的 4 个库做测试。生产库 `/tmp/dc_prod/` 只读不动，**只作为对照参考**，**不会**真的去跑 M1。

---

## 阶段

**1. 写 + 自测（工程师自己做完就过）**
- T1、T23：策略开关 + 库存保护代码
- T2、T3：给 4 个 dev 库加列、加 master.db 表
- T11、T24：拦截门店建新品
- T4、T5、T6：主数据增删查 + 品类映射 + 强信号检测
- T22、T7：批量收编 + 认领代码
- T9、T10、T14：跨仓页面 + UI

每个 T 完成时，工程师跑自己的测试。不通过不算完。

**2. 一条龙的集成测试（工程师在 dev 跑完）**
- 把 `docs/2026-10-03-claim-legacy-csv-preview.csv`（398 行）灌进 dev 4 仓
- 跑批量收编
- 跑强信号检测
- 跑冲突冻结
- 跑扇出
- **对照这张表确认**（用 dev 4 仓做对照，不是生产 5 仓）：

| 检查项 | 必须达到 |
|---|---|
| 4 仓 `items` 行数与原库一致 | 一样 |
| 4 仓 `items.id` 集合与原库一致 | 一样 |
| 4 仓每行 `quantity` 数值（Python `repr` 字符串逐字比对）| 一个不差 |
| `outbound_requests` / `stock_movements` 等引用表行数 | 一个不差 |
| 浮点脏值（dev wh_002 的 `0.34`、`0.4` 这种）逐字保留 | 保留 |

**任一不过**：修代码，重跑。

**3. 未来日常发布的三流程测试（M2 必做）**

工程师把 T8/T10b/T12/T13 写完后，必须在 dev 跑这三种流程看效果。结果写进验收报告。

**3.1 改名/改单位/改品类（主数据字段）**
```
步骤：
  1. 研发中心改 master 的 IC-000001.name = "奶油"
  2. POST /canonical/fanout {ids:[1], warehouse_codes:[wh_001,wh_002,wh_003,rd_001], action:overwrite}
  3. 看 4 仓 items.name 是否更新
  4. 看 quantity / safety_stock / selling_price / unit_cost 是否不变
  5. 看 audit_log（master）是否记下事件

期望：
  4 仓 name → "奶油"（除非门店有 is_alias=1 改过名，会被冻结）
  4 仓 quantity / safety_stock / selling_price / unit_cost 一个未变
  canonical_synced_json 已更新到新值
  master 的 canonical_publish_events 有 1 条 status=complete
  各仓店长收到 canonical_published 通知
```

**3.2 冲突冻结测试（Q4）**
```
步骤：
  1. 准备 1 个 items 行，先扇出一次写入 A 值
  2. 模拟门店自己改字段（A→B）
  3. 总部把主数据改成 C（A≠B≠C）
  4. 扇出 → 该字段应被冻结

期望：
  该行字段保持 B（门店本地值）
  canonical_conflicts 表新增 1 行 status='open'
  仓库状态 = partial_conflict
  冲突页可见：field / local_value(B) / last_synced(A) / canonical_value(C)
  通知门店店长「你的本地值被冻结，待裁决」
```

**3.3 新增主数据**
```
步骤：
  1. 总部创建 IC-000142 麦片（unit=袋，category=生产消耗品）
  2. 扇出到 4 个仓
  3. 看各仓 items 表是否多了一行

期望：
  4 仓各新增一行「麦片」（sku = IC-000142 或自动生成），库存=0，售价=0
  canonical_synced_json 含完整下发值
  后续门店修改 selling_price/quantity 不被覆盖
```

**3.4 停用主数据（Q3）**
```
步骤：
  1. 总部把 IC-000001.status='inactive'
  2. 扇出（仅同步 status 字段）
  3. 验证

期望：
  4 仓 items.canonical_status='disabled'
  4 仓 items.name/unit 不变（status 是 status，不是字段覆盖）
  门店编辑 lock-name 的请求应被拒绝（403）
  库存数值、历史单据、引用表全部不动
```

**任一不过**：修代码，重跑。

**4. 给你看（一次性）**
- 跑了什么、用什么 fixture、结果对照表、所有 T 通过/失败
- 三个流程测试的输入输出对比
- 你过一眼，决定能不能 commit 到 main

---

## 你做的事

- **不做事** —— 工程师在 dev 跑，dev 是隔离沙箱
- **唯一一件事**：第三步看结果，决定 commit 是否推

---

## git 规则（工程师也不会碰）

- 工程师 `git commit` 之前给你看 message
- `git push` 必须你单独说「推」才执行
- 不会绕过 pre-push hook

---

## 节奏

- W2：T1 + T23 + 迁移（动 master.db schema 和 items 列）
- W3：T11 + T24 + T4/T5/T6
- W4：T22 + T7 + T9
- W5：T10 + 集成测试
- W6：给 Eric 看

预计约 3-4 周。在 dev 跑，不动生产。