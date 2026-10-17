"""scripts.dailycheck_dashboard.render_ops —— 运维区块（KPI + 瀑布 + token + 验证矩阵）。

只产出 HTML 片段，由 render.py 注入主模板的两处占位符（${ops_css} / ${ops_html}）。
不 import render.py（避免循环依赖），_esc 在此本地实现。
"""
from __future__ import annotations

import html
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# 瀑布图横轴上限（分钟）：超过则整行按上限截断并标注
AXIS_MAX_MIN = 90
MAX_WATERFALL_ROWS = 6
MAX_MATRIX_ROWS = 6

# source → (中文名, 颜色 class)
SOURCE_LABELS = {
    "webhook": ("部署会话", "acc"),
    "cron": ("定时巡检", "warn"),
    "feishu": ("飞书对话", "teal"),
    "cli": ("命令行", "gray"),
    "unknown": ("未知", "gray"),
}

# 图例用的实色（与 .tk .* 的 CSS 保持一致）
SOURCE_COLORS = {
    "webhook": "#2f6fed",
    "cron": "#f59e0b",
    "feishu": "#0f766e",
    "cli": "#c7d2e4",
    "unknown": "#c7d2e4",
}


def _esc(s: Any) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def _fmt_sec(sec: int | None) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    if sec < 0:
        return "—"
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}:{s:02d}"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _fmt_tok(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _cycle_sec(pr: dict) -> int | None:
    a = _parse_iso(pr.get("created_ts"))
    b = _parse_iso(pr.get("merged_ts"))
    if a is None or b is None:
        return None
    return max(0, int((b - a).total_seconds()))


# ============================================================
# CSS（注入主模板 </style> 之前）
# ============================================================
OPS_CSS = """
    .ops-kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin:14px 0 4px}
    .ops-kpi{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:11px 13px}
    .ops-kpi .ov{font-size:21px;font-weight:700;line-height:1.15;font-variant-numeric:tabular-nums}
    .ops-kpi .ov small{font-size:12px;font-weight:500;color:var(--t2);margin-left:1px}
    .ops-kpi .ok{font-size:11.5px;color:var(--t2);margin-top:2px}
    .ops-kpi.hot .ov{color:var(--warn)}
    .ops-kpi.bad .ov{color:var(--err)}
    .wf-row{display:grid;grid-template-columns:58px 1fr 62px;gap:9px;align-items:center;padding:6px 0}
    .wf-row+.wf-row{border-top:1px solid #f1f2f4}
    .wf-l{font-size:12px;color:var(--t2);font-variant-numeric:tabular-nums}
    .wf-bar{display:flex;height:14px;border-radius:3px;overflow:hidden;background:#f3f5f8}
    .wf-bar i{display:block;height:100%}
    .wf-bar .seg-ci{background:#c7d2e4}
    .wf-bar .seg-up{background:#93c5fd}
    .wf-bar .seg-verify{background:#f59e0b}
    .wf-bar .seg-promote{background:#22c55e}
    .wf-bar .seg-stall{background:repeating-linear-gradient(45deg,#ef4444,#ef4444 2px,transparent 2px,transparent 4px)}
    .wf-t{font-size:12px;text-align:right;font-variant-numeric:tabular-nums}
    .lg{display:flex;flex-wrap:wrap;gap:12px;margin-top:11px;padding-top:10px;border-top:1px solid var(--line);font-size:11.5px;color:var(--t2)}
    .lg span{display:flex;align-items:center;gap:5px}
    .lg i{width:9px;height:9px;border-radius:2px;display:inline-block}
    .cols2{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:12px}
    .tk{display:flex;align-items:flex-end;gap:8px;height:118px;margin-top:12px}
    .tk-c{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;gap:1px;min-width:0}
    .tk-c b{display:block;border-radius:1px 1px 0 0}
    .tk-x{display:flex;gap:8px;margin-top:5px}
    .tk-x span{flex:1;text-align:center;font-size:10.5px;color:var(--t3)}
    .tk .acc{background:var(--accent)}.tk .warn{background:#f59e0b}.tk .teal{background:#0f766e}.tk .gray{background:#c7d2e4}
    .mx{width:100%;border-collapse:collapse;font-size:12px;border:0}
    .mx th{background:transparent;text-align:center;font-size:10.5px;color:var(--t3);font-weight:500;padding:0 0 5px}
    .mx th:first-child{text-align:left}
    .mx td{padding:4px 0;text-align:center;border-top:1px solid #f4f5f7}
    .mx td:first-child{text-align:left;color:var(--t2);font-variant-numeric:tabular-nums}
    .pip{display:inline-block;width:9px;height:9px;border-radius:50%}
    .pip.ok{background:#22c55e}.pip.no{background:#ef4444}.pip.na{background:#e5e7eb}
    .al{list-style:none;margin:0;padding:0;font-size:12.5px}
    .al li{display:flex;justify-content:space-between;gap:10px;padding:6px 0}
    .al li+li{border-top:1px solid #f4f5f7}
    .al .n{font-variant-numeric:tabular-nums;font-weight:600}
    .al .n.ok{color:var(--ok)}.al .n.hot{color:var(--warn)}.al .n.bad{color:var(--err)}
    .ops-empty{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 15px;font-size:12.5px;color:var(--t2)}
    @media(max-width:1024px){.ops-kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}
    @media(max-width:760px){
        .ops-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
        .cols2{grid-template-columns:1fr}
        .wf-row{grid-template-columns:50px 1fr 54px}
        .wf-bar{height:12px}
    }
    @media(max-width:480px){.ops-kpi .ov{font-size:18px}}
"""


