# Branch Protection 接入指引

> 框架 v0.1 强制项：CI 通过 + 四报告齐全才可 merge。
> 这一道闸在 GitHub repo settings 配，需要 owner 手动点。

## 设置步骤

### 1. 进入 repo Settings → Branches

`https://github.com/ericmr1981/DailyCheck/settings/branches`

### 2. 添加 / 编辑 `main` 分支规则

点 **Add rule**（如已有 main 规则则 Edit）：

| 设置项 | 值 | 说明 |
|--------|----|----|
| Branch name pattern | `main` | |
| Require a pull request before merging | ✅ | 强制走 PR |
| Require approvals | 1 | 至少 1 审批（Phase 1 保留人工 review 一道闸；Phase 3 可改 0） |
| Dismiss stale pull request approvals | ✅ | 新 push 清空旧 approve |
| Require status checks to pass before merging | ✅ | **关键** |
| Require branches to be up to date | ✅ | 防止 base 漂移 |
| **Required status checks**（搜索并添加） | `agent-framework-reports` | **本框架新加的 check** |
| | `CI / test (pull_request)` | 兼容旧 CI |
| Require conversation resolution | ✅ | 阻断未解决 review 评论 |
| Require linear history | ✅ | 强制 rebase 或 squash，禁 merge commit |
| Include administrators | ❌（默认） | 管理员不受限；如需严格可勾 |
| Allow force pushes | ❌ | 阻断 force push |
| Allow deletions | ❌ | 阻断删 main |

### 3. 关键 status check 名

**`agent-framework-reports`** — 由 `scripts/agent_framework/check_reports.py` 决定 pass/fail。
GitHub Actions 的 job / step 名必须完全匹配才能在保护规则里搜到。

如果设置时搜不到这个 check，先推一次 PR 让 workflow 跑一次，check 出现后再回来添加。

### 4. 验证

```bash
# 临时建一个空 PR，CI 跑过后看 status check 列表
gh pr create --draft --title "test branch protection"
gh pr checks

# 应该看到：
#   CI / test           pass
#   agent-framework-reports  pass  ← 新 check
```

## Phase 升级路径

| Phase | require_approvals | agent-framework-reports | 其他 status check |
|-------|-------------------|------------------------|------------------|
| 1 | 1 | ✅ 必过 | CI/test 必过 |
| 2 | 1 | ✅ 必过 | CI/test + agent-framework-reports |
| 3 | 0 | ✅ 必过 | 同上（人工 review 由"看报告"替代） |
| 4 | 0 | ✅ 必过 | + Monitor Agent 异常告警 |

## 故障排查

| 现象 | 原因 | 解法 |
|------|------|------|
| status check 搜不到 `agent-framework-reports` | workflow 还没跑过 | 推任意 commit 触发一次，再回来添加 |
| check 名字对但保护规则不生效 | workflow 文件改动后 check 名变了 | 更新规则中的 check 名 |
| 管理员绕过 | Include administrators 未勾 | 勾上 |
| PR 一直灰显"需要 approval" | approval 数不足 | Phase 1 仍需 1 人审批；可让任意 maintainer 点 Approve |

## 当前状态（2026-10-13）

- ⏳ **待 Eric 在 GitHub repo settings 操作**
- 框架已落地（PR 模板 / 脚本 / CI workflow 改造）
- branch protection 是唯一需要手动点的部分