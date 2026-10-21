"""Owner 拍板队列：纯函数 / 数据层 / 站内页 / Agent 接口。

这条链路的故障模式是「静默」——Prod Agent 拦了 PR，但 owner 不知道，
PR 就挂着。所以用例重点不在正常路径，在四种「信号丢失」：
  1. 登记请求畸形 → 整条丢掉
  2. 令牌未配置 → 通道静默失效
  3. 重复登记 → 把已决定的状态覆盖回 pending 或反过来
  4. 批准后没人消费 → 页面显示「已批准」但 PR 永远没合
"""
from __future__ import annotations

import sqlite3

import pytest

import config as config_module
import permissions_manifest
from blueprints import owner_approvals_pure as oap

TOKEN = "test-agent-token-0123456789"


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_normalize_registration_keeps_known_fields():
    got = oap.normalize_registration({
        "pr": "137", "title": "ci: 并行", "url": "https://github.com/x/pull/137",
        "risk_kind": "红线 2", "reason": "改 workflow", "extra": "ignored",
    })
    assert got == {
        "pr_number": 137,
        "pr_title": "ci: 并行",
        "pr_url": "https://github.com/x/pull/137",
        "risk_kind": "红线 2",
        "reason": "改 workflow",
    }


@pytest.mark.parametrize("payload", [
    None, "not-a-dict", [], 42,
])
def test_normalize_registration_rejects_non_object(payload):
    with pytest.raises(ValueError):
        oap.normalize_registration(payload)


@pytest.mark.parametrize("value", [None, "", "abc", 0, -3, True, 1.5])
def test_normalize_registration_rejects_bad_pr(value):
    with pytest.raises(ValueError):
        oap.normalize_registration({"pr": value})


def test_normalize_registration_drops_non_http_url():
    """pr_url 会进模板 href，放行 javascript: 等于开存储型 XSS。"""
    for bad in ("javascript:alert(1)", "data:text/html,x", "//evil", "ftp://x"):
        got = oap.normalize_registration({"pr": 1, "url": bad})
        assert got["pr_url"] == "", bad


def test_normalize_registration_clips_long_text():
    got = oap.normalize_registration({"pr": 1, "title": "x" * 500, "reason": "y" * 900})
    assert len(got["pr_title"]) == oap.MAX_TITLE
    assert len(got["reason"]) == oap.MAX_REASON


def test_token_matches_requires_both_sides():
    assert oap.token_matches("abc", "abc") is True
    assert oap.token_matches("abc", "abd") is False
    assert oap.token_matches("abc", "") is False   # 未配置 ≠ 放行
    assert oap.token_matches("", "abc") is False
    assert oap.token_matches(None, "abc") is False
    assert oap.token_matches("abcd", "abc") is False


def test_decide_allowed_only_from_pending():
    assert oap.decide_allowed(oap.STATUS_PENDING) is True
    assert oap.decide_allowed(oap.STATUS_APPROVED) is False
    assert oap.decide_allowed(oap.STATUS_REJECTED) is False


def test_target_status_rejects_unknown_intent():
    assert oap.target_status("approved") == oap.STATUS_APPROVED
    with pytest.raises(ValueError):
        oap.target_status("merge")


def test_markdown_has_pr_and_link():
    text = oap.build_pending_markdown(
        {"pr_number": 137, "pr_title": "ci: 并行", "risk_kind": "红线 2",
         "reason": "改 .github/workflows"},
        base_url="https://kjtang.cn",
    )
    assert "PR #137" in text
    assert "红线 2" in text
    assert "https://kjtang.cn/ops/approvals" in text


def test_markdown_omits_link_without_base_url():
    """相对链接在企微里点不开，宁可不给。"""
    text = oap.build_pending_markdown({"pr_number": 7}, base_url="")
    assert "](" not in text


# ---------------------------------------------------------------------------
# Agent 接口鉴权
# ---------------------------------------------------------------------------

