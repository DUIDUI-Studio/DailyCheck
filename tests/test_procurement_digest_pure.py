"""周度采购建议邮件 —— 纯渲染层单测（Issue #145）。

锁四件事：
  1. 只挑「DC + 可订 + 有建议量 + 到点/断货」的行（门店行、自采行不得混入）；
  2. 每行必须带上各门店库存与近 7 天消耗，缺数据要显式说明而不是填 0 蒙混；
  3. 纯文本 / HTML 两种形态都包含决策所需四个数字；
  4. 品项名来自数据库，渲染必须转义（防 HTML 注入）。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from blueprints import procurement_digest_pure as P
from blueprints.procurement_digest_pure import (
    build_digest,
    build_subject,
    fmt_cover,
    fmt_money,
    fmt_qty,
    pick_purchase_rows,
    render_html,
    render_text,
)
from blueprints.replenishment_pure import STATUS_CRITICAL, STATUS_DUE, STATUS_HEALTHY

DAY = date(2026, 10, 27)  # 周二


def _row(
    *,
    name="番茄酱",
    qty=0.0,
    suggested=12.0,
    status=STATUS_CRITICAL,
    warehouse_type="distribution_center",
    orderable=True,
    canonical_id=101,
    value=None,
    unit="箱",
    cover=1.5,
):
    return {
        "name": name,
        "sku": f"SKU-{name}",
        "unit": unit,
        "category": "调味酱",
        "abc": "A",
        "warehouse_type": warehouse_type,
        "warehouse_code": "dc_001" if warehouse_type == "distribution_center" else "s1",
        "status": status,
        "orderable": orderable,
        "canonical_id": canonical_id,
        "quantity": qty,
        "on_order": 0.0,
        "rate": 4.0,
        "suggested_qty": suggested,
        "suggested_value": 100.0 if value is None else value,
        "cover_days": cover,
    }


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------


def test_fmt_qty_strips_trailing_zero():
    assert fmt_qty(12.0) == "12"
    assert fmt_qty(1.50) == "1.5"
    assert fmt_qty(0.333) == "0.33"
    assert fmt_qty(None) == "—"
    assert fmt_qty("abc") == "—"


def test_fmt_money_adds_thousands_separator():
    assert fmt_money(1234.5) == "1,234.50"
    assert fmt_money(None) == "—"


def test_fmt_cover_handles_no_signal():
    assert fmt_cover(3.26) == "3.3 天"
    assert fmt_cover(None) == "无消耗信号"


# ---------------------------------------------------------------------------
# 选行
# ---------------------------------------------------------------------------


def test_pick_only_dc_orderable_action_rows():
    rows = [
        _row(name="该采", status=STATUS_CRITICAL),
        _row(name="门店行", warehouse_type="storefront"),
        _row(name="自采", orderable=False),
        _row(name="正常", status=STATUS_HEALTHY),
        _row(name="零建议", suggested=0.0),
    ]
    picked = pick_purchase_rows(rows)
    assert [r["name"] for r in picked] == ["该采"]


def test_pick_sorts_critical_before_due():
    rows = [
        _row(name="到点大额", status=STATUS_DUE, value=9999.0),
        _row(name="断货小额", status=STATUS_CRITICAL, value=10.0),
        _row(name="到点小额", status=STATUS_DUE, value=1.0),
    ]
    picked = pick_purchase_rows(rows)
    assert [r["name"] for r in picked] == ["断货小额", "到点大额", "到点小额"]


def test_pick_ignores_malformed_qty():
    assert pick_purchase_rows([_row(name="坏数", suggested="不是数")]) == []


def test_pick_is_fail_closed_when_orderable_missing():
    """缺 orderable 字段 = 结构变了，此时按「可订」放行会订错货。"""
    row = _row()
    row.pop("orderable")
    assert pick_purchase_rows([row]) == []


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def _digest(rows, **kwargs):
    params = {
        "day": DAY,
        "rows": rows,
        "store_stock": {101: {"s1": 5.0, "s2": 2.0}},
        "consumption_7d": {101: 28.0},
        "stores": [{"code": "s1", "name": "一号店"}, {"code": "s2", "name": "二号店"}],
        "dc_name": "中央配送中心",
        "generated_at": datetime(2026, 10, 27, 8, 0),
    }
    params.update(kwargs)
    return build_digest(**params)


def test_digest_merges_store_stock_and_consumption():
    digest = _digest([_row(qty=3.0)])
    item = digest["items"][0]
    assert item["dc_stock"] == 3.0
    assert item["consumption_7d"] == 28.0
    assert item["store_total"] == 7.0
    assert [s["qty"] for s in item["stores"]] == [5.0, 2.0]
    assert digest["total_value"] == pytest.approx(100.0)


def test_digest_marks_unbound_canonical_and_keeps_store_cells_empty():
    digest = _digest([_row(canonical_id=None)])
    item = digest["items"][0]
    assert item["has_canonical"] is False
    assert item["stores"] == []
    assert item["consumption_7d"] == 0.0


def test_digest_store_missing_in_map_defaults_to_zero():
    """该店库读得出来、但没这个品项 → 0（确实没货）。"""
    digest = _digest(
        [_row()],
        store_stock={101: {"s1": 5.0}},
        stores=[{"code": "s1", "name": "一号店"}, {"code": "s3", "name": "三号店"}],
    )
    assert [s["qty"] for s in digest["items"][0]["stores"]] == [5.0, 0.0]


def test_failed_store_renders_dash_not_zero():
    """M6 回归：整库读取失败必须显示「—」，显示 0 会让采购员按缺货下单。"""
    digest = _digest([_row()], store_stock={101: {"s1": 5.0}}, failed_store_codes=["s2"])
    item = digest["items"][0]
    assert [s["qty"] for s in item["stores"]] == [5.0, None]
    assert item["store_total"] == pytest.approx(5.0)  # 只累加有数的店
    assert "二号店 —" in render_text(digest)
    assert "—" in render_html(digest)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_subject_reports_counts_and_amount():
    digest = _digest([_row(), _row(name="白糖")])
    subject = build_subject(digest)
    assert "2026-10-27" in subject and "周二" in subject
    assert "2 项" in subject
    assert "200.00" in subject


def test_subject_for_empty_digest_says_no_purchase():
    digest = _digest([])
    assert "无需采购" in build_subject(digest)


def test_subject_distinguishes_data_failure_from_no_purchase():
    """M4 回归：数据故障不能在主题里伪装成「本周不用买」。"""
    no_dc = build_subject(_digest([], dc_configured=False))
    assert "未生成" in no_dc and "无需采购" not in no_dc

    failed = build_subject(_digest([], failed_store_codes=["s2"]))
    assert "数据不全" in failed and "无需采购" not in failed


def test_render_text_contains_all_four_answers():
    digest = _digest([_row(qty=3.0)])
    text = render_text(digest)
    assert "建议采购 12 箱" in text
    assert "DC 库存 3" in text
    assert "近 7 天消耗 28" in text
    assert "一号店 5" in text and "二号店 2" in text


def test_render_text_empty_and_truncated_notice():
    empty = render_text(_digest([]))
    assert "本周没有需要采购的品项" in empty

    rows = [_row(name=f"品{i}") for i in range(3)]
    text = render_text(_digest(rows, limit=1))
    # 主题说「另有 2 项」，且金额要说清被省略的那部分值多少钱
    assert "另有 2 项" in text
    assert "¥200.00）" in text


def test_truncation_keeps_total_totals_not_listed_totals():
    """H1 回归：主题与摘要的总数/总额必须是**全量**，不能是被截断后的部分。"""
    rows = [_row(name=f"品{i}") for i in range(30)]
    digest = _digest(rows, limit=20)

    assert digest["item_count"] == 20
    assert digest["item_count_total"] == 30
    assert digest["truncated"] == 10
    assert digest["total_value"] == pytest.approx(3000.0)   # 30 × 100
    assert digest["listed_value"] == pytest.approx(2000.0)  # 20 × 100

    subject = build_subject(digest)
    assert "30 项" in subject and "列出 20" in subject
    assert "3,000.00" in subject

    text = render_text(digest)
    assert "本周需采购 30 项" in text
    assert "3,000.00" in text


def test_render_text_includes_warnings():
    text = render_text(_digest([_row()], warnings=["仓「二号店」库文件缺失"]))
    assert "仓「二号店」库文件缺失" in text


def test_render_html_escapes_item_name():
    digest = _digest([_row(name="<script>alert(1)</script>")])
    html = render_html(digest)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_html_renders_table_and_stores():
    html = render_html(_digest([_row()]))
    assert "配送中心采购建议" in html
    assert "建议采购" in html and "近 7 天消耗" in html
    assert "一号店" in html
    assert "本邮件由 DailyCheck 每周二自动发送" in html


def test_render_html_empty_digest_has_no_table():
    html = render_html(_digest([]))
    assert "<table" not in html
    assert "本周没有需要采购的品项" in html


def test_module_exposes_dc_type_constant():
    assert P.DC_TYPE == "distribution_center"
