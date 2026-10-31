"""Self-contained, accessible worktree evaluation. Data is inert JSON, never JS source."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from .eval_facts import compact_ui_facts, prepare_facts

CSS = """
.eval-module{--eval-green:#26715c;margin-bottom:26px;font-size:12px;min-width:0}
.eval-module p{line-height:1.8;color:var(--t2)}
.eval-module .ev-ribbon{border-left:3px solid var(--eval-green);padding:10px 13px;background:#edf3ef}
.ev-stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border:1px solid var(--line);border-radius:7px;background:var(--card);margin:16px 0}
.ev-stat{padding:16px;border-right:1px solid var(--line);min-width:0}.ev-stat:last-child{border:0}
.ev-stat b{display:block;font:500 23px ui-monospace,monospace;margin:9px 0;overflow-wrap:anywhere}
.ev-stat small,.ev-muted{color:var(--t2);font-size:11px;display:block;line-height:1.7}
.ev-toolbar{display:flex;gap:9px;flex-wrap:wrap;align-items:end;margin-bottom:14px}
.ev-toolbar label{display:grid;gap:5px;min-width:0}.ev-toolbar .ev-search{flex:1 1 230px}
.ev-toolbar input,.ev-toolbar select,.eval-module button{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:5px;padding:9px;max-width:100%;min-height:38px}
.ev-toolbar input,.ev-toolbar select{min-width:0;width:100%}
.eval-module button{cursor:pointer}.eval-module :focus-visible{outline:2px solid var(--eval-green);outline-offset:2px}
.ev-custom{display:flex;gap:8px;flex-wrap:wrap}.ev-custom[hidden]{display:none}
.eval-module .ev-table{min-width:1130px;width:100%;border-collapse:collapse;text-align:left}
.eval-module .ev-table th{background:#eef2f0;color:var(--t2);font-weight:500;white-space:nowrap}
.eval-module .ev-table td,.eval-module .ev-table th{padding:13px 10px;border-bottom:1px solid var(--line);vertical-align:top;line-height:1.7}
.ev-table>thead th:first-child{position:sticky;left:0;z-index:2;background:#eef2f0}
.ev-table .ev-tree{position:sticky;left:0;background:var(--card);max-width:230px;min-width:155px;z-index:1}
.ev-tree button{padding:0;min-height:32px;border:0;text-align:left;font-weight:600;overflow-wrap:anywhere}
.ev-pill{display:inline-block;border:1px solid var(--line);border-radius:3px;padding:1px 5px;margin:2px 3px 2px 0;font-size:10px;overflow-wrap:anywhere;max-width:190px}
.ev-num{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums;white-space:nowrap}
.ev-detail>td{background:#f4f7f5}.ev-detail details{padding:8px 0;border-bottom:1px solid var(--line)}
.ev-detail summary{cursor:pointer;line-height:1.9;overflow-wrap:anywhere}.ev-details-body{padding:8px;overflow-wrap:anywhere}
.ev-details-body dl{display:grid;grid-template-columns:100px minmax(0,1fr);gap:5px}.ev-details-body dd{margin:0}
.ev-details-body .tbl{max-width:100%}.ev-details-body table{min-width:650px;width:100%}
.ev-footer{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0}
.ev-status{font-weight:600}.ev-delivered{color:var(--eval-green)}.ev-count{color:var(--t2)}
.ev-definitions{background:#edf3ef;padding:12px;margin:12px 0;line-height:1.9}
.ev-aux table{min-width:650px;width:100%}.ev-empty{padding:25px;text-align:center}
@media(max-width:760px){.ev-stats{grid-template-columns:repeat(2,minmax(0,1fr))}.ev-stat{border-bottom:1px solid var(--line)}.ev-stat:last-child{grid-column:1/-1}.ev-toolbar label{flex:1 1 130px}.ev-toolbar .ev-search{flex-basis:100%}
.eval-module>.tbl{overflow:visible}.eval-module .ev-table{min-width:0;display:block}.ev-table>thead{display:none}.ev-table>tbody{display:grid;gap:12px}.ev-table>tbody>tr{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));border:1px solid var(--line);border-radius:6px;background:var(--card);min-width:0}
.eval-module .ev-table>tbody>tr>td{display:block;min-width:0;padding:10px;overflow-wrap:anywhere}.ev-table>tbody>tr>td:before{content:attr(data-label);display:block;font-size:10px;color:var(--t2);margin-bottom:3px}
.eval-module .ev-table .ev-tree{grid-column:1/-1;position:static;max-width:none}.ev-table .ev-detail>td{grid-column:1/-1;max-width:100%}.ev-table .ev-detail{display:block}.ev-table .ev-detail>td:before{display:none}.ev-pill{max-width:100%}.ev-details-body{padding:0}.ev-details-body dl{grid-template-columns:80px minmax(0,1fr)}}
"""


def render_worktree_eval(
    *, rows, prs, snapshot=None, pr_data_ok=True, unavailable=False, deploy_results=()
):
    snapshot = snapshot or datetime.now(timezone.utc).isoformat()
    facts = prepare_facts(
        rows,
        prs,
        snapshot=snapshot,
        pr_data_ok=pr_data_ok,
        unavailable=unavailable,
        deploy_results=deploy_results,
    )
    facts = compact_ui_facts(facts)
    payload = json.dumps(
        {"facts": facts, "snapshot": snapshot, "pr_data_ok": pr_data_ok},
        ensure_ascii=False,
        allow_nan=False,
    )
    payload = (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    message = "本轮数据源不可用，回落到库内历史。" if unavailable else ""
    if not pr_data_ok:
        message += "Gitea PR 拉取失败；关联状态未知。"
    if not facts:
        message += "派单账本暂无数据。"
    script = Path(__file__).with_name("eval_view.js").read_text(encoding="utf-8")
    return f"""<style>{CSS}</style><section class="eval-module" aria-labelledby="eval">
<h2 id="eval"><span class="no">6</span>模型效能评估 <span class="hint">工作树 → Worker → 执行 → 模型用量</span></h2>
<p class="ev-ribbon">从工作树看资源投入与交付进展。已知合计保留覆盖率，缺失为 —。{html.escape(message)}</p>
<div class="ev-stats" id="ev-stats" aria-live="polite"></div>
<div class="ev-toolbar">
<label class="ev-search">搜索<input id="ev-search" type="search" placeholder="工作树 / 分支 / Issue / PR / 模型" aria-label="搜索评估记录"></label>
<label>统计窗口<select id="ev-window"><option value="7">近 7 天</option><option value="1">今日</option><option value="30">近 30 天</option><option value="all">全生命周期</option><option value="custom">自定义日期</option></select></label>
<label>角色<select id="ev-role"><option value="">全部角色</option></select></label>
<label>实际模型<select id="ev-model"><option value="">全部模型</option></select></label>
<label>交付状态<select id="ev-status"><option value="">全部状态</option></select></label>
<label>数据新鲜度<select id="ev-quality"><option value="">全部数据</option><option value="fresh">新鲜</option><option value="stale">陈旧</option><option value="unknown">新鲜度未知</option><option value="unavailable">源不可用</option></select></label>
<label>排序<select id="ev-sort"><option value="recent">最近活动</option><option value="tokens">Token 从高到低</option><option value="workers">Worker 数</option><option value="time">累计运行时长</option><option value="name">工作树名称</option></select></label>
</div>
<div class="ev-custom" id="ev-custom" hidden><label>开始日期 <input type="date" id="ev-start" aria-label="开始日期"></label><label>结束日期（含） <input type="date" id="ev-end" aria-label="结束日期"></label></div>
<p id="ev-window-note"></p>
<details class="ev-definitions"><summary>指标口径与覆盖边界</summary>日期按 Asia/Shanghai，边界转换为 UTC 左闭右开。时间完全未知的记录仅在全生命周期视图显示。原始事件保留在账本，页面按执行/会话/实际模型/CST 日精确聚合展示，保留事件数与首末引用；日期筛选总量不截断。事件按发生日期归窗；历史整次总量仅按派单时间归档，不能视为精确日用量。运行时长在窗口截断，同 Worker 先求区间并集，再跨 Worker 累加；工作树历时包含执行间隔。模型筛选时多模型执行的时长无法拆分，保留未知。推理已包含在输出中，只展示子项。<br>已识别 Worker 与未知身份执行分别列出；未归属会话不冒充 Worker。历史路径归组不保证生命周期身份，未知结束不延伸到当前时间。仅有派单源覆盖的工作树可发现会话，未纳入的直接会话无法计量。PR 是关联集合，状态截至快照，非窗口内交付吞吐；同一 PR 跨工作树再次去重。完整交付需要相应 SHA 的两必需 CI、合并、Issue 关闭、生产验证及完整观察窗证据，merged 不等于 delivered。数据新鲜阈值 30 分钟，报告快照不代替采集时间。</details>
<div class="tbl"><table class="ev-table"><thead><tr><th>工作树 / 分支</th><th>PR 关联</th><th>Worker / 执行</th><th>实际角色</th><th>实际模型</th><th>Token 已知合计</th><th>Worker 累计运行</th><th>工作树历时</th><th>交付 / 覆盖</th></tr></thead><tbody id="ev-rows"></tbody></table></div>
<div class="ev-footer"><span id="ev-count" role="status"></span><div><button id="ev-prev" type="button">上一页</button> <span id="ev-page"></span> <button id="ev-next" type="button">下一页</button></div></div>
<details class="ev-aux"><summary>辅助分析 · 角色 × 实际模型 × 复杂度</summary><p>来自相同筛选事实；多模型用量仅进入对应模型段，无法拆分的历史总量单列。样本非独立 PR 归功或模型排名。</p><div class="tbl"><table><thead><tr><th>角色</th><th>实际模型</th><th>复杂度</th><th>执行样本</th><th>已知 Token</th></tr></thead><tbody id="ev-aux"></tbody></table></div></details>
<noscript>筛选与展开需要 JavaScript；原始事实已保留在账本，当前未执行交互投影。</noscript>
<script type="application/json" id="ev-data">{payload}</script><script>{script}</script></section>"""
