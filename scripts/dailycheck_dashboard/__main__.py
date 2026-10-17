"""scripts.dailycheck_dashboard.__main__ —— CLI 入口。

子命令：
  render   拉数据 + 渲染 HTML + 落 SQLite 快照
  history  列最近 N 份快照
  diff     最近两次快照的 KPI 差值（与上次）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 让本包能以 `python -m scripts.dailycheck_dashboard` 形式被调用
# （scripts/ 不在 sys.path 时，__main__.py 显式注入父目录）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from scripts.dailycheck_dashboard import db, fetchers, ops_db, ops_parse
    from scripts.dailycheck_dashboard import render as render_mod
else:
    from . import db, fetchers, ops_db, ops_parse
    from . import render as render_mod

# 默认路径（与 deploy/systemd/dc-dashboard-generator.service 保持一致）
DEFAULT_DB = Path("/var/lib/dailycheck-dashboard/dashboard.db")
DEFAULT_HTML_DIR = Path("/var/lib/dailycheck-dashboard/snapshots")
DEFAULT_HTML_LATEST = Path("/var/lib/dailycheck-dashboard/latest.html")
DEFAULT_INBOX = Path("/var/lib/dailycheck-prod-agent/inbox.jsonl")
DEFAULT_REPO_DIR = Path("/root/DailyCheck")
DEFAULT_REPO = "ericmr1981/DailyCheck"

# 运维事实的三个源（任一路径不存在 → 该区块降级，不影响其余章节）
DEFAULT_AUDIT_LOG = Path("/var/lib/dailycheck-deploy/audit.log")
DEFAULT_VERIF_DIR = Path("/root/DailyCheck/backups/prod-verifications")
DEFAULT_HERMES_DB = Path("/root/.hermes/state.db")

# CST+8 用于显示时间戳
CST = ZoneInfo("Asia/Shanghai")


def _log(msg: str) -> None:
    print(f"[dashboard] {msg}", flush=True)


def _now_iso_cst() -> str:
    """CST+8 ISO 8601。"""
    return datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _collect(label: str, unavailable: list[str], fn) -> list[dict]:
    """跑一个运维采集器；失败只降级该区块（记入 unavailable），不抛给主流程。"""
    try:
        rows = fn()
    except ops_parse.OpsParseError as exc:
        _log(f"  {label} 不可用：{exc}")
        unavailable.append(label)
        return []
    _log(f"  {label}: {len(rows)}")
    return rows


# ============================================================
# 子命令：render
# ============================================================
def cmd_render(args: argparse.Namespace) -> int:
    """拉数据 → 渲染 → 落 HTML + SQLite。"""
    db_path = Path(args.db)
    html_dir = Path(args.html_dir)
    html_latest = Path(args.html_latest) if args.html_latest else None
    inbox_path = Path(args.inbox)
    repo_dir = Path(args.repo_dir)
    trigger_kind = args.trigger_kind
    trigger_event_id = args.trigger_event_id

    _log(f"render start: db={db_path} trigger={trigger_kind} delivery={trigger_event_id}")

    # 0. 先确保基础表存在：db 文件可能已被写事件表的模块先创建过，
    #    而 latest_snapshot() 只判 exists() 不建表 → 会撞 "no such table: snapshots"
    db.init_schema(db_path)

    # 本轮的降级记录（PR 抓取失败也要记在这里，故先于各 fetch 初始化）
    ops_unavailable: list[str] = []

    # 1. 拉数据（容错：单条失败不阻塞整体）
    try:
        issues = fetchers.fetch_issues(DEFAULT_REPO)
        _log(f"  issues: {len(issues)}")
    except fetchers.FetchError as exc:
        _log(f"  issues fetch FAILED: {exc}")
        issues = []

    try:
        prs = fetchers.fetch_prs(DEFAULT_REPO)
        _log(f"  prs: {len(prs)}")
        pr_data_ok = True
    except fetchers.FetchError as exc:
        _log(f"  prs fetch FAILED: {exc}")
        prs = []
        # 拉不到 ≠ 没有：KPI 必须显示「—」而不是 0（gh 未认证时曾把「今日 0 个合并」当真）
        pr_data_ok = False
        ops_unavailable.append("GitHub PR 列表")

    try:
        worktrees = fetchers.fetch_worktrees(repo_dir)
        _log(f"  worktrees: {len(worktrees)}")
    except fetchers.FetchError as exc:
        _log(f"  worktrees fetch FAILED: {exc}")
        worktrees = []

    head_sha, origin_main_sha = fetchers.fetch_head_shas(repo_dir)
    _log(f"  HEAD={head_sha} origin/main={origin_main_sha}")

    try:
        inbox_stats = fetchers.fetch_inbox_stats(inbox_path)
    except fetchers.FetchError as exc:
        _log(f"  inbox fetch FAILED: {exc}")
        inbox_stats = {"lines": 0, "deliveries": 0, "last_action_at": None, "last_pr_number": None}

    summary = fetchers.compute_summary(issues=issues, prs=prs, worktrees=worktrees)
    _log(f"  summary: {summary}")

    # 1b. 运维事实（三个源各自降级：单源坏了只丢那一块）
    ops_fetched_at = _now_iso_cst()
    deploy_events = _collect(
        "部署审计日志", ops_unavailable,
        lambda: ops_parse.parse_audit_log(Path(args.audit_log)),
    )
    verifications = _collect(
        "验证报告", ops_unavailable,
        lambda: ops_parse.parse_verifications(Path(args.verif_dir)),
    )
    token_rows = _collect(
        "Hermes state.db", ops_unavailable,
        lambda: ops_parse.fetch_token_usage(Path(args.hermes_db), days=args.token_days),
    )
    _log(
        f"  ops: deploy={len(deploy_events)} verif={len(verifications)} "
        f"token={len(token_rows)} 不可用={ops_unavailable or '无'}"
    )

    # 1c. 落事件表（幂等 upsert）；写失败不阻塞渲染
    try:
        ops_db.init_ops_schema(db_path)
        ops_db.upsert_deploy_events(db_path, deploy_events)
        ops_db.upsert_token_daily(db_path, token_rows)
        ops_db.upsert_verifications(db_path, verifications)
    except (sqlite3.Error, OSError, ValueError) as exc:
        _log(f"  ops db 写入失败（忽略，不影响渲染）：{exc}")

    # 解析全失败时回落到库里已有事实，避免整块空白
    if not deploy_events:
        deploy_events = ops_db.recent_deploy_events(db_path, limit=10)
    if not verifications:
        verifications = ops_db.recent_verifications(db_path, limit=6)
    if not token_rows:
        token_rows = ops_db.token_series(db_path, days=args.token_days)
    _log(
        f"  ops(渲染用): deploy={len(deploy_events)} verif={len(verifications)} "
        f"token={len(token_rows)}"
    )

    # 2. 与上次快照对比（diff）
    prev = db.latest_snapshot(db_path)
    diff: dict = {}
    if prev is not None:
        prev_s = prev.get("summary", {})
        for key in ("open_issues", "merged_7d_total", "worktrees_total", "closed_unmerged"):
            if key in prev_s:
                diff[key] = prev_s[key]
    _log(f"  diff: {diff}")

    # 3. 渲染 HTML
    ri = render_mod.RenderInput(
        created_at=_now_iso_cst(),
        trigger_kind=trigger_kind,
        trigger_event_id=trigger_event_id,
        head_sha=head_sha,
        origin_main_sha=origin_main_sha,
        summary=summary,
        issues=issues,
        prs=prs,
        worktrees=worktrees,
        inbox_stats=inbox_stats,
        diff=diff,
        deploy_events=deploy_events,
        verifications=verifications,
        token_rows=token_rows,
        ops_unavailable=ops_unavailable,
        ops_fetched_at=ops_fetched_at,
        pr_data_ok=pr_data_ok,
    )
    html_str = render_mod.render(ri)

    # 4. 落 HTML（先写 latest.html，让 nginx / curl 立刻能看到；snapshot id 拿到后再写一份）
    html_dir.mkdir(parents=True, exist_ok=True)
    if html_latest is not None:
        html_latest.parent.mkdir(parents=True, exist_ok=True)
        html_latest.write_text(html_str, encoding="utf-8")
        _log(f"  wrote latest: {html_latest}")

    # 5. 落 SQLite（html_path 先传空字符串，拿到 id 后回填）
    snap_id = db.insert_snapshot(
        db_path,
        trigger_kind=trigger_kind,
        trigger_event_id=trigger_event_id,
        head_sha=head_sha,
        origin_main_sha=origin_main_sha,
        summary=summary,
        html_path="",
        issues=issues,
        prs=prs,
        worktrees=worktrees,
    )

    # 6. 把 HTML 落 snapshots/<id>.html，并回填 db.html_path
    snap_path = html_dir / f"{snap_id}.html"
    snap_path.write_text(html_str, encoding="utf-8")
    _log(f"  wrote snapshot: {snap_path}")

    with db.connect(db_path) as conn:
        with conn:
            conn.execute(
                "UPDATE snapshots SET html_path = ? WHERE id = ?",
                (str(snap_path), snap_id),
            )
    _log(f"render OK: snapshot_id={snap_id}")
    return 0


# ============================================================
# 子命令：history
# ============================================================
def cmd_history(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    snaps = db.list_snapshots(db_path, limit=args.limit)
    if not snaps:
        print("(no snapshots yet)")
        return 0
    print(f"{'id':>5}  {'created_at':<25}  {'trigger':<14}  {'head':<10}  summary")
    print("-" * 90)
    for s in snaps:
        head = (s.get("head_sha") or "—")[:8]
        sm = s.get("summary", {})
        sm_str = ", ".join(f"{k}={v}" for k, v in sm.items())
        print(
            f"{s['id']:>5}  {s['created_at']:<25}  {s['trigger_kind']:<14}  "
            f"{head:<10}  {sm_str}"
        )
    return 0


# ============================================================
# 子命令：diff
# ============================================================
def cmd_diff(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    cur = db.latest_snapshot(db_path)
    if cur is None:
        print("(no current snapshot)")
        return 0
    prev = db.previous_snapshot(db_path)
    if prev is None:
        print(f"(no previous snapshot; current id={cur['id']})")
        return 0

    print(f"current: id={cur['id']}  {cur['created_at']}  trigger={cur['trigger_kind']}")
    print(f"previous: id={prev['id']}  {prev['created_at']}  trigger={prev['trigger_kind']}")
    print()
    print(f"{'metric':<20}  {'prev':>8}  {'cur':>8}  {'Δ':>6}")
    print("-" * 50)
    cs = cur.get("summary", {})
    ps = prev.get("summary", {})
    keys = sorted(set(cs.keys()) | set(ps.keys()))
    for k in keys:
        cv = cs.get(k)
        pv = ps.get(k)
        if isinstance(cv, (int, float)) and isinstance(pv, (int, float)):
            delta = cv - pv
            print(f"{k:<20}  {pv:>8}  {cv:>8}  {delta:>+6}")
    return 0


# ============================================================
# argparse
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DailyCheck 项目任务仪表盘（PR webhook 触发）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_render = sub.add_parser("render", help="拉数据 + 渲染 HTML + 落 SQLite")
    p_render.add_argument("--db", default=str(DEFAULT_DB), help="SQLite db 路径")
    p_render.add_argument("--html-dir", default=str(DEFAULT_HTML_DIR), help="HTML 快照目录")
    p_render.add_argument("--html-latest", default=str(DEFAULT_HTML_LATEST), help="最新 HTML 路径")
    p_render.add_argument("--inbox", default=str(DEFAULT_INBOX), help="inbox.jsonl 路径")
    p_render.add_argument("--repo-dir", default=str(DEFAULT_REPO_DIR), help="git worktree 仓库根")
    p_render.add_argument(
        "--trigger-kind", default="manual",
        choices=sorted(db.TRIGGER_KINDS),
        help="触发来源（manual / deploy / timer / pr_opened …）",
    )
    p_render.add_argument("--trigger-event-id", default=None, help="inbox.jsonl 行的 delivery_id（可选）")
    p_render.add_argument("--audit-log", default=str(DEFAULT_AUDIT_LOG),
                          help="部署审计日志（deploy_state 追加写）")
    p_render.add_argument("--verif-dir", default=str(DEFAULT_VERIF_DIR),
                          help="prod-verifications 目录（八项验证报告）")
    p_render.add_argument("--hermes-db", default=str(DEFAULT_HERMES_DB),
                          help="Hermes state.db（只读，取 token 用量）")
    p_render.add_argument("--token-days", type=int, default=7,
                          help="token 汇总回溯天数（默认 7）")
    p_render.set_defaults(func=cmd_render)

    p_hist = sub.add_parser("history", help="列最近 N 份快照")
    p_hist.add_argument("--db", default=str(DEFAULT_DB))
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    p_diff = sub.add_parser("diff", help="最近两次快照 KPI 差值")
    p_diff.add_argument("--db", default=str(DEFAULT_DB))
    p_diff.set_defaults(func=cmd_diff)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