# ============================================================
# 区块
# ============================================================
def render_ops_block(
    *,
    events: list[dict],
    prs: list[dict],
    verifications: list[dict],
    token_rows: list[dict],
    fetched_at: str = "",
    unavailable: list[str] | None = None,
) -> str:
    """返回整段运维区块 HTML（KPI 条 + 瀑布 + token + 验证矩阵 + 归因）。"""
    unavailable = unavailable or []
    pr_by_num = {int(p["number"]): p for p in prs if p.get("number") is not None}

    kpi_html = _render_kpis(events, prs, token_rows)
    body_parts: list[str] = []

    if events:
        body_parts.append(_render_waterfall(events, pr_by_num, verifications))
    if token_rows:
        body_parts.append(_render_tokens(token_rows))
    if verifications:
        body_parts.append(_render_matrix(verifications))

    if not body_parts:
        note = "运维数据源暂不可用"
        if unavailable:
            note += "：" + "、".join(_esc(u) for u in unavailable)
        return (
            kpi_html
            + '<div class="ops-empty">' + note
            + "。仪表盘其余区块不受影响；接入后本区块自动出现。</div>"
        )

    anomaly = _render_anomaly(events, verifications, token_rows)
    stamp = f'<div class="meta"><span><b>运维数据采集：</b>{_esc(fetched_at)}</span>'
    if unavailable:
        stamp += "<span><b>不可用源：</b>" + _esc("、".join(unavailable)) + "</span>"
    stamp += "</div>"
    return kpi_html + "\n".join(body_parts) + anomaly + stamp


