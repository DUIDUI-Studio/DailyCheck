#!/usr/bin/env bash
# scripts/agent_framework/generate_reports.sh
# 自动产出报告 02（测试）+ 报告 03（代码质量）
# 调用方：.github/workflows/ci.yml 的 agent-framework-reports step
#
# 输出：
#   reports/02-test-report.md
#   reports/03-code-quality.md
#   reports/ci-summary.json（汇总）
# 退出码：
#   0  全绿
#   1  报告产出但有失败项（仍生成报告，让 PR 看清楚）
#   2  生成过程出错

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_DIR="${REPO_ROOT}/reports"
mkdir -p "${OUT_DIR}"

BASE_SHA="${BASE_SHA:-HEAD~1}"
HEAD_SHA="${HEAD_SHA:-""}"
if [[ -z "${HEAD_SHA}" ]]; then
  HEAD_SHA="$(git rev-parse HEAD)"
fi

PYTEST_OUTPUT="/tmp/pytest-output-af.txt"
RUFF_OUTPUT="/tmp/ruff-output-af.txt"
BANDIT_OUTPUT="/tmp/bandit-output-af.txt"

# ============================================================
# 报告 02：测试
# ============================================================
echo "[generate_reports] running pytest ..."

python3 -m pytest \
  --basetemp=/tmp/pytest-dc-af \
  -q --tb=no \
  --ignore=tests/mcp_server \
  > "${PYTEST_OUTPUT}" 2>&1

PYTEST_EXIT=$?

# 提取数字（pytest -q 输出形如 "5 failed, 800 passed in 30.5s"）
PYTEST_SUMMARY="$(tail -5 "${PYTEST_OUTPUT}")"
FAILED=$(echo "${PYTEST_SUMMARY}" | grep -oE '[0-9]+ failed' | head -1 | grep -oE '[0-9]+' || echo "0")
PASSED=$(echo "${PYTEST_SUMMARY}" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '[0-9]+' || echo "0")

# 跑 known_failures 门禁
GATE_RESULT="$(python3 "${REPO_ROOT}/scripts/check_known_failures.py" "${PYTEST_OUTPUT}" 2>&1)" || true
GATE_EXIT=$?

cat > "${OUT_DIR}/02-test-report.md" <<EOF
# 报告 02/4：测试报告（自动产出）

> 生成方式：CI workflow 自动跑 pytest + known_failures 门禁
> 模板：[docs/agent-framework/templates/02-test-report.md](../../docs/agent-framework/templates/02-test-report.md)

## 元信息

| 字段 | 值 |
|------|-----|
| PR | ${PR_NUM:-"（手动运行）"} |
| 分支 | $(git rev-parse --abbrev-ref HEAD) |
| 基线 commit | ${BASE_SHA} |
| 当前 commit | ${HEAD_SHA} |
| 生成时间 | $(date -u +"%Y-%m-%dT%H:%M:%SZ") |

## 分层结果汇总

| 层 | 命令 | 通过 | 失败 | 跳过 | 与基线差 |
|----|------|------|------|------|---------|
| L1 静态扫描 | ruff | 见报告 03 | 见报告 03 | 0 | — |
| L2 单元测试 | pytest | ${PASSED} | ${FAILED} | — | **零新增门禁：${GATE_EXIT}** |
| L3 契约测试 | pytest tests/contract | (含 L2) | (含 L2) | — | — |
| L4 UI 测试 | 待接 | — | — | — |
| L5 本地冒烟 | 报告 04 | — | — | — |

## known_failures 比对

