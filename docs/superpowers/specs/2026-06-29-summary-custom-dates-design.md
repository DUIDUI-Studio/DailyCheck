# 汇总表自定义时间 — 子项目 7 spec

**版本**：v1（2026-06-29）
**状态**：实施中。引用总览 PRD §2.6。
**前置依赖**：无（独立子项目）。
**目标读者**：实施 agent。

---

## 0. 引用与本 spec 自决

引用 PRD：
- §2.6.1 目标
- §2.6.2 URL 契约
- §2.6.3 校验规则
- §2.6.4 聚合口径（沿用现有）
- §2.6.5 导出兼容
- §2.6.6 测试要点
- §3.2/3.3/3.5/3.6
- §8 锁定项

本 spec 自决项：

1. **`range=` 参数处理**：spec §0 写"start/end 优先于 range，range 保留向后兼容一个版本但忽略"。**实现**：`range` 参数被忽略（不报错），仅在 start/end 缺省时**不**回退到 range。**不**做 range→start/end 转换。
2. **快捷按钮**：本周/上周/本月/上月/本季/本年。**实现**：前端按钮 onclick → JS 计算 date range → navigate to `?start=...&end=...`。
3. **时区**：UTC 存储 + 用户浏览器时区展示（spec §0 锁定）。**沿用现有约定**——`computed_at` 用 ISO Z。
4. **导出 CSV**：`/summary/export?start=...&end=...` 接受新参数。**沿用现有 CSV 逻辑**。
5. **`start > end` 错误**：400 + flash "开始日期不能晚于结束日期"（PRD 锁定）。
6. **跨度 > 365 天**：400 + flash（PRD 锁定）。
7. **未来日期**：允许，但 `end` 不超过今天 + 1 天（PRD 锁定）。
8. **格式错误**：400 + flash "日期格式应为 YYYY-MM-DD"（PRD 锁定）。

---

## 1. URL 契约（PRD §2.6.2）

```
GET /summary?start=2026-06-01&end=2026-06-30
GET /summary?start=2026-06-01  → end 缺省 = 今天
GET /summary?end=2026-06-30    → start 缺省 = 过去 7 天
GET /summary                   → 缺省 = 过去 7 天
GET /summary?range=7d          → range 忽略，按缺省
```

---

## 2. 校验（PRD §2.6.3）

| 场景 | 行为 |
|---|---|
| start > end | 400 / flash "开始日期不能晚于结束日期" |
| 跨度 > 365 天 | 400 / flash "时间范围不能超过 1 年" |
| 未来日期 | 允许，但 end 不超过今天 + 1 天 |
| 格式错误 | 400 / flash "日期格式应为 YYYY-MM-DD" |

---

## 3. 聚合口径

PRD §2.6.4 写"沿用现有，不重定义"——进货金额、消耗金额、当前库存金额、周转率、可售天数。**不**改 SQL，仅把 `>= datetime('now','-7 days')` 替换为 `>= start` / `<= end`。

---

## 4. 导出兼容（PRD §2.6.5）

- `/summary/export?start=...&end=...` 输出 CSV，沿用当前可见项。
- `range=` 参数向后兼容（忽略，但 URL 拼接不报错）。

---

## 5. 测试矩阵

### 5.1 单元

- `parse_summary_dates(args) -> (start_date, end_date, error)`:
  - 全部缺省 → (today-7, today)
  - start only → (start, today)
  - end only → (today-7, end)
  - 都有 → (start, end)
  - start > end → error
  - 跨度 > 365 → error
  - 未来日期 end → 拒绝
  - 格式错 → error
  - range=7d → 忽略

### 5.2 集成

- GET /summary?start=2026-06-01 → 200 + context 含 start
- GET /summary?start=2026-06-01&end=2026-06-30 → 200
- GET /summary?start=invalid → 400 + flash
- GET /summary/export?start=...&end=... → CSV 文件
- GET /summary?range=7d → 等同于缺省

### 5.3 E2E

- 浏览器点"本月"按钮 → URL 变 `?start=2026-06-01&end=2026-06-30`

---

## 6. 文件清单

- 新增 `blueprints/summary_dates.py`（pure parse fn）
- 修改 `blueprints/reports.py`（summary view + export）
- 新增 `tests/test_summary_dates_pure.py`
- 新增 `tests/test_summary_dates_route.py`
- 修改 `templates/summary.html`（加快捷按钮）
- 新增 `static/summary_dates.js`（按钮 onclick JS）

---

## 7. 验收门

1. `pytest -q` 全绿
2. `start > end` 真的 400
3. 跨度 > 365 真的 400
4. CSV 导出文件名带日期
