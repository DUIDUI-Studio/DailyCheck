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
VERIF_NAME_RE = re.compile(r"(?P<date>\d{8})-pr-(?P<pr>\d+)\.md$")
VERIF_TS_RE = re.compile(r"报告生成时间[:：]\s*(\S+)")


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
        out.append({
            "ts": m.group("ts"),
            "script": m.group("script").rsplit("/", 1)[-1],
            "pr": pr,
            "sha": lock.get("sha") or None,
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
    """
    groups: dict[tuple[int, str], list[tuple[str, str]]] = {}
    last_sha: dict[int, str] = {}
    for rec in records:
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


def parse_audit_log(path: Path, max_bytes: int = 4 * 1024 * 1024) -> list[dict]:
    """读 audit.log 尾部并出部署事件。不存在 → OpsParseError。"""
    if not path.exists():
        raise OpsParseError(f"audit.log 不存在：{path}")
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # 丢掉被截断的首行
            text = fh.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise OpsParseError(f"audit.log 读失败：{exc}") from exc
    return build_deploy_events(parse_audit_text(text))


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
