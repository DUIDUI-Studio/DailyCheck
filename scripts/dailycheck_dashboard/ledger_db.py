"""scripts.dailycheck_dashboard.ledger_db —— 派单账本（Issue #91 采集 → Issue #92 摄取）。

账本是 append-only JSONL，每行一次派单（字段约定见 Issue #91 / #92，不得单方面改名）。
与 ops_db 的分工：
  ops_db 存部署 / token / 验证这类**系统观测**；本模块存**派单决策**（模型、复杂度、
  成本、重派）。看板每 15 分钟渲染一次，故沿用 ops_db 的幂等 upsert 口径：
  唯一键 (worker_id, ts)，重复摄取只覆盖不新增。

与 db.py 的关系：只借用 connect()（WAL + foreign_keys + autocommit 语义），
不改 db.py 的任何既有表；init_ledger_schema() 由 CLI 显式调用。
"""
from __future__ import annotations

import json
import math
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .db import _now_iso, connect
from .ops_parse import OpsParseError

SCHEMA_VERSION = 1

TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")

# 复杂度分档（与 Issue #92 约定一致）：<4.0 / 4.0–6.4 / >=6.5 / unknown
BAND_LOW = "low"
BAND_MID = "mid"
BAND_HIGH = "high"
BAND_UNKNOWN = "unknown"
BAND_ORDER = {BAND_LOW: 0, BAND_MID: 1, BAND_HIGH: 2, BAND_UNKNOWN: 3}

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS dispatch_ledger (
    worker_id        TEXT    NOT NULL,
    ts               TEXT    NOT NULL,
    issue            INTEGER,
    role             TEXT,
    agent            TEXT,
    model_requested  TEXT,
    model_effective  TEXT,
    effort           TEXT,
    complexity_score REAL,
    task_id          TEXT,
    worktree_path    TEXT,
    pr               INTEGER,
    tokens_json      TEXT,
    cost             REAL,
    warnings_json    TEXT,
    updated_at       TEXT    NOT NULL,
    PRIMARY KEY (worker_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_dispatch_ledger_ts
    ON dispatch_ledger(ts);
CREATE INDEX IF NOT EXISTS idx_dispatch_ledger_issue
    ON dispatch_ledger(issue);
CREATE INDEX IF NOT EXISTS idx_dispatch_ledger_role
    ON dispatch_ledger(role);
"""


def init_ledger_schema(db_path: Path) -> None:
    """幂等建表（autocommit 下 executescript 自带 commit）。"""
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.executescript(_LEDGER_DDL)


# ------------------------------------------------------------ 解析
def _as_str(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_float(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            result = float(value)
        except OverflowError:
            return None
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    # JSON 标准不允许 NaN/Infinity，但 Python json.loads 会接受字面量；
    # 让它们进聚合会把复杂度判成 high、把成本合计污染成 nan。
    return result if math.isfinite(result) else None


def _norm_ts(raw) -> str | None:
    """ISO 8601（可带 Z / ±HH:MM）→ UTC 秒级 `YYYY-MM-DDTHH:MM:SSZ`。

    归一化后再落库，避免同一派单因时区写法不同在 (worker_id, ts) 上重复。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # 无时区标记 → 按 UTC 解释（与 fetchers._norm_ts 同口径）
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_tokens(raw) -> dict | None:
    """tokens 可整块为 null；单字段也可为 null（历史降级行）。"""
    if not isinstance(raw, dict):
        return None
    tokens = {key: _as_int(raw.get(key)) for key in TOKEN_KEYS}
    if not any(value is not None for value in tokens.values()):
        return None
    return tokens


def _norm_warnings(raw) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    return [str(item) for item in raw]


def _normalise(obj: dict) -> dict | None:
    """账本行 → 内部行。缺 ts / worker_id 的行没有幂等身份，直接丢。"""
    ts = _norm_ts(obj.get("ts"))
    worker_id = _as_str(obj.get("worker_id"))
    if ts is None or worker_id is None:
        return None
    return {
        "ts": ts,
        "worker_id": worker_id,
        "issue": _as_int(obj.get("issue")),
        "role": _as_str(obj.get("role")),
        "agent": _as_str(obj.get("agent")),
        "model_requested": _as_str(obj.get("model_requested")),
        "model_effective": _as_str(obj.get("model_effective")),
        "effort": _as_str(obj.get("effort")),
        "complexity_score": _as_float(obj.get("complexity_score")),
        "task_id": _as_str(obj.get("task_id")),
        "worktree_path": _as_str(obj.get("worktree_path")),
        "pr": _as_int(obj.get("pr")),
        "tokens": _norm_tokens(obj.get("tokens")),
        "cost": _as_float(obj.get("cost")),
        "warnings": _norm_warnings(obj.get("warnings")),
    }


def parse_ledger(path: Path) -> list[dict]:
    """读账本 JSONL。

    文件不存在 / 读失败 / 整份无可解析行 → OpsParseError（调用方按 _collect 降级）。
    单行损坏（半行 scp、手改坏）只跳过该行，不拖垮整块 —— append-only 日志的
    尾部半行是正常竞态，不是全局故障。
    """
    if not path.exists():
        raise OpsParseError(f"派单账本不存在：{path}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OpsParseError(f"派单账本读取失败：{exc}") from exc

    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(obj, dict):
            continue
        row = _normalise(obj)
        if row is not None:
            rows.append(row)

    if not rows and text.strip():
        raise OpsParseError(f"派单账本无可解析行：{path}")
    return rows


# ------------------------------------------------------------ 写入
def upsert_dispatch_ledger(db_path: Path, rows: list[dict]) -> int:
    """幂等写派单账本。主键 (worker_id, ts)，重复摄取只覆盖不新增。"""
    if not rows:
        return 0
    init_ledger_schema(db_path)
    now = _now_iso()
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO dispatch_ledger
                        (worker_id, ts, issue, role, agent, model_requested,
                         model_effective, effort, complexity_score, task_id,
                         worktree_path, pr, tokens_json, cost, warnings_json,
                         updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r["worker_id"],
                            r["ts"],
                            r.get("issue"),
                            r.get("role"),
                            r.get("agent"),
                            r.get("model_requested"),
                            r.get("model_effective"),
                            r.get("effort"),
                            r.get("complexity_score"),
                            r.get("task_id"),
                            r.get("worktree_path"),
                            r.get("pr"),
                            json.dumps(r["tokens"], ensure_ascii=False)
                            if r.get("tokens") is not None else None,
                            r.get("cost"),
                            json.dumps(r["warnings"], ensure_ascii=False)
                            if r.get("warnings") is not None else None,
                            now,
                        )
                        for r in rows
                    ],
                )
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
    return len(rows)


# ------------------------------------------------------------ 查询 / 聚合
def _load_json(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _load_rows(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    with connect(db_path) as conn:
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT * FROM dispatch_ledger ORDER BY ts, worker_id")
            out: list[dict] = []
            for row in cur.fetchall():
                item = dict(row)
                item["tokens"] = _load_json(item.pop("tokens_json", None))
                item["warnings"] = _load_json(item.pop("warnings_json", None))
                out.append(item)
            return out


def complexity_band(score: float | None) -> str:
    if score is None:
        return BAND_UNKNOWN
    if score < 4.0:
        return BAND_LOW
    if score < 6.5:
        return BAND_MID
    return BAND_HIGH


def _has_token_data(tokens: dict | None) -> bool:
    return isinstance(tokens, dict) and any(
        tokens.get(key) is not None for key in TOKEN_KEYS
    )


def _token_total(tokens: dict | None) -> int:
    if not isinstance(tokens, dict):
        return 0
    return sum(
        value for key in TOKEN_KEYS
        if isinstance(value := tokens.get(key), int) and not isinstance(value, bool)
    )


def _dedupe(rows: list[dict]) -> list[dict]:
    """与 DB 的 (worker_id, ts) 主键同口径：后出现的行覆盖先出现的。"""
    seen: dict[tuple[str, str], dict] = {}
    for row in rows:
        worker_id = row.get("worker_id")
        ts = row.get("ts")
        if not worker_id or not ts:
            continue
        seen[(worker_id, ts)] = row
    return list(seen.values())


def aggregate_rows(rows: list[dict]) -> list[dict]:
    """按 role × model_effective × 复杂度档聚合。

    返回每组的派单数、重派次数（同 issue 多行 → 计数减一）、tokens / cost 合计、
    无数据行数（tokens 为 null），以及关联 PR 与派单时间（供渲染 join PR 状态 /
    交付周期）。null 一律跳过求和，并计入 null_rows —— 不把「没数据」当 0。
    """
    groups: dict[tuple[str, str, str], dict] = {}

    for row in _dedupe(rows):
        role = row.get("role") or "unknown"
        model = row.get("model_effective") or "unknown"
        band = complexity_band(row.get("complexity_score"))
        key = (role, model, band)
        group = groups.get(key)
        if group is None:
            group = {
                "role": role,
                "model": model,
                "band": band,
                "dispatches": 0,
                "tokens": 0,
                "cost": 0.0,
                "cost_rows": 0,
                "null_rows": 0,
                "_issue_counts": {},
                "_pr_rows": [],
            }
            groups[key] = group

        group["dispatches"] += 1

        tokens = row.get("tokens")
        if _has_token_data(tokens):
            group["tokens"] += _token_total(tokens)
        else:
            group["null_rows"] += 1

        cost = row.get("cost")
        if cost is not None:
            group["cost"] += float(cost)
            group["cost_rows"] += 1

        issue = row.get("issue")
        if issue is not None:
            group["_issue_counts"][issue] = group["_issue_counts"].get(issue, 0) + 1

        pr = row.get("pr")
        if pr is not None:
            group["_pr_rows"].append((int(pr), row.get("ts") or ""))

    out: list[dict] = []
    for group in groups.values():
        issue_counts = group.pop("_issue_counts")
        pr_rows = group.pop("_pr_rows")
        group["issue_rows"] = sum(issue_counts.values())
        # 重派次数 = 同 issue 的派单行数 - 该 issue 的唯一派单次数
        group["retries"] = sum(count - 1 for count in issue_counts.values())
        group["pr_rows"] = sorted(pr_rows, key=lambda item: item[1])
        group["pr_numbers"] = sorted({pr for pr, _ in pr_rows})
        out.append(group)

    out.sort(key=lambda g: (g["role"], g["model"], BAND_ORDER[g["band"]]))
    return out


def aggregate_dispatch_ledger(db_path: Path) -> list[dict]:
    """从 dispatch_ledger 表读全部行并聚合（窗口 = 库内已摄取的全部账本）。"""
    return aggregate_rows(_load_rows(db_path))
