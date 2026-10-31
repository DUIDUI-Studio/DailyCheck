"""scripts.dailycheck_dashboard.ops_parse —— 运维事实采集（纯解析 + 只读查询）。

三个数据源，各自独立降级（一源坏了不能整页白屏）：

  1. /var/lib/dailycheck-deploy/audit.log  部署阶段时间戳（UTC，追加写）
  2. backups/prod-verifications/*.md       八项验证结果（verify.sh 产出）
  3. <hermes>/state.db  sessions 表          token 用量（只读打开，不复制 2GB 文件）

设计取舍：
  - audit.log 没有 phase 语义可依赖（phase 字段记的是「执行前」的快照，跨脚本漂移），
    所以只按**脚本身份 + 时间戳顺序**切段：up.sh / verify.sh / promote.sh / switch.sh。
  - 同一条 PR 的 switch.sh 会出现多次（verify check 4 的回滚演练也调 switch.sh），
    取**最后一次**才是真正的切流。
  - 「中断」不猜：段耗时超过阈值就标 stalled，渲染侧只上色，不改事实。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# 阶段脚本（按部署主链顺序）
STAGE_SCRIPTS = ("up.sh", "verify.sh", "promote.sh", "switch.sh")

# 这些阶段正常应当是秒级；超过阈值即“卡住”（verify 天然 10-15 分钟，不在此列）
FAST_STAGES = ("up", "promote")
STALL_THRESHOLD_SEC = 300

AUDIT_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(?P<script>\S+)\s*(?P<rest>.*)$"
)
PR_ARG_RE = re.compile(r"\bpr=(\d+)\b")
# pipeline-event 行（F1/F4/F5 新增的异常留痕）：kind=<k>，detail 跟在 sha/session 之后
KIND_ARG_RE = re.compile(r"\bkind=([\w-]+)\b")
# session 有两个来源：阶段行的行尾 JSON（lock.session）、pipeline-event 的 session= 参数。
# 两处都必须认，否则人工接管统计会漏掉一半（单一归一化口径，见 Issue #51→#66 教训）。
SESSION_JSON_RE = re.compile(r'"session":\s*"([^"]*)"')
SESSION_ARG_RE = re.compile(r"\bsession=(\S+)")
VERIF_NAME_RE = re.compile(r"(?P<date>\d{8})-pr-(?P<pr>\d+)\.md$")
VERIF_TS_RE = re.compile(r"报告生成时间[:：]\s*(\S+)")

# 自动闭环会话的命名前缀。判定用**白名单**：只有确定是自动的才算自动，
# 其余非空 session 一律归「人工介入」——宁可多报也不漏报（与「零新增失败」同一哲学）。
# 实测样本（88 行 audit.log）：hermes-* / pipeline-* / webhook-* / release-* = 自动；
# dev-takeover / dev-recovery-101 / dev-111 / manual-switch-1018 = 人工。
AUTO_SESSION_PREFIXES = ("hermes-", "pipeline-", "webhook-", "release-")


class OpsParseError(RuntimeError):
    """数据源不可用 / 格式不认。调用方应降级为「该区块显示不可用」。"""


# ============================================================
# 时间工具（内部一律 UTC ISO，显示才转 CST+8）
# ============================================================
def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def utc_to_cst(ts: str | None) -> str:
    """'2026-10-17T13:18:37Z' → '2026-10-17 21:18'。解析不了就原样返回。"""
    if not ts:
        return ""
    try:
        return (_parse_ts(ts) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def _diff_sec(a: str, b: str) -> int | None:
    try:
        return int((_parse_ts(b) - _parse_ts(a)).total_seconds())
    except ValueError:
        return None


# ============================================================
# 1. audit.log → 部署事件
# ============================================================
def parse_audit_text(text: str) -> list[dict]:
    """逐行解析 audit.log 为 {ts, script, pr, sha}（sha 可缺）。

    行格式：`2026-10-17T05:48:41Z up.sh pr=85 {"active":…,"lock":{…}}`
    JSON 是行尾的对象；pr 优先取 JSON 里的 lock.pr，退回 `pr=` 参数。
    """
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = AUDIT_LINE_RE.match(line)
        if not m:
            continue
        rest = m.group("rest")
        payload: dict = {}
        # ⚠️ 必须用 find（第一个 `{`＝行尾 JSON 对象的起点），不能用 rfind：
        #    JSON 里嵌了 "lock": {...}，rfind 会抓到内层对象，解析必然失败
        #    → lock.pr/lock.sha 全丢，promote/switch 行会被整行丢掉（2026-10-17 实 hit）。
        brace = rest.find("{")
        args = rest
        if brace >= 0:
            args = rest[:brace]
            try:
                payload = json.loads(rest[brace:])
            except json.JSONDecodeError:
                payload = {}
        lock = payload.get("lock") if isinstance(payload, dict) else None
        lock = lock if isinstance(lock, dict) else {}
        pr = lock.get("pr")
        if not isinstance(pr, int):
            pm = PR_ARG_RE.search(args)
            pr = int(pm.group(1)) if pm else None
        if pr is None:
            continue
        # session：优先行尾 JSON 的 lock.session（阶段行），退回 session= 参数
        # （pipeline-event 行）。两个来源同一口径，避免统计漏一半。
        session = lock.get("session")
        if not isinstance(session, str) or not session:
            sm = SESSION_ARG_RE.search(args)
            session = sm.group(1) if sm else ""
        # kind：只有 pipeline-event 行才有（异常留痕），阶段行为空
        km = KIND_ARG_RE.search(args)
        out.append({
            "ts": m.group("ts"),
            "script": m.group("script").rsplit("/", 1)[-1],
            "pr": pr,
            "sha": lock.get("sha") or None,
            "session": session or "",
            "kind": km.group(1) if km else "",
        })
    return out


def _stage_marks(marks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """原始 (ts, script) 序列 → 各阶段代表时间戳，按时间升序。

    up/verify/promote 取首次出现（命令只跑一次），switch 取最后一次（切流）。
    """
    picked: dict[str, str] = {}
    for ts, script in marks:
        if script not in STAGE_SCRIPTS:
            continue
        if script == "switch.sh":
            picked[script] = ts
        else:
            picked.setdefault(script, ts)
    return sorted((ts, name[:-3]) for name, ts in picked.items())


def build_deploy_events(records: list[dict]) -> list[dict]:
    """按 (pr, sha) 归组并切段。返回 deploy_events 行（未落库形态）。

    段名 = 「该段起点阶段」：up / verify / promote。
    只有 ≥2 个阶段点时才产生段，单点部署（只 up 过）总时长为 0。

    ⚠️ 只用 STAGE_SCRIPTS 的记录归组：`pipeline-event`（F1/F4/F5 新增的异常
    留痕行）不是部署阶段，若一并归组会为「只出现过一条事件、从没跑过 up」的
    PR 造出幽灵部署事件（segments=[]、total=0），污染「今日部署」计数。
    """
    groups: dict[tuple[int, str], list[tuple[str, str]]] = {}
    last_sha: dict[int, str] = {}
    for rec in records:
        if rec.get("script") not in STAGE_SCRIPTS:
            continue
        if rec.get("sha"):
            last_sha[rec["pr"]] = rec["sha"]
        sha = rec.get("sha") or last_sha.get(rec["pr"]) or ""
        groups.setdefault((rec["pr"], sha), []).append((rec["ts"], rec["script"]))

    events: list[dict] = []
    for (pr, sha), raw_marks in groups.items():
        marks = sorted(raw_marks)
        stages = _stage_marks(marks)
        segments: list[dict] = []
        for i in range(len(stages) - 1):
            ts, name = stages[i]
            nxt = stages[i + 1][0]
            sec = _diff_sec(ts, nxt)
            segments.append({
                "name": name,
                "sec": sec,
                "stalled": bool(
                    sec is not None and sec > STALL_THRESHOLD_SEC and name in FAST_STAGES
                ),
            })
        total = _diff_sec(marks[0][0], marks[-1][0]) if len(marks) > 1 else 0
        max_gap = max(
            (_diff_sec(marks[i][0], marks[i + 1][0]) or 0)
            for i in range(len(marks) - 1)
        ) if len(marks) > 1 else 0
        events.append({
            "pr": pr,
            "sha": sha,
            "started_at": marks[0][0],
            "finished_at": marks[-1][0],
            "total_sec": total,
            "max_gap_sec": max_gap,
            "segments": segments,
        })
    events.sort(key=lambda e: e["started_at"], reverse=True)
    return events


def classify_session(session: str) -> str:
    """会话来源分类：``auto`` / ``manual`` / ``unlabeled``。

    白名单式：只有确定是自动闭环的前缀才算 ``auto``，其余非空一律 ``manual``。
    宁可多报不漏报——漏报人工接管会让「闭环成立」这个结论变成假的。

    空 session 单列 ``unlabeled``：session 机制是后加的（实测 88 行里 58 行为空），
    把空值算进任何一侧都会让比率失真。
    """
    if not session:
        return "unlabeled"
    return "auto" if session.startswith(AUTO_SESSION_PREFIXES) else "manual"


# 异常留痕事件的已知 kind（F1/F4/F5 由 deploy_pipeline.sh 的 _audit_event 写入）。
# 仅作解析层的「已知集合」参考；kind → 人话的**展示映射**在 render_ops.py
# （展示层职责，解析层不碰文案）。未知 kind 照样透传，渲染侧原样显示不丢。
KNOWN_EVENT_KINDS = frozenset({
    "worktree-drift",
    "observe-window-shortened",
    "skip-verify",
    "dashboard-hook-failed",
})

# 写这些 kind 的埋点所在文件（相对仓库根）。探测它有没有 `_audit_event`
# 就能判定「异常留痕为 0」到底是「真没发生」还是「压根没人在记」。
EVENT_INSTRUMENTATION_SCRIPT = "scripts/blue_green/deploy_pipeline.sh"
# ⚠️ 匹配**函数定义形态**（行首 `_audit_event()` 或 `_audit_event ()`），
# 不能做裸子串匹配：脚本里的注释 / 文档字符串只要提到 `_audit_event`
# 就会让探测假阳性判成 deployed，「0 起 = 无异常」这个假信号随即复活
# （2026-10-19 实现时实测命中）。
EVENT_INSTRUMENTATION_RE = re.compile(r"^\s*_audit_event\s*\(\s*\)\s*\{", re.M)


def detect_event_instrumentation(repo_dir: Path) -> str:
    """探测异常留痕埋点是否已部署。返回三态：

    - ``"deployed"``    已部署脚本定义了 `_audit_event` → 事件数为 0 是真没发生
    - ``"absent"``      脚本存在但无该函数 → **0 不代表没发生**，只是没人在记
    - ``"unknown"``     脚本读不到（路径不对 / 权限）→ 无法判定，不猜

    ⚠️ 为什么必须三态而不是 bool：``absent`` 与 ``unknown`` 的处置不同。
    前者要明说「埋点未部署，别把 0 读成没问题」；后者连「没部署」都不能断言
    —— 把「读不到」说成「未部署」同样是编造事实（与本模块「不猜」原则一致）。

    背景（2026-10-19 实测）：#118 先于 #116 合并，生产脚本仍是
    `PY="${PYTHON:-python3}"` 且无 `_audit_event`，看板却显示
    「钩子失败均未发生」—— 而实测生产系统 python3 对
    `scripts.dailycheck_dashboard` 抛 ModuleNotFoundError，即钩子一直是坏的。
    这正是本模块铁律 #4 要禁止的「把没记录说成没发生」。
    """
    script = repo_dir / EVENT_INSTRUMENTATION_SCRIPT
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"
    return "deployed" if EVENT_INSTRUMENTATION_RE.search(text) else "absent"


def build_loop_integrity(
    records: list[dict], repo_dir: Path | None = None
) -> dict:
    """从 audit.log 记录算「闭环完整性」——这套流程到底有没有真闭环。

    为什么要有这个（2026-10-18 复盘）：白皮书声称「CI 全绿→合并人工介入 = 0」，
    但那是**取样偏差**——它只统计到 merge，而事故全发生在 merge 之后。
    把窗口延长到观察窗结束，实测两天 3 次人工接管。这类事实必须从看板可见，
    不能只在事后 review 对话里才发现。

    ⚠️ 只报有事实依据的数字，不猜（与 `_render_anomaly` 同一原则）：
      - 人工/自动比率**只在有标注的记录内**算，未标注条数单独透出
      - 回滚次数**不可观测**（`switch.sh` 只审计 target=green，切蓝不留痕），
        故列入 blind_spots，而不是报「0 次」——把「没记录」说成「没发生」
        比不报更坏
      - 异常事件数同理：``repo_dir`` 给定时探测埋点是否已部署，未部署则
        计入 blind_spots，绝不渲染成「均未发生」
    """
    auto = manual = unlabeled = 0
    manual_sessions: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for rec in records:
        cls = classify_session(rec.get("session") or "")
        if cls == "auto":
            auto += 1
        elif cls == "manual":
            manual += 1
            s = rec.get("session") or ""
            manual_sessions[s] = manual_sessions.get(s, 0) + 1
        else:
            unlabeled += 1
        kind = rec.get("kind") or ""
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1

    labeled = auto + manual

    # 异常留痕埋点状态：未部署时「0 起」不代表没发生，必须计入盲区
    instrumentation = (
        detect_event_instrumentation(repo_dir) if repo_dir is not None else "unknown"
    )
    blind = [
        "回滚次数不可观测：switch.sh 只审计 target=green，切蓝（回滚）不留痕",
    ]
    if instrumentation != "deployed":
        why = (
            "埋点未部署（deploy_pipeline.sh 无 _audit_event）"
            if instrumentation == "absent"
            else "埋点状态不可知（deploy_pipeline.sh 读不到）"
        )
        blind.append(f"异常留痕事件不可观测：{why} → 「0 起」不代表没发生")

    return {
        "auto_records": auto,
        "manual_records": manual,
        "unlabeled_records": unlabeled,
        # 比率分母只含已标注记录；labeled=0 时给 None（渲染成「—」，不是 0%）
        "auto_rate": (auto / labeled) if labeled else None,
        "manual_sessions": sorted(manual_sessions),
        "manual_prs": sorted({int(r["pr"]) for r in records
                              if classify_session(r.get("session") or "") == "manual"
                              and r.get("pr") is not None}),
        "events": kinds,
        # 只有埋点真部署了，事件数为 0 才有「无异常」的资格
        "events_observable": instrumentation == "deployed",
        "instrumentation": instrumentation,
        "blind_spots": blind,
    }


def _read_audit_text(path: Path, max_bytes: int) -> str:
    """读 audit.log 尾部（超出 max_bytes 则丢弃被截断的首行）。"""
    if not path.exists():
        raise OpsParseError(f"audit.log 不存在：{path}")
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # 丢掉被截断的首行
            return fh.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise OpsParseError(f"audit.log 读失败：{exc}") from exc


def parse_audit_records(path: Path, max_bytes: int = 4 * 1024 * 1024) -> list[dict]:
    """读 audit.log → **原始记录**（一次读取，供 events 与 integrity 共用）。

    单一归一化口径：部署事件与闭环完整性必须由同一份记录派生，
    否则两侧会漂移（同型缺陷见 inbox_watcher，Issue #51→#66）。
    """
    return parse_audit_text(_read_audit_text(path, max_bytes))


def parse_audit_log(path: Path, max_bytes: int = 4 * 1024 * 1024) -> list[dict]:
    """读 audit.log 尾部并出部署事件。不存在 → OpsParseError。"""
    return build_deploy_events(parse_audit_records(path, max_bytes))


# ============================================================
# 2. prod-verifications/*.md → 八项验证结果
# ============================================================
def parse_verification_text(text: str, pr: int, generated_at: str = "") -> dict | None:
    """解析一份验证报告。表格行 `| 1 | 集成测试 … | ✅ PASS | 备注 |`。"""
    checks: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        checks.append({
            "n": int(cells[0]),
            "name": cells[1],
            "ok": "✅" in cells[2],
            "note": cells[3] if len(cells) > 3 else "",
        })
    if not checks:
        return None
    ts = ""
    m = VERIF_TS_RE.search(text)
    if m:
        ts = m.group(1)
    return {
        "pr": pr,
        "generated_at": ts or generated_at,
        "passed": sum(1 for c in checks if c["ok"]),
        "total": len(checks),
        "checks": checks,
    }


def parse_verifications(directory: Path) -> list[dict]:
    """目录下全部 `YYYYMMDD-pr-N.md` → 按生成时间升序。"""
    if not directory.is_dir():
        raise OpsParseError(f"验证报告目录不存在：{directory}")
    out: list[dict] = []
    for path in sorted(directory.glob("*-pr-*.md")):
        m = VERIF_NAME_RE.search(path.name)
        if not m:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%SZ")
        rec = parse_verification_text(text, int(m.group("pr")), generated_at=mtime)
        if rec is not None:
            rec["path"] = str(path)
            out.append(rec)
    out.sort(key=lambda r: (r["generated_at"], r["pr"]))
    return out


# ============================================================
# 3. Hermes state.db → 日 token 用量
# ============================================================
TOKEN_SQL = """
SELECT date(started_at, 'unixepoch', '+8 hours') AS day,
       source,
       COUNT(*)                              AS sessions,
       SUM(COALESCE(input_tokens, 0))        AS input_tok,
       SUM(COALESCE(output_tokens, 0))       AS output_tok,
       SUM(COALESCE(cache_read_tokens, 0))   AS cache_read_tok,
       SUM(COALESCE(cache_write_tokens, 0))  AS cache_write_tok,
       SUM(COALESCE(reasoning_tokens, 0))    AS reasoning_tok,
       SUM(COALESCE(tool_call_count, 0))     AS tool_calls
