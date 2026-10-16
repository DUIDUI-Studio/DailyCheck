#!/usr/bin/env python3
# prod_agent/inbox_rotate.py —— inbox.jsonl 归档器（7 天窗口，防反压挂死）
#
# 设计目标（Issue #47）：
#   inbox 是反压闸门：行数 ≥ MAX_INBOX_LINES（receiver 默认 1000）后 receiver
#   对 GitHub 返回 503。inbox 只增不减 → 长期必达上限 → 新 PR 通知被拒。
#   本脚本把「超过 N 天」的旧行归档到 inbox.archive.jsonl，让 inbox 行数回落。
#
# 不丢行语义（核心设计，与 receiver 的并发关系）：
#   receiver 每次写都 open('a')→write→close（毫秒级持 fd），本脚本：
#     1. rename inbox → inbox.jsonl.rotating（原子；已打开旧 fd 的 writer 仍写旧 inode）
#     2. 立即重建空 inbox（此后新 open 都落在新文件）
#     3. 读 rotating：超龄行 → append 到 archive；其余行 → append 回新 inbox
#     4. 收敛循环：继续读 rotating，把「步骤 3 之后新增的行」（receiver 持旧 fd
#        的窗口写入）同样分类回填；连续一轮无增长即认为旧 fd 已干涸
#     5. 残留未终结尾片段原样 append 回新 inbox（字节保真，不丢）
#     6. 删除 rotating
#   任何时点、任何顺序写入的行，最终都落在 archive 或 inbox，二者都不丢。
#   行序可能变化 —— watcher 是全扫 + handled 去重，与行序无关。
#
# 其它：
#   - flock(inbox.rotate.lock) 防并发（timer 与手动同时跑）；上次崩溃残留的
#     rotating 会被本次运行先自愈处理
#   - 坏行（非法 JSON / 无 received_at_ms）一律保留（保守，交人工排查）
#   - 仅 Unix（fcntl）；部署态为 Linux，本地 macOS 亦可跑（CI 同）
#
# 部署（详见 docs/2026-10-15-webhook-setup.md §3.8）：
#   - systemd: deploy/systemd/dc-inbox-rotate.service + .timer（每日 00:30）
#
# 用法：
#   python3 scripts/prod_agent/inbox_rotate.py \
#       --inbox /var/lib/dailycheck-prod-agent/inbox.jsonl

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_SETTLE_ROUNDS = 10  # 收敛循环上限（轮）
DEFAULT_SETTLE_INTERVAL = 0.2  # 收敛循环间隔（秒）
MS_PER_DAY = 86_400_000


