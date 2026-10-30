"""scripts.model_eval.collect_pure —— 派单 token/成本账本纯函数层（无 IO）。

数据源（文件读取由 collect.py 负责）：
  1. dispatch.jsonl            Nightly Dispatch Drain 派单事件（type == "dispatch"）
  2. Claude transcript         ~/.claude/projects/<sanitized>/<session>.jsonl
  3. Codex rollout             ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
  4. pricing.json              每 1M token 单价（CNY；缺模型 → cost=null + warning）

设计取舍：
  - 会话归属只认 ``cwd == 派单 worktree_path``。同一 worktree 被多次派单时，按
    session 文件 mtime 落在 ``[本次派单 ts, 同 worktree 下次派单 ts)`` 切分。
  - Claude transcript 里同一条 assistant 消息会以多行重复出现（``message.id`` 相同），
    usage 必须按 message.id 去重后再求和，否则重复行会把 token 数放大数倍。
  - Codex 的 token 计数是**累计值**（legacy ``token_count.info.total_token_usage`` /
    现行 ``token_usage_record.thread_token_usage``），只取末次，绝不逐事件求和。
  - 缺 worktree_path 的历史派单行仍入账本（tokens/cost=null + ``no_worktree_path``），
    任何缺数据都降级为 warning，不丢行、不抛异常。
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

UNKNOWN_MODEL = "unknown"


# ============================================================
# 基础类型
# ============================================================
@dataclass(frozen=True)
class Usage:
    """单个模型（或一次会话）的 token 用量。"""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            reasoning=self.reasoning + other.reasoning,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "reasoning": self.reasoning,
        }


@dataclass(frozen=True)
class Transcript:
    """一个 transcript 文件解析后的摘要。"""

    source: str  # "claude" | "codex"
    path: str
    cwd: str | None
    mtime: float  # epoch seconds
    usage_by_model: dict[str, Usage]
    warnings: tuple[str, ...] = ()

    def models(self) -> list[str]:
        return sorted(self.usage_by_model)


# ============================================================
# 通用工具
# ============================================================
def normalize_path(path: object) -> str | None:
    """worktree 路径归一化：去尾部斜杠 / 重复斜杠；不解析 symlink（纯函数）。"""
    if not isinstance(path, str) or not path.strip():
        return None
    out = path.strip().replace("\\", "/")
    while "//" in out:
        out = out.replace("//", "/")
    if len(out) > 1:
        out = out.rstrip("/")
    return out or None


def parse_ts(value: object) -> float | None:
    """ISO 8601（含 Z / +08:00）→ epoch seconds；解析不了返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _obj(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ============================================================
# Claude transcript
# ============================================================
def parse_claude_transcript(lines: Iterable[str], path: str, mtime: float) -> Transcript:
    """逐行解析 Claude session transcript。

    一行形如 ``{"cwd": ..., "message": {"id", "model", "usage": {...}}}``。
    同一 ``message.id`` 的重复行只计一次（实测同一条消息会写 3 行）。
    损坏行计入 ``bad_lines:<path>:<n>`` warning，不影响其余行。
    """
    cwd: str | None = None
    seen_ids: set[str] = set()
    usage_by_model: dict[str, Usage] = {}
    bad = 0

    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            bad += 1
            continue
        if not isinstance(record, dict):
            bad += 1
            continue

        record_cwd = normalize_path(record.get("cwd"))
        if record_cwd:
            cwd = record_cwd

        message = _obj(record.get("message"))
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue

        message_id = message.get("id") or record.get("uuid")
        # 没有 id 的畸形行按行号区分，避免把不同消息误当重复。
        dedupe_key = str(message_id) if message_id else f"__line_{idx}"
        if dedupe_key in seen_ids:
            continue
        seen_ids.add(dedupe_key)

        model = str(message.get("model") or UNKNOWN_MODEL)
        details = _obj(usage.get("output_tokens_details"))
        delta = Usage(
            input=_as_int(usage.get("input_tokens")),
            output=_as_int(usage.get("output_tokens")),
            cache_read=_as_int(usage.get("cache_read_input_tokens")),
            cache_write=_as_int(usage.get("cache_creation_input_tokens")),
            reasoning=_as_int(details.get("thinking_tokens") or usage.get("reasoning_tokens")),
        )
        usage_by_model[model] = usage_by_model.get(model, Usage()) + delta

    warnings = [f"bad_lines:{path}:{bad}"] if bad else []
    return Transcript(
        source="claude",
        path=path,
        cwd=normalize_path(cwd),
        mtime=mtime,
        usage_by_model=usage_by_model,
        warnings=tuple(warnings),
    )


# ============================================================
# Codex rollout
# ============================================================
def _codex_usage_from_total(total: dict) -> Usage:
    """把 Codex 的累计 total 归一成与 Claude 一致的字段口径。

    Codex 的 ``input_tokens`` 含 ``cached_input_tokens``；拆开记，避免计价时
    缓存命中被按全价重复收一次。
    """
    cached = _as_int(total.get("cached_input_tokens"))
    total_input = _as_int(total.get("input_tokens"))
    return Usage(
        input=max(0, total_input - cached),
        output=_as_int(total.get("output_tokens")),
        cache_read=cached,
        cache_write=_as_int(total.get("cache_write_input_tokens")),
        reasoning=_as_int(total.get("reasoning_output_tokens")),
    )


def parse_codex_transcript(lines: Iterable[str], path: str, mtime: float) -> Transcript:
    """逐行解析 Codex rollout。

    现行格式：``turn_context.payload.model`` + ``token_usage_record.payload.thread_token_usage``
    （每 thread 的累计值，取末次）。兼容旧格式：``session_meta.payload.model`` +
    ``token_count.payload.info.total_token_usage``（累计值，取末次）。
    两种累计源同时出现时以 ``token_usage_record`` 为准，绝不把两者相加。
    """
    cwd: str | None = None
    model: str | None = None
    models: set[str] = set()
    legacy_total: dict | None = None
    thread_totals: dict[str, dict] = {}
    bad = 0

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            bad += 1
            continue
        if not isinstance(record, dict):
            bad += 1
            continue

        payload = _obj(record.get("payload"))
        kind = record.get("type")

        if kind in ("session_meta", "turn_context"):
            cwd = normalize_path(payload.get("cwd")) or cwd
            payload_model = payload.get("model")
            if isinstance(payload_model, str) and payload_model.strip():
                model = payload_model.strip()
                models.add(model)
        elif kind == "token_count":
            info = _obj(payload.get("info"))
            total = info.get("total_token_usage")
            if isinstance(total, dict):
                legacy_total = total
        elif kind == "token_usage_record":
            thread = payload.get("thread_id") or payload.get("session_id") or "__default"
            total = payload.get("thread_token_usage")
            if isinstance(total, dict):
                thread_totals[str(thread)] = total

    if thread_totals:
        totals = list(thread_totals.values())
    elif legacy_total is not None:
        totals = [legacy_total]
    else:
        totals = []

    usage = Usage()
    for total in totals:
        usage = usage + _codex_usage_from_total(total)

    warnings: list[str] = []
    if bad:
        warnings.append(f"bad_lines:{path}:{bad}")
    if len(models) > 1:
        warnings.append(f"model_changed:{path}")

    usage_by_model = {model or UNKNOWN_MODEL: usage} if totals else {}
    return Transcript(
        source="codex",
        path=path,
        cwd=normalize_path(cwd),
        mtime=mtime,
        usage_by_model=usage_by_model,
        warnings=tuple(warnings),
    )


# ============================================================
# 成本
# ============================================================
def compute_cost(usage: Usage, price: dict) -> float:
    """tokens × 每 1M token 单价（CNY）。价格缺字段按 0 计。"""
    def rate(key: str) -> float:
        value = price.get(key, 0.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    return (
        usage.input * rate("input")
        + usage.output * rate("output")
        + usage.cache_read * rate("cache_read")
        + usage.cache_write * rate("cache_write")
    ) / 1_000_000


# ============================================================
# 派单事件 / 会话归属
# ============================================================
def event_worktree(event: dict) -> str | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    return normalize_path(data.get("worktree_path"))


def _worktree_windows(dispatch_events: list[dict]) -> dict[str, list[tuple[float, float | None, int]]]:
    """每个 worktree 的派单时间窗：[ts, 同 worktree 下次派单 ts)。"""
    raw: dict[str, list[tuple[float, int]]] = {}
    for index, event in enumerate(dispatch_events):
        worktree = event_worktree(event)
        start = parse_ts(event.get("ts"))
        # 坏 ts 的派单行无法归窗（但账本行仍保留，见 build_ledger）。
        if worktree is None or start is None:
            continue
        raw.setdefault(worktree, []).append((start, index))

    windows: dict[str, list[tuple[float, float | None, int]]] = {}
    for worktree, items in raw.items():
        items.sort(key=lambda item: (item[0], item[1]))
        for position, (start, index) in enumerate(items):
            end = items[position + 1][0] if position + 1 < len(items) else None
            windows.setdefault(worktree, []).append((start, end, index))
    return windows


def assign_transcripts(
    dispatch_events: list[dict], transcripts: list[Transcript]
) -> dict[int, list[Transcript]]:
    """session → 派单：cwd 相同 + mtime 落在该派单的时间窗。"""
    windows = _worktree_windows(dispatch_events)
    assigned: dict[int, list[Transcript]] = {}
    for transcript in sorted(transcripts, key=lambda item: item.path):
        if not transcript.cwd:
            continue
        for start, end, index in windows.get(transcript.cwd, []):
            if transcript.mtime >= start and (end is None or transcript.mtime < end):
                assigned.setdefault(index, []).append(transcript)
                break
    return assigned


# ============================================================
# 账本行
# ============================================================
def _merge_usage(transcripts: list[Transcript]) -> dict[str, Usage]:
    merged: dict[str, Usage] = {}
    for transcript in transcripts:
        for model, usage in transcript.usage_by_model.items():
            merged[model] = merged.get(model, Usage()) + usage
    return merged


def _ledger_row(
    event: dict,
    transcripts: list[Transcript],
    prices: dict,
) -> dict:
    data = _obj(event.get("data"))
    worktree = event_worktree(event)
    warnings: list[str] = []

    tokens: dict[str, int] | None = None
    cost: float | None = None
    model_effective: str | None = None

    if parse_ts(event.get("ts")) is None:
        warnings.append("bad_ts")

    if worktree is None:
        warnings.append("no_worktree_path")
    elif not transcripts:
        warnings.append("no_transcripts")
    else:
        for transcript in transcripts:
            warnings.extend(transcript.warnings)

        usage_by_model = _merge_usage(transcripts)
        total = Usage()
        for usage in usage_by_model.values():
            total = total + usage
        tokens = total.as_dict()

        real_models = [model for model in sorted(usage_by_model) if model != UNKNOWN_MODEL]
        if len(real_models) > 1:
            warnings.append("multiple_models")
        if real_models:
            model_effective = ",".join(real_models)

        cost_sum = 0.0
        priced = True
        for model, usage in usage_by_model.items():
            price = prices.get(model)
            if not isinstance(price, dict):
                priced = False
                warnings.append(f"no_pricing:{model}")
                continue
            cost_sum += compute_cost(usage, price)
        cost = round(cost_sum, 6) if priced else None

    if data.get("agent"):
        agent = data.get("agent")
    else:
        sources = sorted({transcript.source for transcript in transcripts})
        agent = sources[0] if len(sources) == 1 else ("mixed" if sources else None)

    return {
        "ts": event.get("ts"),
        "issue": event.get("issue"),
        "role": data.get("role"),
        "agent": agent,
        "model_requested": data.get("model") or data.get("model_requested"),
        "model_effective": model_effective,
        "effort": data.get("effort"),
        "complexity_score": data.get("complexity_score"),
        "worker_id": data.get("worker_id"),
        "task_id": data.get("task_id"),
        "worktree_path": worktree,
        "pr": data.get("pr"),
        "tokens": tokens,
        "cost": cost,
        "warnings": _dedupe(warnings),
    }


def build_ledger(
    dispatch_events: list[dict],
    transcripts: list[Transcript],
    pricing: dict | None = None,
) -> list[dict]:
    """派单事件 + transcript → 每次派单一行的账本（按 ts 稳定排序）。"""
    prices = _obj(_obj(pricing).get("prices"))
    assigned = assign_transcripts(dispatch_events, transcripts)

    rows = [
        _ledger_row(event, assigned.get(index, []), prices)
        for index, event in enumerate(dispatch_events)
    ]
    # None ts 排到最后；稳定排序保证同一时刻的派单维持文件顺序。
    rows.sort(key=lambda row: _sort_ts(row.get("ts")))
    return rows


def _sort_ts(value: object) -> float:
    ts = parse_ts(value)
    return float("inf") if ts is None else ts


def dumps_ledger(rows: Iterable[dict]) -> str:
    """账本 → JSONL 文本（键顺序固定，保证同输入同输出）。"""
    return "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
    )
