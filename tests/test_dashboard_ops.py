"""运维仪表盘（交付看板 / token / 验证矩阵）的回归网。

覆盖三件事：
  1. **audit.log 解析**：嵌套 JSON 的 `{` 定位（rfind → find 的真实 bug）、
     阶段切段语义（switch 取最后一次）、卡顿判定、单点部署不炸。
  2. **事件表幂等**：同一份事实重复摄取只覆盖不新增（每 15 分钟一次渲染的安全前提）。
  3. **降级**：任一数据源不可用时其余区块照常渲染，页面不白屏。

不测（部署态人工验证）：真 systemd timer 拉起、真 gh API、真 2GB state.db。
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.dailycheck_dashboard import ops_db, ops_parse, render, render_ops

_SHA_A = "a" * 40
_SHA_B = "b" * 40

AUDIT_SAMPLE = """\
2026-10-17T05:48:41Z up.sh pr=85 {"active": true, "lock": {"pr": 85, "sha": "@A@", "phase": "acquire"}}
2026-10-17T05:48:56Z verify.sh pr=85 {"active": true, "lock": {"pr": 85, "sha": "@A@", "phase": "acquire"}}
2026-10-17T06:11:25Z switch.sh target=green {"active": true, "lock": {"pr": 85, "sha": "@A@", "phase": "promote-running"}}
2026-10-17T12:54:35Z up.sh pr=97 {"active": true, "lock": {"pr": 97, "sha": "@B@", "phase": "pre-up-reset"}}
2026-10-17T12:55:37Z verify.sh pr=97 {"active": true, "lock": {"pr": 97, "sha": "@B@", "phase": "verify-start"}}
2026-10-17T13:05:45Z switch.sh target=green {"active": true, "lock": {"pr": 97, "sha": "@B@", "phase": "verify-wait-10m"}}
2026-10-17T13:11:01Z promote.sh {"active": true, "lock": {"pr": 97, "sha": "@B@", "phase": "verify-done"}}
2026-10-17T13:18:37Z switch.sh target=green {"active": true, "lock": {"pr": 97, "sha": "@B@", "phase": "promote-done"}}
""".replace("@A@", _SHA_A).replace("@B@", _SHA_B)

VERIF_SAMPLE = """\
## 2. 八项检查 + dry_run 证据

| # | 检查项 | 状态 | 备注 |
|---|--------|------|------|
| 1 | 集成测试 | ✅ PASS | FAILED tests/test_recipe_cost_route.py::test_x |
| 2 | 模拟用户操作 | ✅ PASS |  |
| 3 | 性能比对 | ❌ FAIL | 慢 40% |