def _api_headers(token: str | None = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


@pytest.fixture
def agent_token(monkeypatch):
    monkeypatch.setattr(config_module, "OPS_AGENT_TOKEN", TOKEN)
    return TOKEN


def test_api_without_token_is_401(logged_client):
    client, _ = logged_client
    resp = client.post("/api/ops/approvals", json={"pr": 1})
    assert resp.status_code == 401


def test_api_with_wrong_token_is_403(logged_client, agent_token):
    client, _ = logged_client
    resp = client.post(
        "/api/ops/approvals", json={"pr": 1},
        headers=_api_headers("wrong-token"),
    )
    assert resp.status_code == 403


def test_api_disabled_when_token_unset(logged_client, monkeypatch):
    """未配置令牌 = 通道关闭。绝不能「没配就放行」。"""
    monkeypatch.setattr(config_module, "OPS_AGENT_TOKEN", "")
    client, _ = logged_client
    for method, path in (
        ("post", "/api/ops/approvals"),
        ("get", "/api/ops/approvals"),
        ("post", "/api/ops/approvals/1/consume"),
    ):
        resp = getattr(client, method)(path, headers=_api_headers())
        assert resp.status_code == 403, path


# ---------------------------------------------------------------------------
# 登记 → 页面 → 决定 → 消费
# ---------------------------------------------------------------------------

def test_register_then_visible_on_page(logged_client, agent_token):
    client, _ = logged_client
    resp = client.post(
        "/api/ops/approvals",
        json={"pr": 137, "title": "ci: 并行", "risk_kind": "红线 2", "reason": "改 workflow"},
        headers=_api_headers(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["item"]["status"] == oap.STATUS_PENDING

    page = client.get("/ops/approvals").get_data(as_text=True)
    assert "PR #137" in page
    assert "ci: 并行" in page


def test_register_rejects_bad_payload(logged_client, agent_token):
    client, _ = logged_client
    resp = client.post("/api/ops/approvals", json={"pr": "abc"}, headers=_api_headers())
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_reregister_resets_decision(logged_client, agent_token):
    """返工后重新登记：必须回到 pending，且清掉上一轮的批准记录。"""
    client, _ = logged_client
    payload = {"pr": 137, "title": "v1", "reason": "第一版"}
    client.post("/api/ops/approvals", json=payload, headers=_api_headers())
    client.post("/ops/approvals/137/decide", data={"decision": "approved"})

    client.post("/api/ops/approvals", json={"pr": 137, "title": "v2", "reason": "返工"},
                headers=_api_headers())
    items = client.get("/api/ops/approvals", headers=_api_headers()).get_json()["items"]
    assert items == []          # 已重置回 pending，不再出现在「待执行」里

    page = client.get("/ops/approvals").get_data(as_text=True)
    assert "v2" in page
    assert "已由" not in page


def test_decide_approved_then_agent_sees_it(logged_client, agent_token):
    client, _ = logged_client
    client.post("/api/ops/approvals", json={"pr": 137}, headers=_api_headers())
    client.post("/ops/approvals/137/decide", data={"decision": "approved", "note": "同意"})

    items = client.get("/api/ops/approvals", headers=_api_headers()).get_json()["items"]
    assert len(items) == 1
    assert items[0]["pr_number"] == 137
    assert items[0]["status"] == oap.STATUS_APPROVED
    assert items[0]["decided_by"] == "admin"
    assert items[0]["decision_note"] == "同意"


def test_decide_repeated_same_status_is_idempotent(logged_client, agent_token):
    """重复点同一个按钮不能刷新 decided_at —— 否则审计时间失真。"""
    client, _ = logged_client
    client.post("/api/ops/approvals", json={"pr": 137}, headers=_api_headers())
    client.post("/ops/approvals/137/decide", data={"decision": "approved"})

    import db as db_module
    first = _master(db_module).execute(
        "SELECT decided_at FROM owner_approvals WHERE pr_number=137").fetchone()[0]

    client.post("/ops/approvals/137/decide", data={"decision": "approved"})
    second = _master(db_module).execute(
        "SELECT decided_at FROM owner_approvals WHERE pr_number=137").fetchone()[0]
    assert first == second


def test_decide_flip_is_rejected(logged_client, agent_token):
    """已批准后不能再改判：Agent 可能已经拿着旧决定去 merge 了。"""
    client, _ = logged_client
    client.post("/api/ops/approvals", json={"pr": 137}, headers=_api_headers())
    client.post("/ops/approvals/137/decide", data={"decision": "approved"})
    client.post("/ops/approvals/137/decide", data={"decision": "rejected"})

    import db as db_module
    row = _master(db_module).execute(
        "SELECT status FROM owner_approvals WHERE pr_number=137").fetchone()
    assert row[0] == oap.STATUS_APPROVED


def test_decide_unknown_pr_is_soft_failure(logged_client, agent_token):
    client, _ = logged_client
    resp = client.post("/ops/approvals/999/decide", data={"decision": "approved"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert "不在待拍板队列" in resp.get_data(as_text=True)


def test_decide_invalid_decision_ignored(logged_client, agent_token):
    client, _ = logged_client
    client.post("/api/ops/approvals", json={"pr": 137}, headers=_api_headers())
    client.post("/ops/approvals/137/decide", data={"decision": "merge"})

    import db as db_module
    row = _master(db_module).execute(
        "SELECT status FROM owner_approvals WHERE pr_number=137").fetchone()
    assert row[0] == oap.STATUS_PENDING


def test_consume_marks_executed_and_hides_from_agent(logged_client, agent_token):
    client, _ = logged_client
    client.post("/api/ops/approvals", json={"pr": 137}, headers=_api_headers())
    client.post("/ops/approvals/137/decide", data={"decision": "approved"})

    resp = client.post("/api/ops/approvals/137/consume", json={"result": "merged"},
                       headers=_api_headers())
    assert resp.status_code == 200

    items = client.get("/api/ops/approvals", headers=_api_headers()).get_json()["items"]
    assert items == []

    # 消费后 status 仍是 approved：决定本身不能被「已执行」覆盖掉
    import db as db_module
    row = _master(db_module).execute(
        "SELECT status, consumed_at, consume_result FROM owner_approvals "
        "WHERE pr_number=137").fetchone()
    assert row[0] == oap.STATUS_APPROVED
    assert row[1]
    assert row[2] == "merged"


def test_consume_twice_is_404(logged_client, agent_token):
    client, _ = logged_client
    client.post("/api/ops/approvals", json={"pr": 137}, headers=_api_headers())
    client.post("/ops/approvals/137/decide", data={"decision": "approved"})
    client.post("/api/ops/approvals/137/consume", json={"result": "merged"},
                headers=_api_headers())
    resp = client.post("/api/ops/approvals/137/consume", json={"result": "merged"},
                       headers=_api_headers())
    assert resp.status_code == 404


def test_consume_unknown_pr_is_404(logged_client, agent_token):
    client, _ = logged_client
    resp = client.post("/api/ops/approvals/999/consume", json={"result": "merged"},
                       headers=_api_headers())
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 页面鉴权
# ---------------------------------------------------------------------------

def test_page_requires_login(logged_client):
    client, _ = logged_client
    with client.session_transaction() as sess:
        sess.clear()
    resp = client.get("/ops/approvals")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_page_forbidden_for_viewer(viewer_client):
    client, _ = viewer_client
    assert client.get("/ops/approvals").status_code == 403


def test_page_warns_when_agent_token_missing(logged_client, monkeypatch):
    """令牌没配时必须写在页面上 —— 否则 owner 会以为「没有待办」。"""
    monkeypatch.setattr(config_module, "OPS_AGENT_TOKEN", "")
    client, _ = logged_client
    body = client.get("/ops/approvals").get_data(as_text=True)
    assert "OPS_AGENT_TOKEN" in body


def test_empty_queue_says_so(logged_client, agent_token):
    client, _ = logged_client
    body = client.get("/ops/approvals").get_data(as_text=True)
    assert "没有待拍板项" in body


# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------

def test_routes_registered_in_matrix():
    m = permissions_manifest.ROUTE_MATRIX
    assert m["owner_approvals.list_approvals"] == permissions_manifest.PLATFORM
    assert m["owner_approvals.decide"] == permissions_manifest.PLATFORM
    for ep in ("api_register", "api_list", "api_consume"):
        assert m[f"owner_approvals.{ep}"] == permissions_manifest.AGENT


def test_csrf_exempt_only_for_agent_endpoints(logged_client, agent_token):
    """Agent 接口豁免 CSRF，页面 POST 不豁免 —— 混了就是给 CSRF 开后门。

    直接验行为而不是验标记：Flask-WTF 1.3 的 exempt 只把 ``模块.函数``
    记进集合、不再给函数打属性，断属性会得到一个恒真的假绿。
    """
    client, _ = logged_client
    app = client.application
    app.config["WTF_CSRF_FORCE_ENABLED"] = True   # TESTING 下默认关闭
    try:
        client.post("/api/ops/approvals", json={"pr": 1}, headers=_api_headers())
        # Agent 接口不带 csrf_token 也必须穿过 CSRF 层（落到鉴权/业务）
        agent_resp = client.post(
            "/api/ops/approvals", json={"pr": 2}, headers=_api_headers()
        )
        assert agent_resp.status_code == 200, agent_resp.get_data(as_text=True)
        # 页面按钮不带 csrf_token 必须被拦
        page_resp = client.post("/ops/approvals/2/decide", data={"decision": "approved"})
        assert page_resp.status_code == 400
    finally:
        app.config["WTF_CSRF_FORCE_ENABLED"] = False


def _master(db_module) -> sqlite3.Connection:
    conn = sqlite3.connect(db_module.MASTER_DB)
    return conn
