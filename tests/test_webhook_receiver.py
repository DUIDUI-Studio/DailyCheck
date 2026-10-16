"""webhook_receiver 实装测试 — GitHub PR Webhook 接收器

验证（v1）：
- HMAC-SHA256 验签：错误/缺失签名 → 401
- 仅处理 pull_request event（其他 event 200 ignored）
- 仅处理 action ∈ {opened, synchronize, reopened}（其他 200 ignored）
- 幂等按 delivery_id：重复 delivery → 200 duplicate
- 反压：inbox ≥ MAX_INBOX_LINES → 503
- inbox 落盘格式：每行一个 JSON，含 pr_number/title/head_sha 等关键字段
- /health 端点：返回 inbox 大小
- 缺 WEBHOOK_SECRET env：500

不测（实跑在 VPS 由 Prod Agent 验）：
- 真 gunicorn 启动
- 真 nginx 8443 反代
- 真 GitHub repo webhook 投递
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sys
from pathlib import Path

# 把 scripts/blue_green 加进 import path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "blue_green"))

import webhook_receiver  # noqa: E402

# 探测口令：随机化避免「写死值被幂等分支掩盖」
_TEST_SECRET = f"test-secret-{secrets.token_hex(8)}"


def _make_signature(secret: str, payload: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _make_pr_payload(
    *,
    action: str = "opened",
    number: int = 42,
    title: str = "feat: add foo",
    head_sha: str = "deadbeef" * 5,
    base_ref: str = "main",
    author: str = "eric",
    draft: bool = False,
) -> bytes:
    return json.dumps({
        "action": action,
        "number": number,
        "pull_request": {
            "title": title,
            "html_url": f"https://github.com/ericmr1981/DailyCheck/pull/{number}",
            "state": "open",
            "draft": draft,
            "head": {"sha": head_sha, "ref": "feat/issue-N-foo"},
            "base": {"ref": base_ref},
            "user": {"login": author},
        },
        "repository": {
            "full_name": "ericmr1981/DailyCheck",
            "visibility": "public",
        },
    }, ensure_ascii=False).encode("utf-8")


# ============================================================
# env 校验
# ============================================================
def test_app_imports_without_webhook_secret():
    """import 不应因缺 WEBHOOK_SECRET 而退出（推迟到请求处理时校验）。"""
    # 如果 import 时 sys.exit，本测试根本跑不到
    assert webhook_receiver.app is not None


# ============================================================
# 1. HMAC 验签
# ============================================================
def test_webhook_rejects_missing_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setattr(webhook_receiver, "INBOX_PATH", tmp_path / "inbox.jsonl")
    client = webhook_receiver.app.test_client()
    payload = _make_pr_payload()
    resp = client.post(
        "/webhook",
        data=payload,
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "del-1"},
    )
    assert resp.status_code == 401
    assert b"invalid signature" in resp.data
    # 不写 inbox
    assert not (tmp_path / "inbox.jsonl").exists()


def test_webhook_rejects_wrong_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setattr(webhook_receiver, "INBOX_PATH", tmp_path / "inbox.jsonl")
    client = webhook_receiver.app.test_client()
    payload = _make_pr_payload()
    bad_sig = "sha256=" + "0" * 64
    resp = client.post(
        "/webhook",
        data=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "del-2",
            "X-Hub-Signature-256": bad_sig,
        },
    )
    assert resp.status_code == 401
    assert not (tmp_path / "inbox.jsonl").exists()


def test_webhook_rejects_signature_without_sha256_prefix(tmp_path, monkeypatch):
    """签名缺少 'sha256=' 前缀应拒（防格式错误绕过）。"""
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setattr(webhook_receiver, "INBOX_PATH", tmp_path / "inbox.jsonl")
    client = webhook_receiver.app.test_client()
    payload = _make_pr_payload()
    sig_no_prefix = hmac.new(_TEST_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhook",
        data=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "del-3",
            "X-Hub-Signature-256": sig_no_prefix,  # 缺 sha256= 前缀
        },
    )
    assert resp.status_code == 401


def test_webhook_rejects_missing_secret_returns_500(monkeypatch):
    """WEBHOOK_SECRET 未配置 → 500（服务配置错，不是攻击）。"""
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", None)
    client = webhook_receiver.app.test_client()
    resp = client.post(
        "/webhook",
        data=_make_pr_payload(),
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "del-4"},
    )
    assert resp.status_code == 500


# ============================================================
# 2. event 过滤
# ============================================================
def test_webhook_ignores_non_pull_request_event(tmp_path, monkeypatch):
    """非 pull_request event（如 push / issues）→ 200 ignored 不写 inbox。"""
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setattr(webhook_receiver, "INBOX_PATH", tmp_path / "inbox.jsonl")
    client = webhook_receiver.app.test_client()
    payload = _make_pr_payload()
    sig = _make_signature(_TEST_SECRET, payload)
    for event in ("push", "issues", "issue_comment", "release", "ping"):
        resp = client.post(
            "/webhook",
            data=payload,
            headers={
                "X-GitHub-Event": event,
                "X-GitHub-Delivery": f"del-{event}",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        assert b"ignored" in resp.data
    # inbox 应空
    assert not (tmp_path / "inbox.jsonl").exists()


# ============================================================
# 3. action 过滤
# ============================================================
def test_webhook_ignores_unsupported_action(tmp_path, monkeypatch):
    """closed / edited / labeled / assigned 等不在白名单 → 200 ignored。"""
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setattr(webhook_receiver, "INBOX_PATH", tmp_path / "inbox.jsonl")
    client = webhook_receiver.app.test_client()
    for action in ("closed", "edited", "labeled", "assigned", "review_requested"):
        payload = _make_pr_payload(action=action)
        sig = _make_signature(_TEST_SECRET, payload)
        resp = client.post(
            "/webhook",
            data=payload,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": f"del-action-{action}",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        assert b"ignored" in resp.data
    assert not (tmp_path / "inbox.jsonl").exists()


def test_webhook_accepts_all_listed_actions(tmp_path, monkeypatch):
    """白名单 {opened, synchronize, reopened} 三种 action 全部应 accepted。"""
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setattr(webhook_receiver, "INBOX_PATH", tmp_path / "inbox.jsonl")
    client = webhook_receiver.app.test_client()
    for i, action in enumerate(("opened", "synchronize", "reopened")):
        payload = _make_pr_payload(action=action, number=100 + i)
        sig = _make_signature(_TEST_SECRET, payload)
        resp = client.post(
            "/webhook",
            data=payload,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": f"del-accept-{action}",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 202
        assert b"queued" in resp.data
    # inbox 应有 3 行
    inbox = tmp_path / "inbox.jsonl"
    assert inbox.exists()
    assert _inbox_line_count(inbox) == 3


# ============================================================
# 4. 幂等
# ============================================================
def test_webhook_idempotent_on_duplicate_delivery(tmp_path, monkeypatch):
    """同一 delivery_id 第二次投递 → 200 duplicate，inbox 不重复写。"""
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setattr(webhook_receiver, "INBOX_PATH", tmp_path / "inbox.jsonl")
    client = webhook_receiver.app.test_client()
    payload = _make_pr_payload(number=200)
    sig = _make_signature(_TEST_SECRET, payload)
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "del-dup-1",
        "X-Hub-Signature-256": sig,
    }
    # 第一次：202 queued
    resp1 = client.post("/webhook", data=payload, headers=headers)
    assert resp1.status_code == 202
    # 第二次：200 duplicate
    resp2 = client.post("/webhook", data=payload, headers=headers)
    assert resp2.status_code == 200
    assert b"duplicate" in resp2.data
    # inbox 只 1 行
    assert _inbox_line_count(tmp_path / "inbox.jsonl") == 1


def test_webhook_400_on_missing_delivery_id(tmp_path, monkeypatch):
    """缺 X-GitHub-Delivery 头 → 400（GitHub 永远会发，但容错拒伪）。"""
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setattr(webhook_receiver, "INBOX_PATH", tmp_path / "inbox.jsonl")
    client = webhook_receiver.app.test_client()
    payload = _make_pr_payload()
    sig = _make_signature(_TEST_SECRET, payload)
    resp = client.post(
        "/webhook",
        data=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            # 缺 X-GitHub-Delivery
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 400


# ============================================================
# 5. 反压
# ============================================================
def test_webhook_503_when_inbox_full(tmp_path, monkeypatch):
    """inbox ≥ MAX_INBOX_LINES → 503，GitHub 暂停重试。"""
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setattr(webhook_receiver, "INBOX_PATH", tmp_path / "inbox.jsonl")
    monkeypatch.setattr(webhook_receiver, "MAX_INBOX_LINES", 3)
    client = webhook_receiver.app.test_client()
    # 先填满 3 条
    for i in range(3):
        payload = _make_pr_payload(number=i + 1)
        sig = _make_signature(_TEST_SECRET, payload)
        resp = client.post(
            "/webhook",
            data=payload,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": f"del-fill-{i}",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 202
    # 第 4 条：应 503
    payload = _make_pr_payload(number=4)
    sig = _make_signature(_TEST_SECRET, payload)
    resp = client.post(
        "/webhook",
        data=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "del-fill-4",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 503
    assert b"inbox full" in resp.data
    # inbox 还是 3 行
    assert _inbox_line_count(tmp_path / "inbox.jsonl") == 3


# ============================================================
# 6. inbox 落盘格式
# ============================================================
def test_inbox_record_contains_all_required_fields(tmp_path, monkeypatch):
    """inbox 每行 JSON 必须含 PR 关键字段。"""
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setattr(webhook_receiver, "INBOX_PATH", tmp_path / "inbox.jsonl")
    client = webhook_receiver.app.test_client()
    payload = _make_pr_payload(
        action="opened", number=300, title="fix: bug", head_sha="abc123",
        base_ref="main", author="eric", draft=False,
    )
    sig = _make_signature(_TEST_SECRET, payload)
    resp = client.post(
        "/webhook",
        data=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "del-fields-1",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 202
    rec = _read_first_line(tmp_path / "inbox.jsonl")
    assert rec["delivery_id"] == "del-fields-1"
    assert rec["event"] == "pull_request"
    assert rec["action"] == "opened"
    assert rec["pr_number"] == 300
    assert rec["pr_title"] == "fix: bug"
    assert rec["pr_head_sha"] == "abc123"
    assert rec["pr_base_ref"] == "main"
    assert rec["pr_author"] == "eric"
    assert rec["pr_draft"] is False
    assert rec["repo_full_name"] == "ericmr1981/DailyCheck"
    assert "received_at_ms" in rec
    assert isinstance(rec["received_at_ms"], int)
    assert rec["received_at_ms"] > 0


# ============================================================
# 7. /health
# ============================================================
def test_health_returns_inbox_size(tmp_path, monkeypatch):
    """GET /health 返回 inbox 当前大小。"""
    monkeypatch.setattr(webhook_receiver, "WEBHOOK_SECRET", _TEST_SECRET)
    monkeypatch.setattr(webhook_receiver, "INBOX_PATH", tmp_path / "inbox.jsonl")
    client = webhook_receiver.app.test_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["inbox_size"] == 0
    # 写一条后
    payload = _make_pr_payload()
    sig = _make_signature(_TEST_SECRET, payload)
    client.post(
        "/webhook",
        data=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "del-health-1",
            "X-Hub-Signature-256": sig,
        },
    )
    resp = client.get("/health")
    data = resp.get_json()
    assert data["inbox_size"] == 1


# ============================================================
# 8. 辅助：inbox 行数 / 读首行
# ============================================================
def _inbox_line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def _read_first_line(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                return json.loads(line)
    raise ValueError("empty inbox")
