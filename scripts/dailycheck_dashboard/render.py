"""scripts.dailycheck_dashboard.render —— HTML 模板与渲染。

模板内嵌为模块级常量（零外部文件依赖；用 string.Template 简化替换）。
样式 100% 沿用上一版手写 dashboard.html 的视觉语言（banner+kpis+任务卡片+表格）。
"""
from __future__ import annotations

import html
import time
from dataclasses import dataclass, field
from string import Template
from typing import Any


# ============================================================
# 渲染输入
# ============================================================
@dataclass
class RenderInput:
    created_at: str
    trigger_kind: str
    trigger_event_id: str | None
    head_sha: str | None
    origin_main_sha: str | None
    summary: dict
    issues: list[dict] = field(default_factory=list)
    prs: list[dict] = field(default_factory=list)
    worktrees: list[dict] = field(default_factory=list)
    inbox_stats: dict = field(default_factory=dict)
    diff: dict | None = None  # 与上次快照的 KPI 差值（v1: {open_issues: +1, ...}）


# ============================================================
# 工具
# ============================================================
def _esc(s: Any) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def _diff_arrow(prev: int, cur: int) -> str:
    """v1 KPI 差异指示：↑N / ↓N / →。"""
    if prev is None:
        return '<span class="cv q">新</span>'
    delta = cur - prev
    if delta == 0:
        return '<span class="cv q">→</span>'
    if delta > 0:
        return f'<span class="cv bad">↑{delta}</span>'
    return f'<span class="cv ok">↓{abs(delta)}</span>'


def _badge_model_class(model: str) -> str:
    """按模型名哈希到 5 色徽章（紫/蓝/灰/青/橙）。"""
    palette = ["purple", "blue", "gray", "teal", "orange"]
    h = sum(ord(c) for c in model) % len(palette)
    return palette[h]


