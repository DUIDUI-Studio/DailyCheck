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
    # ⚠️ 样本时间必须钉在 CST 今天中午，不能用 `now - 600`：
    # CI 常在 UTC 16:0x（= CST 次日 00:0x）跑，`now-600` 会落到 CST **前一天**，
    # 而断言要求是「今天」→ 必然判红（2026-10-18 CI 实 hit，本地非午夜时段跑不出）。
    # 钉中午后，±700s 的偏移无论如何都跨不过午夜。
    now = _cst_noon_epoch()
    _make_state_db(db_path, [
        ("s1", "webhook", now - 600, now, 100, 10, 1000, 0, 0, 5),
        ("s2", "cron", now - 700, now, 20, 2, 30, 0, 0, 1),
        ("s3", "cron", now - 3 * 86400, now, 5, 1, 7, 0, 0, 0),
    ])
    rows = ops_parse.fetch_token_usage(db_path, days=7)
    by_key = {(r["day"], r["source"]): r for r in rows}
    today_cst = datetime.fromtimestamp(now, timezone.utc).astimezone(
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
    assert "Gitea 拉取失败" in html
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


# ============================================================
# KPI 的「今日」语义
# ============================================================
def _kpi_values(html_text: str) -> dict[str, str]:
    """从 KPI 条抽 {label: 值}（值里的 <small> 单位一并剥掉）。"""
    return {
        label: re.sub(r"<[^>]+>", "", value).strip()
        for value, label in re.findall(
            r'<div class="ov">(.*?)</div><div class="ok">([^<]*)</div>', html_text, re.S
        )
    }


def _cst_noon_utc_iso() -> str:
    """CST 今天 12:00 对应的 UTC ISO —— 保证换算回 CST 仍是「今天」。"""
    cst = datetime.now(timezone(timedelta(hours=8)))
    return (cst.replace(hour=12, minute=0, second=0, microsecond=0)
            - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cst_noon_epoch() -> float:
    """CST 今天 12:00 的 epoch（与 _cst_noon_utc_iso 同一时刻，只是数值形态）。

    给需要 REAL 时间戳的 sessions 表用。钉在正午而非 `time.time()`：
    CI 常在 UTC 16:0x（= CST 次日 00:0x）跑，用 now 会让「now-600s」这类
    偏移样本掉进 CST 前一天，断言「今天」必然判红。
    """
    cst = datetime.now(timezone(timedelta(hours=8)))
    return cst.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


def test_kpi_counts_only_todays_deploys():
    events = [{
        "pr": 105,
        "started_at": _cst_noon_utc_iso(),
        "segments": [{"name": "verify", "sec": 600}],
    }]
    vals = _kpi_values(render_ops._render_kpis(events, [], []))
    assert vals["今日部署"] == "1 次"
    assert vals["验证到切流"] == "10:00"


def test_kpi_today_boundary_is_tz_independent(monkeypatch):
    """钉死时刻在 CST 午夜窗口内，断言「今天」判据与进程时区无关。

    回归：`today` 曾用 `time.strftime`（进程本地时区）。生产 unit 未声明 TZ
    → 进程 now() 即 CST，侥幸正确；但 CI 与 dev 容器是 UTC，在 UTC 16:00–24:00
    （= CST 次日 00:00–08:00）窗口内本地时区的「今天」比 CST 少一天，与
    `_cst_day()`（显式 +8h）错位 → 「今日部署」恒显 0 次。
    2026-10-18 16:06 UTC 的 CI 实 hit（本地非午夜时段跑不出来）。

    钉死时刻 + 切进程 TZ 后，本用例在任何真实时间下都有区分度——不依赖
    「恰好在这个窗口跑 CI」这种运气。
    """
    # 钉死：UTC 2026-10-18 16:30 = CST 2026-10-19 00:30（正落在出问题的窗口内）
    frozen_utc = datetime(2026, 10, 18, 16, 30, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_utc.astimezone(tz) if tz else frozen_utc.replace(tzinfo=None)

    monkeypatch.setattr(render_ops, "datetime", _FrozenDatetime)

    # 事件时刻 = CST 2026-10-19 12:00，与钉死的「CST 今天」同日
    events = [{
        "pr": 105,
        "started_at": "2026-10-19T04:00:00Z",
        "segments": [{"name": "verify", "sec": 600}],
    }]

    tz_names = ["UTC", "Asia/Shanghai"]
    if hasattr(time, "tzset"):
        tz_names.append("America/New_York")
    for tz_name in tz_names:
        if hasattr(time, "tzset"):
            monkeypatch.setenv("TZ", tz_name)
            time.tzset()
        try:
            vals = _kpi_values(render_ops._render_kpis(events, [], []))
            assert vals["今日部署"] == "1 次", (
                f"进程 TZ={tz_name} 时「今日部署」判错（应按 CST 口径算 1 次）：{vals}"
            )
        finally:
            if hasattr(time, "tzset"):
                monkeypatch.delenv("TZ", raising=False)
                time.tzset()


def test_kpi_today_is_strict_when_no_deploy_today():
    """今天没部署 = 0 次，**不能回退成历史全部次数**。

    2026-10-18 生产实测命中：当天零部署，卡片却显示「12 次」
    （`today_events or events` 的兜底把历史当成了今天）。
    """
    past = (datetime.now(timezone.utc) - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = [{
        "pr": 97,
        "started_at": past,
        "segments": [{"name": "verify", "sec": 608}],
    }]
    html = render_ops._render_kpis(events, [], [])
    vals = _kpi_values(html)

    assert vals["今日部署"] == "0 次"
    assert vals["验证到切流"] == "—"  # 不给历史均值冒充今日
    assert "#97" in html              # 但要说清「最近一次」是哪次，否则 0 会被读成没跑


# ============================================================
# 闭环完整性（2026-10-18 复盘：让 owner 不必 review 就能看见流程真相）
# ============================================================

LOOP_AUDIT_SAMPLE = """\
2026-10-17T05:48:41Z up.sh pr=85 {"active": true, "lock": {"pr": 85, "sha": "@A@", "session": "pipeline-85"}}
2026-10-17T06:11:25Z switch.sh target=green {"active": true, "lock": {"pr": 85, "sha": "@A@", "session": "pipeline-85"}}
2026-10-17T12:18:08Z up.sh pr=96 {"active": true, "lock": {"pr": 96, "sha": "@B@", "session": "hermes-96"}}
2026-10-17T12:38:02Z switch.sh target=green {"active": true, "lock": {"pr": 96, "sha": "@B@", "session": "dev-takeover"}}
2026-10-17T01:57:56Z up.sh pr=80 {"active": true, "lock": {"pr": 80, "sha": "@A@", "session": ""}}
2026-10-18T16:05:00Z pipeline-event kind=worktree-drift pr=110 sha=a80926b session=pipeline-110 verify 期间 HEAD 变了
2026-10-18T16:10:00Z pipeline-event kind=observe-window-shortened pr=111 sha=07e8fef session=dev-111 observe=10min
""".replace("@A@", _SHA_A).replace("@B@", _SHA_B)


def test_classify_session_uses_whitelist():
    """分类是白名单式：只有确定的自动前缀算 auto，其余非空一律 manual。

    宁可多报不漏报——漏报人工接管会让「闭环成立」变成假结论。
    """
    for s in ("hermes-96", "pipeline-110", "webhook-ci-pr84", "release-84"):
        assert ops_parse.classify_session(s) == "auto", s
    for s in ("dev-takeover", "dev-111", "manual-switch-1018", "anything-new"):
        assert ops_parse.classify_session(s) == "manual", s


def test_classify_session_empty_is_unlabeled_not_auto():
    """空 session 单列 unlabeled —— 算进 auto 会把自动率虚高。"""
    assert ops_parse.classify_session("") == "unlabeled"


def test_loop_integrity_excludes_unlabeled_from_rate_denominator():
    """自动率分母**只含有标注记录**。

    实测 88 行里 58 行是空 session（早期无机制）；摊进任何一侧都让比率失真。
    """
    recs = ops_parse.parse_audit_text(LOOP_AUDIT_SAMPLE)
    li = ops_parse.build_loop_integrity(recs)

    assert li["auto_records"] == 4      # pipeline-85 ×2 + hermes-96 + pipeline-110
    assert li["manual_records"] == 2    # dev-takeover + dev-111
    assert li["unlabeled_records"] == 1  # pr=80 那条空 session
    # 4 / (4+2) —— 分母不含未标注的 1 条
    assert abs(li["auto_rate"] - 4 / 6) < 1e-9


def test_loop_integrity_rate_is_none_when_nothing_labeled():
    """全无标注时 auto_rate 必须是 None（渲染成「—」），不能报 100% 假绿。"""
    recs = ops_parse.parse_audit_text(
        '2026-10-17T01:57:56Z up.sh pr=80 {"active": true, "lock": {"pr": 80, "session": ""}}\n'
    )
    li = ops_parse.build_loop_integrity(recs)

    assert li["auto_rate"] is None
    assert li["unlabeled_records"] == 1


def test_loop_integrity_collects_manual_sessions_and_prs():
    """人工接管的会话名与 PR 号都要能追到 —— 只给个数字没法定位。"""
    recs = ops_parse.parse_audit_text(LOOP_AUDIT_SAMPLE)
    li = ops_parse.build_loop_integrity(recs)

    assert li["manual_sessions"] == ["dev-111", "dev-takeover"]
    assert li["manual_prs"] == [96, 111]


def test_loop_integrity_counts_event_kinds():
    """异常留痕事件按 kind 计数（F1/F4/F5 写入的 pipeline-event 行）。"""
    recs = ops_parse.parse_audit_text(LOOP_AUDIT_SAMPLE)
    li = ops_parse.build_loop_integrity(recs)

    assert li["events"] == {"worktree-drift": 1, "observe-window-shortened": 1}


def test_loop_integrity_declares_rollback_blind_spot():
    """回滚次数**不可观测**必须声明为盲区，绝不报 0。

    `switch.sh` 只审计 target=green，切蓝不留痕。把「没记录」说成「没发生」
    比不报更坏 —— 那会让人以为「回滚从没发生＝部署一直成功」。
    """
    recs = ops_parse.parse_audit_text(LOOP_AUDIT_SAMPLE)
    li = ops_parse.build_loop_integrity(recs)

    assert any("回滚" in b for b in li["blind_spots"]), li["blind_spots"]
    assert "rollback_count" not in li, "不得伪造一个回滚计数"


def test_detect_event_instrumentation_three_states(tmp_path):
    """埋点探测三态：deployed / absent / unknown，各自语义不同。

    ⚠️ 必须三态而非 bool：absent（脚本在但无埋点）要明说「0 不代表没发生」，
    unknown（脚本读不到）连「未部署」都不能断言 —— 把「读不到」说成
    「未部署」同样是编造事实。
    """
    # deployed：脚本含 _audit_event
    ok_dir = tmp_path / "ok"
    (ok_dir / "scripts" / "blue_green").mkdir(parents=True)
    (ok_dir / "scripts/blue_green/deploy_pipeline.sh").write_text(
        "_audit_event() { :; }\n", encoding="utf-8")
    assert ops_parse.detect_event_instrumentation(ok_dir) == "deployed"

    # absent：脚本在但无埋点（生产 2026-10-19 实测形态）
    bare_dir = tmp_path / "bare"
    (bare_dir / "scripts" / "blue_green").mkdir(parents=True)
    (bare_dir / "scripts/blue_green/deploy_pipeline.sh").write_text(
        'PY="${PYTHON:-python3}"\n', encoding="utf-8")
    assert ops_parse.detect_event_instrumentation(bare_dir) == "absent"

    # unknown：脚本读不到
    assert ops_parse.detect_event_instrumentation(tmp_path / "nonexistent") == "unknown"


def test_detect_event_instrumentation_ignores_comments(tmp_path):
    """回归：注释/文档里提到 `_audit_event` **不得**让探测假阳性判成 deployed。

    2026-10-19 实测命中：裸子串匹配下，一个只写了「# 无 _audit_event」注释的
    脚本被判成 deployed → events_observable=True → 看板又渲染「均未发生」，
    假信号复活。必须匹配函数定义形态。
    """
    d = tmp_path / "commented"
    (d / "scripts" / "blue_green").mkdir(parents=True)
    (d / "scripts/blue_green/deploy_pipeline.sh").write_text(
        'PY="${PYTHON:-python3}"\n'
        "# 无 _audit_event\n"
        "# TODO: 调用 _audit_event 留痕\n",
        encoding="utf-8")

    assert ops_parse.detect_event_instrumentation(d) == "absent"


def test_detect_event_instrumentation_matches_real_definition(tmp_path):
    """真实定义形态必须命中（含缩进 / 空格变体），否则修复会把埋点判成未部署。"""
    for i, body in enumerate([
        '_audit_event() {\n  :\n}\n',
        '_audit_event ()\n{\n  :\n}\n',
        '  _audit_event() {  :\n  }\n',
    ]):
        d = tmp_path / f"real{i}"
        (d / "scripts" / "blue_green").mkdir(parents=True)
        (d / "scripts/blue_green/deploy_pipeline.sh").write_text(body, encoding="utf-8")
        assert ops_parse.detect_event_instrumentation(d) == "deployed", body


def test_events_zero_without_instrumentation_is_not_reported_as_clean(tmp_path):
    """核心假信号回归：埋点未部署时，事件数 0 **不得**渲染成「无异常」。

    2026-10-19 实测命中：#118（看板）先于 #116（埋点）合并，生产脚本无
    `_audit_event`，看板却显示「钩子失败均未发生」——而实测生产系统 python3
    对 scripts.dailycheck_dashboard 抛 ModuleNotFoundError，钩子一直是坏的。
    这违反同一模块的铁律「把没记录说成没发生比不报更坏」。

    ⚠️ 必须用**不含 pipeline-event 行**的样本（AUDIT_SAMPLE）：
    LOOP_AUDIT_SAMPLE 自带 2 条事件，events 非空 → 永远走不到「0 起」分支，
    而这个分支正是要区分「真无异常」与「没人在记」的地方。
    """
    recs = ops_parse.parse_audit_text(AUDIT_SAMPLE)
    assert not ops_parse.build_loop_integrity(recs)["events"], "样本必须无事件行"

    # 埋点在 → 0 起是真无异常
    deployed = _write_instrumented_repo(tmp_path)
    li_ok = ops_parse.build_loop_integrity(recs, repo_dir=deployed)
    assert li_ok["events_observable"] is True
    assert not any("异常留痕" in b for b in li_ok["blind_spots"])
    html_ok = render_ops._render_loop_integrity(li_ok)
    assert "均未发生" in html_ok

    # 埋点不在 → 0 起必须标不可观测，且计入盲区
    li_bad = ops_parse.build_loop_integrity(recs, repo_dir=tmp_path / "nope")
    assert li_bad["events_observable"] is False
    assert any("异常留痕" in b for b in li_bad["blind_spots"]), li_bad["blind_spots"]
    html_bad = render_ops._render_loop_integrity(li_bad)
    assert "均未发生" not in html_bad, "埋点未部署却宣称无异常 = 假信号"
    assert "不可观测" in html_bad
    assert 'class="n hot"' in html_bad, "不可观测必须是告警色，不能是绿"


def test_events_card_shows_dash_when_not_observable():
    """KPI 卡：不可观测时显示「—」+ hot，不显示「0 起」（0 会被读成正常）。"""
    recs = ops_parse.parse_audit_text(LOOP_AUDIT_SAMPLE)
    html = render_ops._render_loop_integrity(
        ops_parse.build_loop_integrity(recs, repo_dir=None))
    vals = _kpi_values(html)

    assert vals["异常留痕"] == "—"
    assert "埋点未部署，不可观测" in html


def _write_instrumented_repo(base: Path) -> Path:
    """在 base 下建一个含 _audit_event 埋点的假仓库，返回仓库根。

    用 tmp_path 而非 tempfile.mkdtemp：后者会泄漏目录且不受 pytest 清理管理。
    """
    (base / "scripts" / "blue_green").mkdir(parents=True, exist_ok=True)
    (base / "scripts/blue_green/deploy_pipeline.sh").write_text(
        "_audit_event() { :; }\n", encoding="utf-8")
    return base


def test_pipeline_event_does_not_create_ghost_deploy_event():
    """回归：`pipeline-event` 行不得造出幽灵部署事件。

    只出现过一条事件、从没跑过 up 的 PR，若被归组会得到 segments=[]、
    total=0 的假「部署」，污染「今日部署」计数（本次实现时实测命中）。
    """
    recs = ops_parse.parse_audit_text(LOOP_AUDIT_SAMPLE)
    events = ops_parse.build_deploy_events(recs)

    prs = {e["pr"] for e in events}
    assert 110 not in prs, "pr=110 只有一条 pipeline-event，不该算部署"
    assert 111 not in prs, "pr=111 只有一条 pipeline-event，不该算部署"
    assert {85, 96, 80} <= prs, f"真跑过阶段的 PR 丢了：{prs}"
    # 注：pr=80 只有 up.sh 一条 → segments=[] 是**合法单点部署**（非幽灵），
    # 该行为由 test_build_deploy_events_single_point_does_not_crash 覆盖，此处不重复断言。


def test_parse_audit_extracts_session_from_both_forms():
    """session 有两个来源，必须都认：阶段行的 lock.session、事件行的 session= 参数。

    只认一处会让统计漏一半（单一归一化口径，Issue #51→#66 同型教训）。
    """
    recs = ops_parse.parse_audit_text(LOOP_AUDIT_SAMPLE)
    by_pr_script = {(r["pr"], r["script"]): r for r in recs}

    # 阶段行：session 来自行尾 JSON 的 lock.session
    assert by_pr_script[(85, "up.sh")]["session"] == "pipeline-85"
    # 事件行：session 来自 `session=` 参数（该行没有 JSON）
    assert by_pr_script[(110, "pipeline-event")]["session"] == "pipeline-110"
    assert by_pr_script[(110, "pipeline-event")]["kind"] == "worktree-drift"


def test_loop_integrity_renders_kpis_and_detail_rows(tmp_path):
    """渲染层：四张 KPI 卡 + 明细行都要出现，盲区行必须在场。

    用「埋点已部署」的仓库，使盲区回到基线的 1 处（回滚），
    异常事件也能正常渲染成人话。
    """
    repo = _write_instrumented_repo(tmp_path)
    recs = ops_parse.parse_audit_text(LOOP_AUDIT_SAMPLE)
    html = render_ops._render_loop_integrity(
        ops_parse.build_loop_integrity(recs, repo_dir=repo))

    assert 'id="loop"' in html
    assert "闭环完整性" in html
    vals = _kpi_values(html)
    for label in ("自动闭环率", "人工接管", "异常留痕", "数据盲区"):
        assert label in vals, f"缺 KPI 卡：{label}"
    assert vals["人工接管"] == "2 次"
    assert vals["数据盲区"] == "1 处"
    assert vals["异常留痕"] == "2 起"  # 埋点在 → 事件数真实可观测
    # 明细：异常事件用中文人话，未标注与盲区都要露出来
    assert "工作树漂移" in html
    assert "观察窗被压缩" in html
    assert "未标注会话" in html
    assert "回滚次数不可观测" in html


def test_loop_integrity_renders_dash_when_rate_unknown():
    """无标注记录时自动率渲染成「—」并标 hot，不能显示成 100% 的绿。"""
    recs = ops_parse.parse_audit_text(
        '2026-10-17T01:57:56Z up.sh pr=80 {"active": true, "lock": {"pr": 80, "session": ""}}\n'
    )
    html = render_ops._render_loop_integrity(ops_parse.build_loop_integrity(recs))
    vals = _kpi_values(html)

    assert vals["自动闭环率"] == "—"
    assert "无标注会话" in html


def test_loop_integrity_empty_dict_renders_nothing():
    """audit.log 不可用时（空 dict）本节整体不渲染，不留空壳标题。"""
    assert render_ops._render_loop_integrity({}) == ""


def test_ops_block_includes_loop_section_even_without_deploy_events():
    """关键：没有部署事件时闭环完整性**仍要渲染** —— 盲区本身就是事实。

    若跟着 body_parts 一起消失，「回滚不可观测」这类盲区就永远看不见。
    """
    html = render_ops.render_ops_block(
        events=[], prs=[], verifications=[], token_rows=[],
        loop_integrity={"auto_records": 0, "manual_records": 0,
                        "unlabeled_records": 0, "auto_rate": None,
                        "manual_sessions": [], "manual_prs": [], "events": {},
                        "blind_spots": ["回滚次数不可观测：switch.sh 只审计 target=green"]},
    )
    assert "运维数据源暂不可用" in html
    assert 'id="loop"' in html
    assert "回滚次数不可观测" in html


def test_ops_block_renumbers_anomaly_after_loop_section():
    """章节号：闭环完整性占 4，异常归因必须顺延为 5（不得两个 4）。"""
    recs = ops_parse.parse_audit_text(AUDIT_SAMPLE)
    events = ops_parse.build_deploy_events(recs)
    html = render_ops.render_ops_block(
        events=events, prs=[], verifications=[], token_rows=[],
        loop_integrity=ops_parse.build_loop_integrity(recs),
    )
    assert '<span class="no">4</span>闭环完整性' in html
    assert '<span class="no">5</span>异常归因' in html