报告生成时间: 2026-10-17T12:55:37Z
"""


# ============================================================
# 1. audit.log 解析
# ============================================================
def test_parse_audit_extracts_nested_lock_fields():
    """回归：行尾 JSON 里嵌了 lock 对象，必须取**第一个** `{`，否则 lock.pr/sha 全丢。

    历史 bug：用 rfind 取到内层 `{`，json.loads 拿到 `…}}` 直接抛 JSONDecodeError，
    promote/switch 行（没有 pr= 参数可退）被整行丢弃，瀑布图只剩 up 一段。
    """
    recs = ops_parse.parse_audit_text(AUDIT_SAMPLE)
    assert len(recs) == 8
    assert all(r["sha"] for r in recs), "嵌套 JSON 没解析出来"
    assert [r["pr"] for r in recs] == [85, 85, 85, 97, 97, 97, 97, 97]
    assert recs[6]["script"] == "promote.sh", "promote 行不该被丢"


def test_parse_audit_falls_back_to_pr_argument():
    """JSON 缺失时退回 `pr=` 参数（老格式 / 截断行）。"""
    line = "2026-10-17T05:48:41Z up.sh pr=42\n"
    recs = ops_parse.parse_audit_text(line)
    assert len(recs) == 1
    assert recs[0]["pr"] == 42
    assert recs[0]["sha"] is None


def test_parse_audit_skips_unattributable_lines():
    """没有 pr 也没有 lock 的行直接丢，不能凭空造事件。"""
    assert ops_parse.parse_audit_text("2026-10-17T05:48:41Z garbage\n") == []


def test_build_deploy_events_stage_segments():
    ev = {e["pr"]: e for e in ops_parse.build_deploy_events(ops_parse.parse_audit_text(AUDIT_SAMPLE))}
    e97 = ev[97]
    assert e97["started_at"] == "2026-10-17T12:54:35Z"
    assert e97["finished_at"] == "2026-10-17T13:18:37Z"
    assert e97["total_sec"] == 1442
    assert [(s["name"], s["sec"]) for s in e97["segments"]] == [
        ("up", 62), ("verify", 924), ("promote", 456),
    ]


def test_build_deploy_events_switch_last_wins():
    """同一条 PR 的 switch.sh 会出现多次（verify check 4 的回滚演练也调它），取最后一次。"""
    recs = ops_parse.parse_audit_text(
        "2026-10-17T00:00:00Z up.sh pr=1 {\"lock\": {\"pr\": 1, \"sha\": \"s1\"}}\n"
        "2026-10-17T00:01:00Z verify.sh pr=1 {\"lock\": {\"pr\": 1, \"sha\": \"s1\"}}\n"
        "2026-10-17T00:02:00Z switch.sh target=blue {\"lock\": {\"pr\": 1, \"sha\": \"s1\"}}\n"
        "2026-10-17T00:12:00Z switch.sh target=green {\"lock\": {\"pr\": 1, \"sha\": \"s1\"}}\n"
    )
    e = ops_parse.build_deploy_events(recs)[0]
    assert e["finished_at"] == "2026-10-17T00:12:00Z"
    assert e["total_sec"] == 720


def test_build_deploy_events_flags_only_fast_stage_stalls():
    """up/promote 正常是秒级，>5 分钟算卡顿；verify 天然 10-15 分钟，不判卡顿。"""
    ev = {e["pr"]: e for e in ops_parse.build_deploy_events(ops_parse.parse_audit_text(AUDIT_SAMPLE))}
    promote_seg = [s for s in ev[97]["segments"] if s["name"] == "promote"][0]
    verify_seg = [s for s in ev[97]["segments"] if s["name"] == "verify"][0]
    assert promote_seg["stalled"] is True, "promote 卡 7:36（#97 会话中途终止）必须标出"
    assert verify_seg["stalled"] is False, "verify 15:24 属正常区间"


def test_build_deploy_events_single_point_does_not_crash():
    """只 up 过（失败在 verify 前）也要出记录，总时长 0、无段。"""
    recs = ops_parse.parse_audit_text(
        "2026-10-17T00:00:00Z up.sh pr=9 {\"lock\": {\"pr\": 9, \"sha\": \"s\"}}\n"
    )
    e = ops_parse.build_deploy_events(recs)[0]
    assert e["total_sec"] == 0
    assert e["segments"] == []


def test_parse_audit_log_missing_file_raises(tmp_path):
    with pytest.raises(ops_parse.OpsParseError):
        ops_parse.parse_audit_log(tmp_path / "nope.log")


# ============================================================
# 2. 验证报告
# ============================================================
def test_parse_verification_text_counts_pass():
    rec = ops_parse.parse_verification_text(VERIF_SAMPLE, pr=97)
    assert rec["total"] == 3
    assert rec["passed"] == 2
    assert rec["generated_at"] == "2026-10-17T12:55:37Z"
    assert [c["ok"] for c in rec["checks"]] == [True, True, False]


def test_parse_verifications_reads_dir_and_sorts(tmp_path):
    d = tmp_path / "verif"
    d.mkdir()
    (d / "20261016-pr-80.md").write_text(
        VERIF_SAMPLE.replace("2026-10-17T12:55:37Z", "2026-10-16T10:00:00Z"), encoding="utf-8"
    )
    (d / "20261017-pr-97.md").write_text(VERIF_SAMPLE, encoding="utf-8")
    (d / "README.md").write_text("not a report", encoding="utf-8")
    recs = ops_parse.parse_verifications(d)
    assert [r["pr"] for r in recs] == [80, 97], "非报告文件要跳过，且按生成时间升序"
    assert recs[-1]["path"].endswith("20261017-pr-97.md")


def test_parse_verifications_missing_dir_raises(tmp_path):
    with pytest.raises(ops_parse.OpsParseError):
        ops_parse.parse_verifications(tmp_path / "nope")


# ============================================================
# 3. token 用量（只读打开 state.db）
# ============================================================
def _make_state_db(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE sessions (
            id TEXT, source TEXT, started_at REAL, ended_at REAL,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER, tool_call_count INTEGER
        )"""
    )
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_fetch_token_usage_groups_by_cst_day(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    _make_state_db(db_path, [
        ("s1", "webhook", now - 600, now, 100, 10, 1000, 0, 0, 5),
        ("s2", "cron", now - 700, now, 20, 2, 30, 0, 0, 1),
        ("s3", "cron", now - 3 * 86400, now, 5, 1, 7, 0, 0, 0),
    ])
    rows = ops_parse.fetch_token_usage(db_path, days=7)
    by_key = {(r["day"], r["source"]): r for r in rows}
    today_cst = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d")
    assert (today_cst, "webhook") in by_key
    assert by_key[(today_cst, "webhook")]["input_tok"] == 100
    assert by_key[(today_cst, "cron")]["sessions"] == 1
    assert len(rows) == 3


def test_fetch_token_usage_missing_db_raises(tmp_path):
    with pytest.raises(ops_parse.OpsParseError):
        ops_parse.fetch_token_usage(tmp_path / "nope.db")


# ============================================================
# 4. 事件表幂等
# ============================================================
def test_upsert_is_idempotent(tmp_path):
    """渲染每 15 分钟一次、部署钩子还会再来一次 —— 重复摄取必须只覆盖不新增。"""
    db_path = tmp_path / "dash.db"
    events = ops_parse.build_deploy_events(ops_parse.parse_audit_text(AUDIT_SAMPLE))
    for _ in range(3):
        ops_db.upsert_deploy_events(db_path, events)
    assert len(ops_db.recent_deploy_events(db_path, limit=10)) == 2

    rows = [
        {"day": "2026-10-17", "source": "cron", "sessions": 1, "input_tok": 10},
        {"day": "2026-10-17", "source": "webhook", "sessions": 2, "input_tok": 20},
    ]
    for _ in range(2):
        ops_db.upsert_token_daily(db_path, rows)
    series = ops_db.token_series(db_path, days=7)
    assert len(series) == 2


def test_upsert_deploy_events_roundtrip_segments(tmp_path):
    db_path = tmp_path / "dash.db"
    ops_db.upsert_deploy_events(
        db_path, ops_parse.build_deploy_events(ops_parse.parse_audit_text(AUDIT_SAMPLE))
    )
    top = ops_db.recent_deploy_events(db_path, limit=1)[0]
    assert top["pr"] == 97
    assert [s["name"] for s in top["segments"]] == ["up", "verify", "promote"]


# ============================================================
# 5. 渲染（含降级）
# ============================================================
def _sample_input(**kw):
    base = dict(
        created_at="2026-10-17T21:52:00+08:00",
        trigger_kind="deploy",
        trigger_event_id="pr-97",
        head_sha="a" * 40,
        origin_main_sha="b" * 40,
        summary={"open_issues": 0, "merged_7d_total": 19, "worktrees_total": 3,
                 "closed_unmerged": 0},
        prs=[{"number": 97, "state": "MERGED", "created_ts": "2026-10-17T12:42:47Z",
              "merged_ts": "2026-10-17T12:54:09Z"}],
        deploy_events=ops_parse.build_deploy_events(
            ops_parse.parse_audit_text(AUDIT_SAMPLE)
        ),
        verifications=[ops_parse.parse_verification_text(VERIF_SAMPLE, pr=97)],
        token_rows=[{"day": time.strftime("%Y-%m-%d"), "source": "webhook",
                     "sessions": 61, "input_tok": 4_000_000, "cache_read_tok": 38_000_000}],
        ops_fetched_at="2026-10-17T21:52:00+08:00",
    )
    base.update(kw)
    return render.RenderInput(**base)


def test_render_contains_ops_sections():
    html = render.render(_sample_input())
    for marker in ("交付看板", "wf-row", "Token 用量", "部署验证矩阵", "异常归因", "ops-kpi"):
        assert marker in html, f"缺区块：{marker}"
    assert "seg-stall" in html, "#97 的 promote 卡顿要在瀑布图上标红"


def test_render_degrades_when_all_sources_missing():
    """三个运维源全挂：页面照常出，只在该区块说明不可用。"""
    html = render.render(_sample_input(
        deploy_events=[], verifications=[], token_rows=[],
        ops_unavailable=["部署审计日志", "验证报告", "Hermes state.db"],
    ))
    assert "运维数据源暂不可用" in html
    assert "Hermes state.db" in html
    assert "Issue 追踪" in html and "PR 管线" in html, "其余章节不受影响"


def test_render_shows_dash_not_zero_when_pr_fetch_failed():
    """GitHub 拉不到 PR ≠ 今天没合并 PR：KPI 必须显示「—」，否则读成「今天 0 个」。"""
    html = render.render(_sample_input(
        prs=[], pr_data_ok=False, ops_unavailable=["GitHub PR 列表"],
    ))
    assert "GitHub 拉取失败" in html
    value = re.search(
        r'<div class="ov">([^<]*)</div><div class="ok">今日合并 PR</div>', html
    ).group(1)
    assert value.strip() == "—", f"拉取失败时不该显示 {value!r}"


def test_render_ops_block_empty_has_kpi_but_no_waterfall():
    html = render_ops.render_ops_block(
        events=[], prs=[], verifications=[], token_rows=[], unavailable=["部署审计日志"]
    )
    assert "ops-kpi" in html
    assert "wf-row" not in html
    assert "部署审计日志" in html


def test_fmt_sec_boundaries():
    assert render_ops._fmt_sec(None) == "—"
    assert render_ops._fmt_sec(0) == "0s"
    assert render_ops._fmt_sec(59) == "59s"
    assert render_ops._fmt_sec(62) == "1:02"
    assert render_ops._fmt_sec(3600 + 62) == "1:01:02"
