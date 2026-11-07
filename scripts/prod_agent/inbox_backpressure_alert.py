#!/usr/bin/env python3
"""DailyCheck inbox 反压水位巡检 + 自愈 + 告警。

背景（2026-11-03）：inbox.jsonl 是 webhook 接收端的反压闸门，
行数 >= MAX_INBOX_LINES(=1000) 时 receiver 对**新**事件返回 503 "inbox full"
→ Gitea 重试耗尽后放弃 → CI 转绿的 status 事件从未进入 inbox
→ Prod Agent 的 webhook 唤醒链断在第一环（PR #141 实例）。

本脚本：
  1) 读 inbox 行数
  2) >= 水位阈值 → 先跑一次 inbox_rotate.py（按年龄归档）自愈
  3) 自愈后仍 >= 硬阈值 → 企微群告警（人工介入）
  4) 任何异常都只打日志，不抛栈（cron 场景）
只读 + 调用已有 rotate 脚本，不直接改 inbox 内容。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

INBOX = Path(os.environ.get("DC_INBOX", "/var/lib/dailycheck-prod-agent/inbox.jsonl"))
ROTATE = "/root/DailyCheck/scripts/prod_agent/inbox_rotate.py"
PY = "/root/DailyCheck/.venv/bin/python"
ENV_FILE = Path("/etc/dailycheck/wecom-notify.env")
WARN_RATIO = float(os.environ.get("DC_INBOX_WARN_RATIO", "0.8"))
MAX_LINES = int(os.environ.get("MAX_INBOX_LINES", "1000"))
HARD_LINES = int(os.environ.get("DC_INBOX_HARD_LINES", str(int(MAX_LINES * 0.95))))


def count_lines() -> int:
    try:
        with INBOX.open("rb") as fh:
            return sum(1 for _ in fh)
    except FileNotFoundError:
        return 0


def webhook_url() -> str:
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == "WECOM_STORE_ORDER_WEBHOOK":
            return v.strip().strip('"').strip("'")
    return ""


def notify(title: str, detail: str) -> bool:
    url = webhook_url()
    if not url:
        print("[alert] wecom webhook not configured; skip", file=sys.stderr)
        return False
    content = f"**{title}**\n{detail}"
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode() or "{}")
        ok = body.get("errcode") == 0
        print(f"[alert] wecom errcode={body.get('errcode')}", file=sys.stderr)
        return ok
    except Exception as exc:  # noqa: BLE001
        print(f"[alert] notify failed: {type(exc).__name__}", file=sys.stderr)
        return False


def main() -> int:
    n = count_lines()
    warn_at = int(MAX_LINES * WARN_RATIO)
    print(f"[inbox-alert] lines={n} max={MAX_LINES} warn_at={warn_at} hard_at={HARD_LINES}")
    if n < warn_at:
        return 0

    # 自愈：先按年龄归档一次
    try:
        r = subprocess.run([PY, ROTATE, "--inbox", str(INBOX)],
                           capture_output=True, text=True, timeout=120)
        print(f"[inbox-alert] rotate rc={r.returncode} {r.stdout.strip()[-200:]}")
    except Exception as exc:  # noqa: BLE001
        print(f"[inbox-alert] rotate failed: {type(exc).__name__}")

    after = count_lines()
    print(f"[inbox-alert] after_rotate lines={after}")
    if after >= HARD_LINES:
        notify(
            "⚠️ DailyCheck webhook inbox 反压告警",
            f"- inbox 行数 **{after}**（上限 {MAX_LINES}，硬阈值 {HARD_LINES}）\n"
            f"- 已自动执行 inbox_rotate.py 仍超阈值 → **新 PR 的 webhook 事件会被拒（503）**\n"
            f"- 影响：CI 转绿后 Prod Agent 不会被自动唤醒\n"
            f"- 处置：检查 `inbox.jsonl` 增长速率与 `dc-inbox-rotate.timer` 是否在跑",
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
