"""Issue 精确化层测试：page_path → 代码位置，以及正文的「可执行性四要素」。

为什么要单独测这一层：派发的 Issue 是交给自动巡检 agent 执行的任务单，
它的精确度直接决定「自动跑」还是「转人工」。这里覆盖三件事：

1. 反射探测本身（含失败路径 —— 探测失败绝不能让派发跟着失败）
2. 正文结构（代码线索 / 验收标准 / 范围边界 / 信息缺口）
3. 页面预览（管理员能在派发前确认自动定位对不对）

``with_commits=False`` 是刻意的：测试不依赖宿主仓库的 git 历史，
否则 CI 上的浅克隆和本地工作区会得出不同结果。
"""
from __future__ import annotations

import pytest

from blueprints import feedback_dispatch, feedback_introspect

# ---------------------------------------------------------------------------
# page_path 清洗
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "https://evil.example/x",
    "//evil.example/x",
    "javascript:alert(1)",
    "inventory",
    "",
    None,
])
def test_clean_path_rejects_non_site_values(raw):
    assert feedback_introspect.clean_path(raw) == ""


@pytest.mark.parametrize("raw,expected", [
    ("/inventory", "/inventory"),
    ("/inventory?page=2", "/inventory"),
    ("/inventory#top", "/inventory"),
    ("/inventory?a=1#b", "/inventory"),
    ("  /reports  ", "/reports"),
])
def test_clean_path_keeps_only_the_path(raw, expected):
    assert feedback_introspect.clean_path(raw) == expected


# ---------------------------------------------------------------------------
# 路由反查
# ---------------------------------------------------------------------------


def test_match_endpoint_resolves_a_real_route(logged_client):
    client, _ = logged_client
    endpoint, rule = feedback_introspect.match_endpoint(client.application, "/inventory")
    assert endpoint, "/inventory 是既有路由，必须能反查到 endpoint"
    assert rule == "/inventory"


def test_match_endpoint_returns_empty_for_unknown_route(logged_client):
    client, _ = logged_client
    assert feedback_introspect.match_endpoint(
        client.application, "/definitely-not-a-route") == ("", "")


def test_match_endpoint_ignores_query_string(logged_client):
    client, _ = logged_client
    endpoint, rule = feedback_introspect.match_endpoint(
        client.application, "/inventory?focus=3")
    assert endpoint
    assert rule == "/inventory"


def test_match_endpoint_tolerates_none_app():
    assert feedback_introspect.match_endpoint(None, "/inventory") == ("", "")


# ---------------------------------------------------------------------------
# 源码定位
# ---------------------------------------------------------------------------


def test_locate_view_finds_source_file_and_line(logged_client):
    client, _ = logged_client
    endpoint, _ = feedback_introspect.match_endpoint(client.application, "/inventory")
    info = feedback_introspect.locate_view(client.application, endpoint)
    assert info.get("source", "").endswith("items.py")
    assert int(info.get("line") or 0) > 0


def test_locate_view_of_unknown_endpoint_is_empty(logged_client):
    client, _ = logged_client
    assert feedback_introspect.locate_view(client.application, "nope.nope") == {}


def test_collect_full_shape_without_commits(logged_client):
    client, _ = logged_client
    clues = feedback_introspect.collect(
        client.application, "/inventory", with_commits=False)
    assert clues["matched"] is True
    assert clues["endpoint"]
    assert clues["source"].endswith("items.py")
    assert clues["commits"] == []


def test_collect_unmatched_page_is_soft(logged_client):
    client, _ = logged_client
    clues = feedback_introspect.collect(
        client.application, "/no-such-page", with_commits=False)
    assert clues["matched"] is False
    assert clues["page_path"] == "/no-such-page"


def test_collect_external_url_never_reaches_router(logged_client):
    client, _ = logged_client
    clues = feedback_introspect.collect(
        client.application, "https://evil.example/x", with_commits=False)
    assert clues["matched"] is False
    assert clues["page_path"] == ""


def test_recent_commits_on_bad_repo_is_empty():
    """git 不存在 / 不是仓库 / 超时 —— 一律返回空表，不抛异常。"""
    assert feedback_introspect.recent_commits("/nonexistent/repo", ["a.py"]) == []
    assert feedback_introspect.recent_commits("", ["a.py"]) == []
    assert feedback_introspect.recent_commits("/tmp", []) == []


# ---------------------------------------------------------------------------
# Issue 正文：可执行性四要素
# ---------------------------------------------------------------------------

_TICKET = {
    "id": 7,
    "ticket_no": "FB-20261017-0007",
    "title": "库存页导出没反应",
    "body": "点了导出按钮，没有任何反应，连下载提示都没有。",
    "page_path": "/inventory",
    "category": "bug",
    "category_label": "功能异常",
    "created_at": "2026-10-17 10:00:00",
    "submitter": "staff",
}

