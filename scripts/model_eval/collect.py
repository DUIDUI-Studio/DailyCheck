"""scripts.model_eval.collect —— 派单 token/成本账本 IO / CLI 层（Issue #91）。

运行：
    python3 -m scripts.model_eval collect [--dry-run]

幂等语义 = **全量重生成**：每次从 dispatch.jsonl 完整重建 ``--out``，同输入同输出。

只读边界：本模块只读 dispatch.jsonl / transcript / pricing，唯一写入路径是
``--out``（默认 ``~/.local/state/dailycheck/eval/ledger.jsonl``）。绝不写入
``~/.local/state/dailycheck/automation/``。

降级策略：单个 transcript 文件解析失败只影响该文件（warning 记到对应账本行）；
pricing 缺模型 → cost=null + warning；dispatch.jsonl 本身不可读才退出非 0。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

from .collect_pure import (
    Transcript,
    build_ledger,
    dumps_ledger,
    event_worktree,
    normalize_path,
    parse_claude_transcript,
    parse_codex_transcript,
    parse_ts,
)

DEFAULT_DISPATCH = "~/.local/state/dailycheck/automation/state/dispatch.jsonl"
DEFAULT_OUT = "~/.local/state/dailycheck/eval/ledger.jsonl"
DEFAULT_CLAUDE_DIR = "~/.claude/projects"
DEFAULT_CODEX_DIR = "~/.codex/sessions"
DEFAULT_PRICING = str(Path(__file__).with_name("pricing.json"))


# ============================================================
# 文件发现（Claude 目录名是 cwd 的非字母数字替换，只做候选过滤，cwd 仍以文件内容为准）
# ============================================================
def claude_dir_name(path: str) -> str:
    """'/Users/x/orca/w' → '-Users-x-orca-w'（与 ~/.claude/projects 命名一致）。"""
    normalized = normalize_path(path) or ""
    return re.sub(r"[^A-Za-z0-9]", "-", normalized)


def _recent_files(root: Path, pattern: str, min_mtime: float) -> Iterator[Path]:
    if not root.is_dir():
        return
    files = sorted(root.rglob(pattern))
    for path in files:
        try:
            if path.stat().st_mtime < min_mtime:
                continue
        except OSError:
            continue
        yield path


def _recent_claude_files(claude_dir: Path, worktrees: list[str], min_mtime: float) -> Iterator[Path]:
    if not claude_dir.is_dir():
        return
    if worktrees:
        wanted = {claude_dir_name(worktree) for worktree in worktrees}
        dirs = [path for path in claude_dir.iterdir() if path.is_dir() and path.name in wanted]
        if not dirs:
            # 命名规则漂移时不静默漏采：退回全目录扫描，靠文件内 cwd 再判定归属。
            dirs = [path for path in claude_dir.iterdir() if path.is_dir()]
    else:
        dirs = [path for path in claude_dir.iterdir() if path.is_dir()]

    files: list[Path] = []
    for directory in dirs:
        files.extend(_recent_files(directory, "*.jsonl", min_mtime))
    yield from sorted(files)


def _parse_file(path: Path, source: str, parser, mtime: float) -> Transcript:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return parser(handle, str(path), mtime)
    except Exception as exc:  # 单文件失败只降级该文件，不中断整次采集
        return Transcript(
            source=source,
            path=str(path),
            cwd=None,
            mtime=mtime,
            usage_by_model={},
            warnings=(f"parse_error:{path}:{type(exc).__name__}",),
        )


def discover_transcripts(
    dispatch_events: list[dict],
    claude_dir: Path,
    codex_dir: Path,
) -> tuple[list[Transcript], list[str]]:
    """发现并解析可能归属派单的 transcript（按 ts 下界 + Claude 目录名预筛）。"""
    worktrees = sorted({wt for wt in (event_worktree(event) for event in dispatch_events) if wt})
    # 没有任何派单带 worktree_path 时，所有行都会降级为 no_worktree_path —— 无需扫盘。
    if not worktrees:
        return [], []

    starts = [ts for ts in (parse_ts(event.get("ts")) for event in dispatch_events) if ts is not None]
    min_mtime = min(starts) if starts else 0.0

    transcripts: list[Transcript] = []
    warnings: list[str] = []
    for path in _recent_claude_files(claude_dir, worktrees, min_mtime):
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            warnings.append(f"stat_error:{path}:{type(exc).__name__}")
            continue
        transcripts.append(_parse_file(path, "claude", parse_claude_transcript, mtime))

    for path in _recent_files(codex_dir, "rollout-*.jsonl", min_mtime):
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            warnings.append(f"stat_error:{path}:{type(exc).__name__}")
            continue
        transcripts.append(_parse_file(path, "codex", parse_codex_transcript, mtime))

    return transcripts, warnings


# ============================================================
# 输入读取
# ============================================================
def read_dispatch_events(path: Path) -> tuple[list[dict], list[str]]:
    """读 dispatch.jsonl，只保留 ``type == "dispatch"`` 的行。

    dispatch.jsonl 是主输入：文件不可读时抛 OSError，由 CLI 退出非 0
    （单行损坏只记 warning，不丢其它行）。
    """
    events: list[dict] = []
    bad = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                bad += 1
                continue
            if isinstance(record, dict) and record.get("type") == "dispatch":
                events.append(record)
    warnings = [f"bad_dispatch_lines:{path}:{bad}"] if bad else []
    return events, warnings


def load_pricing(path: Path) -> tuple[dict, list[str]]:
    """读价目表；缺失 / 损坏 → 空价目表（所有 cost 降级为 null + warning）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"prices": {}}, [f"pricing_unavailable:{path}:{type(exc).__name__}"]
    prices = data.get("prices") if isinstance(data, dict) else None
    if not isinstance(prices, dict):
        return {"prices": {}}, [f"pricing_missing_prices:{path}"]
    return {"currency": data.get("currency"), "prices": prices}, []


