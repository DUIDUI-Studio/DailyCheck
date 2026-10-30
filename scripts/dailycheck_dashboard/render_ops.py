"""scripts.dailycheck_dashboard.render_ops —— 运维区块（KPI + 瀑布 + token + 验证矩阵）。

只产出 HTML 片段，由 render.py 注入主模板的两处占位符（${ops_css} / ${ops_html}）。
不 import render.py（避免循环依赖），_esc 在此本地实现。
"""
from __future__ import annotations

import html
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# 全站统一时区口径：CST+8（生产 unit 无 TZ 声明 → 进程 now() 即 CST）。
# ⚠️ 必须显式写死偏移，**不能用 time.strftime 的进程本地时区**：CI 与 dev 容器
# 都是 UTC，在 UTC 16:00–24:00（= CST 次日 00:00–08:00）这个窗口，本地时区判的
# 「今天」会比 CST 少一天 → 与 _cst_day() / TOKEN_SQL 的 '+8 hours' 错位，
# 「今日部署」恒显 0（2026-10-18 CI 实 hit，两个用例判红，本地 UTC 环境可复现）。
CST_OFFSET = timedelta(hours=8)
CST = timezone(CST_OFFSET)

# 瀑布图横轴上限（分钟）：超过则整行按上限截断并标注
AXIS_MAX_MIN = 90
MAX_WATERFALL_ROWS = 6
MAX_MATRIX_ROWS = 6
MAX_EVAL_ROWS = 40

# 复杂度分档 → 表头文案（与 ledger_db.complexity_band 的键一一对应）
EVAL_BAND_LABELS = {
    "low": "<4.0",
    "mid": "4.0–6.4",
    "high": "≥6.5",
    "unknown": "未知",
}

# source → (中文名, 颜色 class)
SOURCE_LABELS = {
    "webhook": ("部署会话", "acc"),
    "cron": ("定时巡检", "warn"),
    "feishu": ("飞书对话", "teal"),
    "cli": ("命令行", "gray"),
    "unknown": ("未知", "gray"),
}

