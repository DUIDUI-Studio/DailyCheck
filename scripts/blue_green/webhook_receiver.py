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
#   2. 仅处理 pull_request + status event（issue 由 DailyCheck 自己的 automation 处理）
#   3. action 白名单分事件：pull_request ∈ {opened, synchronize, synchronized, reopened}
#      —— 注意 Gitea 的 synchronize 拼作 "synchronized"（多一个 d），两个都收
#      status = 有 open PR 关联就落盘；中间态由下游 reducer 静默记账，
#      只有确定性 readiness gate 通过后才产生一次 MERGE_READY provider 调用
#
# Gitea 适配（2026-10-22，GitHub 账号停用后迁至 hub1 自托管 Gitea 28.1）：
#   - Gitea 同时发 GitHub 兼容头：X-GitHub-Event / X-GitHub-Delivery /
#     X-Hub-Signature-256（格式与 GitHub 完全一致 `sha256=<hex>`）→ 验签与幂等逻辑不用改
#   - 事件名差异：Gitea 没有 check_suite，改用 status（每次 commit status 变化触发一次）；
#     status 的 payload 没有 PR 关联字段，需按 sha 反查 open PR；决定性 check
#     判定与 transition-only MERGE_READY 由 prod_agent/event_reducer.py 负责
#   - PR 细分事件（pull_request_sync / pull_request_comment）仍以
#     X-GitHub-Event: pull_request 发送，细分类别在 X-GitHub-Event-Type 里
#   - Gitea 默认禁止向内网投递 webhook，需 app.ini 配
#     [webhook] ALLOWED_HOST_LIST = 172.28.129.215,172.28.129.216
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
#     Events: 勾 "Pull requests" + "Check suites"

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, abort, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.hosting import (  # noqa: E402
    HostingClient,
    HostingConfig,
    HostingError,
    Provider,
    provider,
    require_shadow_path,
)

_GITHUB_INTAKE_LOCK = threading.Lock()

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

# ---- Gitea 侧（status 事件需要反查 PR 与 check 汇总）----
GITEA_URL = os.environ.get("GITEA_URL", "http://172.28.129.216:3000").rstrip("/")
GITEA_TOKEN_FILE = os.environ.get("GITEA_TOKEN_FILE", "/root/.gitea-token")
GITEA_TOKEN = os.environ.get("GITEA_TOKEN")  # 直接给值则优先，否则读文件
GITEA_REPO = os.environ.get("GITEA_REPO", "eric/dailycheck")
GITEA_TIMEOUT = int(os.environ.get("GITEA_TIMEOUT", "10"))

# 仅处理的事件 + 分事件 action 白名单
#   pull_request —— PR 生命周期触点（开 / 更新 / 重开）
#   check_suite  —— CI 完成触点（仅 completed + 有 PR 关联才落盘）
# 三事件并存：
#   pull_request —— PR 生命周期（GitHub / Gitea 共用，Gitea 的 synchronize 拼作 synchronized）
#   check_suite  —— GitHub 的 CI 完成事件（保留：迁移期兼容 + 既有测试覆盖）
#   status       —— Gitea 的 CI 完成事件（替代 check_suite）
ALLOWED_EVENTS = frozenset({"pull_request", "check_suite", "status"})
ALLOWED_ACTIONS_BY_EVENT: dict[str, frozenset[str]] = {
    "pull_request": frozenset({"opened", "synchronize", "synchronized", "reopened"}),
    "check_suite": frozenset({"completed"}),
    "status": frozenset(),  # status 事件无 action 字段，走独立分支
}

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
    for path in (INBOX_PATH, PROCESSED_PATH):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            rows = list(f)
        for line in rows:
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