# ============================================================
# 运行
# ============================================================
def _write_ledger(out: Path, payload: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, out)


def run_collect(args: argparse.Namespace) -> int:
    dispatch_path = Path(os.path.expanduser(args.dispatch_jsonl))
    out_path = Path(os.path.expanduser(args.out))
    claude_dir = Path(os.path.expanduser(args.claude_dir))
    codex_dir = Path(os.path.expanduser(args.codex_dir))
    pricing_path = Path(os.path.expanduser(args.pricing))

    try:
        events, warnings = read_dispatch_events(dispatch_path)
    except OSError as exc:
        print(f"[model_eval] 无法读取 dispatch.jsonl: {dispatch_path}: {exc}", file=sys.stderr)
        return 2

    pricing, pricing_warnings = load_pricing(pricing_path)
    warnings.extend(pricing_warnings)

    transcripts, transcript_warnings = discover_transcripts(events, claude_dir, codex_dir)
    warnings.extend(transcript_warnings)

    rows = build_ledger(events, transcripts, pricing)
    payload = dumps_ledger(rows)

    if args.dry_run:
        sys.stdout.write(payload)
    else:
        _write_ledger(out_path, payload)

    for warning in warnings:
        print(f"[model_eval] WARNING {warning}", file=sys.stderr)
    target = "(dry-run, 未落盘)" if args.dry_run else str(out_path)
    print(
        f"[model_eval] dispatch={len(events)} transcripts={len(transcripts)} "
        f"ledger={len(rows)} rows → {target}",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.model_eval")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="从 dispatch.jsonl + 本机 transcript 重建账本")
    collect.add_argument("--dispatch-jsonl", default=DEFAULT_DISPATCH)
    collect.add_argument("--out", default=DEFAULT_OUT)
    collect.add_argument("--claude-dir", default=DEFAULT_CLAUDE_DIR)
    collect.add_argument("--codex-dir", default=DEFAULT_CODEX_DIR)
    collect.add_argument("--pricing", default=DEFAULT_PRICING)
    collect.add_argument("--dry-run", action="store_true", help="只打印 JSONL，不落盘")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "collect":
        return run_collect(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