# ------------------------------------------------------------ KPI
def _render_kpis(events: list[dict], prs: list[dict], token_rows: list[dict]) -> str:
    # 与 db._now_iso() 同口径：用进程本地时区判「今天」（prod 进程 = CST+8）
    today = time.strftime("%Y-%m-%d")
    cutoff = time.time() - 7 * 86400

    # 交付周期只取近 7 天合并的 PR，且用中位数（#59 那种 572 分钟的离群值会毁掉平均数）
    cycles = sorted(
        c for p in prs
        if (c := _cycle_sec(p)) is not None and (_epoch(p.get("merged_ts")) or 0) >= cutoff
    )
    merged_today = sum(1 for p in prs if _cst_day(p.get("merged_ts")) == today)
    median_cycle = cycles[len(cycles) // 2] if cycles else None

    today_events = [e for e in events if _cst_day(e.get("started_at")) == today]
    verify_secs = [
        seg["sec"] for e in (today_events or events)
        for seg in e.get("segments", [])
        if seg.get("name") == "verify" and seg.get("sec")
    ]
    avg_verify = int(sum(verify_secs) / len(verify_secs)) if verify_secs else None

    tok_today = sum(_row_total(r) for r in token_rows if r.get("day") == today)
    stalls = sum(
        1 for e in events for seg in e.get("segments", []) if seg.get("stalled")
    )
    deploy_count = len(today_events) if today_events else len(events)

    def card(value: str, unit: str, label: str, sub: str, cls: str = "") -> str:
        unit_html = f"<small> {unit}</small>" if unit else ""
        return (
            f'<div class="ops-kpi {cls}"><div class="ov">{value}{unit_html}</div>'
            f'<div class="ok">{_esc(label)}</div>'
            f'<div class="ok" style="color:var(--t3)">{_esc(sub)}</div></div>'
        )

    return (
        '<div class="ops-kpis">'
        + card(str(merged_today), "个", "今日合并 PR", f"近 7 天样本 {len(cycles)} 个")
        + card(_fmt_sec(median_cycle), "", "交付周期（中位）", "创建 → 合并")
        + card(str(deploy_count), "次", "今日部署", f"近 {len(events)} 次有记录")
        + card(_fmt_sec(avg_verify), "", "平均验证耗时", "八项检查")
        + card(_fmt_tok(tok_today), "", "今日 Token", f"近 7 天 {len(token_rows)} 组")
        + card(str(stalls), "处", "部署卡顿", "up/promote 段 > 5 分钟", "bad" if stalls else "")
        + "</div>"
    )


def _epoch(ts: str | None) -> float | None:
    parsed = _parse_iso(ts)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def _cst_day(ts: str | None) -> str:
    parsed = _parse_iso(ts)
    if parsed is None:
        return ""
    return (parsed + timedelta(hours=8)).strftime("%Y-%m-%d")


def _row_total(row: dict) -> int:
    return sum(int(row.get(k) or 0) for k in (
        "input_tok", "output_tok", "cache_read_tok", "cache_write_tok", "reasoning_tok",
    ))


# ------------------------------------------------------------ 瀑布
def _render_waterfall(events: list[dict], pr_by_num: dict, verifications: list[dict]) -> str:
    verif_by_pr = {int(v["pr"]): v for v in verifications}
    rows = events[:MAX_WATERFALL_ROWS]

    prepared = []
    for e in rows:
        pr = int(e["pr"])
        meta = pr_by_num.get(pr, {})
        cycle = _cycle_sec(meta) or 0
        segs = e.get("segments") or []
        total = cycle + int(e.get("total_sec") or 0)
        prepared.append({"e": e, "pr": pr, "cycle": cycle, "segs": segs, "total": total})

    axis = max((p["total"] for p in prepared), default=0) or 1
    axis = min(axis, AXIS_MAX_MIN * 60)

    out = [
        '<h2 id="ops"><span class="no">1</span>交付看板 · 部署流水线',
        '<span class="hint">每行一个 PR：创建→合并→上线；红色斜纹 = 卡顿</span></h2>',
        '<div class="note" style="padding:10px 14px">',
    ]
    for p in prepared:
        e, pr = p["e"], p["pr"]
        segs_html = ""
        if p["cycle"]:
            w = p["cycle"] / axis * 100
            segs_html += f'<i class="seg-ci" style="width:{w:.2f}%" title="创建→合并 {_fmt_sec(p["cycle"])}"></i>'
        for seg in p["segs"]:
            sec = seg.get("sec") or 0
            cls = "seg-stall" if seg.get("stalled") else f"seg-{seg.get('name', 'up')}"
            title = _esc(f"{seg.get('name')} {_fmt_sec(sec)}")
            segs_html += f'<i class="{cls}" style="width:{sec / axis * 100:.2f}%" title="{title}"></i>'
        verif = verif_by_pr.get(pr)
        badge = ""
        if verif:
            ok = verif.get("passed") == verif.get("total")
            badge = (
                f'<span class="b {"ok" if ok else "warn"}" style="margin-left:6px">'
                f'{verif.get("passed")}/{verif.get("total")}</span>'
            )
        out.append(
            f'<div class="wf-row"><div class="wf-l">#{pr}{badge}</div>'
            f'<div class="wf-bar">{segs_html}</div>'
            f'<div class="wf-t">{_fmt_sec(p["total"])}</div></div>'
        )
    out.append("</div>")
    out.append(
        '<div class="lg">'
        '<span><i style="background:#c7d2e4"></i>创建 → 合并（CI + 等待）</span>'
        '<span><i style="background:#93c5fd"></i>up</span>'
        '<span><i style="background:#f59e0b"></i>verify 八项</span>'
        '<span><i style="background:#22c55e"></i>promote + 切流</span>'
        '<span><i style="background:repeating-linear-gradient(45deg,#ef4444,#ef4444 2px,transparent 2px,transparent 4px)"></i>卡顿（up/promote &gt; 5 分钟）</span>'
        "</div>"
    )
    return "".join(out)


# ------------------------------------------------------------ Token
def _render_tokens(token_rows: list[dict]) -> str:
    by_day: dict[str, dict[str, int]] = {}
    for r in token_rows:
        day = r.get("day") or ""
        by_day.setdefault(day, {})
        by_day[day][r.get("source") or "unknown"] = _row_total(r)
    days = sorted(by_day)[-7:]
    if not days:
        return ""
    totals = {d: sum(by_day[d].values()) for d in days}
    peak = max(totals.values()) or 1
    sources = ["webhook", "cron", "feishu"]
    for d in days:
        for s in by_day[d]:
            if s not in sources:
                sources.append(s)

    cols = []
    for d in days:
        stack = ""
        for s in sources:
            v = by_day[d].get(s, 0)
            if not v:
                continue
            h = v / peak * 100
            label, cls = SOURCE_LABELS.get(s, (s, "gray"))
            stack += f'<b class="{cls}" style="height:{h:.1f}%" title="{_esc(label)} {_fmt_tok(v)}"></b>'
        cols.append(f'<div class="tk-c">{stack}</div>')
    axis = "".join(
        f"<span>{d[5:]}<br>{_fmt_tok(totals[d])}</span>" for d in days
    )

    cache = sum(int(r.get("cache_read_tok") or 0) for r in token_rows)
    grand = sum(_row_total(r) for r in token_rows) or 1
    hit = cache / grand * 100

    legend = "".join(
        '<span><i style="width:9px;height:9px;border-radius:2px;display:inline-block;'
        f'background:{SOURCE_COLORS.get(s, "#c7d2e4")}"></i>'
        f'{_esc(SOURCE_LABELS.get(s, (s, ""))[0])}</span>'
        for s in sources
    )

    return (
        '<h2 id="token"><span class="no">2</span>Token 用量 · 近 7 日'
        f'<span class="hint">缓存读取占 {hit:.0f}%（成本字段库内恒为 0，暂不显示金额）</span></h2>'
        '<div class="note">'
        f'<div class="tk">{"".join(cols)}</div>'
        f'<div class="tk-x">{axis}</div>'
        f'<div class="lg">{legend}</div></div>'
    )


# ------------------------------------------------------------ 验证矩阵
def _render_matrix(verifications: list[dict]) -> str:
    rows = verifications[-MAX_MATRIX_ROWS:][::-1]
    width = max((len(v.get("checks") or []) for v in rows), default=0)
    head = "".join(f"<th>{i + 1}</th>" for i in range(width))
    body = []
    for v in rows:
        checks = {c["n"]: c for c in (v.get("checks") or [])}
        cells = "".join(
            f'<td><i class="pip {"ok" if checks[i]["ok"] else "no"}" '
            f'title="{_esc(checks[i]["name"])}"></i></td>'
            if i in checks else '<td><i class="pip na"></i></td>'
            for i in range(1, width + 1)
        )
        body.append(
            f'<tr><td>#{v["pr"]}</td>{cells}'
            f'<td style="border-top:1px solid #f4f5f7">'
            f'<span class="b {"ok" if v["passed"] == v["total"] else "warn"}">'
            f'{v["passed"]}/{v["total"]}</span></td></tr>'
        )
    return (
        '<h2 id="verif"><span class="no">3</span>部署验证矩阵'
        f'<span class="hint">最近 {len(rows)} 次 · 八项检查</span></h2>'
        '<div class="note"><div class="tbl"><table class="mx">'
        f'<thead><tr><th>PR</th>{head}<th>通过</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
        '<div class="lg"><span><i style="background:#22c55e;width:9px;height:9px;border-radius:50%"></i>PASS</span>'
        '<span><i style="background:#ef4444;width:9px;height:9px;border-radius:50%"></i>FAIL</span>'
        '<span style="color:var(--t3)">第 1 项若为琥珀色属已知基线失败，不是新增</span></div></div>'
    )


# ------------------------------------------------------------ 归因
def _render_anomaly(events: list[dict], verifications: list[dict], token_rows: list[dict]) -> str:
    stalls = 0
    slowest = 0
    for e in events:
        for seg in e.get("segments", []):
            if seg.get("stalled"):
                stalls += 1
                slowest = max(slowest, int(seg.get("sec") or 0))
    imperfect = sum(1 for v in verifications if v.get("passed") != v.get("total"))
    deploy_sessions = sum(
        int(r.get("sessions") or 0) for r in token_rows if r.get("source") == "webhook"
    )

    def li(label: str, value: str, cls: str = "ok") -> str:
        return f'<li><span>{_esc(label)}</span><span class="n {cls}">{_esc(value)}</span></li>'

    return (
        '<h2 id="anomaly"><span class="no">4</span>异常归因'
        '<span class="hint">只列有事实依据的项，不猜</span></h2>'
        '<div class="note"><ul class="al">'
        + li("up / promote 段卡顿", f"{stalls} 处" + (f"（最长 {_fmt_sec(slowest)}）" if slowest else ""),
             "bad" if stalls else "ok")
        + li("验证未满项", f"{imperfect} 次", "hot" if imperfect else "ok")
        + li("部署会话（近 7 日）", f"{deploy_sessions} 个", "ok")
        + li("成本口径", "未接入价目表", "hot")
        + "</ul></div>"
    )
