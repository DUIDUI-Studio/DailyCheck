"""派单账本摄取 + 模型效能评估区块（Issue #92）的回归网。

覆盖四件事：
  1. **解析**：ISO 8601 时区归一（+08:00 / Z 同一事实只有一个键）、单行损坏只跳过、
     整份不可解析才降级。
  2. **幂等**：(worker_id, ts) 唯一键，重复摄取只覆盖不新增。
  3. **聚合**：role × model × 复杂度档；null 数据跳过求和并单列，重派按 issue 计。
  4. **渲染**：PR 状态 join / 交付周期 / 覆盖率 / HTML 转义 / 数据源不可用降级。

不测（部署态人工验证）：scp 桥、真 systemd timer、真 Gitea 数据。
"""
from __future__ import annotations

import sqlite3

import pytest

from scripts.dailycheck_dashboard import ledger_db, ops_parse, render, render_ops


def _row(**kw) -> dict:
    base = {
        "ts": "2026-10-30T10:00:00+08:00",
        "issue": 92,
        "role": "developer",
        "agent": "claude",
        "model_requested": "deepseek-v4",
        "model_effective": "qwen3.8-max",
        "effort": "high",
        "complexity_score": 6.7,
        "worker_id": "ctx_1",
        "task_id": "task_1",
        "worktree_path": "/Users/ericmr/work",
        "pr": 93,
        "tokens": {
            "input": 100, "output": 50, "cache_read": 0,
            "cache_write": 0, "reasoning": 0,
        },
        "cost": 0.12,
        "warnings": [],
    }
    base.update(kw)
    return base


def _write_ledger(path, rows) -> None:
    import json
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def _aggregate(path, rows):
    _write_ledger(path, rows)
    return ledger_db.aggregate_rows(ledger_db.parse_ledger(path))