# 异常留痕事件 kind → 人话（展示层职责；解析层只透传 kind，不碰文案）。
# 未知 kind 用 `.get(k, k)` 原样显示 —— 新事件类型不会静默丢失。
EVENT_KIND_LABELS = {
    "worktree-drift": "工作树漂移（verify 期间被 git pull 换树）",
    "observe-window-shortened": "观察窗被压缩到机制下限以下",
    "skip-verify": "跳过了八项验证（人工应急）",
    "dashboard-hook-failed": "仪表盘钩子渲染失败（靠 timer 兜底）",
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
    .loop-kpis{grid-template-columns:repeat(4,minmax(0,1fr))}
    .ops-kpi{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:11px 13px}
    .ops-kpi .ov{font-size:21px;font-weight:700;line-height:1.15;font-variant-numeric:tabular-nums}
    .ops-kpi .ov small{font-size:12px;font-weight:500;color:var(--t2);margin-left:1px}
    .ops-kpi .ok{font-size:11.5px;color:var(--t2);margin-top:2px}
    .ops-kpi.hot .ov{color:var(--warn)}
    .ops-kpi.bad .ov{color:var(--err)}
    .ops-kpi.ok .ov{color:var(--ok)}
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
    .ev .n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
    .ev .k{white-space:nowrap;font-weight:600}
    .ev .m{font-variant-numeric:tabular-nums;white-space:nowrap}
    .al{list-style:none;margin:0;padding:0;font-size:12.5px}
    .al li{display:flex;justify-content:space-between;gap:10px;padding:6px 0}
    .al li+li{border-top:1px solid #f4f5f7}
    .al .n{font-variant-numeric:tabular-nums;font-weight:600}
    .al .n.ok{color:var(--ok)}.al .n.hot{color:var(--warn)}.al .n.bad{color:var(--err)}
    .ops-empty{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 15px;font-size:12.5px;color:var(--t2)}
    @media(max-width:1024px){
        .ops-kpis{grid-template-columns:repeat(3,minmax(0,1fr))}
        .loop-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
    }
    @media(max-width:760px){
        .ops-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
        .loop-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
        .cols2{grid-template-columns:1fr}
        .wf-row{grid-template-columns:50px 1fr 54px}
        .wf-bar{height:12px}
    }
    @media(max-width:480px){
        .ops-kpi .ov{font-size:18px}
        .loop-kpis{grid-template-columns:1fr}
    }
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
    pr_data_ok: bool = True,
    loop_integrity: dict | None = None,
    eval_groups: list[dict] | None = None,
    eval_unavailable: bool = False,
) -> str:
    """返回整段运维区块 HTML（KPI 条 + 瀑布 + token + 验证矩阵 + 闭环完整性 + 归因）。"""
    unavailable = unavailable or []
    loop_integrity = loop_integrity or {}
    pr_by_num = {int(p["number"]): p for p in prs if p.get("number") is not None}

    kpi_html = _render_kpis(events, prs, token_rows, pr_data_ok=pr_data_ok)
    body_parts: list[str] = []

    if events:
        body_parts.append(_render_waterfall(events, pr_by_num, verifications))
    if token_rows:
        body_parts.append(_render_tokens(token_rows))
    if verifications:
        body_parts.append(_render_matrix(verifications))

    # 闭环完整性**不受 body_parts 空判影响**：即使没有部署事件（audit.log 缺失），
    # 「数据盲区」本身也是要暴露的事实——否则整块消失，盲区就永远看不见。
    loop_html = _render_loop_integrity(loop_integrity)

    # 派单账本：只有调用方明确给了数据或「源不可用」才渲染，避免程序化调用
    # （测试 / 旧调用点）凭空多出一个空壳区块。
    eval_html = ""
    if eval_groups or eval_unavailable:
        eval_html = render_eval_block(
            groups=eval_groups or [],
            prs=prs,
            pr_data_ok=pr_data_ok,
            unavailable=eval_unavailable,
        )

    if not body_parts:
        note = "运维数据源暂不可用"
        if unavailable:
            note += "：" + "、".join(_esc(u) for u in unavailable)
        return (
            kpi_html
            + '<div class="ops-empty">' + note
            + "。仪表盘其余区块不受影响；接入后本区块自动出现。</div>"
            + loop_html
            # 账本坏了也是独立事实：明确告诉读者是「模型效能」这一块不可用
            + eval_html
        )

    anomaly = _render_anomaly(events, verifications, token_rows)
    stamp = f'<div class="meta"><span><b>运维数据采集：</b>{_esc(fetched_at)}</span>'
    if unavailable:
        stamp += "<span><b>不可用源：</b>" + _esc("、".join(unavailable)) + "</span>"
    stamp += "</div>"
    return kpi_html + "\n".join(body_parts) + loop_html + anomaly + eval_html + stamp


# ------------------------------------------------------------ KPI
def _render_kpis(
    events: list[dict], prs: list[dict], token_rows: list[dict], *, pr_data_ok: bool = True
) -> str:
    # ⚠️ 「今天」必须显式按 CST 算，与 _cst_day() 同源（都用 CST 常量）。
    # 旧版用 time.strftime 的进程本地时区，注释写着「prod 进程 = CST+8」——
    # 这个假设只在生产成立：CI / dev 容器是 UTC，UTC 16:00–24:00 窗口内本地
    # 时区的「今天」比 CST 少一天，导致下方 `_cst_day(...) == today` 恒不成立，
    # 「今日部署」「今日合并 PR」全部归零（2026-10-18 CI 实 hit）。
    today = datetime.now(CST).strftime("%Y-%m-%d")
    cutoff = time.time() - 7 * 86400

    # 交付周期只取近 7 天合并的 PR，且用中位数（#59 那种 572 分钟的离群值会毁掉平均数）
    cycles = sorted(
        c for p in prs
        if (c := _cycle_sec(p)) is not None and (_epoch(p.get("merged_ts")) or 0) >= cutoff
    )
    merged_today = sum(1 for p in prs if _cst_day(p.get("merged_ts")) == today)
    median_cycle = cycles[len(cycles) // 2] if cycles else None

    today_events = [e for e in events if _cst_day(e.get("started_at")) == today]
    # 「今日」必须严格：今天没部署就显示 0 / —，**不能回退成历史全部**。
    # 回退会把「今天一次没部署」渲染成「今天部署了 12 次」—— 与「拉不到 PR 列表
    # 却显示 0 个合并」是同一类错误，只是方向相反。2026-10-18 生产实测命中：
    # 当天没有部署，卡片仍显示 12 次。
    verify_secs = [
        seg["sec"] for e in today_events
        for seg in e.get("segments", [])
        if seg.get("name") == "verify" and seg.get("sec")
    ]
    avg_verify = int(sum(verify_secs) / len(verify_secs)) if verify_secs else None

    tok_today = sum(_row_total(r) for r in token_rows if r.get("day") == today)
    stalls = sum(
        1 for e in events for seg in e.get("segments", []) if seg.get("stalled")
    )
    deploy_count = len(today_events)

    # 今天没部署时，副标题要说清「最近一次是哪天」，否则 0 会被读成「数据没跑」
    last_event = max(events, key=lambda e: e.get("started_at") or "", default=None)
    if today_events or last_event is None:
        deploy_sub = f"近 {len(events)} 次有记录"
        verify_sub = "verify → switch（含 promote）"
    else:
        deploy_sub = f"最近一次 #{last_event.get('pr')} · {_cst_day(last_event.get('started_at'))}"
        verify_sub = "今日无部署，不取历史值"

    def card(value: str, unit: str, label: str, sub: str, cls: str = "") -> str:
        unit_html = f"<small> {unit}</small>" if unit else ""
        return (
            f'<div class="ops-kpi {cls}"><div class="ov">{value}{unit_html}</div>'
            f'<div class="ok">{_esc(label)}</div>'
            f'<div class="ok" style="color:var(--t3)">{_esc(sub)}</div></div>'
        )

    # 拉不到 PR 列表 ≠ 今天没合并 —— 前者显示「—」，后者才是 0
    if pr_data_ok:
        c_merged = card(str(merged_today), "个", "今日合并 PR", f"近 7 天样本 {len(cycles)} 个")
        c_cycle = card(_fmt_sec(median_cycle), "", "交付周期（中位）", "创建 → 合并")
    else:
        c_merged = card("—", "", "今日合并 PR", "Gitea 拉取失败", "hot")
        c_cycle = card("—", "", "交付周期（中位）", "Gitea 拉取失败", "hot")

    return (
        '<div class="ops-kpis">'
        + c_merged
        + c_cycle
        + card(str(deploy_count), "次", "今日部署", deploy_sub)
        # ⚠️ 不能叫「八项检查耗时」：promote.sh 不总留审计行，段常常从 verify 一路并到切流
        + card(_fmt_sec(avg_verify), "", "验证到切流", verify_sub)
        + card(_fmt_tok(tok_today), "", "今日 Token", f"近 7 天 {len(token_rows)} 组")
        + card(str(stalls), "处", "部署卡顿", f"近 {len(events)} 次中 up/promote > 5 分钟",
               "bad" if stalls else "")
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
    # 复用模块级 CST_OFFSET：与 _render_kpis 的 today 同源，杜绝「两处各自 +8」
    # 将来改一处漏一处（同型缺陷见 inbox_watcher 的归一化漂移，Issue #51→#66）。
    return (parsed + CST_OFFSET).strftime("%Y-%m-%d")


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
def _render_loop_integrity(li: dict) -> str:
    """闭环完整性：这套流程到底有没有真闭环（2026-10-18 复盘新增）。

    为什么单列一节：现有 KPI 度量的是**吞吐与耗时**（合并数 / 周期 / 部署次数），
    但复盘发现的问题全在**闭环的真实性**——人工接管了几次、有没有工作树漂移、
    观察窗有没有被悄悄压缩。这些在原有版面里一个都看不见，只能靠事后 review
    对话才发现。本节把它们变成一等公民。

    只报有事实依据的数字（与 _render_anomaly 同一原则）：
      - 自动率的分母**只含有标注的会话**，未标注条数单列，不摊进任何一侧
      - 不可观测的量（回滚次数）明说「盲区」，绝不显示 0 —— 把「没记录」
        说成「没发生」比不报更坏
    """
    if not li:
        return ""

    auto = int(li.get("auto_records") or 0)
    manual = int(li.get("manual_records") or 0)
    unlabeled = int(li.get("unlabeled_records") or 0)
    rate = li.get("auto_rate")
    events = li.get("events") or {}
    manual_sessions = li.get("manual_sessions") or []
    manual_prs = li.get("manual_prs") or []
    blind = li.get("blind_spots") or []
    # fail-closed：缺这个键就当作「不可观测」。没有证据时断言「无异常」
    # 正是本次要修的假信号（#118 合并时埋点还在 #116 未部署）。
    events_observable = bool(li.get("events_observable"))

    def card(value: str, unit: str, label: str, sub: str, cls: str = "") -> str:
        unit_html = f"<small> {unit}</small>" if unit else ""
        return (
            f'<div class="ops-kpi {cls}"><div class="ov">{value}{unit_html}</div>'
            f'<div class="ok">{_esc(label)}</div>'
            f'<div class="ok" style="color:var(--t3)">{_esc(sub)}</div></div>'
        )

    # 自动率：无标注记录时显示「—」，不显示 100%（那会被读成「全闭环」）
    if rate is None:
        c_rate = card("—", "", "自动闭环率", "无标注会话，不可判", "hot")
    else:
        pct = f"{rate * 100:.0f}"
        c_rate = card(pct, "%", "自动闭环率", f"{auto} 自动 / {manual} 人工",
                      "bad" if manual else "ok")
    c_manual = card(str(manual), "次", "人工接管",
                    f"涉及 PR {', '.join('#' + str(p) for p in manual_prs[:5]) or '—'}",
                    "bad" if manual else "")
    n_events = sum(events.values())
    if events_observable:
        c_event = card(str(n_events), "起", "异常留痕",
                       f"{len(events)} 类" if events else "无异常事件")
    else:
        # 埋点没部署 → 0 是「没人在记」，不是「没发生」。显示「—」而非 0，
        # 否则一个坏掉的采集会看起来像一切正常。
        c_event = card("—", "", "异常留痕", "埋点未部署，不可观测", "hot")
    c_blind = card(str(len(blind)), "处", "数据盲区",
                   "下列指标不可观测", "hot" if blind else "")

    rows: list[str] = []
    # 异常事件明细：三态。有事件→列出；埋点在且为 0→才能说「均未发生」；
    # 埋点不在→必须说清「0 不代表没发生」，绝不渲染成绿色 OK。
    if events:
        detail = "、".join(
            f"{EVENT_KIND_LABELS.get(k, k)} × {v}"
            for k, v in sorted(events.items(), key=lambda kv: -kv[1])
        )
        rows.append(f'<li><span>异常事件明细</span><span class="n hot">{_esc(detail)}</span></li>')
    elif events_observable:
        rows.append('<li><span>异常事件明细</span><span class="n ok">无（漂移 / 压缩观察窗 / 跳过验证 / 钩子失败均未发生）</span></li>')
    else:
        rows.append(
            '<li><span>异常事件明细</span><span class="n hot">'
            '不可观测 —— 埋点未部署，「0 起」不代表没发生'
            '（已部署的 deploy_pipeline.sh 缺 _audit_event）</span></li>'
        )
    if unlabeled:
        rows.append(
            f'<li><span>未标注会话</span><span class="n hot">{unlabeled} 条 —— 早期无 session 机制，不计入自动率分母</span></li>'
        )
    if manual_sessions:
        rows.append(
            '<li><span>人工接管会话</span><span class="n bad">'
            + _esc(", ".join(manual_sessions)) + "</span></li>"
        )
    for b in blind:
        rows.append(f'<li><span>数据盲区</span><span class="n hot">{_esc(b)}</span></li>')

    return (
        '<h2 id="loop"><span class="no">4</span>闭环完整性'
        '<span class="hint">流程有没有真闭环，而不是跑得快不快</span></h2>'
        '<div class="ops-kpis loop-kpis">'
        + c_rate + c_manual + c_event + c_blind
        + "</div>"
        + '<div class="note"><ul class="al">' + "".join(rows) + "</ul></div>"
    )


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
        '<h2 id="anomaly"><span class="no">5</span>异常归因'
        '<span class="hint">只列有事实依据的项，不猜</span></h2>'
        '<div class="note"><ul class="al">'
        + li("up / promote 段卡顿", f"{stalls} 处" + (f"（最长 {_fmt_sec(slowest)}）" if slowest else ""),
             "bad" if stalls else "ok")
        + li("验证未满项", f"{imperfect} 次", "hot" if imperfect else "ok")
        + li("部署会话（近 7 日）", f"{deploy_sessions} 个", "ok")
        + li("成本口径", "未接入价目表", "hot")
        + "</ul></div>"
    )


# ------------------------------------------------------------ 模型效能评估
def _fmt_cost(value: float | None) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def _eval_pr_cell(group: dict, pr_by_num: dict, pr_data_ok: bool) -> str:
    """关联 PR 状态：已合并 / 开放 / 关闭未合并。拉不到 PR 列表时出「—」。"""
    numbers = group.get("pr_numbers") or []
    if not numbers:
        return '<td class="n">—</td>'
    if not pr_data_ok:
        return '<td class="n" title="Gitea PR 拉取失败，状态不可得">—</td>'

    merged = opened = closed = unknown = 0
    for number in numbers:
        meta = pr_by_num.get(int(number))
        if meta is None:
            unknown += 1
            continue
        state = (meta.get("state") or "").upper()
        if state == "MERGED":
            merged += 1
        elif state == "OPEN":
            opened += 1
        elif state == "CLOSED":
            closed += 1
        else:
            unknown += 1

    if merged + opened + closed == 0:
        return '<td class="n" title="PR 状态不可得">—</td>'
    title = f"已合并 {merged} · 开放 {opened} · 关闭未合并 {closed}"
    if unknown:
        title += f" · 状态未知 {unknown}"
    return f'<td class="n" title="{_esc(title)}">{merged}/{opened}/{closed}</td>'


def _eval_cycle_cell(group: dict, pr_by_num: dict) -> str:
    """平均交付周期：dispatch ts → PR merged_at（仅统计可得样本）。"""
    cycles: list[int] = []
    for number, ts in group.get("pr_rows") or []:
        meta = pr_by_num.get(int(number))
        if meta is None:
            continue
        dispatched = _parse_iso(ts)
        merged = _parse_iso(meta.get("merged_ts"))
        if dispatched is None or merged is None:
            continue
        cycles.append(max(0, int((merged - dispatched).total_seconds())))
    if not cycles:
        return '<td class="n" title="无已合并 PR 可算">—</td>'
    avg = int(sum(cycles) / len(cycles))
    return f'<td class="n" title="{len(cycles)} 个已合并 PR 样本">{_fmt_sec(avg)}</td>'


def render_eval_block(
    *,
    groups: list[dict],
    prs: list[dict],
    pr_data_ok: bool = True,
    unavailable: bool = False,
) -> str:
    """模型效能评估表：每行 = role × 模型 × 复杂度档。

    数据来自派单账本（Issue #91 采集，Issue #92 摄取）；PR 状态从既有 fetch_prs
    数据 join，不额外拉 Gitea。所有用户可控字段一律过 _esc。
    底部汇总窗口内总成本 / 总派单 / 数据覆盖率（有 tokens 的行 / 总行）。
    """
    pr_by_num = {int(p["number"]): p for p in prs if p.get("number") is not None}
    hint = "派单账本 · role × 模型 × 复杂度档"
    if unavailable:
        hint += " · 本轮回落到库内历史"
    header = (
        '<h2 id="eval"><span class="no">6</span>模型效能评估'
        f'<span class="hint">{_esc(hint)}</span></h2>'
    )

    if not groups:
        if unavailable:
            msg = "模型效能数据源不可用（派单账本缺失或无法解析）"
        else:
            msg = "派单账本暂无数据（窗口内无派单记录）"
        return header + f'<div class="ops-empty">{_esc(msg)}。其余区块不受影响。</div>'

    rows = groups[:MAX_EVAL_ROWS]
    body: list[str] = []
    for group in rows:
        dispatches = int(group.get("dispatches") or 0)
        if dispatches <= 0:
            continue
        band = group.get("band") or "unknown"
        band_label = EVAL_BAND_LABELS.get(band, band)
        role = group.get("role") or "unknown"
        model = group.get("model") or "unknown"
        role_label = "未知" if role == "unknown" else role
        model_label = "未知" if model == "unknown" else model

        issue_rows = int(group.get("issue_rows") or 0)
        retries = int(group.get("retries") or 0)
        if issue_rows:
            rate = retries / dispatches * 100
            retry_cell = f"{rate:.0f}%"
            retry_title = f"重派 {retries} 次 / 派单 {dispatches}（{issue_rows} 行带 issue 关联）"
        else:
            retry_cell = "—"
            retry_title = "无 issue 关联，不可判重派"

        null_rows = int(group.get("null_rows") or 0)
        tokens = int(group.get("tokens") or 0)
        if null_rows >= dispatches:
            token_cell = "—"
            token_title = "全部行无 tokens 数据"
        else:
            token_cell = _fmt_tok(tokens)
            token_title = f"{tokens:,} tokens" + (
                f"（{null_rows} 行无数据已跳过）" if null_rows else ""
            )

        cost_rows = int(group.get("cost_rows") or 0)
        if cost_rows:
            cost_cell = _fmt_cost(float(group.get("cost") or 0))
            cost_title = f"{cost_rows}/{dispatches} 行有成本数据"
        else:
            cost_cell = "—"
            cost_title = "全部行无成本数据"

        pr_cell = _eval_pr_cell(group, pr_by_num, pr_data_ok)
        cycle_cell = _eval_cycle_cell(group, pr_by_num)

        body.append(
            "<tr>"
            f'<td class="k">{_esc(role_label)}</td>'
            f'<td class="k">{_esc(model_label)}</td>'
            f'<td class="m">{_esc(band_label)}</td>'
            f'<td class="n">{dispatches}</td>'
            f'<td class="n" title="{_esc(retry_title)}">{_esc(retry_cell)}</td>'
            f'<td class="n" title="{_esc(token_title)}">{_esc(token_cell)}</td>'
            f'<td class="n" title="{_esc(cost_title)}">{_esc(cost_cell)}</td>'
            f"{pr_cell}{cycle_cell}"
            "</tr>"
        )

    total_dispatch = sum(int(g.get("dispatches") or 0) for g in groups)
    total_cost = sum(float(g.get("cost") or 0) for g in groups)
    cost_rows = sum(int(g.get("cost_rows") or 0) for g in groups)
    data_rows = sum(
        int(g.get("dispatches") or 0) - int(g.get("null_rows") or 0) for g in groups
    )
    coverage = (data_rows / total_dispatch * 100) if total_dispatch else 0.0

    cost_total = _fmt_cost(total_cost) if cost_rows else "—"
    hint_extra = (
        f'<span style="color:var(--t3)">仅显示前 {len(rows)} 组（共 {len(groups)} 组）</span>'
        if len(groups) > len(rows) else ""
    )

    return (
        header
        + '<div class="note"><div class="tbl"><table class="ev">'
        "<thead><tr>"
        "<th>角色</th><th>模型</th><th>复杂度</th>"
        '<th class="n">派单</th><th class="n">重派率</th>'
        '<th class="n">Tokens</th><th class="n">估算成本</th>'
        '<th class="n" title="已合并 / 开放 / 关闭未合并">PR 状态</th>'
        '<th class="n">平均交付周期</th>'
        "</tr></thead>"
        f'<tbody>{"".join(body)}</tbody></table></div>'
        '<div class="lg">'
        f"<span><b>窗口内总成本：</b>{_esc(cost_total)}"
        f"（{cost_rows}/{total_dispatch} 行有数据）</span>"
        f"<span><b>总派单：</b>{total_dispatch}</span>"
        f"<span><b>数据覆盖率：</b>{coverage:.0f}%"
        f"（{data_rows}/{total_dispatch} 行有 tokens）</span>"
        + (f"<span>{hint_extra}</span>" if hint_extra else "")
        + "</div></div>"
    )
