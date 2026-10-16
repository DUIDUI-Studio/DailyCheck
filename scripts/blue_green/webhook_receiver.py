#!/usr/bin/env python3
# blue_green/webhook_receiver.py —— GitHub PR Webhook 接收器
#
# 设计目标：
#   Dev Agent 提完 PR → GitHub repo 触发 webhook → VPS 这台 receiver 接收
#   → 解析 + 验签 + 幂等 + 反压 → 写入 inbox.jsonl
#   → Prod Agent 订阅 inbox.jsonl（inotify / tail -F）→ 跑 merge 决策
#
# 设计铁律（与 production-readiness 配套）：
#   1. HMAC-SHA256 验签 (X-Hub-Signature-256 + WEBHOOK_SECRET env)，拒伪 + 拒重放
#   2. 仅处理 pull_request event（issue 由 DailyCheck 自己的 automation 处理）
#   3. 仅处理 action ∈ {opened, synchronize, reopened}（closed 等跳过）
#   4. 幂等按 GitHub delivery_id（GitHub 会重试 6 次，receiver 必须幂等）
#   5. 反压：inbox 行数 ≥ MAX_INBOX_LINES 返回 503，GitHub 暂停重试
#   6. 仅监听 127.0.0.1（不暴露公网，由 nginx 在 443 做 TLS 终结 + proxy_pass）
#   7. /health 端点：探活 + 报告 inbox 行数
#
# 部署（详见 docs/2026-10-15-webhook-setup.md）：
#   - systemd: deploy/systemd/dc-webhook-receiver.service
#   - nginx: deploy/nginx/hook.kjtang.cn.conf（独立文件部署到 sites-enabled/hook.kjtang.cn）
#   - DNS: 阿里云加 hook.kjtang.cn A 记录 → VPS 公网 IP
#   - 证书: hook 独立 DV 证书 /etc/nginx/ssl/hook.kjtang.cn.{pem,key}
#     （阿里云免费"个人测试证书"= 单域名产品，无法给主站证书加 SAN，须单独申请）
#   - GitHub repo: Settings → Webhooks → Add
#     URL: https://hook.kjtang.cn/webhook
#     Secret: WEBHOOK_SECRET（32 字节强密码）
#     Events: 只勾 "Pull requests"

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from flask import Flask, abort, jsonify, request

# ============================================================
# 配置（env 驱动，禁写死）
# ============================================================
# 注意：import 期只设默认值，env 校验推迟到请求处理时（避免测试 import 即退出）
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
INBOX_PATH = Path(os.environ.get("INBOX_PATH", "/var/lib/dailycheck-prod-agent/inbox.jsonl"))
PROCESSED_PATH = Path(
    os.environ.get("PROCESSED_PATH", "/var/lib/dailycheck-prod-agent/processed.jsonl")
)
MAX_INBOX_LINES = int(os.environ.get("MAX_INBOX_LINES", "1000"))
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8443"))

# 仅处理的事件 + action
ALLOWED_EVENTS = frozenset({"pull_request"})
ALLOWED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})

# ============================================================
# Flask app
# ============================================================
app = Flask(__name__)


# ============================================================
# 辅助函数
# ============================================================
def _require_secret() -> None:
    """请求处理时校验 WEBHOOK_SECRET 已设。"""
    if not WEBHOOK_SECRET:
        abort(500, "WEBHOOK_SECRET not configured")


def _verify_signature(payload: bytes, signature: str) -> bool:
    """校验 GitHub HMAC-SHA256 签名。

    签名格式：`sha256=<hex>`
    算法：HMAC-SHA256(key=WEBHOOK_SECRET, msg=payload)
    校验：constant-time 防时序攻击。
    """
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _read_inbox_delivery_ids() -> set[str]:
    """读 inbox 已存在的 delivery_id 集合（幂等用）。"""
    ids: set[str] = set()
    if not INBOX_PATH.exists():
        return ids
    with INBOX_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # 容错：跳过坏行（inbox 偶尔写坏不挂服务）
                continue
            did = rec.get("delivery_id")
            if isinstance(did, str) and did:
                ids.add(did)
    return ids


def _inbox_line_count() -> int:
    if not INBOX_PATH.exists():
        return 0
    with INBOX_PATH.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f if _.strip())


def _append_inbox(record: dict) -> None:
    INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    # append-only，单进程（systemd 配 Restart=always + 单实例）
    # 不需要文件锁，假设同时只有一个 receiver 进程在写
    with INBOX_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ============================================================
# 路由
# ============================================================
@app.post("/webhook")
def webhook():
    _require_secret()
    # 1. HMAC 验签
    signature = request.headers.get("X-Hub-Signature-256", "")
    payload = request.get_data()
    if not _verify_signature(payload, signature):
        # 不暴露 detail（防攻击者探测）
        abort(401, "invalid signature")

    # 2. event 过滤
    event = request.headers.get("X-GitHub-Event", "")
    if event not in ALLOWED_EVENTS:
        return jsonify({"status": "ignored", "reason": "event", "event": event}), 200

    # 3. 解析 JSON
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        abort(400, "invalid json")

    # 4. action 过滤
    action = data.get("action")
    if action not in ALLOWED_ACTIONS:
        return jsonify({"status": "ignored", "reason": "action", "action": action}), 200

    # 5. delivery_id 幂等
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    if not delivery_id:
        abort(400, "missing delivery id")
    seen = _read_inbox_delivery_ids()
    if delivery_id in seen:
        return jsonify({"status": "duplicate", "delivery_id": delivery_id}), 200

    # 6. 反压
    cur_count = _inbox_line_count()
    if cur_count >= MAX_INBOX_LINES:
        # 503 让 GitHub 暂停重试（默认指数退避 1s/2s/4s/.../1h）
        abort(503, "inbox full")

    # 7. 提取 PR 关键字段 + 写 inbox
    pr = data.get("pull_request") or {}
    repo = data.get("repository") or {}
    user = pr.get("user") or {}
    head = pr.get("head") or {}
    base = pr.get("base") or {}

    record = {
        "delivery_id": delivery_id,
        "event": event,
        "action": action,
        "received_at_ms": int(time.time() * 1000),
        "pr_number": data.get("number"),
        "pr_title": pr.get("title", ""),
        "pr_url": pr.get("html_url", ""),
        "pr_state": pr.get("state", ""),
        "pr_draft": bool(pr.get("draft", False)),
        "pr_head_sha": head.get("sha", ""),
        "pr_base_ref": base.get("ref", ""),
        "pr_author": user.get("login", ""),
        "repo_full_name": repo.get("full_name", ""),
        "repo_visibility": repo.get("visibility", repo.get("private", None)),
    }
    _append_inbox(record)

    return jsonify({
        "status": "queued",
        "delivery_id": delivery_id,
        "inbox_size": cur_count + 1,
    }), 202


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "inbox_path": str(INBOX_PATH),
        "inbox_size": _inbox_line_count(),
        "max_inbox_lines": MAX_INBOX_LINES,
    }), 200


# ============================================================
# 入口（仅供 dev/CI 直接 python 跑，prod 走 gunicorn）
# ============================================================
if __name__ == "__main__":
    # dev only；prod 用 gunicorn
    #   gunicorn -w 1 -b 127.0.0.1:8443 scripts.blue_green.webhook_receiver:app
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False)
