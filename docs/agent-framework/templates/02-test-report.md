# 报告 2/4：测试报告（Test Report）

> **用途**：分层汇总本次改动触发的所有测试结果 + 覆盖率 diff。
> **触发**：Dev Agent 每次 PR 前必出。
> **门禁**：基线对比零增长（参照 `tests/known_failures.txt`）+ 覆盖率不低于基线。

---

## 1. 元信息

| 字段 | 值 |
|------|-----|
| PR | # |
| 分支 | `xxx` |
| 基线 commit | `sha`（取自分支 rebase 点） |
| 当前 commit | `sha` |
| Dev Agent | 标识 |
| 生成时间 | ISO8601 |

---

## 2. 分层结果汇总

| 层 | 命令 | 通过 | 失败 | 跳过 | 与基线差 |
|----|------|------|------|------|---------|
| L1 静态扫描 | `ruff check .` + `mypy ...` + `bandit -r .` | N | 0 | 0 | 0（零新增告警） |
| L2 单元测试 | `pytest tests/unit -q` | X | 0 | Y | 失败数 ≤ 基线 |
| L3 契约测试 | `pytest tests/contract -q` | X | 0 | 0 | 同上 |
| L4 UI 测试 | `playwright test` | X | 0 | Y | 同上 |
| L5 本地冒烟 | `scripts/blue_green/up.sh && curl -I ...` | X | 0 | 0 | N/A |

**门禁**：任何层"与基线差 > 0" 即阻塞 PR。

---

## 3. 覆盖率 diff

| 模块 | 基线覆盖率 | 当前覆盖率 | diff |
|------|----------|-----------|------|
| `blueprints/items.py` | 78% | 82% | +4% ✅ |
| `db/migrate.py` | 65% | 65% | 0 ✅ |
| `mcp_server/main.py` | 41% | 41% | 0 ✅ |

**门禁**：覆盖率 < 基线 → 阻塞 PR。

---

## 4. 新增/修改用例

| 用例文件 | 新增 | 修改 | 关键场景 |
|---------|------|------|---------|
| `tests/unit/test_items_edit.py` | 3 | 1 | 边界：空名、超长名、Unicode |
| `tests/contract/test_openapi_diff.py` | 1 | 0 | API 变更兼容性 |

---

## 6. 已知失败清单（known_failures 比对）

```diff
# tests/known_failures.txt 与基线对比
- 新增失败项: 0  ← 必须为 0
- 消失失败项: 0
- 维持失败项: 18（与基线一致）
```

---

## 7. 报告签名

```
Dev Agent: <name> <commit-sha>
报告生成时间: <ISO8601>
```