# ============================================================
# HTML 模板（与上一版 dashboard.html 同结构）
# ============================================================
HTML_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DailyCheck · 项目任务仪表盘</title>
<style>
    :root{
        --bg:#f6f7f9;--card:#fff;--line:#e7e9ee;
        --t1:#111827;--t2:#6b7280;--t3:#9ca3af;
        --ok:#1a7f37;--warn:#b45309;--err:#c73a3a;--idle:#9aa4b1;--accent:#2f6fed;
    }
    *{box-sizing:border-box}
    html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
    body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;background:var(--bg);color:var(--t1)}
    .wrap{max-width:1080px;margin:0 auto;padding:20px 20px 60px}
    .top{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:4px;flex-wrap:wrap;gap:8px}
    h1{font-size:21px;margin:0}
    .sub{color:var(--t2);font-size:12.5px}
    nav{position:sticky;top:0;z-index:10;background:rgba(246,247,249,.92);backdrop-filter:blur(6px);
        display:flex;gap:4px;padding:10px 0 8px;margin-bottom:8px;border-bottom:1px solid var(--line);flex-wrap:wrap}
    nav a{font-size:13px;color:var(--t2);text-decoration:none;padding:4px 10px;border-radius:8px}
    nav a:hover{background:#e9edf3;color:var(--t1)}
    .banner{display:flex;align-items:center;gap:14px;border-radius:14px;padding:16px 20px;border:1px solid;margin:14px 0 16px}
    .banner.ok{background:#f0fdf4;border-color:#bbf7d0}
    .banner.warn{background:#fffbeb;border-color:#fde68a}
    .banner.err{background:#fef2f2;border-color:#fecaca}
    .dot{width:12px;height:12px;border-radius:50%;flex:none}
    .banner.ok .dot{background:#22c55e}
    .banner.warn .dot{background:#f59e0b}
    .banner.err .dot{background:#ef4444}
    .banner.ok .bt{color:#15803d}.banner.warn .bt{color:#b45309}.banner.err .bt{color:#b91c1c}
    .bt{font-size:17px;font-weight:700}
    .banner .bs{font-size:12.5px;color:var(--t2);margin-top:3px}
    .banner .btime{margin-left:auto;font-size:12px;color:var(--t3);flex:none}
    .kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:6px}
    .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
    .kpi .kv{font-size:25px;font-weight:700;line-height:1.15}
    .kpi .kv small{font-size:13px;font-weight:500;color:var(--t2)}
    .kpi .kk{font-size:12.5px;color:var(--t2);margin-top:3px}
    .kpi .kdiff{font-size:11.5px;color:var(--t3);margin-top:2px}
    .kpi.warn .kv{color:var(--warn)}
    .kpi.err .kv{color:var(--err)}
    h2{font-size:16px;margin:30px 0 12px;display:flex;align-items:center;gap:8px}
    h2 .no{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:7px;
        background:#1d4ed8;color:#fff;font-size:12.5px;font-weight:700;flex:none}
    h2 .hint{font-size:12px;color:var(--t3);font-weight:400;margin-left:auto}
    .tasks{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
    .task{background:var(--card);border:1px solid var(--line);border-top:3px solid var(--t3);border-radius:12px;padding:13px 15px 14px}
    .task.ok{border-top-color:#22c55e}
    .task.err{border-top-color:#ef4444}
    .task.pending{border-top-color:#93c5fd}
    .task.warn{border-top-color:#f59e0b}
    .task .th{display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap}
    .task .tn{font-size:14px;font-weight:700}
    .task .tm{font-size:12px;color:var(--t2);margin-top:5px;line-height:1.6}
    .task .tl{margin-top:8px;padding-top:8px;border-top:1px dashed var(--line);font-size:12px;color:var(--t2);line-height:1.7}
    .issue{display:flex;gap:10px;padding:11px 14px;border-radius:10px;margin-bottom:8px;font-size:13px;border:1px solid;align-items:flex-start}
    .issue.warn{background:#fffbeb;border-color:#fde68a}
    .issue.err{background:#fef2f2;border-color:#fecaca}
    .issue .it{font-weight:600;white-space:nowrap}
    .issue.warn .it{color:#b45309}.issue.err .it{color:#b91c1c}
    .issue .id{color:var(--t2);line-height:1.55;flex:1;min-width:0}
    .issue .ia{color:var(--t3);font-size:12px;margin-top:2px}
    table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden;font-size:13px;border:1px solid var(--line)}
    th{background:#f3f5f8;text-align:left;padding:8px 12px;font-weight:600;color:#4b5563}
    td{padding:7px 12px;border-top:1px solid #f1f2f4}
    tr:hover td{background:#fafbfc}
    .num{text-align:right;font-variant-numeric:tabular-nums}
    .tbl{overflow-x:auto;-webkit-overflow-scrolling:touch}
    .tl,.tm,.issue .id,.note,.meta span,td{overflow-wrap:break-word}
    .b{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;white-space:nowrap}
    .b.ok{background:#e6f7ec;color:#0a7a35}
    .b.warn{background:#fdf0e0;color:#b45309}
    .b.miss{background:#f0f0f0;color:#999}
    .b.info{background:#e8f0fe;color:#1d4ed8}
    .b.danger{background:#fef2f2;color:#b91c1c}
    .b.purple{background:#7c3aed18;color:#7c3aed;font-weight:600}
    .b.blue{background:#2563eb18;color:#2563eb;font-weight:600}
    .b.gray{background:#6b728018;color:#6b7280;font-weight:600}
    .b.teal{background:#0f766e18;color:#0f766e;font-weight:600}
    .b.orange{background:#ea580c18;color:#ea580c;font-weight:600}
    .note{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:13px 17px;font-size:12.5px;color:#4b5563;line-height:1.9}
    .note code{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:12px}
    .meta{display:flex;gap:14px;font-size:11.5px;color:var(--t3);margin-top:8px;flex-wrap:wrap}
    .meta b{color:var(--t2);font-weight:600}
    .cv{font-weight:700;font-size:12px}
    .cv.ok{color:#0a7a35}
    .cv.q{color:#9ca3af;font-weight:400}
    .cv.bad{color:#dc2626}
    /* ---------- 响应式：平板（iPad 竖屏 / 小笔记本）---------- */
    @media(max-width:1024px){
        .wrap{padding:18px 18px 52px}
    }
    /* ---------- 响应式：手机 / 小屏 ---------- */
    @media(max-width:760px){
        .wrap{padding:16px 14px calc(48px + env(safe-area-inset-bottom))}
        h1{font-size:19px}
        nav{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
        nav::-webkit-scrollbar{display:none}
        nav a{white-space:nowrap;flex:none;padding:6px 10px}
        .banner{flex-wrap:wrap;padding:14px 16px;gap:10px}
        .banner .btime{display:none}
        .bt{font-size:15.5px}
        .kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
        .kpi{padding:12px 14px}
        .kpi .kv{font-size:22px}
        h2{font-size:15px;flex-wrap:wrap}
        h2 .hint{margin-left:0;flex-basis:100%}
        .tasks{grid-template-columns:1fr}
        .issue{flex-direction:column;gap:2px;padding:10px 12px}
        .tbl table{min-width:560px}
        th,td{padding:7px 10px;font-size:12.5px}
        .note{padding:12px 14px}
        .meta{gap:4px 10px}
    }
    /* ---------- 响应式：小屏手机 ---------- */
    @media(max-width:480px){
        .wrap{padding:14px 12px calc(40px + env(safe-area-inset-bottom))}
        h1{font-size:17.5px}
        .sub{font-size:11.5px}
        .banner{padding:12px 14px;margin:10px 0 12px}
        .bt{font-size:15px}
        .banner .bs{font-size:12px}
        .kpi .kv{font-size:19.5px}
        .kpi .kk{font-size:12px}
        .kpi .kdiff{font-size:11px}
        .tbl table{min-width:520px}
        h2{margin:22px 0 10px}
    }
</style>
</head>
<body>
<div class="wrap">

<div class="top">
    <h1>DailyCheck · 项目任务仪表盘</h1>
    <span class="sub">生成于 ${created_at} · 触发：${trigger_label}</span>
</div>

<nav>
    <a href="#monitor">① 任务运行</a>
    <a href="#issues">② Issue 追踪</a>
    <a href="#prs">③ PR 管线</a>
    <a href="#worktrees">④ Worktree</a>
    <a href="#inbox">⑤ Inbox 事件</a>
</nav>

<div class="banner ${banner_class}">
    <div class="dot"></div>
    <div>
        <div class="bt">${banner_title}</div>
        <div class="bs">${banner_sub}</div>
    </div>
    <div class="btime">HEAD ${head_short} · origin/main ${origin_main_short}</div>
</div>

<div class="kpis">
    <div class="kpi ${kpi_open_class}">
        <div class="kv">${open_issues}<small> 个</small></div>
        <div class="kk">Open Issues</div>
        <div class="kdiff">与上次：${diff_open}</div>
    </div>
    <div class="kpi">
        <div class="kv">${merged_7d_total}<small> 个</small></div>
        <div class="kk">已合并 PR（近 7 天）</div>
        <div class="kdiff">与上次：${diff_merged}</div>
    </div>
    <div class="kpi ${kpi_wt_class}">
        <div class="kv">${worktrees_total}<small> 个</small></div>
        <div class="kk">Worktree 在用</div>
        <div class="kdiff">与上次：${diff_wt}</div>
    </div>
    <div class="kpi">
        <div class="kv">${closed_unmerged}<small> 个</small></div>
        <div class="kk">未合并 PR（CLOSED）</div>
        <div class="kdiff">与上次：${diff_closed}</div>
    </div>
</div>

<h2 id="monitor">
    <span class="no">1</span>
    任务运行监控 · 触发链组件
    <span class="hint">PR webhook → inbox.jsonl → 生成快照</span>
</h2>

<div class="tasks">
    <div class="task ok">
        <div class="th">
            <span class="tn">🤖 CI · 拉 PR 即跑</span>
            <span class="b ok">在用</span>
        </div>
        <div class="tm">pull_request 触发 · <code>.github/workflows/ci.yml</code></div>
        <div class="tl">ruff + pytest + known_failures 门禁 + contract + 报告自检</div>
    </div>
    <div class="task ok">
        <div class="th">
            <span class="tn">🔔 Webhook → Inbox → Agent</span>
            <span class="b ok">已部署</span>
        </div>
        <div class="tm">PR webhook → inbox.jsonl → dc-inbox-watcher（≤5s）→ Prod Agent</div>
        <div class="tl">首个全自动 merge 闭环 = PR #52（10-16 15:02）<br>receiver 仅转发 action ∈ {opened,synchronize,reopened}</div>
    </div>
    <div class="task ok">
        <div class="th">
            <span class="tn">📊 Dashboard · 本脚本</span>
            <span class="b ok">${dash_status}</span>
        </div>
        <div class="tm">dc-dashboard-generator 订阅同一 inbox，独立 listener 触发渲染</div>
        <div class="tl">本次触发：${trigger_kind} · delivery=${trigger_event_id_short}<br>落 HTML 到 <code>${html_path_display}</code> + 写 SQLite 快照</div>
    </div>
    <div class="task warn">
        <div class="th">
            <span class="tn">⏰ 兜底轮询</span>
            <span class="b info">已部署</span>
        </div>
        <div class="tm">cron <code>02ee9e01cb13</code> · <code>*/30</code> · 脚本 <code>dailycheck_pr_poll.py</code></div>
        <div class="tl">≤30 分钟保底；Issue #51（watcher retry-gating）待修</div>
    </div>
</div>

<h2 id="issues">
    <span class="no">2</span>
    Issue 追踪
    <span class="hint">Open ${open_issues_count} · 总 ${issues_total}</span>
</h2>

${issues_html}

<h2 id="prs">
    <span class="no">3</span>
    PR 管线
    <span class="hint">已合并（近 7 天）${merged_7d_count} · 未合并（CLOSED）${closed_unmerged_count} · 全部 ${prs_total}</span>
</h2>

${prs_html}

<h2 id="worktrees">
    <span class="no">4</span>
    Worktree 在用
    <span class="hint">${worktrees_count} 个</span>
</h2>

${worktrees_html}

<h2 id="inbox">
    <span class="no">5</span>
    Inbox 事件（dc-webhook-receiver 写入）
    <span class="hint">实时扫描 inbox.jsonl</span>
</h2>

<div class="note">
    <b>Inbox 统计</b>：${inbox_lines} 行 / ${inbox_deliveries} 个唯一 delivery_id<br>
    <b>最近事件</b>：${inbox_last_pr}（${inbox_last_action_at}）<br>
    <b>说明</b>：本视图统计当前 inbox.jsonl；rotate 归档后历史自动清理。详细触发审计走 <code>dc-inbox-watcher</code> state。
</div>

<div class="meta">
    <span><b>数据快照时间：</b>${created_at}</span>
    <span><b>HEAD：</b>${head_sha_full}</span>
    <span><b>origin/main：</b>${origin_main_full}</span>
    <span><b>触发：</b>${trigger_kind} (delivery=${trigger_event_id_short})</span>
    <span><b>HTML：</b>${html_path_display}</span>
</div>

</div>
</body>
</html>
""")


# ============================================================
# 渲染（按章节分块）
# ============================================================
def render(input: RenderInput) -> str:
    """主入口。返回完整 HTML 字符串。"""
    diff = input.diff or {}
    s = input.summary

    # Banner 健康度判定
    open_issues = s.get("open_issues", 0)
    closed_unmerged = s.get("closed_unmerged", 0)
    if open_issues >= 5 or closed_unmerged >= 8:
        banner_class = "err"
        banner_title = "多项待处理 · 需介入"
    elif open_issues >= 1 or closed_unmerged >= 1:
        banner_class = "warn"
        banner_title = "部分待处理 · 主力工作流正常"
    else:
        banner_class = "ok"
        banner_title = "全部正常，无需处理"
    inbox = input.inbox_stats
    banner_sub = (
        f"{open_issues} 个 Open Issue · {closed_unmerged} 个未合并 PR（CLOSED）· "
        f"inbox {inbox.get('lines', 0)} 行 / {inbox.get('deliveries', 0)} 个事件"
    )

    # KPI 颜色 + 差异
    kpi_open_class = "warn" if open_issues >= 1 else ""
    kpi_wt_class = "warn" if s.get("worktrees_total", 0) >= 5 else ""

    # 触发标签
    trigger_label = _trigger_label(input.trigger_kind)
    dash_status = "本次已渲染"
    head_short = (input.head_sha or "—")[:8]
    origin_main_short = (input.origin_main_sha or "—")[:8]

    # 章节 HTML
    issues_html = _render_issues(input.issues)
    prs_html = _render_prs(input.prs)
    worktrees_html = _render_worktrees(input.worktrees)

    # PR 状态分布计数
    merged_7d_count = _count_merged_7d(input.prs)
    closed_unmerged_count = sum(1 for p in input.prs if p.get("state") == "CLOSED")

    # inbox 显示
    inbox_last_pr = f"PR #{inbox['last_pr_number']}" if inbox.get("last_pr_number") else "—"
    inbox_last_action_at = _ts_to_iso(inbox.get("last_action_at")) if inbox.get("last_action_at") else "—"

    return HTML_TEMPLATE.substitute(
        # banner / top
        created_at=_esc(input.created_at),
        trigger_label=_esc(trigger_label),
        banner_class=banner_class,
        banner_title=_esc(banner_title),
        banner_sub=_esc(banner_sub),
        head_short=_esc(head_short),
        origin_main_short=_esc(origin_main_short),
    # KPIs
    kpi_open_class=kpi_open_class,
    kpi_wt_class=kpi_wt_class,
    open_issues=open_issues,
    merged_7d_total=s.get("merged_7d_total", 0),
    worktrees_total=s.get("worktrees_total", 0),
    closed_unmerged=closed_unmerged,
    diff_open=_diff_arrow(diff.get("open_issues"), open_issues),
    diff_merged=_diff_arrow(diff.get("merged_7d_total"), s.get("merged_7d_total", 0)),
    diff_wt=_diff_arrow(diff.get("worktrees_total"), s.get("worktrees_total", 0)),
    diff_closed=_diff_arrow(diff.get("closed_unmerged"), closed_unmerged),
        # task card 3
        dash_status=_esc(dash_status),
        trigger_kind=_esc(input.trigger_kind),
        trigger_event_id_short=_esc((input.trigger_event_id or "—")[:16]),
        html_path_display=_esc("snapshots/<id>.html + latest.html"),
        # issues
        issues_html=issues_html,
        open_issues_count=open_issues,
        issues_total=len(input.issues),
        # prs
        prs_html=prs_html,
        merged_7d_count=merged_7d_count,
        closed_unmerged_count=closed_unmerged_count,
        prs_total=len(input.prs),
        # worktrees
        worktrees_html=worktrees_html,
        worktrees_count=len(input.worktrees),
        # inbox
        inbox_lines=inbox.get("lines", 0),
        inbox_deliveries=inbox.get("deliveries", 0),
        inbox_last_pr=_esc(inbox_last_pr),
        inbox_last_action_at=_esc(inbox_last_action_at),
        # meta
        head_sha_full=_esc(input.head_sha or "—"),
        origin_main_full=_esc(input.origin_main_sha or "—"),
    )


def _trigger_label(kind: str) -> str:
    return {
        "pr_opened": "PR opened",
        "pr_synchronize": "PR synchronize",
        "pr_reopened": "PR reopened",
        "manual": "手动",
        "init": "首次初始化",
    }.get(kind, kind)


def _ts_to_iso(ms_str: str) -> str:
    try:
        ms = int(ms_str)
    except (TypeError, ValueError):
        return ms_str
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


def _count_merged_7d(prs: list[dict]) -> int:
    """按 merged_at 日期严格过滤（距今 ≤7 天）。"""
    import time as _t
    cutoff_str = _t.strftime("%Y-%m-%d", _t.localtime(_t.time() - 7 * 86400))
    return sum(
        1
        for p in prs
        if p.get("state") == "MERGED"
        and p.get("merged_at")
        and p["merged_at"] >= cutoff_str
    )


# ============================================================
# 章节渲染
# ============================================================
def _render_issues(issues: list[dict]) -> str:
    open_issues = [i for i in issues if i.get("state") == "OPEN"]
    closed_recent = sorted(
        [i for i in issues if i.get("state") == "CLOSED" and i.get("closed_at")],
        key=lambda x: x.get("closed_at") or "",
        reverse=True,
    )[:5]
    if not open_issues:
        body = '<div class="issue warn"><div class="id">无 Open Issue</div></div>'
    else:
        body = "\n".join(_render_issue_row(i) for i in open_issues)
    details = ""
    if closed_recent:
        rows = "\n".join(
            f'<tr><td><b>#{i["number"]}</b></td><td>{_esc(i.get("title", ""))}</td>'
            f'<td class="cd">{_esc(i.get("closed_at", ""))}</td></tr>'
            for i in closed_recent
        )
        details = (
            '<details><summary>最近 Closed Issue（最多 5 条）</summary>'
            f'<div class="inner"><div class="tbl"><table><tr><th>#</th><th>标题</th><th>关闭日</th></tr>{rows}</table></div></div>'
            '</details>'
        )
    return body + "\n" + details


def _render_issue_row(i: dict) -> str:
    severity = "err" if "已修入 main 但仍 OPEN" in (i.get("title") or "") else "warn"
    return (
        f'<div class="issue {severity}">'
        f'<div class="it">#{i["number"]}</div>'
        f'<div class="id">{_esc(i.get("title", ""))}'
        f'<div class="ia">{_esc(i.get("created_at") or "—")} '
        f'· state={_esc(i.get("state") or "")} '
        f'· closed={_esc(i.get("closed_at") or "—")}</div></div>'
        f'</div>'
    )


def _render_prs(prs: list[dict]) -> str:
    if not prs:
        return '<div class="note">无 PR</div>'

    # 按 state 分桶
    buckets: dict[str, list[dict]] = {"MERGED": [], "OPEN": [], "CLOSED": []}
    for p in prs:
        st = p.get("state") or "OPEN"
        buckets.setdefault(st, []).append(p)

    sections: list[str] = []

    # 已合并（按 merged_at 倒序，限 20）
    merged = sorted(buckets.get("MERGED", []), key=lambda x: x.get("merged_at") or "", reverse=True)[:20]
    if merged:
        rows = "\n".join(
            f'<tr><td><b>#{p["number"]}</b></td><td>{_esc(p.get("head_ref", ""))}</td>'
            f'<td>{_esc(p.get("title", ""))}</td>'
            f'<td class="cd">{_esc(p.get("merged_at", ""))}</td></tr>'
            for p in merged
        )
        sections.append(
            f'<div class="note" style="padding:8px 14px; margin:0 0 8px"><b>已合并（近 20）</b></div>'
            f'<div class="tbl"><table><tr><th>PR</th><th>分支</th><th>标题</th><th>合并日</th></tr>{rows}</table></div>'
        )

    # OPEN（限 20）
    open_prs = buckets.get("OPEN", [])[:20]
    if open_prs:
        rows = "\n".join(
            f'<tr><td><b>#{p["number"]}</b></td><td>{_esc(p.get("head_ref", ""))}</td>'
            f'<td>{_esc(p.get("title", ""))}</td>'
            f'<td class="cd">{_esc(p.get("created_at", ""))}</td></tr>'
            for p in open_prs
        )
        sections.append(
            f'<div class="note" style="padding:8px 14px; margin:14px 0 8px"><b>Open（最多 20）</b></div>'
            f'<div class="tbl"><table><tr><th>PR</th><th>分支</th><th>标题</th><th>创建日</th></tr>{rows}</table></div>'
        )

    # CLOSED（限 10）
    closed = buckets.get("CLOSED", [])[:10]
    if closed:
        rows = "\n".join(
            f'<tr><td><b>#{p["number"]}</b></td><td>{_esc(p.get("head_ref", ""))}</td>'
            f'<td>{_esc(p.get("title", ""))}</td>'
            f'<td class="cd">{_esc(p.get("created_at", ""))}</td></tr>'
            for p in closed
        )
        sections.append(
            f'<div class="note" style="padding:8px 14px; margin:14px 0 8px"><b>Closed 未合并（最多 10）</b></div>'
            f'<div class="tbl"><table><tr><th>PR</th><th>分支</th><th>标题</th><th>创建日</th></tr>{rows}</table></div>'
        )

    return "\n".join(sections) if sections else '<div class="note">无 PR 数据</div>'


def _render_worktrees(worktrees: list[dict]) -> str:
    if not worktrees:
        return '<div class="note">无 worktree（不在 git 仓库内？）</div>'
    rows = "\n".join(
        f'<tr><td><code>{_esc(w.get("path", ""))}</code></td>'
        f'<td><b>{_esc(w.get("branch") or "（detached）")}</b></td>'
        f'<td class="cd">{_esc((w.get("head_sha") or "—")[:8])}</td>'
        f'<td><span class="b info">活跃</span></td></tr>'
        for w in worktrees
    )
    return f'<div class="tbl"><table><tr><th>路径</th><th>分支</th><th>HEAD</th><th>状态</th></tr>{rows}</table></div>'