\`\`\`
${GATE_RESULT}
\`\`\`

## pytest 输出尾部（最后 5 行）

\`\`\`
${PYTEST_SUMMARY}
\`\`\`

## 报告签名

Dev Agent: CI 自动 · ${HEAD_SHA:0:7}
报告生成时间: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF

# ============================================================
# 报告 03：代码质量
# ============================================================
echo "[generate_reports] running ruff ..."

if [[ -n "${BASE_SHA}" ]]; then
  CHANGED_PY=$(git diff --name-only "${BASE_SHA}"..HEAD -- '*.py' 2>/dev/null || true)
else
  CHANGED_PY=$(git diff --name-only HEAD~1 -- '*.py' 2>/dev/null || true)
fi

RUFF_BASELINE_LINES=0
if [[ -n "${CHANGED_PY}" ]]; then
  ruff check "${CHANGED_PY}" > "${RUFF_OUTPUT}" 2>&1 || true
  # ruff 输出形如 `path/file.py:11:5: E401 Module imported but unused`，
  # wc -l 数的是行数（含空行/进度条），不是告警数。改用 grep -c 数真正告警。
  # 注意：grep -c 找不到匹配时 exit 1 + 输出 "0"，不能用 `|| echo 0`（会双 0）。
  RUFF_WARNINGS=$(grep -cE '^[^:]+:[0-9]+:[0-9]+: [A-Z][0-9]+ ' "${RUFF_OUTPUT}" 2>/dev/null || true)
  RUFF_WARNINGS=${RUFF_WARNINGS:-0}
  # 去掉末尾换行（grep -c 有时含 trailing newline）
  RUFF_WARNINGS=$(echo "${RUFF_WARNINGS}" | tr -d '\n' | head -c 16)
else
  echo "no python files changed" > "${RUFF_OUTPUT}"
  RUFF_WARNINGS=0
fi

# 复杂度（radon，CD/CC > 10 视为警示）
RADON_OUTPUT="/tmp/radon-output-af.txt"
if command -v radon >/dev/null 2>&1; then
  radon cc -s -n B "${REPO_ROOT}/blueprints" "${REPO_ROOT}/db" "${REPO_ROOT}/mcp_server" > "${RADON_OUTPUT}" 2>&1 || true
  HIGH_CC=$(grep -E '^[^ ]+ *|.*[CD] [0-9]+' "${RADON_OUTPUT}" 2>/dev/null | grep -E '\b[C-D] [0-9]{2,}\b' | wc -l || true)
  HIGH_CC=${HIGH_CC:-0}
  HIGH_CC=$(echo "${HIGH_CC}" | tr -d '\n' | head -c 16)
else
  echo "radon not installed" > "${RADON_OUTPUT}"
  HIGH_CC=""  # 空 → JSON 模板用 ${HIGH_CC:-null} 渲染为 null
fi

# 安全扫描（bandit，CI -lll 高危）
if command -v bandit >/dev/null 2>&1; then
  bandit -r "${REPO_ROOT}/blueprints" "${REPO_ROOT}/db" "${REPO_ROOT}/mcp_server" -lll > "${BANDIT_OUTPUT}" 2>&1 || true
  HIGH_VULN=$(grep -E 'Severity: High' "${BANDIT_OUTPUT}" | wc -l || true)
  HIGH_VULN=${HIGH_VULN:-0}
  HIGH_VULN=$(echo "${HIGH_VULN}" | tr -d '\n' | head -c 16)
else
  echo "bandit not installed" > "${BANDIT_OUTPUT}"
  HIGH_VULN=""  # 空 → JSON 模板用 ${HIGH_VULN:-null} 渲染为 null
fi

cat > "${OUT_DIR}/03-code-quality.md" <<EOF
# 报告 03/4：代码质量报告（自动产出）

> 生成方式：CI workflow 自动跑 ruff + radon + bandit
> 模板：[docs/agent-framework/templates/03-code-quality.md](../../docs/agent-framework/templates/03-code-quality.md)

## 元信息

| 字段 | 值 |
|------|-----|
| PR | ${PR_NUM:-"（手动运行）"} |
| 分支 | $(git rev-parse --abbrev-ref HEAD) |
| 基线 commit | ${BASE_SHA} |
| 当前 commit | ${HEAD_SHA} |
| 生成时间 | $(date -u +"%Y-%m-%dT%H:%M:%SZ") |

## Lint（ruff）

| 工具 | 命令 | 当前告警 | 状态 |
|------|------|---------|------|
| ruff | ruff check 改动 .py 文件 | ${RUFF_WARNINGS} | $([ "${RUFF_WARNINGS}" -eq 0 ] && echo "✅" || echo "⚠️") |

\`\`\`
$(cat "${RUFF_OUTPUT}")
\`\`\`

## 复杂度（radon）

| 指标 | 当前 |
|------|------|
| 改动文件圈复杂度 > 10 函数数 | ${HIGH_CC} |

\`\`\`
$(head -30 "${RADON_OUTPUT}")
\`\`\`

## 安全扫描（bandit）

| 指标 | 当前 |
|------|------|
| 高危漏洞 | ${HIGH_VULN} |

\`\`\`
$(head -10 "${BANDIT_OUTPUT}")
\`\`\`

## 报告签名

Dev Agent: CI 自动 · ${HEAD_SHA:0:7}
报告生成时间: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF

# ============================================================
# 汇总 JSON（给 check_reports.py 用）
# ============================================================
cat > "${OUT_DIR}/ci-summary.json" <<EOF
{
  "pr_num": "${PR_NUM:-}",
  "base_sha": "${BASE_SHA}",
  "head_sha": "${HEAD_SHA}",
  "pytest": {
    "passed": ${PASSED},
    "failed": ${FAILED},
    "gate_exit": ${GATE_EXIT}
  },
  "ruff": {
    "warnings": ${RUFF_WARNINGS}
  },
  "radon": {
    "high_cc": ${HIGH_CC:-null}
  },
  "bandit": {
    "high_vuln": ${HIGH_VULN:-null}
  },
  "generated_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}
EOF

echo "[generate_reports] reports written to ${OUT_DIR}/"
echo "[generate_reports] pytest exit=${PYTEST_EXIT}, known_failures gate=${GATE_EXIT}"

# 整体退出码：known_failures 门禁过了就 0；否则 1
if [[ ${GATE_EXIT} -eq 0 && ${RUFF_WARNINGS} -eq 0 ]]; then
  exit 0
else
  exit 1
fi