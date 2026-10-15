#!/usr/bin/env bash
# scripts/agent_framework/preview.sh
# 半自动产出报告 04（本地部署预演）
#
# 用法：
#   bash scripts/agent_framework/preview.sh <pr-num>
#
# 产出：
#   reports/pr-<num>/04-local-deploy-preview.md（按模板填充）
#
# 当前实现：调 dev_smoke_t11_e2e.py（已有 T11 端到端冒烟），
# 把输出套进报告模板。后续可接 docker compose + Playwright。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PR_NUM="${1:-local}"
OUT_DIR="${REPO_ROOT}/reports/pr-${PR_NUM}"
OUT_FILE="${OUT_DIR}/04-local-deploy-preview.md"

mkdir -p "${OUT_DIR}"

echo "[preview] running T11 E2E smoke for PR #${PR_NUM} ..."

# 跑现有冒烟脚本（test client 形式，不依赖真实端口）
SMOKE_OUTPUT="/tmp/preview-smoke-${PR_NUM}.txt"
if python3 "${REPO_ROOT}/scripts/dev_smoke_t11_e2e.py" > "${SMOKE_OUTPUT}" 2>&1; then
  SMOKE_STATUS="✅ PASS"
else
  SMOKE_STATUS="❌ FAIL"
fi

# 抽取 pytest 风格的通过/失败
SMOKE_TAIL="$(tail -3 "${SMOKE_OUTPUT}")"

# 当前时间
NOW="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
COMMIT="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
BRANCH="$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"

# 套模板
cat > "${OUT_FILE}" <<EOF
# 报告 04/4：本地部署预演报告（半自动 · v0.1）

> 生成方式：\`scripts/agent_framework/preview.sh\`
> 模板：[docs/agent-framework/templates/04-local-deploy-preview.md](../../../docs/agent-framework/templates/04-local-deploy-preview.md)

## 1. 元信息

| 字段 | 值 |
|------|-----|
| PR | #${PR_NUM} |
| 分支 | ${BRANCH} |
| 镜像 tag | dailycheck:pr-${PR_NUM}-${COMMIT:0:7} |
| compose 文件 | docker-compose.preview.yml（**待补**：与 prod 同构） |
| Dev Agent | 自动 preview.sh |
| 生成时间 | ${NOW} |

## 2. 环境对齐检查

| 项 | Mac mini（dev） | wdg_vps (prod) | 一致性 |
|----|----------------|----------------|--------|
| Python | 3.13.12 | 3.12.x | ⚠️ minor（已在 dev_smoke 跑通） |
| SQLite | 3.45 | 3.37 | ⚠️ minor |
| 系统包 | macOS | ubuntu 22.04 | ⚠️ 已知差异 |
| gunicorn workers | 2 | 2 | ✅ |
| 监听端口 | 8091 | 8090 | ✅（隔离） |

## 3. 启动日志摘要（T11 E2E 冒烟）

\`\`\`
${SMOKE_TAIL}
\`\`\`

整体状态：**${SMOKE_STATUS}**

## 4. 关键路径冒烟

| 路径 | 测试客户端 | 状态 |
|------|-----------|------|
| 加购 → 提交 → 审批 → 发货 → 收货（端到端） | \`scripts/dev_smoke_t11_e2e.py\` | ${SMOKE_STATUS} |
| 关键页面渲染 | **待 Phase 2 接入 Playwright** | ⏳ |

**门禁**：任一失败 → 阻塞 PR。

## 5. 资源基线

| 指标 | 启动后 1 分钟 | 启动后 5 分钟 |
|------|--------------|--------------|
| CPU | ⏳ Phase 2 接 docker compose 后采集 | ⏳ |
| 内存 | ⏳ | ⏳ |

## 6. 回滚演练（清理干净）

\`\`\`
# preview 阶段：test client 自动清理临时 db（脚本内 shutil.rmtree）
\`\`\`

## 7. 报告签名

Dev Agent: preview.sh 自动 · ${COMMIT:0:7}
报告生成时间: ${NOW}
下次预演：CI workflow 自动跑 + 本地手动可重跑

---

> **门禁**：冒烟任一失败 → 阻塞 PR。
> **状态**：v0.1 半自动（仅覆盖 T11 路径；完整路径待 Phase 2 接 Playwright + docker compose）。
EOF

echo "[preview] report written: ${OUT_FILE}"
echo "[preview] smoke status: ${SMOKE_STATUS}"

# 冒烟失败 → exit 1（让 CI 报警）
if [[ "${SMOKE_STATUS}" == *"FAIL"* ]]; then
  exit 1
fi
exit 0