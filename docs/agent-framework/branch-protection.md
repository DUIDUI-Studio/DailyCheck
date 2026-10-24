# Branch Protection 现状与变更方式（Gitea）

> 2026-10-22 仓库迁至自托管 Gitea（`eric/dailycheck`），分支保护随之迁移。
> 2026-10-24 用 API 实测核对过线上配置，本文记录**实际生效值**，不是待办清单。

## 1. 当前生效配置（`main`）

实测方式：

```bash
curl -s -H "Authorization: token $T" \
  http://172.28.129.216:3000/api/v1/repos/eric/dailycheck/branch_protections/main
```

| 字段 | 实际值 | 含义 |
|------|--------|------|
| `enable_push` | `false` | **禁止直推 main**，只能走分支 + PR |
| `enable_push_whitelist` | `false` | 无白名单（管理员也受同一约束） |
| `enable_status_check` | `true` | 必需状态检查开启 |
| `status_check_contexts` | `CI / test (pull_request)`<br>`CI / agent-framework-reports (pull_request)` | 两个必需 check，**必须带 `(pull_request)` 后缀** |
| `required_approvals` | `0` | 不强制人工审批（merge 判定由 Prod Agent 承担，见 workflow.md §四） |
| `enable_force_push` | `false` | 禁止 force push |

## 2. 为什么 check 名必须带后缀

Gitea Actions 上报的 context 是 `<workflow 名> / <job 名> (<事件>)`，本仓库是
`CI / test (pull_request)`。只写 `test` 或 `agent-framework-reports` 都会**永远判定未全绿**，
merge 时返回 `Not all required status checks successful` —— 这是迁移时最容易踩的一脚。

查某个 commit 的实际 context 列表：

```bash
curl -s -H "Authorization: token $T" \
  "http://172.28.129.216:3000/api/v1/repos/eric/dailycheck/commits/<sha>/status" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['state']);[print(' -',s['context'],s['status']) for s in d['statuses']]"
```

## 3. 变更方式（需 owner 拍板）

改分支保护属红线（workflow.md §四 第 3 条），**先问 owner 再动**。改完用 §1 的 GET 复核。

```bash
# 例：把必需 check 改成两个（字段名以 swagger 的 EditBranchProtectionOption 为准）
cat > /tmp/bp.json <<'JSON'
{
  "enable_push": false,
  "enable_status_check": true,
  "status_check_contexts": [
    "CI / test (pull_request)",
    "CI / agent-framework-reports (pull_request)"
  ],
  "required_approvals": 0,
  "enable_force_push": false
}
JSON
curl -s -X PATCH \
  -H "Authorization: token $T" -H "Content-Type: application/json" \
  --data-binary @/tmp/bp.json \
  http://172.28.129.216:3000/api/v1/repos/eric/dailycheck/branch_protections/main
```

## 4. 故障排查

| 现象 | 原因 | 解法 |
|------|------|------|
| PR 全绿但合并按钮灰 | context 名不匹配（少了 `(pull_request)` 后缀） | 按 §2 查实际 context，再按 §3 改 |
| 推 main 报 `403 user cannot commit to repo` | 直推受保护分支 | 建分支 + PR（这是设计行为，不是权限故障） |
| 保护规则改了没生效 | 改的是错的 branch_name，或 PATCH 用了创建接口 | 用 `PATCH .../branch_protections/main`，改完 GET 复核 |

## 5. 与 GitHub 时代的差异

| 项 | GitHub | Gitea |
|---|---|---|
| 配置入口 | Settings → Branches（勾选式） | API `branch_protections`（见 §3）；Web 在 Settings → Branches |
| 必需 check 名 | job 名（如 `test`） | `<workflow> / <job> (pull_request)` |
| 管理员豁免 | Include administrators 可选 | `enable_push_whitelist=false` 即管理员也禁直推 |
| force push | Allow force pushes | `enable_force_push=false` |

> 历史注记：本文件在 GitHub 时代是「待 owner 手动点」的操作指引（2026-10-13）。
> 迁移后保护规则已由 API 配好并通过实测核对，故改写为现状 + 变更方式。