FROM sessions
WHERE started_at >= strftime('%s', 'now', ?)
GROUP BY day, source
ORDER BY day, source
"""


def fetch_token_usage(state_db: Path, days: int = 7) -> list[dict]:
    """按 CST 日 × source 汇总 token。

    ⚠️ 只读打开（mode=ro）：state.db 2GB 且 Hermes 在线，绝不复制/写入。
    ⚠️ 成本字段（estimated_cost_usd / actual_cost_usd）在库内恒为 0（2988/2988 实测），
       所以本函数只出 token 数，不出金额——要金额得另配价目表。
    """
    if not state_db.exists():
        raise OpsParseError(f"state.db 不存在：{state_db}")
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        raise OpsParseError(f"state.db 打开失败：{exc}") from exc
    try:
        rows = conn.execute(TOKEN_SQL, (f"-{int(days)} day",)).fetchall()
    except sqlite3.Error as exc:
        raise OpsParseError(f"state.db 查询失败：{exc}") from exc
    finally:
        conn.close()
    return [
        {
            "day": r[0],
            "source": r[1] or "unknown",
            "sessions": int(r[2] or 0),
            "input_tok": int(r[3] or 0),
            "output_tok": int(r[4] or 0),
            "cache_read_tok": int(r[5] or 0),
            "cache_write_tok": int(r[6] or 0),
            "reasoning_tok": int(r[7] or 0),
            "tool_calls": int(r[8] or 0),
        }
        for r in rows
    ]


def parse_deploy_results(path: Path) -> list[dict]:
    """Existing deploy_report events, supplementary evidence only (rc=0 is insufficient)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OpsParseError(f"部署结果不可用：{path}") from exc
    out = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(item, dict) and item.get("pr") and item.get("sha"):
            # Do not expose log paths, free-text reasons, or credentials in the dashboard.
            out.append({k: item.get(k) for k in ("pr", "sha", "rc", "ts", "report_comment")})
    return out
