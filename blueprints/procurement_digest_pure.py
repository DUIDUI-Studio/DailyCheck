"""周度采购建议邮件 —— 纯渲染层（零 I/O、零 sqlite、零 smtplib）。

职责边界
--------
`procurement_digest.py` 负责取数（跨仓 sqlite）与投递（SMTP），本模块只把
**已经算好的 dict** 变成邮件主题 / 纯文本 / HTML。这样渲染可以单测，
不依赖 Flask app、数据库或网络。

口径（Issue #145）
------------------
邮件正文必须回答四件事，缺一不可：

1. **哪些需要采购** —— 配送中心（DC）本周该向供应商下的品项；
2. **采购数量** —— 看板算出来的 `suggested_qty`（已按起订单位取整）；
3. **这些品项在各家门店的库存** —— 按 canonical_id 跨门店取；
4. **最近 7 天消耗量** —— 门店侧 7 天实际消耗合计，不是日均 × 7。

为什么不是「所有 status=critical/due 的行」
------------------------------------------
* 门店行是「向 DC 订货」，DC 行才是「向供应商采购」，两者是同一批货的两层，
  混在一张采购单里会让数量翻倍（见 replenishment_data 的 scope 语义）。
* `is_orderable=0` 的品项已改门店自采，DC 不再统配 —— 采购建议里出现它们
  会直接导致多订一批没人要的货。
所以入口 `pick_purchase_rows()` 只放行「DC + 可订 + 有建议量 + 到点/断货」。

Spec: docs/ARCHITECTURE.md §3.11 周度采购建议邮件。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from .replenishment_pure import (
    STATUS_CRITICAL,
    STATUS_DUE,
    STATUS_LABELS,
)

DC_TYPE = "distribution_center"

# 采购建议只认这两种状态：healthy 不该买，overstock 更不该买。
ACTION_STATUSES = (STATUS_CRITICAL, STATUS_DUE)

# 邮件里最多列这么多行，其余折成「另有 N 项」。20 行 ≈ 一屏半，
# 再多就没人看了 —— 完整明细走看板链接。
DEFAULT_LIMIT = 20

_STATUS_TONE = {
    STATUS_CRITICAL: "#b91c1c",
    STATUS_DUE: "#b45309",
}


# ---------------------------------------------------------------------------
# 数值格式化
# ---------------------------------------------------------------------------


def fmt_qty(value: Any, digits: int = 2) -> str:
    """12.0 → "12"；1.50 → "1.5"；None / 非数 → "—"。

    数量在邮件里给人看，整数不该显示成 "12.0"；小数最多两位。
    """
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return "—"
    if number == int(number):
        return str(int(number))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def fmt_money(value: Any) -> str:
    """1234.5 → "1,234.50"；None / 非数 → "—"。"""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number != number:
        return "—"
    return f"{number:,.2f}"


def fmt_cn_date(day: date) -> str:
    """2026-10-27 → "2026-10-27（周二）"。"""
    weekday = "一二三四五六日"[day.weekday()]
    return f"{day.strftime('%Y-%m-%d')}（周{weekday}）"


def fmt_cover(days: Any) -> str:
    """覆盖天数：0.9 → "0.9 天"；None → "无消耗信号"。"""
    if days is None:
        return "无消耗信号"
    text = fmt_qty(days, 1)
    return "—" if text == "—" else f"{text} 天"


# ---------------------------------------------------------------------------
# 选行
# ---------------------------------------------------------------------------


def pick_purchase_rows(rows: Iterable[dict], limit: int | None = DEFAULT_LIMIT) -> tuple[list[dict], int]:
    """从看板 rows 挑出「本周该由 DC 向供应商采购」的行。

    返回 (截断后的行, 被截掉的条数)。排序：断货风险优先，其次建议金额降序。
    缺 `suggested_qty` / `warehouse_type` 的行直接跳过 —— 宁可少列，
    也不把来路不明的行塞进采购单。
    """
    picked: list[dict] = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        if row.get("warehouse_type") != DC_TYPE:
            continue
        if row.get("status") not in ACTION_STATUSES:
            continue
        if not row.get("orderable", True):
            continue
        try:
            qty = float(row.get("suggested_qty") or 0.0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        picked.append(row)

    picked.sort(key=lambda r: (
        0 if r.get("status") == STATUS_CRITICAL else 1,
        -float(r.get("suggested_value") or 0.0),
        str(r.get("name") or ""),
    ))

    if limit is None or limit <= 0:
        return picked, 0
    return picked[:limit], max(0, len(picked) - limit)


def _store_cell(stores: Sequence[dict], stock: dict | None) -> list[dict]:
    """把某品项在各门店的库存整理成 [{name, qty}]，缺数按 0。"""
    out: list[dict] = []
    for store in stores:
        code = str(store.get("code") or "")
        qty = 0.0
        if isinstance(stock, dict):
            try:
                qty = float(stock.get(code) or 0.0)
            except (TypeError, ValueError):
                qty = 0.0
        out.append({"name": str(store.get("name") or code or "未命名门店"), "qty": qty})
    return out


def build_digest(
    *,
    day: date,
    rows: Iterable[dict],
    store_stock: dict[int, dict[str, float]] | None = None,
    consumption_7d: dict[int, float] | None = None,
    stores: Sequence[dict] | None = None,
    dc_name: str = "配送中心",
    window_days: int = 7,
    limit: int | None = DEFAULT_LIMIT,
    warnings: Sequence[str] | None = None,
    generated_at: datetime | None = None,
) -> dict:
    """装配成可直接渲染的 digest dict（纯函数，输入什么就渲染什么）。

    `rows` 是看板 `build_board()` 的 `rows`（含所有仓、所有状态）；
    这里只挑 DC 的采购动作行，再为每行补齐门店库存与 7 天消耗。
    """
    store_stock = store_stock or {}
    consumption_7d = consumption_7d or {}
    stores = list(stores or ())

    picked, truncated = pick_purchase_rows(rows, limit=limit)

    items: list[dict] = []
    total_value = 0.0
    for row in picked:
        cid = row.get("canonical_id")
        try:
            cid_key = int(cid) if cid is not None else None
        except (TypeError, ValueError):
            cid_key = None
        stock = store_stock.get(cid_key) if cid_key is not None else None
        # 未绑主数据（canonical_id 为空）就没有跨店汇总的键 —— 不能拿 0 冒充满
        # 库存，那会让「这家店确实没货」和「这品项根本对不上店」看起来一样。
        store_rows = _store_cell(stores, stock) if cid_key is not None else []
        consumed = 0.0
        if cid_key is not None:
            try:
                consumed = float(consumption_7d.get(cid_key) or 0.0)
            except (TypeError, ValueError):
                consumed = 0.0
        value = float(row.get("suggested_value") or 0.0)
        total_value += value
        items.append({
            "name": str(row.get("name") or ""),
            "sku": str(row.get("sku") or ""),
            "unit": str(row.get("unit") or ""),
            "category": str(row.get("category") or "未分类"),
            "abc": str(row.get("abc") or ""),
            "status": str(row.get("status") or ""),
            "status_label": STATUS_LABELS.get(str(row.get("status") or ""), "—"),
            "status_tone": _STATUS_TONE.get(str(row.get("status") or ""), "#334155"),
            "suggested_qty": float(row.get("suggested_qty") or 0.0),
            "dc_stock": float(row.get("quantity") or 0.0),
            "dc_on_order": float(row.get("on_order") or 0.0),
            "consumption_7d": consumed,
            "cover_days": row.get("cover_days"),
            "value": value,
            "stores": store_rows,
            "store_total": sum(s["qty"] for s in store_rows),
            "has_canonical": cid_key is not None,
        })

    return {
        "day": day,
        "date_label": fmt_cn_date(day),
        "generated_at": (generated_at or datetime.now()).strftime("%Y-%m-%d %H:%M"),
        "dc_name": dc_name,
        "window_days": int(window_days),
        "items": items,
        "item_count": len(items),
        "truncated": truncated,
        "total_value": round(total_value, 2),
        "stores": [{"code": str(s.get("code") or ""), "name": str(s.get("name") or "")} for s in stores],
        "warnings": [w for w in (warnings or ()) if w],
    }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def build_subject(digest: dict) -> str:
    """邮件主题。空清单也要能一眼看出「这周不用买」。"""
    label = digest.get("date_label") or ""
    count = int(digest.get("item_count") or 0)
    if count <= 0:
        return f"【采购建议】{label} 无需采购"
    return (
        f"【采购建议】{label} {count} 项"
        f" · 参考金额 ¥{fmt_money(digest.get('total_value'))}"
    )


def _store_line(item: dict) -> str:
    stores = item.get("stores") or []
    if not stores:
        return "未绑定主数据，无法跨店汇总"
    return "、".join(f"{s['name']} {fmt_qty(s['qty'])}" for s in stores)


def render_text(digest: dict) -> str:
    """纯文本版（不支持 HTML 的客户端 / 手机通知预览用）。"""
    lines: list[str] = [
        f"配送中心采购建议 · {digest.get('date_label', '')}",
        "=" * 34,
        f"配送中心：{digest.get('dc_name', '配送中心')}",
        f"数据口径：近 {digest.get('window_days', 7)} 天门店实际消耗",
        f"生成时间：{digest.get('generated_at', '')}",
        "",
    ]

    items = digest.get("items") or []
    if not items:
        lines.append("本周没有需要采购的品项（无断货风险 / 无到点补货）。")
    else:
        lines.append(
            f"本周需采购 {digest.get('item_count', 0)} 项，"
            f"参考金额 ¥{fmt_money(digest.get('total_value'))}。"
        )
        lines.append("")
        for idx, item in enumerate(items, start=1):
            unit = item.get("unit") or ""
            lines.append(
                f"{idx}. {item.get('name') or item.get('sku')}"
                f"　建议采购 {fmt_qty(item.get('suggested_qty'))} {unit}"
                f"｜DC 库存 {fmt_qty(item.get('dc_stock'))}"
                f"｜近 7 天消耗 {fmt_qty(item.get('consumption_7d'))}"
                f"｜{item.get('status_label')}"
                f"｜可覆盖 {fmt_cover(item.get('cover_days'))}"
            )
            lines.append(f"   门店库存：{_store_line(item)}")
        if digest.get("truncated"):
            lines.append("")
            lines.append(
                f"另有 {digest['truncated']} 项已省略，完整清单见系统「补货看板」。"
            )

    warnings = digest.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("数据说明：")
        for text in warnings:
            lines.append(f"- {text}")

    lines.append("")
    lines.append("本邮件由系统每周二自动发送；如需调整收件人或发送时间请联系管理员。")
    return "\n".join(lines)


def _html_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def render_html(digest: dict) -> str:
    """HTML 版。内联样式 + table 布局：邮件客户端（尤其企微/Outlook）
    对 <style> 与 flex 支持不一致，只有 inline style 是安全区。"""
    items = digest.get("items") or []
    rows_html: list[str] = []
    for idx, item in enumerate(items, start=1):
        tone = item.get("status_tone") or "#334155"
        stores = item.get("stores") or []
        if stores:
            store_text = "<br>".join(
                f"{_html_escape(s['name'])} <b>{fmt_qty(s['qty'])}</b>" for s in stores
            )
        else:
            store_text = '<span style="color:#94a3b8">未绑定主数据</span>'
        rows_html.append(
            "<tr>"
            f'<td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:13px;color:#0f172a">'
            f"{idx}. {_html_escape(item.get('name'))}"
            f'<div style="color:#94a3b8;font-size:11px">{_html_escape(item.get("sku"))}'
            f"　{_html_escape(item.get('category'))}</div></td>"
            f'<td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;'
            f'font-size:14px;font-weight:600;color:#0f172a;white-space:nowrap">'
            f"{fmt_qty(item.get('suggested_qty'))} {_html_escape(item.get('unit'))}</td>"
            f'<td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;'
            f'font-size:13px;color:#334155;white-space:nowrap">{fmt_qty(item.get("dc_stock"))}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;'
            f'font-size:13px;color:#334155;white-space:nowrap">{fmt_qty(item.get("consumption_7d"))}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:12px;'
            f'color:#475569">{store_text}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:12px;'
            f'color:{tone};white-space:nowrap">{_html_escape(item.get("status_label"))}'
            f'<div style="color:#94a3b8;font-size:11px">{_html_escape(fmt_cover(item.get("cover_days")))}</div></td>'
            "</tr>"
        )

    if items:
        summary = (
            f"本周需采购 <b>{digest.get('item_count', 0)}</b> 项，"
            f"参考金额 <b>¥{fmt_money(digest.get('total_value'))}</b>"
        )
        table = (
            '<table style="width:100%;border-collapse:collapse;margin-top:12px">'
            '<thead><tr style="background:#f8fafc">'
            '<th style="padding:8px 10px;text-align:left;font-size:12px;color:#64748b;font-weight:600">品项</th>'
            '<th style="padding:8px 10px;text-align:right;font-size:12px;color:#64748b;font-weight:600">建议采购</th>'
            '<th style="padding:8px 10px;text-align:right;font-size:12px;color:#64748b;font-weight:600">DC 库存</th>'
            '<th style="padding:8px 10px;text-align:right;font-size:12px;color:#64748b;font-weight:600">近 7 天消耗</th>'
            '<th style="padding:8px 10px;text-align:left;font-size:12px;color:#64748b;font-weight:600">各门店库存</th>'
            '<th style="padding:8px 10px;text-align:left;font-size:12px;color:#64748b;font-weight:600">状态</th>'
            "</tr></thead><tbody>"
            + "".join(rows_html)
            + "</tbody></table>"
        )
        if digest.get("truncated"):
            table += (
                f'<p style="color:#64748b;font-size:12px;margin:8px 0 0">'
                f"另有 {digest['truncated']} 项已省略，完整清单见系统「补货看板」。</p>"
            )
    else:
        summary = "本周没有需要采购的品项（无断货风险 / 无到点补货）。"
        table = ""

    warnings = digest.get("warnings") or []
    warning_html = ""
    if warnings:
        warning_html = (
            '<div style="margin-top:16px;padding:10px 12px;background:#fffbeb;'
            'border:1px solid #fde68a;border-radius:6px">'
            '<div style="font-size:12px;color:#92400e;font-weight:600;margin-bottom:4px">数据说明</div>'
            '<ul style="margin:0;padding-left:18px;color:#92400e;font-size:12px">'
            + "".join(f"<li>{_html_escape(w)}</li>" for w in warnings)
            + "</ul></div>"
        )

    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        "'Microsoft YaHei',sans-serif;max-width:860px;margin:0 auto;color:#0f172a\">"
        f'<h2 style="font-size:18px;margin:0 0 4px">配送中心采购建议 · {_html_escape(digest.get("date_label"))}</h2>'
        f'<p style="color:#64748b;font-size:12px;margin:0 0 12px">'
        f'{_html_escape(digest.get("dc_name"))}　·　数据口径：近 {digest.get("window_days", 7)} 天门店实际消耗　·　'
        f'生成于 {_html_escape(digest.get("generated_at"))}</p>'
        f'<p style="font-size:14px;margin:0 0 4px">{summary}</p>'
        f"{table}{warning_html}"
        '<p style="color:#94a3b8;font-size:11px;margin-top:20px;border-top:1px solid #e2e8f0;padding-top:10px">'
        "本邮件由 DailyCheck 每周二自动发送。采购数量已按起订单位取整；"
        "门店自采品项（DC 不统配）不在本清单内。</p>"
        "</div>"
    )
