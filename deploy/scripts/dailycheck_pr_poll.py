#!/usr/bin/env python3
"""DailyCheck open PR 兜底轮询（30 分钟）—— webhook 快通道的保底发现通道。

背景（Issue #47 / docs/2026-10-15-webhook-setup.md §3.8）：
  快通道 = GitHub webhook → dc-webhook-receiver → inbox.jsonl → dc-inbox-watcher
           → dc-notify-prod-agent → Hermes dailycheck-pr 订阅（≤5s 唤起判定）。
  本脚本是「快通道整体故障时」的兜底：每 30 分钟直接查 GitHub open PR。

静默语义（skill `no-agent-cron-script-pattern`）：
  - 无 open PR        → 零 stdout（cron agent 收到空输出 → 回 [SILENT]，无投递）
  - 有 open PR        → stdout 输出 PR 清单（agent 按 dailycheck-prod-agent skill 判定）
  - 查询失败/配置错误 → stdout 输出 ⚠️ 告警（保底通道自身故障必须可见，不得静默）

测试：DCPOLL_REPO=cli/cli python3 dailycheck_pr_poll.py   # 借用别的仓库验证「有 PR」输出
"""
import json
import os
import subprocess
import sys

GH = "/usr/bin/gh"
TOKEN_FILE = "/root/.gh-token"
TIMEOUT = 60
DEFAULT_REPO = "ericmr1981/DailyCheck"
# 仅用于测试输出格式（如 DCPOLL_REPO=cli/cli）；生产 cron 运行时不设置该变量
REPO = os.environ.get("DCPOLL_REPO", DEFAULT_REPO)


def _alert(msg: str) -> int:
    print(f"⚠️ DailyCheck PR 兜底轮询异常：{msg}")
    return 0


def main() -> int:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        return _alert(f"无法读取 {TOKEN_FILE}（{exc}）")
    if not token:
        return _alert(f"{TOKEN_FILE} 为空")

    env = dict(os.environ, GH_TOKEN=token)
    try:
        proc = subprocess.run(
            [
                GH,
                "pr",
                "list",
                "--repo",
                REPO,
                "--state",
                "open",
                "--limit",
                "20",
                "--json",
                "number,title,headRefName,headRefOid,mergeable,isDraft,statusCheckRollup",
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return _alert(f"gh 超时（{TIMEOUT}s）")
    except OSError as exc:
        return _alert(f"gh 调用失败（{exc}）")

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().replace("\n", " ")[:200]
        return _alert(f"gh 退出码 {proc.returncode}: {stderr}")

    try:
        prs = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _alert("gh 输出非 JSON")

    if not prs:
        return 0  # 无 open PR — 静默（设计语义）

    lines = [f"📋 {REPO} open PR 共 {len(prs)} 个（30 分钟兜底轮询）："]
    for pr in prs:
        checks = pr.get("statusCheckRollup") or []
        states = (
            ",".join(
                sorted(
                    {
                        str(c.get("conclusion") or c.get("state") or "?").lower()
                        for c in checks
                    }
                )
            )
            or "none"
        )
        lines.append(
            f"- PR #{pr.get('number')}「{pr.get('title')}」 "
            f"branch={pr.get('headRefName')} head={str(pr.get('headRefOid') or '')[:8]} "
            f"mergeable={pr.get('mergeable')} draft={pr.get('isDraft')} checks=[{states}]"
        )
    lines.append(
        "请按 dailycheck-prod-agent skill 走 merge 判定：CI 两必需 check（test + "
        "agent-framework-reports）全绿才进队列；红线文件命中转 owner。无异议才 merge。"
    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