_COMMENTS = [{
    "username": "admin",
    "is_admin": 1,
    "created_at": "2026-10-17 11:00:00",
    "body": "确认导出按钮漏绑事件，需要修复并保证下载能触发。",
}]

_CLUES = {
    "matched": True,
    "page_path": "/inventory",
    "rule": "/inventory",
    "endpoint": "items.inventory",
    "func": "inventory",
    "source": "blueprints/items.py",
    "line": 263,
    "template": "inventory.html",
    "commits": [{"sha": "3d9f2a1", "date": "2026-10-15", "subject": "导出按钮改样式"}],
}


def test_issue_body_states_it_is_already_decided():
    """第一句必须消掉「这是不是产品决策」的疑问 —— 否则巡检会转人工。"""
    body = feedback_dispatch.build_issue_body(_TICKET, _COMMENTS, code_clues=_CLUES)
    assert "已确认需要修复" in body
    assert "不需要再回问" in body


def test_issue_body_carries_code_clues():
    body = feedback_dispatch.build_issue_body(_TICKET, _COMMENTS, code_clues=_CLUES)
    assert "## 代码线索（自动生成）" in body
    assert "blueprints/items.py:263" in body
    assert "templates/inventory.html" in body
    assert "3d9f2a1" in body


def test_issue_body_has_acceptance_and_scope_sections():
    body = feedback_dispatch.build_issue_body(_TICKET, _COMMENTS, code_clues=_CLUES)
    assert "## 验收标准（逐项核对）" in body
    assert "known_failures.txt" in body          # 零新增失败是可判定的
    assert "## 范围边界" in body
    assert "不要自行扩大范围" in body


def test_issue_body_without_clues_admits_it():
    """定位失败要写实话 + 下一步，而不是留白或假装有线索。"""
    body = feedback_dispatch.build_issue_body(
        _TICKET, _COMMENTS, code_clues={"matched": False, "page_path": "/inventory"})
    assert "未能自动定位" in body
    assert "`blueprints/` 全文检索" in body


def test_issue_body_keeps_closed_loop_section():
    body = feedback_dispatch.build_issue_body(_TICKET, _COMMENTS, code_clues=_CLUES)
    assert "## 闭环要求" in body


def test_issue_body_quotes_each_line_of_user_text():
    ticket = dict(_TICKET, body="第一行\n第二行")
    body = feedback_dispatch.build_issue_body(ticket, _COMMENTS, code_clues=_CLUES)
    assert "> 第一行" in body
    assert "> 第二行" in body


# ---------------------------------------------------------------------------
# 信息缺口
# ---------------------------------------------------------------------------


def test_detect_gaps_flags_short_body_and_missing_clues():
    ticket = {"body": "坏了", "page_path": ""}
    gaps = feedback_dispatch.detect_gaps(ticket, [], {"matched": False})
    joined = "\n".join(gaps)
    assert "没有可自动定位的页面路径" in joined
    assert "少于" in joined
    assert "没有管理员处理意见" in joined


def test_detect_gaps_ignores_verbose_input():
    gaps = feedback_dispatch.detect_gaps(
        _TICKET, feedback_dispatch.admin_notes(_COMMENTS), _CLUES)
    assert gaps, "复现步骤和期望行为本来就没提供，必须一直提示"
    assert not any("没有可自动定位" in g for g in gaps)
    assert not any("没有管理员处理意见" in g for g in gaps)


def test_admin_notes_filters_non_admin_and_blank():
    comments = [
        {"is_admin": 0, "body": "用户自己补一句"},
        {"is_admin": 1, "body": "   "},
        {"is_admin": 1, "body": "管理员的意见"},
    ]
    notes = feedback_dispatch.admin_notes(comments)
    assert len(notes) == 1
    assert notes[0]["body"] == "管理员的意见"


# ---------------------------------------------------------------------------
# 页面预览（管理员派发前能看到自动定位结果）
# ---------------------------------------------------------------------------


def _submit(client, page_path="/inventory"):
    client.post("/feedback", data={
        "title": "库存页导出没反应",
        "body": "点了导出按钮，没有任何反应，连下载提示都没有。",
        "category": "bug",
        "page_path": page_path,
    })


def _ticket_id():
    import sqlite3

    import config

    conn = sqlite3.connect(config.MASTER_DB)
    try:
        return conn.execute("SELECT id FROM feedback_tickets").fetchone()[0]
    finally:
        conn.close()


def test_admin_detail_shows_clue_preview(logged_client):
    client, _ = logged_client
    _submit(client)
    html = client.get(f"/admin/feedback/{_ticket_id()}").get_data(as_text=True)
    assert "派发后 Issue 会自动带上这段代码线索" in html
    assert "items.py" in html
    assert 'data-fill-template' in html


def test_admin_detail_hints_when_clue_unavailable(logged_client):
    client, _ = logged_client
    _submit(client, page_path="/no-such-page")
    html = client.get(f"/admin/feedback/{_ticket_id()}").get_data(as_text=True)
    assert "未能自动定位到路由" in html
