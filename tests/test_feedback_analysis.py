"""反馈二审制 + LLM 预分析测试。

四层覆盖：
1. 提示词 / 解析（``feedback_analysis_pure``）—— 模型输出是不可信输入，
   围栏、前后缀、字段缺失、类型错乱都必须能吃下或明确拒绝。
2. 代码片段（``feedback_introspect.collect_snippets``）—— 必须带行号，
   且不许顺着 ``page_path`` 穿越出仓库。
3. 状态机与落库（``feedback_pure``）—— open→analyzing→analyzed、
   失败退回 open、二审前可忽略、角标口径。
4. 编排（``feedback_llm._run`` 同步调用）—— 成功落 analysis/草稿，
   失败落 analysis_error 且状态回 open，全程不出网。

外通道一律拦在 ``_chat`` / ``create_issue`` 之前：测试绝不出网。
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error

import pytest

from blueprints import feedback_analysis_pure as analysis_pure
from blueprints import feedback_introspect, feedback_llm, feedback_pure

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def master_conn(tmp_path, monkeypatch):
    """独立的临时 master.db（纯逻辑用例用；路由用例走 ``logged_client``）。

    ⚠️ 不要和 ``logged_client`` 同时使用：两者都会 monkeypatch
    ``db.MASTER_DB``，后生效的那个会让前者的连接指向另一份文件。
    """
    import db as db_module
    from db import init_master_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    import config as config_module

    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    init_master_db()

    conn = sqlite3.connect(master_path)
    conn.row_factory = sqlite3.Row
    ts = "2026-10-25 10:00:00"
    conn.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (ts,))
    conn.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (2, 'store_user', 'x', 0, ?)", (ts,))
    conn.commit()
    yield conn
    conn.close()


def _conn() -> sqlite3.Connection:
    """路由用例的直连（``logged_client`` 建的那份临时 master.db）。"""
    import config

    conn = sqlite3.connect(config.MASTER_DB)
    conn.row_factory = sqlite3.Row
    return conn

# ---------------------------------------------------------------------------
# 1. 解析
# ---------------------------------------------------------------------------

GOOD = {
    "verdict": "confirmed",
    "severity": "high",
    "summary": "导出接口没做空结果判断",
    "root_cause": "blueprints/reports.py 的导出分支未处理空列表",
    "affected": ["blueprints/reports.py:120 — 空列表时 IndexError"],
    "reproduce": "报表页选空区间点导出",
    "acceptance": ["空区间点导出能下载空表头 xlsx", "有数据时导出内容不变"],
    "gaps": ["未提供浏览器版本"],
    "notes": "",
}


def test_parse_analysis_plain_json():
    out = analysis_pure.parse_analysis(json.dumps(GOOD, ensure_ascii=False))
    assert out["verdict"] == "confirmed"
    assert out["verdict_label"] == "确认是缺陷，需要修"
    assert out["severity"] == "high"
    assert out["source"] == analysis_pure.SOURCE_LLM
    assert len(out["acceptance"]) == 2


def test_parse_analysis_handles_code_fence():
    raw = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    assert analysis_pure.parse_analysis(raw)["summary"] == GOOD["summary"]


def test_parse_analysis_handles_leading_prose():
    raw = "分析如下：\n" + json.dumps(GOOD, ensure_ascii=False) + "\n以上。"
    assert analysis_pure.parse_analysis(raw)["verdict"] == "confirmed"


def test_parse_analysis_rejects_empty_and_unusable():
    with pytest.raises(ValueError):
        analysis_pure.parse_analysis("")
    with pytest.raises(ValueError):
        analysis_pure.parse_analysis("模型挂了，没有返回 JSON")
    # 只有结构没有内容 = 等于没分析，必须拒绝而不是产出一张空卡
    with pytest.raises(ValueError):
        analysis_pure.parse_analysis(json.dumps({"verdict": "confirmed"}))


def test_parse_analysis_normalizes_loose_fields():
    """模型偶尔回中文枚举 / 字符串列表 / 缺 severity —— 归一化而不是整条丢弃。"""
    raw = json.dumps({
        "verdict": "确认需要修复",
        "summary": "空表导出报错",
        "acceptance": "空区间能导出\n有数据时正常",
        "affected": {"reports.py": "第 120 行"},
    }, ensure_ascii=False)
    out = analysis_pure.parse_analysis(raw)
    assert out["verdict"] == "confirmed"
    assert out["severity"] == "medium"          # 缺字段回落默认
    assert out["acceptance"] == ["空区间能导出", "有数据时正常"]
    assert out["affected"] == ["reports.py：第 120 行"]


def test_parse_analysis_clips_oversize_lists():
    raw = json.dumps({
        "summary": "x",
        "acceptance": [f"第{i}项" for i in range(20)],
    }, ensure_ascii=False)
    out = analysis_pure.parse_analysis(raw)
    assert len(out["acceptance"]) == analysis_pure.MAX_LIST_ITEMS


# ---------------------------------------------------------------------------
# 2. 提示词
# ---------------------------------------------------------------------------

def test_build_messages_carries_evidence():
    ticket = {
        "ticket_no": "FB-20261025-0001",
        "title": "导出没反应",
        "body": "点了导出按钮什么都没发生",
        "category": "bug",
        "page_path": "/report/health",
    }
    clues = {
        "matched": True,
        "rule": "/report/health",
        "endpoint": "reports.health",
        "source": "blueprints/reports.py",
        "line": 120,
        "template": "report_health.html",
    }
    msgs = analysis_pure.build_messages(
        ticket, [{"is_admin": True, "body": "确认是导出按钮"}], clues,
        "--- blueprints/reports.py:117 ---\n117\tdef health():")
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    assert "FB-20261025-0001" in user
    assert "点了导出按钮什么都没发生" in user
    assert "确认是导出按钮" in user
    assert "blueprints/reports.py:120" in user
    assert "templates/report_health.html" in user
    assert "117\tdef health():" in user


def test_build_messages_flags_transient_page():
    clues = {"matched": False, "page_path": "/select-warehouse",
             "reason": "transient_page"}
    user = analysis_pure.build_messages({"title": "t", "body": "b"}, [], clues)[1]["content"]
    assert "中转页" in user
    assert "不能作为出问题位置的证据" in user


# ---------------------------------------------------------------------------
# 3. 兜底与渲染
# ---------------------------------------------------------------------------

def test_fallback_analysis_is_labelled_not_fake():
    out = analysis_pure.fallback_analysis(
        {"title": "数据不对", "category": "data", "ticket_no": "FB-1"},
        [], {"matched": False, "reason": "no_path"}, reason="HTTP 500")
    assert out["source"] == analysis_pure.SOURCE_TEMPLATE
    assert out["source_label"].startswith("模板兜底")
    assert out["severity"] == "high"            # 数据类默认最高
    assert out["acceptance"]                     # 仍给可核对的验收项
    assert any("HTTP 500" in g for g in out["gaps"])


def test_render_analysis_keeps_checkbox_out_of_list_items():
    out = analysis_pure.render_analysis({
        "verdict_label": "疑似问题，需先复现",
        "severity_label": "中",
        "acceptance": ["能复现"],
        "gaps": ["缺复现步骤"],
        "source": analysis_pure.SOURCE_TEMPLATE,
        "source_label": "模板兜底（未调用模型）",
    })
    assert "疑似问题" in out
    assert "模板兜底" in out
    assert "- [ ]" not in out


def test_analysis_view_tolerates_corrupt_row():
    assert analysis_pure.analysis_view({"analysis_text": "{not json"}) == {}
    assert analysis_pure.analysis_view({"analysis_text": ""}) == {}
    view = analysis_pure.analysis_view({
        "analysis_text": json.dumps({"verdict": "confirmed", "summary": "s"})})
    assert view["verdict_label"] == "确认是缺陷，需要修"
    assert view["source"] == analysis_pure.SOURCE_LLM


def test_build_draft_marks_page_override_and_analysis():
    ticket = {
        "ticket_no": "FB-20261025-0002",
        "title": "导出没反应",
        "body": "点了导出按钮什么都没发生，反复试了好几次都一样",
        "page_path": "/select-warehouse",
        "analysis_page_path": "/report/health",
    }
    clues = {"matched": True, "rule": "/report/health",
             "endpoint": "reports.health", "source": "blueprints/reports.py",
             "line": 1, "template": "report_health.html"}
    title, body = analysis_pure.build_draft(
        ticket, [{"is_admin": True, "body": "确认是导出按钮的问题，修一下"}],
        clues, {"verdict_label": "确认是缺陷，需要修", "severity_label": "高",
                "summary": "导出分支没判空", "source": analysis_pure.SOURCE_LLM,
                "source_label": "模型分析"})
    assert title.startswith("[反馈] FB-20261025-0002")
    assert "实际页面（管理员指定）" in body
    assert "/report/health" in body
    assert "## 分析（机器预判，非最终结论）" in body
    assert "导出分支没判空" in body


def test_build_draft_without_analysis_omits_section():
    _title, body = analysis_pure.build_draft(
        {"ticket_no": "FB-3", "title": "t", "body": "b" * 30}, [], {}, {})
    assert "## 分析（机器预判" not in body


# ---------------------------------------------------------------------------
# 4. 代码片段
# ---------------------------------------------------------------------------

def test_collect_snippets_reads_labelled_lines():
    clues = {"matched": True, "source": "blueprints/feedback_pure.py", "line": 70,
             "template": ""}
    text = feedback_introspect.collect_snippets(clues)
    assert "--- blueprints/feedback_pure.py:" in text
    assert "def " in text


def test_collect_snippets_ignores_unmatched_and_escapes():
    assert feedback_introspect.collect_snippets({"matched": False}) == ""
    # 路径穿越：bound 在仓库根之外，必须读不到
    escaped = feedback_introspect._read_snippet(
        feedback_introspect.Path(feedback_introspect.config.BASE_DIR),
        "../../etc/passwd", 1, 10, 4000)
    assert escaped == ""
    # 非白名单后缀同样拒绝
    assert feedback_introspect._read_snippet(
        feedback_introspect.Path(feedback_introspect.config.BASE_DIR),
        "requirements.txt", 1, 10, 4000) == ""


# ---------------------------------------------------------------------------
# 5. 状态机
# ---------------------------------------------------------------------------

@pytest.fixture
def ticket(master_conn):
    return feedback_pure.create_ticket(
        master_conn, user_id=2, title="导出没反应", body="点了没反应" * 5)


def test_analyzing_state_machine(ticket, master_conn):
    tid = ticket["id"]
    assert feedback_pure.mark_analyzing(master_conn, tid, "/report/health") is True
    row = feedback_pure.get_ticket(master_conn, tid)
    assert row["status"] == "analyzing"
    assert row["analysis_page_path"] == "/report/health"

    # 已在分析中：连点第二次不能起第二个线程
    assert feedback_pure.mark_analyzing(master_conn, tid) is False

    assert feedback_pure.save_analysis(
        master_conn, tid, "{}", "MiniMax-M3.1-Flash", "标题", "正文" * 30) is True
    row = feedback_pure.get_ticket(master_conn, tid)
    assert row["status"] == "analyzed"
    assert row["analysis_model"] == "MiniMax-M3.1-Flash"
    assert row["draft_title"] == "标题"
    assert row["analysis_error"] is None


def test_fail_analysis_returns_to_open(ticket, master_conn):
    tid = ticket["id"]
    feedback_pure.mark_analyzing(master_conn, tid)
    assert feedback_pure.fail_analysis(master_conn, tid, "HTTP 500") is True
    row = feedback_pure.get_ticket(master_conn, tid)
    assert row["status"] == "open"
    assert row["analysis_error"] == "HTTP 500"
    # 失败后可重试；重试会清掉旧错误
    assert feedback_pure.mark_analyzing(master_conn, tid) is True
    assert feedback_pure.get_ticket(master_conn, tid)["analysis_error"] is None


def test_fail_analysis_only_from_analyzing(ticket, master_conn):
    assert feedback_pure.fail_analysis(master_conn, ticket["id"], "x") is False


def test_save_analysis_requires_analyzing(ticket, master_conn):
    """没走一审就落分析结果 = 绕过状态机，必须拒绝。"""
    assert feedback_pure.save_analysis(
        master_conn, ticket["id"], "{}", "m", "t", "b") is False


def test_pending_counts_and_ordering(ticket, master_conn):
    other = feedback_pure.create_ticket(
        master_conn, user_id=2, title="第二条", body="b" * 10)
    feedback_pure.mark_analyzing(master_conn, other["id"])
    feedback_pure.save_analysis(master_conn, other["id"], "{}", "m", "t", "b" * 40)

    assert feedback_pure.count_open(master_conn) == 2   # open + analyzed
    rows = feedback_pure.list_for_admin(master_conn)
    assert [r["id"] for r in rows] == [other["id"], ticket["id"]]


def test_ignore_works_from_analyzed(ticket, master_conn):
    feedback_pure.mark_analyzing(master_conn, ticket["id"])
    feedback_pure.save_analysis(master_conn, ticket["id"], "{}", "m", "t", "b")
    assert feedback_pure.mark_ignored(master_conn, ticket["id"], 1) is True
    assert feedback_pure.get_ticket(master_conn, ticket["id"])["status"] == "ignored"


def test_normalize_draft_rejects_blank():
    with pytest.raises(ValueError):
        feedback_pure.normalize_draft("", "正文" * 30)
    with pytest.raises(ValueError):
        feedback_pure.normalize_draft("标题", "太短")
    assert feedback_pure.normalize_draft(" 标题 ", " 正文" * 20)[0] == "标题"


# ---------------------------------------------------------------------------
# 6. 模型客户端（不出网）
# ---------------------------------------------------------------------------

def test_chat_without_key_raises(monkeypatch):
    monkeypatch.setattr(feedback_llm.config, "FEEDBACK_LLM_API_KEY", "")
    with pytest.raises(feedback_llm.LLMError):
        feedback_llm._chat([{"role": "user", "content": "x"}])


def test_chat_maps_http_error_to_llm_error(monkeypatch):
    monkeypatch.setattr(feedback_llm.config, "FEEDBACK_LLM_API_KEY", "k")

    def boom(*_a, **_k):
        raise urllib.error.HTTPError("u", 429, "rate", {}, None)

    monkeypatch.setattr(feedback_llm.urllib.request, "urlopen", boom)
    with pytest.raises(feedback_llm.LLMError) as exc:
        feedback_llm._chat([{"role": "user", "content": "x"}])
    assert "429" in str(exc.value)


class _FakeResponse:
    """最小 urlopen 替身：只要能当上下文管理器 + read 就行。"""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _stub_reply(monkeypatch, payload):
    """让 ``urlopen`` 直接吐指定 JSON（全程不出网）。"""
    monkeypatch.setattr(feedback_llm.config, "FEEDBACK_LLM_API_KEY", "k")
    monkeypatch.setattr(feedback_llm.config, "FEEDBACK_LLM_BASE_URL", "https://llm.test")
    monkeypatch.setattr(
        feedback_llm.urllib.request, "urlopen", lambda *_a, **_k: _FakeResponse(payload))


def test_chat_returns_content(monkeypatch):
    _stub_reply(monkeypatch, {
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": '{"summary": "ok"}'}}],
    })
    assert feedback_llm._chat([{"role": "user", "content": "x"}]) == '{"summary": "ok"}'


def test_chat_reports_truncation_not_structure_error(monkeypatch):
    """推理模型吃光 max_tokens 时 message 里**没有 content 键**。

    生产 ticket 9 就是这样降级的：原先按 KeyError 一律报「响应结构不可解析」，
    把「预算不够」伪装成「上游返回物怪异」，排查被引向错误方向。
    """
    _stub_reply(monkeypatch, {
        "choices": [{"finish_reason": "length",
                     "message": {"role": "assistant", "reasoning_content": "推推推"}}],
        "usage": {"completion_tokens_details": {"reasoning_tokens": 1599}},
    })
    with pytest.raises(feedback_llm.LLMError) as exc:
        feedback_llm._chat([{"role": "user", "content": "x"}])
    msg = str(exc.value)
    assert "截断" in msg and f"max_tokens={feedback_llm.MAX_OUTPUT_TOKENS}" in msg
    assert "1599" in msg, "必须报出实际 reasoning 用量，否则没法判断要不要再抬预算"
    assert "响应结构不可解析" not in msg


def test_chat_reports_empty_content_with_finish_reason(monkeypatch):
    """content 存在但为空、finish_reason 正常 —— 是另一类问题，不能混报截断。"""
    _stub_reply(monkeypatch, {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "  "}}],
    })
    with pytest.raises(feedback_llm.LLMError) as exc:
        feedback_llm._chat([{"role": "user", "content": "x"}])
    assert "空内容" in str(exc.value) and "stop" in str(exc.value)


def test_chat_still_rejects_broken_structure(monkeypatch):
    """真·结构异常（无 choices / 空列表）仍归到不可解析，语义不能糊掉。"""
    for payload in ({"error": "boom"}, {"choices": []},
                    {"choices": [{"message": "not-a-dict"}]}):
        _stub_reply(monkeypatch, payload)
        with pytest.raises(feedback_llm.LLMError) as exc:
            feedback_llm._chat([{"role": "user", "content": "x"}])
        assert "响应结构不可解析" in str(exc.value), payload


def test_output_budget_covers_reasoning_tokens():
    """守着预算别再被改回「正文够用」的 1600。

    实测同一张单 reasoning 要 3325 tokens 才收敛，4096 都不够；长单（prompt 2074）
    更要 6559~8190 —— 8192 恰好压在上沿，2026-10-26 生产冒烟三次里两次被截断。
    所以下限守 16384（能覆盖实测最坏 8190 并留一倍余量）。这条用例的存在意义是：
    下次有人觉得「回复才 1800 字符，1600/8192 够了」时会被挡住。
    """
    assert feedback_llm.MAX_OUTPUT_TOKENS >= 16384


def test_chat_sends_current_output_budget(monkeypatch):
    """请求体里的 ``max_tokens`` 必须取自常量，不能硬编码。

    2026-10-26 ticket 11：8192 下 reasoning 8190 吃满、无 content → 降级模板；
    16384 才通过。若只改常量、payload 还写死旧值，线上等于没改 —— 这条用例把
    常量换成一个哨兵值，只有「真的读模块常量」才能通过。
    """
    seen = {}

    def fake_urlopen(request, *_a, **_k):
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({
            "choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
        })

    monkeypatch.setattr(feedback_llm.config, "FEEDBACK_LLM_API_KEY", "k")
    monkeypatch.setattr(feedback_llm.config, "FEEDBACK_LLM_BASE_URL", "https://llm.test")
    monkeypatch.setattr(feedback_llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(feedback_llm, "MAX_OUTPUT_TOKENS", 23456)

    feedback_llm._chat([{"role": "user", "content": "x"}])
    assert seen["payload"]["max_tokens"] == 23456


def test_timeout_covers_reasoning_latency(monkeypatch):
    """超时与输出预算是**一对**，只抬一边等于没修。

    2026-10-25 生产 ticket 9：max_tokens 已抬到 8192，但默认 90s 超时先到，
    单次真实耗时 108s → TimeoutError → 照样落 ``analysis_model=template``，
    和「预算不够」在库里长得一模一样。
    下限守 280：16384 预算吃满约 205s（按实测约 80 tok/s 反推），280 只留约
    1.37 倍余量 —— 再往下调就等于把「预算翻倍」换来的余量又还回去了。
    """
    monkeypatch.delattr(feedback_llm.config, "FEEDBACK_LLM_TIMEOUT", raising=False)
    assert feedback_llm._timeout() >= 280


def test_timeout_falls_back_on_garbage_value(monkeypatch):
    """env 写错不该把分析打死，回到同一个下限。"""
    monkeypatch.setattr(feedback_llm.config, "FEEDBACK_LLM_TIMEOUT", "abc")
    assert feedback_llm._timeout() >= 280


def test_chat_reports_timeout_with_limit_and_elapsed(monkeypatch):
    """超时文案要带上限 —— 光看 ``TimeoutError`` 分不清是网络不稳还是等得不够久。"""
    monkeypatch.setattr(feedback_llm.config, "FEEDBACK_LLM_API_KEY", "k")
    monkeypatch.setattr(feedback_llm.config, "FEEDBACK_LLM_BASE_URL", "https://llm.test")
    monkeypatch.setattr(feedback_llm.config, "FEEDBACK_LLM_TIMEOUT", 300.0)

    def boom(*_a, **_k):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(feedback_llm.urllib.request, "urlopen", boom)
    with pytest.raises(feedback_llm.LLMError) as exc:
        feedback_llm._chat([{"role": "user", "content": "x"}])
    msg = str(exc.value)
    assert "调用超时" in msg and "300" in msg
    assert "调用失败" not in msg, "超时不能被归到泛化的「调用失败」里"


def test_analyze_parses_model_reply(monkeypatch):
    monkeypatch.setattr(
        feedback_llm, "_chat", lambda _msgs: json.dumps(GOOD, ensure_ascii=False))
    assert feedback_llm.analyze({}, [], {})["verdict"] == "confirmed"


def test_analyze_raises_on_unparseable_reply(monkeypatch):
    monkeypatch.setattr(feedback_llm, "_chat", lambda _msgs: "抱歉，我无法分析")
    with pytest.raises(feedback_llm.LLMError):
        feedback_llm.analyze({}, [], {})


# ---------------------------------------------------------------------------
# 7. 编排（同步跑线程主体）
# ---------------------------------------------------------------------------

def _mk_ticket(title="导出没反应"):
    conn = _conn()
    try:
        return feedback_pure.create_ticket(
            conn, user_id=1, title=title, body="点了没反应" * 5)
    finally:
        conn.close()


def _analyzable():
    """建一张单并置为 analyzing，返回 ticket_id。"""
    conn = _conn()
    try:
        t = feedback_pure.create_ticket(
            conn, user_id=1, title="导出没反应",
            body="点了导出按钮什么都没发生，反复试了好几次都一样。",
            category="bug", page_path="/report/health")
        feedback_pure.mark_analyzing(conn, t["id"])
        return t["id"]
    finally:
        conn.close()


def test_run_saves_analysis(logged_client, monkeypatch):
    client, _ = logged_client
    tid = _analyzable()
    monkeypatch.setattr(
        feedback_llm, "analyze", lambda *a, **k: analysis_pure.parse_analysis(
            json.dumps(GOOD, ensure_ascii=False)))
    monkeypatch.setattr(feedback_llm.config, "FEEDBACK_LLM_MODEL", "test-model")

    feedback_llm._run(client.application, tid)

    conn = _conn()
    row = feedback_pure.get_ticket(conn, tid)
    conn.close()
    assert row["status"] == "analyzed"
    assert row["analysis_model"] == "test-model"
    assert row["draft_body"].startswith("> 本单由管理员在站内判定后派发")
    assert analysis_pure.analysis_view(row)["verdict"] == "confirmed"


def test_run_degrades_to_template_on_failure(logged_client, monkeypatch):
    client, _ = logged_client
    tid = _analyzable()

    def boom(*_a, **_k):
        raise feedback_llm.LLMError("HTTP 500")

    monkeypatch.setattr(feedback_llm, "analyze", boom)

    feedback_llm._run(client.application, tid)

    conn = _conn()
    row = feedback_pure.get_ticket(conn, tid)
    conn.close()
    # 降级也要进二审，而不是退回一审：正文已有，管理员能直接派发。
    assert row["status"] == "analyzed"
    assert row["analysis_model"] == feedback_llm.MODEL_TEMPLATE_LABEL
    assert analysis_pure.analysis_view(row)["source"] == analysis_pure.SOURCE_TEMPLATE


def test_run_skips_when_status_changed(logged_client, monkeypatch):
    """分析期间管理员已忽略 → 线程结果作废，不许把 ignored 改回 analyzed。"""
    client, _ = logged_client
    tid = _analyzable()
    conn = _conn()
    feedback_pure.mark_ignored(conn, tid, 1)
    conn.close()
    monkeypatch.setattr(
        feedback_llm, "analyze",
        lambda *a, **k: pytest.fail("状态已变，不该再调模型"))

    feedback_llm._run(client.application, tid)
    conn = _conn()
    assert feedback_pure.get_ticket(conn, tid)["status"] == "ignored"
    conn.close()


def test_launch_refuses_in_dry_run(logged_client):
    client, _ = logged_client
    client.application.config["DRY_RUN"] = True
    try:
        assert feedback_llm.launch(client.application, 1) is False
    finally:
        client.application.config["DRY_RUN"] = False


# ---------------------------------------------------------------------------
# 8. 路由
# ---------------------------------------------------------------------------

def test_analyze_route_template_mode_goes_to_second_review(logged_client):
    client, _ = logged_client
    t = _mk_ticket()

    resp = client.post(f"/admin/feedback/{t['id']}/analyze",
                       data={"mode": "template", "comment": "确认是按钮问题"},
                       follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "二审：确认要发出去的 Issue" in html
    assert "确认派发" in html
    # 模板渲染留残留 = 页面上会出现 {{ ... }} 字面量，比报错更难发现。
    assert "{{" not in html and "{%" not in html

    conn = _conn()
    row = feedback_pure.get_ticket(conn, t["id"])
    conn.close()
    assert row["status"] == "analyzed"
    assert analysis_pure.analysis_view(row)["source"] == analysis_pure.SOURCE_TEMPLATE
    assert "确认是按钮问题" in row["draft_body"]


def test_analyze_route_launches_background_llm(logged_client, monkeypatch):
    client, _ = logged_client
    t = _mk_ticket()
    seen: list[int] = []
    monkeypatch.setattr(feedback_llm, "launch",
                        lambda _app, tid: seen.append(tid) or True)

    client.post(f"/admin/feedback/{t['id']}/analyze", data={},
                follow_redirects=True)

    assert seen == [t["id"]]
    conn = _conn()
    assert feedback_pure.get_ticket(conn, t["id"])["status"] == "analyzing"
    conn.close()


def test_analyze_route_is_idempotent(logged_client, monkeypatch):
    client, _ = logged_client
    t = _mk_ticket()
    calls: list[int] = []
    monkeypatch.setattr(feedback_llm, "launch",
                        lambda _app, tid: calls.append(tid) or True)

    client.post(f"/admin/feedback/{t['id']}/analyze", data={})
    client.post(f"/admin/feedback/{t['id']}/analyze", data={})

    assert calls == [t["id"]]      # 第二次不再起线程


def test_dispatch_route_rejects_open_ticket(logged_client):
    """二审闸不能绕过：没走过一审的单子直接派发要被拦回。"""
    client, _ = logged_client
    t = _mk_ticket()

    resp = client.post(f"/admin/feedback/{t['id']}/dispatch",
                       data={"draft_title": "t", "draft_body": "b" * 60},
                       follow_redirects=True)
    assert "请先点「通过，开始分析」" in resp.get_data(as_text=True)
    conn = _conn()
    assert feedback_pure.get_ticket(conn, t["id"])["status"] == "open"
    conn.close()


def test_second_review_edits_are_what_gets_dispatched(logged_client, monkeypatch):
    """二审正文以管理员提交的为准 —— 这是二审存在的意义。"""
    from blueprints import feedback_dispatch

    client, _ = logged_client
    t = _mk_ticket()
    conn = _conn()
    feedback_pure.mark_analyzing(conn, t["id"])
    feedback_pure.save_analysis(
        conn, t["id"], "{}", "m", "原标题", "原正文" * 20)
    conn.close()

    monkeypatch.setattr(feedback_dispatch, "dispatch_enabled", lambda: True)
    captured: dict = {}

    def fake_create(title, body, label=None):
        captured.update(title=title, body=body)
        return {"number": 77, "url": "http://x/77"}

    monkeypatch.setattr(feedback_dispatch, "create_issue", fake_create)

    client.post(f"/admin/feedback/{t['id']}/dispatch",
                data={"draft_title": "改过的标题", "draft_body": "改过的正文" * 10},
                follow_redirects=True)

    assert captured["title"] == "改过的标题"
    assert "改过的正文" in captured["body"]
    conn = _conn()
    row = feedback_pure.get_ticket(conn, t["id"])
    conn.close()
    assert row["status"] == "dispatched"
    assert row["draft_title"] == "改过的标题"


def test_analyzing_page_does_not_offer_dispatch(logged_client):
    client, _ = logged_client
    t = _mk_ticket()
    conn = _conn()
    feedback_pure.mark_analyzing(conn, t["id"])
    conn.close()

    html = client.get(f"/admin/feedback/{t['id']}").get_data(as_text=True)
    assert "分析中" in html
    assert "确认派发" not in html