def _log(msg: str) -> None:
    print(f"[inbox_rotate] {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"[inbox_rotate] WARNING: {msg}", flush=True)


# ============================================================
# 行工具（与 inbox_watcher 同语义：\n 切行、只消费完整行）
# ============================================================
def split_complete(segment: bytes) -> tuple[list[str], int]:
    """切出 segment 中的完整行；返回 (行列表, 已消费字节数)。

    已消费部分 = 到最后一个 \\n（含）为止；其后的未终结片段不消费（留下轮）。
    空行被丢弃（字节计为已消费，不会回填）。
    """
    if not segment:
        return [], 0
    last_nl = segment.rfind(b"\n")
    if last_nl < 0:
        return [], 0
    consumed = last_nl + 1
    text = segment[:consumed].decode("utf-8", "replace")
    # 只按 \n 切：JSON 字符串里可能含裸 U+2028/U+2029（ensure_ascii=False）
    return [ln.rstrip("\r") for ln in text.split("\n") if ln.strip()], consumed


def classify_line(line: str, cutoff_ms: float) -> str:
    """返回 "archive"（早于 cutoff）或 "keep"（保留；含无法判定的坏行）。"""
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return "keep"
    ts = rec.get("received_at_ms") if isinstance(rec, dict) else None
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return "keep"
    return "archive" if ts < cutoff_ms else "keep"


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ============================================================
# 主逻辑
# ============================================================
def _drain_rotating(
    rotating: Path,
    inbox: Path,
    archive: Path,
    cutoff_ms: float,
    settle_rounds: int,
    settle_interval: float,
) -> dict:
    """把 rotating 分类回填后删除。返回统计 dict。"""
    stats = {"archived": 0, "kept": 0, "tail_restored": 0}
    processed = 0
    prev_size = -1
    rounds = 0
    while True:
        try:
            data = rotating.read_bytes()
        except FileNotFoundError:
            break
        segment = data[processed:]
        lines, consumed = split_complete(segment)
        for line in lines:
            if classify_line(line, cutoff_ms) == "archive":
                _append_line(archive, line)
                stats["archived"] += 1
            else:
                _append_line(inbox, line)
                stats["kept"] += 1
        processed += consumed

        # 收敛判定：连续一轮无增长 → 旧 fd 已干涸
        rounds += 1
        if len(data) == prev_size and not lines:
            break
        if rounds >= settle_rounds:
            _warn(
                f"settle rounds exhausted ({settle_rounds}); "
                f"restoring tail bytes verbatim"
            )
            break
        prev_size = len(data)
        if settle_interval > 0:
            time.sleep(settle_interval)

    # 残留未终结尾片段：原样回填（字节保真）
    try:
        rest = rotating.read_bytes()[processed:]
    except FileNotFoundError:
        rest = b""
    if rest:
        with inbox.open("ab") as f:
            f.write(rest)
        stats["tail_restored"] = 1
    rotating.unlink(missing_ok=True)
    return stats


def rotate_inbox(
    inbox: Path,
    archive: Path,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    settle_rounds: int = DEFAULT_SETTLE_ROUNDS,
    settle_interval: float = DEFAULT_SETTLE_INTERVAL,
    now_ms: float | None = None,
) -> dict:
    """执行一次归档。返回统计 dict（含 nothing_to_do 场景）。"""
    if now_ms is None:
        now_ms = time.time() * 1000
    cutoff_ms = now_ms - max_age_days * MS_PER_DAY
    rotating = inbox.with_name(inbox.name + ".rotating")

    stats = {"archived": 0, "kept": 0, "tail_restored": 0, "recovered": False}

    archive.parent.mkdir(parents=True, exist_ok=True)

    # 上次崩溃残留：先自愈（其内容已无写入者，正常流程处理即可）
    if rotating.exists():
        _warn(f"stale rotating file found ({rotating}); recovering it first")
        sub = _drain_rotating(
            rotating, inbox, archive, cutoff_ms, settle_rounds, settle_interval
        )
        stats.update({k: stats[k] + sub[k] for k in ("archived", "kept", "tail_restored")})
        stats["recovered"] = True

    if not inbox.exists():
        stats["nothing_to_do"] = True
        return stats

    # 1. 原子改名：此刻旧 fd 的写入仍落旧 inode（步骤 4/5 兜底）
    os.replace(inbox, rotating)
    # 2. 立即重建空 inbox（receiver 在改名窗口内 open('a') 也会自动创建，幂等）
    inbox.touch()
    # 3-6. 分类回填 + 收敛 + 清理
    sub = _drain_rotating(rotating, inbox, archive, cutoff_ms, settle_rounds, settle_interval)
    stats.update({k: stats[k] + sub[k] for k in ("archived", "kept", "tail_restored")})
    return stats


# ============================================================
# CLI
# ============================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="inbox.jsonl 7 天归档器（Issue #47）")
    parser.add_argument("--inbox", required=True, help="inbox.jsonl 路径")
    parser.add_argument(
        "--archive-file",
        default=None,
        help="归档文件路径（默认 <inbox 同目录>/inbox.archive.jsonl）",
    )
    parser.add_argument(
        "--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS, help="保留天数（默认 7）"
    )
    parser.add_argument(
        "--settle-rounds", type=int, default=DEFAULT_SETTLE_ROUNDS, help="收敛循环上限轮数"
    )
    parser.add_argument(
        "--settle-interval", type=float, default=DEFAULT_SETTLE_INTERVAL, help="收敛循环间隔秒"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    inbox = Path(args.inbox)
    archive = (
        Path(args.archive_file)
        if args.archive_file
        else inbox.with_name("inbox.archive.jsonl")
    )
    lock_path = inbox.with_name(inbox.name + ".rotate.lock")

    try:
        lock_fh = lock_path.open("w")
    except OSError as exc:
        _warn(f"cannot open lock file ({exc})")
        return 1
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _log("another rotate is running; exiting")
            return 0

        start = time.time()
        stats = rotate_inbox(
            inbox,
            archive,
            max_age_days=args.max_age_days,
            settle_rounds=args.settle_rounds,
            settle_interval=args.settle_interval,
        )
        if stats.get("nothing_to_do"):
            _log(f"nothing to do: inbox not found ({inbox})")
            return 0
        _log(
            "done: archived={archived} kept={kept} tail_restored={tail} "
            "recovered={rec} duration={dur:.2f}s".format(
                archived=stats["archived"],
                kept=stats["kept"],
                tail=stats["tail_restored"],
                rec=stats["recovered"],
                dur=time.time() - start,
            )
        )
        return 0
    finally:
        lock_fh.close()


if __name__ == "__main__":
    sys.exit(main())