# ============================================================
# 1. 解析
# ============================================================
def test_parse_normalises_timezone_to_utc(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _write_ledger(path, [
        _row(worker_id="w1", ts="2026-10-30T10:00:00+08:00"),
        _row(worker_id="w2", ts="2026-10-30T02:00:00Z"),
    ])
    rows = ledger_db.parse_ledger(path)
    assert [r["ts"] for r in rows] == [
        "2026-10-30T02:00:00Z",
        "2026-10-30T02:00:00Z",
    ], "同一事实的 +08:00 / Z 两种写法必须归一，否则 upsert 会出一行两条"


def test_parse_skips_half_line_but_keeps_valid_rows(tmp_path):
    """append-only 日志尾部半行是正常竞态，只丢该行，不拖垮整块。"""
    path = tmp_path / "ledger.jsonl"
    _write_ledger(path, [_row(worker_id="w1")])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-10-30T03:00:00Z", "worker_id": "w2"')  # 半行
        fh.write("\n")
        fh.write("not json\n")
        fh.write('{"ts": "2026-10-30T04:00:00Z"}\n')  # 缺 worker_id，无幂等身份
    rows = ledger_db.parse_ledger(path)
    assert len(rows) == 1
    assert rows[0]["worker_id"] == "w1"


def test_parse_rejects_non_finite_numbers(tmp_path):
    """NaN / Infinity 不是有效测量值：放进聚合会把复杂度判 high、成本污染成 nan。"""
    path = tmp_path / "ledger.jsonl"
    _write_ledger(path, [_row(
        worker_id="w1",
        complexity_score=float("nan"),
        cost=float("inf"),
        pr=float("nan"),
        tokens={"input": float("nan")},
    )])
    row = ledger_db.parse_ledger(path)[0]
    assert row["complexity_score"] is None
    assert row["cost"] is None
    assert row["pr"] is None
    assert row["tokens"] is None


def test_parse_missing_file_raises(tmp_path):
    with pytest.raises(ops_parse.OpsParseError):
        ledger_db.parse_ledger(tmp_path / "nope.jsonl")


def test_parse_unparseable_file_raises(tmp_path):
    """整份都不是账本行 → 降级为「数据源不可用」，而不是渲染 0。"""
    path = tmp_path / "ledger.jsonl"
    path.write_text("not json\nstill not json\n", encoding="utf-8")
    with pytest.raises(ops_parse.OpsParseError):
        ledger_db.parse_ledger(path)


def test_empty_file_is_empty_not_unavailable(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text("", encoding="utf-8")
    assert ledger_db.parse_ledger(path) == []


# ============================================================
# 2. 幂等 upsert
# ============================================================
def _count_rows(db_path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM dispatch_ledger").fetchone()[0]
    finally:
        conn.close()


def test_upsert_is_idempotent(tmp_path):
    """每 15 分钟渲染一次 + scp 重推同一份账本 → 行数不能增长。"""
    path = tmp_path / "ledger.jsonl"
    rows = [_row(worker_id="w1"), _row(worker_id="w2", ts="2026-10-30T03:00:00Z")]
    _write_ledger(path, rows)
    parsed = ledger_db.parse_ledger(path)
    db_path = tmp_path / "dash.db"

    for _ in range(3):
        ledger_db.upsert_dispatch_ledger(db_path, parsed)
    assert _count_rows(db_path) == 2
    assert sum(g["dispatches"] for g in ledger_db.aggregate_dispatch_ledger(db_path)) == 2


def test_upsert_same_key_last_wins(tmp_path):
    """同一 (worker_id, ts) 的修订行覆盖旧值，不新增。"""
    db_path = tmp_path / "dash.db"
    ledger_db.upsert_dispatch_ledger(db_path, [
        ledger_db._normalise(_row(worker_id="w1", model_effective="old",
                                  tokens={"input": 1}, cost=0.01)),
        ledger_db._normalise(_row(worker_id="w1", model_effective="new",
                                  tokens={"input": 9}, cost=0.09)),
    ])
    assert _count_rows(db_path) == 1
    group = ledger_db.aggregate_dispatch_ledger(db_path)[0]
    assert group["model"] == "new"
    assert group["tokens"] == 9


# ============================================================
# 3. 聚合
# ============================================================
@pytest.mark.parametrize(
    ("score", "band"),
    [
        (None, "unknown"),
        (0, "low"),
        (3.99, "low"),
        (4.0, "mid"),
        (6.49, "mid"),
        (6.5, "high"),
        (9.9, "high"),
    ],
)
def test_complexity_band_boundaries(score, band):
    assert ledger_db.complexity_band(score) == band


def test_aggregate_groups_by_role_model_and_band(tmp_path):
    groups = _aggregate(tmp_path / "l.jsonl", [
        _row(worker_id="w1", role="developer", model_effective="m1", complexity_score=6.7),
        _row(worker_id="w2", role="developer", model_effective="m1", complexity_score=4.2),
        _row(worker_id="w3", role="reviewer", model_effective="m1", complexity_score=6.7),
    ])
    keys = {(g["role"], g["model"], g["band"]) for g in groups}
    assert keys == {
        ("developer", "m1", "high"),
        ("developer", "m1", "mid"),
        ("reviewer", "m1", "high"),
    }


def test_aggregate_skips_null_tokens_and_cost(tmp_path):
    """历史降级行（tokens / cost 为 null）跳过求和，并计入「无数据行数」。"""
    groups = _aggregate(tmp_path / "l.jsonl", [
        _row(worker_id="w1", tokens={"input": 10, "output": 5}, cost=0.2),
        _row(worker_id="w2", tokens=None, cost=None),
        _row(worker_id="w3", tokens=None, cost=None),
    ])
    group = groups[0]
    assert group["dispatches"] == 3
    assert group["tokens"] == 15, "null 行不能当 0 参与也不该污染合计"
    assert group["cost"] == pytest.approx(0.2)
    assert group["cost_rows"] == 1
    assert group["null_rows"] == 2


def test_aggregate_counts_retries_per_issue(tmp_path):
    """同 issue 多行 dispatch → 重派 = 行数 - 唯一 issue 数。"""
    groups = _aggregate(tmp_path / "l.jsonl", [
        _row(worker_id="w1", issue=92),
        _row(worker_id="w2", issue=92, ts="2026-10-30T03:00:00Z"),
        _row(worker_id="w3", issue=91, ts="2026-10-30T04:00:00Z"),
    ])
    group = groups[0]
    assert group["dispatches"] == 3
    assert group["issue_rows"] == 3
    assert group["retries"] == 1


def test_aggregate_unknown_role_and_model_are_explicit(tmp_path):
    groups = _aggregate(tmp_path / "l.jsonl", [
        _row(worker_id="w1", role=None, model_effective=None),
    ])
    assert groups[0]["role"] == "unknown"
    assert groups[0]["model"] == "unknown"


# ============================================================
# 4. 渲染
# ============================================================
def _groups_for_render():
    return ledger_db.aggregate_rows([
        ledger_db._normalise(_row(worker_id="w1", pr=93)),
        ledger_db._normalise(_row(worker_id="w2", ts="2026-10-30T03:00:00Z", pr=93,
                                  tokens=None, cost=None)),
        ledger_db._normalise(_row(
            worker_id="w3", ts="2026-10-30T04:00:00Z", role="reviewer",
            model_effective="other", complexity_score=3.0, pr=94,
            tokens={"input": 20}, cost=0.5,
        )),
    ])


def test_render_eval_block_joins_pr_status_and_cycle():
    groups = _groups_for_render()
    html = render_ops.render_eval_block(
        groups=groups,
        prs=[
            {"number": 93, "state": "MERGED", "merged_ts": "2026-10-30T05:00:00Z"},
            {"number": 94, "state": "OPEN"},
        ],
    )
    assert "模型效能评估" in html
    assert "已合并 1 · 开放 0 · 关闭未合并 0" in html  # PR #93 的 title
    assert "已合并 0 · 开放 1 · 关闭未合并 0" in html  # PR #94 的 title
    assert "数据覆盖率" in html
    assert "总派单" in html
    assert "窗口内总成本" in html
    # 覆盖率 = 2/3 行有 tokens
    assert "67%（2/3 行有 tokens）" in html


def test_render_eval_block_pr_fetch_failed_shows_dash():
    html = render_ops.render_eval_block(
        groups=_groups_for_render(), prs=[], pr_data_ok=False,
    )
    assert "Gitea PR 拉取失败" in html


def test_render_eval_block_unavailable_shows_source_message():
    html = render_ops.render_eval_block(groups=[], prs=[], unavailable=True)
    assert "数据源不可用" in html
    assert "派单账本" in html


def test_render_eval_block_escapes_user_controlled_fields():
    groups = ledger_db.aggregate_rows([
        ledger_db._normalise(_row(worker_id="w1", role="<script>alert(1)</script>",
                                  model_effective="m&m")),
    ])
    html = render_ops.render_eval_block(groups=groups, prs=[])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "m&amp;m" in html


def test_ops_block_renders_eval_section_only_when_wired():
    token_rows = [{"day": "2026-10-30", "source": "webhook", "input_tok": 10}]
    without = render_ops.render_ops_block(
        events=[], prs=[], verifications=[], token_rows=token_rows,
    )
    assert "模型效能评估" not in without, "未接线时不应凭空多出空壳区块"

    with_eval = render_ops.render_ops_block(
        events=[], prs=[], verifications=[], token_rows=token_rows,
        eval_groups=_groups_for_render(),
    )
    assert "模型效能评估" in with_eval
    assert '<span class="no">6</span>模型效能评估' in with_eval
    assert '<span class="no">5</span>异常归因' in with_eval, "原有章节号不得被打乱"


def test_ops_block_shows_eval_unavailable_without_crashing_others():
    html = render_ops.render_ops_block(
        events=[], prs=[], verifications=[],
        token_rows=[{"day": "2026-10-30", "source": "cron", "input_tok": 1}],
        eval_groups=[], eval_unavailable=True,
    )
    assert "数据源不可用" in html
    assert "Token 用量" in html, "账本坏了不能连累其他区块"


def test_full_render_includes_eval_block():
    ri = render.RenderInput(
        created_at="2026-10-30T12:00:00+08:00",
        trigger_kind="manual",
        trigger_event_id=None,
        head_sha=None,
        origin_main_sha=None,
        summary={"open_issues": 0, "merged_7d_total": 0, "worktrees_total": 0,
                 "closed_unmerged": 0},
        eval_groups=_groups_for_render(),
    )
    html = render.render(ri)
    assert "模型效能评估" in html
    assert "窗口内总成本" in html
