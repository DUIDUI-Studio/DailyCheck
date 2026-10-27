#!/usr/bin/env python3
"""DailyCheck open PR 兜底轮询（30 分钟）—— webhook 快通道的保底发现通道。

背景（Issue #47 / docs/2026-10-15-webhook-setup.md §3.8）：
  快通道 = Gitea webhook → dc-webhook-receiver → inbox.jsonl → dc-inbox-watcher
           → dc-notify-prod-agent → Hermes wdg-pr 订阅（≤5s 唤起判定）。
  本脚本是「快通道整体故障时」的兜底：每 30 分钟直接查 Gitea open PR。

迁移说明（2026-10-22，GitHub 账号停用后迁至 hub1 自托管 Gitea）：
  原实现调 `gh pr list --json`，依赖 GitHub CLI。现改为直连 Gitea REST API：
    - 无需安装 gh / tea，仅用标准库 urllib
    - `statusCheckRollup` → Gitea 的 `/commits/{sha}/status`（combined status）
    - 注意 Gitea 的 status context 形如 `CI / test (pull_request)`，
      与 GitHub 的裸 `test` 不同，下游判定须按「包含 pytest/check 关键字」匹配，
      不能做精确等值比较

静默语义（skill `no-agent-cron-script-pattern`）：
  - 无 open PR        → 零 stdout（cron agent 收到空输出 → 回 [SILENT]，无投递）
  - 有 open PR        → stdout 输出 PR 清单（agent 按 dailycheck-prod-agent skill 判定）
  - 查询失败/配置错误 → stdout 输出 ⚠️ 告警（保底通道自身故障必须可见，不得静默）

配置（env 驱动）：
  GITEA_URL       — 默认 http://172.28.129.216:3000（hub1 内网）
  GITEA_TOKEN_FILE— 默认 /root/.gitea-token
  DCPOLL_REPO     — 默认 eric/dailycheck；仅用于调试替换目标仓库

测试：DCPOLL_REPO=erce/dailycheck python3 dailycheck_pr_poll.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

GITEA_URL = os.environ.get("GITEA_URL", "http://172.28.129.216:3000").rstrip("/")
TOKEN_FILE = os.environ.get("GITEA_TOKEN_FILE", "/root/.gitea-token")
TIMEOUT = 60
DEFAULT_REPO = "eric/dailycheck"
# 仅用于测试输出格式；生产 cron 运行时不设置该变量
REPO = os.environ.get("DCPOLL_REPO", DEFAULT_REPO)


def _alert(msg: str) -> int:
    print(f"⚠️ DailyCheck PR 兜底轮询异常：{msg}")
    return 0


def _api_get(path: str, token: str):
    """GET Gitea API，返回解析后的 JSON。"""
    req = urllib.request.Request(
        f"{GITEA_URL}/api/v1{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _combined_check_states(token: str, sha: str) -> str:
    """取某 commit 的 combined status，压成一行状态串（等价 statusCheckRollup）。

    Gitea status context 形如 `CI / test (pull_request)`；这里只输出
    `context=status` 对，下游按需匹配。
    """
    if not sha:
        return "none"
    try:
        data = _api_get(f"/repos/{REPO}/commits/{sha}/status", token)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
        return "unknown"
    statuses = data.get("statuses") or []
    if not statuses:
        return "none"
    parts = sorted(
        f"{s.get('context', '?')}={s.get('status', '?')}" for s in statuses
    )
    return ";".join(parts)


def main() -> int:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        return _alert(f"无法读取 {TOKEN_FILE}（{exc}）")
    if not token:
        return _alert(f"{TOKEN_FILE} 为空")

    try:
        prs = _api_get(f"/repos/{REPO}/pulls?state=open&limit=20", token)
    except urllib.error.HTTPError as exc:
        return _alert(f"Gitea API HTTP {exc.code}（{exc.reason}）")
    except urllib.error.URLError as exc:
        return _alert(f"Gitea 不可达（{exc.reason}）")
    except ValueError:
        return _alert("Gitea 返回非 JSON")
    except TimeoutError:
        return _alert(f"Gitea 请求超时（{TIMEOUT}s）")

    if not isinstance(prs, list):
        return _alert(f"Gitea 返回结构异常（{type(prs).__name__}）")

    if not prs:
        return 0  # 无 open PR — 静默（设计语义）

    lines = [f"📋 {REPO} open PR 共 {len(prs)} 个（30 分钟兜底轮询）："]
    for pr in prs:
        head = pr.get("head") or {}
        sha = head.get("sha", "")
        checks = _combined_check_states(token, sha)
        lines.append(
            f"- PR #{pr.get('number')}「{pr.get('title')}」 "
            f"branch={head.get('ref')} head={sha[:8]} "
            f"mergeable={pr.get('mergeable')} draft={pr.get('draft')} checks=[{checks}]"
        )
    lines.append(
        "请按 dailycheck-prod-agent skill 走 merge 判定：CI 两必需 check"
        "（context 含 `test` 与 `agent-framework-reports`）全为 success 才进队列；"
        "红线文件命中转 owner。无异议才 merge。"
    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