def _build_pull_request_record(delivery_id: str, action: str, data: dict) -> dict:
    """pull_request 事件 → inbox 记录（原字段集，保持向后兼容）。"""
    pr = data.get("pull_request") or {}
    repo = data.get("repository") or {}
    user = pr.get("user") or {}
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    return {
        "delivery_id": delivery_id,
        "event": "pull_request",
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


def _build_check_suite_record(delivery_id: str, action: str, data: dict) -> dict:
    """check_suite 事件 → inbox 记录（字段形状与 pull_request 记录对齐）。

    check_suite payload 没有 PR 标题 / 作者 / state 字段——填空串；
    下游（watcher 过滤 / 订阅模板）依赖的 pr_number / pr_head_sha /
    repo_full_name 从 suite 及其关联 PR 提取。check_* 为通道专属字段。

    前置（handler 已过滤）：pull_requests 非空 且 conclusion == success。
    """
    check_suite = data.get("check_suite") or {}
    repo = data.get("repository") or {}
    prs = check_suite.get("pull_requests") or []
    pr = prs[0] if prs else {}
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    repo_full_name = repo.get("full_name", "")
    pr_number = pr.get("number")
    return {
        "delivery_id": delivery_id,
        "event": "check_suite",
        "action": action,
        "received_at_ms": int(time.time() * 1000),
        "pr_number": pr_number,
        "pr_title": "",
        "pr_url": (
            f"https://github.com/{repo_full_name}/pull/{pr_number}" if pr_number else ""
        ),
        "pr_state": "",
        "pr_draft": False,
        "pr_head_sha": check_suite.get("head_sha") or head.get("sha", ""),
        "pr_base_ref": base.get("ref", ""),
        "pr_author": "",
        "repo_full_name": repo_full_name,
        "repo_visibility": repo.get("visibility", repo.get("private", None)),
        "check_conclusion": check_suite.get("conclusion", ""),
        "check_head_branch": check_suite.get("head_branch", ""),
        "check_suite_id": check_suite.get("id"),
    }


# ============================================================
# Gitea 查询（status 事件反查 PR 与 check 汇总）
# ============================================================
def _gitea_token() -> str | None:
    """取 API token：环境变量优先，否则读文件。取不到返回 None（不抛，降级为忽略）。"""
    if GITEA_TOKEN:
        return GITEA_TOKEN.strip()
    try:
        with open(GITEA_TOKEN_FILE, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _gitea_get(path: str, token: str) -> object:
    """GET Gitea API，返回解析后的 JSON。失败抛异常，由调用方兜底。"""
    req = urllib.request.Request(
        f"{GITEA_URL}/api/v1{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=GITEA_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_pr_for_sha(sha: str, token: str) -> dict | None:
    """按 head sha 反查 open PR（Gitea 的 status 事件 payload 不带 PR 信息）。"""
    if not sha:
        return None
    prs = _gitea_get(f"/repos/{GITEA_REPO}/pulls?state=open&limit=50", token)
    if not isinstance(prs, list):
        return None
    for pr in prs:
        head = pr.get("head") or {}
        if head.get("sha") == sha:
            return pr
    return None


def _build_status_record(delivery_id: str, data: dict, pr: dict) -> dict:
    """status 事件 → inbox 记录。

    字段形状与 check_suite 记录**完全对齐**，且 event 字段仍写 "check_suite"，
    使下游 watcher / 订阅模板无需改动即可继续工作（Gitea 侧的真实事件名记在
    source_event 里供排查）。
    """
    repo = data.get("repository") or {}
    repo_full_name = repo.get("full_name") or GITEA_REPO
    pr_number = pr.get("number")
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    user = pr.get("user") or {}
    return {
        "delivery_id": delivery_id,
        "event": "check_suite",  # 向下游保持既有契约
        "source_event": "status",
        "action": "completed",
        "received_at_ms": int(time.time() * 1000),
        "pr_number": pr_number,
        "pr_title": pr.get("title", ""),
        "pr_url": pr.get("html_url", ""),
        "pr_state": pr.get("state", ""),
        "pr_draft": bool(pr.get("draft", False)),
        "pr_head_sha": head.get("sha", ""),
        "pr_base_ref": base.get("ref", ""),
        "pr_author": user.get("login", ""),
        "repo_full_name": repo_full_name,
        "repo_visibility": repo.get("visibility", repo.get("private", None)),
        "check_conclusion": data.get("state", ""),
        "check_head_branch": head.get("ref", ""),
        "check_suite_id": data.get("id"),
        "status_context": data.get("context", ""),
    }


def _github_webhook(payload: bytes):
    """Native GitHub events are hints in a separately configured shadow inbox."""
    try:
        config = HostingConfig.from_environment()
        shadow = require_shadow_path(INBOX_PATH)
        require_shadow_path(PROCESSED_PATH)
        if (INBOX_PATH.resolve() != (shadow / "inbox.jsonl").resolve()
                or PROCESSED_PATH.resolve() != (shadow / "processed.jsonl").resolve()):
            abort(503, "GitHub requires isolated shadow intake")
        data = json.loads(payload)
        if not isinstance(data, dict):
            abort(400, "invalid payload")
        repository = data.get("repository")
        if not isinstance(repository, dict) or repository.get("id") != config.repo_id or repository.get("full_name") != config.repo:
            abort(400, "repository mismatch")
        event = request.headers.get("X-GitHub-Event", "")
        action = data.get("action")
        if event == "pull_request":
            if action not in {"opened", "synchronize", "reopened", "ready_for_review"}:
                return jsonify(status="ignored", reason="action"), 200
            source = data.get("pull_request")
            if not isinstance(source, dict) or not isinstance(source.get("head"), dict):
                abort(400, "invalid pull request")
            sha = source["head"].get("sha")
        elif event in {"check_suite", "check_run", "workflow_run"}:
            if action not in {"completed", "requested", "rerequested", "created", "in_progress"}:
                return jsonify(status="ignored", reason="action"), 200
            source = data.get(event)
            if not isinstance(source, dict):
                abort(400, "invalid check payload")
            sha = source.get("head_sha")
        elif event == "status":
            source = data
            sha = data.get("sha")
        else:
            return jsonify(status="ignored", reason="event"), 200
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            abort(400, "invalid head sha")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", delivery):
            abort(400, "invalid delivery id")
        # The supported receiver is one worker with multiple threads.
        with _GITHUB_INTAKE_LOCK:
            seen = _read_inbox_delivery_ids()
            client = HostingClient(config)
            client.verify_repository()
            # Native check associations can be absent (forks) or contain multiple PRs.
            # Enumerate all current open PRs; bind only exact H and target base identity.
            prs = client.pages(f"/repos/{config.repo}/pulls", params={"state": "open"})
            matches = []
            for pr in prs:
                head, base = pr.get("head"), pr.get("base")
                if not isinstance(head, dict) or not isinstance(base, dict):
                    raise HostingError("invalid_pr_payload")
                if head.get("sha") != sha:
                    continue
                base_repo = base.get("repo")
                if not isinstance(base_repo, dict) or base_repo.get("id") != config.repo_id:
                    raise HostingError("invalid_pr_base")
                if base.get("ref") != config.base_ref or pr.get("state") != "open":
                    continue
                if event == "pull_request" and pr.get("number") != data.get("number"):
                    continue
                if type(pr.get("number")) is not int or pr["number"] <= 0:
                    raise HostingError("invalid_pr_number")
                matches.append(pr)
            if not matches:
                return jsonify(status="ignored", reason="no_current_pr"), 200
            matches = [pr for pr in matches if f"{delivery}:pr:{pr['number']}" not in seen]
            if not matches:
                return jsonify(status="duplicate", delivery_id=delivery), 200
            if _inbox_line_count() + len(matches) > MAX_INBOX_LINES:
                abort(503, "inbox full")
            for pr in matches:
                did = f"{delivery}:pr:{pr['number']}"
                if event == "pull_request":
                    record = _build_pull_request_record(did, action, {**data, "pull_request": pr})
                else:
                    hint = {**data, "state": source.get("conclusion") or source.get("status") or source.get("state") or "pending"}
                    record = _build_status_record(did, hint, pr)
                    record["source_event"] = event
                    record["check_suite_id"] = source.get("id")
                record.update(hosting_provider="github", intake_mode="shadow", source_delivery_id=delivery, pr_base_sha=pr["base"].get("sha"))
                _append_inbox(record)
        return jsonify(status="queued", delivery_id=delivery, mode="shadow"), 202
    except (HostingError, OSError):
        abort(503, "hosting lookup unavailable")
    except (ValueError, UnicodeError):
        abort(400, "invalid payload")


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

    try:
        if provider() is Provider.GITHUB:
            return _github_webhook(payload)
    except HostingError:
        abort(503, "invalid hosting configuration")

    # 2. event 过滤
    event = request.headers.get("X-GitHub-Event", "")
    if event not in ALLOWED_EVENTS:
        return jsonify({"status": "ignored", "reason": "event", "event": event}), 200

    # 3. 解析 JSON
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError):
        abort(400, "invalid json")
    if not isinstance(data, dict):
        abort(400, "invalid payload")

    # 4. action 过滤（按事件类型分白名单；status 事件无 action，跳过）
    action = data.get("action")
    if event != "status" and action not in ALLOWED_ACTIONS_BY_EVENT[event]:
        return jsonify(
            {"status": "ignored", "reason": "action", "event": event, "action": action}
        ), 200

    # 4.5 CI 业务过滤：只把关联到 open PR 的更新交给 reducer。
    #     reducer 负责精确 required-context 匹配、Gitea readiness 查询和
    #     transition-only MERGE_READY；中间态/失败落账但不唤醒 provider。
    if event == "check_suite":
        check_suite = data.get("check_suite") or {}
        if not (check_suite.get("pull_requests") or []):
            return jsonify({"status": "ignored", "reason": "no_pr"}), 200

    status_pr: dict | None = None
    if event == "status":
        sha = str(data.get("sha") or "")
        token = _gitea_token()
        if not token:
            # 拿不到 token 时不静默丢弃：返回 503 让 Gitea 重试，故障必须可见
            abort(503, "GITEA_TOKEN not configured")
        try:
            status_pr = _resolve_pr_for_sha(sha, token)
            if not status_pr:
                return jsonify({"status": "ignored", "reason": "no_pr"}), 200
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as exc:
            # 反查失败：让 Gitea 重试，避免快通道静默丢失
            abort(503, f"gitea lookup failed: {type(exc).__name__}")

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

    # 7. 提取关键字段 + 写 inbox（按事件类型分叉构造，字段形状对齐）
    if event == "pull_request":
        record = _build_pull_request_record(delivery_id, action, data)
    elif event == "check_suite":
        record = _build_check_suite_record(delivery_id, action, data)
    else:  # status（status_pr 已在 4.5 解出）
        record = _build_status_record(delivery_id, data, status_pr or {})
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
