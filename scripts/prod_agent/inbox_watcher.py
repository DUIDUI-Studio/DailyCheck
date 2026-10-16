#!/usr/bin/env python3
# prod_agent/inbox_watcher.py —— inbox.jsonl 订阅器（Prod Agent 秒级唤醒桥）
#
# 设计目标（Issue #47）：
#   dc-webhook-receiver 把 GitHub PR webhook 落进 inbox.jsonl（秒级），
#   本脚本常驻订阅 inbox：新条目出现后立即（≤5s）触发「merge 决策」入口
#   （--trigger-cmd，事件 JSON 原样从 stdin 传入），替代 Prod Agent 30 分钟
#   轮询的最坏延迟；30 分钟兜底轮询仍保留（防 webhook 漏投 / 本脚本停机窗口）。
#
# 设计铁律：
#   1. 纯 stdlib + 跨平台（macOS 开发 / Linux 生产）：轮询 stat(inode,size,mtime)
#      实现 tail -F 语义 —— 不用 inotify（Linux-only）、不引三方库
#   2. 全文件扫描 + handled_ids 集合去重：重启 / rotate 换 inode / 停机期间新增
#      等任何时序下，保证「每条 delivery_id 至多触发一次；未触发过的必然补触发」
#   3. 只处理以 \n 结尾的完整行（半行留待下一轮）—— writer（receiver）是 append-only
#   4. 机械过滤只做两件事：repo_full_name == --repo、action ∈ 白名单（纵深防御，
#      receiver 已过滤一次）；draft 等业务语义交给 Prod Agent 判定，本脚本不判断
#   5. 触发失败（非 0 / 超时 / 命令不存在）→ 指数退避重试（base→cap）+ WARNING
#      留痕，绝不静默丢弃（30 分钟兜底轮询仍在）
#   6. 状态文件原子写（tmp + os.replace）；handled_ids / bad_hashes 随 inbox 内容
#      prune，防状态无限膨胀
#
# 部署（详见 docs/2026-10-15-webhook-setup.md §3.8）：
#   - systemd: deploy/systemd/dc-inbox-watcher.service（Restart=always / 5s）
#   - 触发命令样例: deploy/scripts/dc-notify-prod-agent.sh
#
# 用法：
#   python3 scripts/prod_agent/inbox_watcher.py \
#       --inbox /var/lib/dailycheck-prod-agent/inbox.jsonl \
#       --trigger-cmd '/usr/local/bin/dc-notify-prod-agent'
#
#   --once 手动模式：处理一轮当前未触发条目后退出（退出码 1 = 有触发失败）。
#
# 注意：首次部署（无状态文件）时，inbox 中「已存在的全部条目」会被视为待触发
#   —— 保守设计（宁多勿漏），部署手册 §3.8 已说明。

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ============================================================
# 默认值（可被 CLI 覆盖）
# ============================================================
DEFAULT_REPO = "ericmr1981/DailyCheck"
DEFAULT_ACTIONS = "opened,synchronize,reopened"
DEFAULT_POLL_INTERVAL = 0.5  # 秒；≤5s 触发验收的主要来源
DEFAULT_TRIGGER_TIMEOUT = 30.0  # 秒；单次触发命令超时
DEFAULT_RETRY_BASE = 1.0  # 秒；首次失败退避
DEFAULT_RETRY_MAX = 60.0  # 秒；退避封顶
STATE_VERSION = 1


def _log(msg: str) -> None:
    print(f"[inbox_watcher] {msg}", flush=True)


def _warn(msg: str) -> None:
    # 拦截 / 失败属必须留痕事件，一律 WARNING 级（与 [dry_run] BLOCKED 同规）
    print(f"[inbox_watcher] WARNING: {msg}", flush=True)


# ============================================================
# 状态（handled_ids / 失败退避表 / 坏行指纹）
# ============================================================
@dataclass
class WatcherState:
    handled_ids: set[str] = field(default_factory=set)
    # delivery_id -> {"n": 连续失败次数, "next": 下次可重试的 unix 秒}
    failures: dict[str, dict] = field(default_factory=dict)
    # 坏行内容指纹（sha256 前 16 位）——同一坏行只 WARNING 一次，防日志刷屏
    bad_hashes: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> WatcherState:
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _warn(f"state file unreadable ({exc}); starting with empty state")
            return cls()
        if not isinstance(raw, dict):
            _warn("state file malformed (not an object); starting with empty state")
            return cls()
        handled = raw.get("handled_ids")
        failures = raw.get("failures")
        bad = raw.get("bad_hashes")
        return cls(
            handled_ids={x for x in handled if isinstance(x, str)}
            if isinstance(handled, list)
            else set(),
            failures={
                k: v
                for k, v in failures.items()
                if isinstance(k, str) and isinstance(v, dict)
            }
            if isinstance(failures, dict)
            else {},
            bad_hashes={x for x in bad if isinstance(x, str)}
            if isinstance(bad, list)
            else set(),
        )

    def save(self, path: Path) -> None:
        payload = {
            "version": STATE_VERSION,
            "handled_ids": sorted(self.handled_ids),
            "failures": self.failures,
            "bad_hashes": sorted(self.bad_hashes),
            "updated_at_ms": int(time.time() * 1000),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)  # 原子替换，崩溃不留半截状态


# ============================================================
# inbox 读取（只取完整行）
# ============================================================
def read_complete_lines(path: Path) -> list[str] | None:
    """读 inbox，只返回以 \\n 结尾的完整行；尾部半行留待下一轮。

    返回 None = 文件暂不可读（不存在 / IO 错误），调用方按「无变化」处理。
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _warn(f"cannot read inbox ({exc})")
        return None
    if not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        data = data[: cut + 1] if cut >= 0 else b""
    text = data.decode("utf-8", "replace")
    # 只按 \n 切（JSONL 唯一行分隔符）。不用 splitlines()：它会把 JSON 字符串
    # 里的裸 U+2028/U+2029 等也当行边界（json.dumps ensure_ascii=False 不转义它们）
    return [ln.rstrip("\r") for ln in text.split("\n") if ln.strip()]


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", "replace")).hexdigest()[:16]


def _parse_record(line: str) -> dict | None:
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    return rec if isinstance(rec, dict) else None


def _delivery_id(rec: dict) -> str | None:
    did = rec.get("delivery_id")
    return did if isinstance(did, str) and did else None


# ============================================================
# 触发
# ============================================================
def run_trigger(argv: list[str], payload: str, timeout: float) -> tuple[bool, str]:
    """执行触发命令，事件 JSON 原样走 stdin。

    返回 (ok, detail)。非 0 退出 / 超时 / 命令不存在 → ok=False（进入退避重试）。
    """
    try:
        proc = subprocess.run(
            argv,
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, f"trigger command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return False, f"trigger timed out after {timeout}s"
    except OSError as exc:
        return False, f"trigger spawn error: {exc}"
    if proc.returncode == 0:
        return True, ""
    stderr_tail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")
    detail = stderr_tail.strip().splitlines()
    return False, f"rc={proc.returncode}" + (f" {detail[-1][:200]}" if detail else "")


# ============================================================
# 核心处理
# ============================================================
@dataclass
class WatcherConfig:
    inbox: Path
    state_file: Path
    trigger_argv: list[str]
    repo: str
    actions: frozenset[str]
    trigger_timeout: float = DEFAULT_TRIGGER_TIMEOUT
    retry_base: float = DEFAULT_RETRY_BASE
    retry_max: float = DEFAULT_RETRY_MAX


def _retry_delay(cfg: WatcherConfig, attempts: int) -> float:
    """第 attempts 次失败后的退避时长：base * 2^(n-1)，封顶 retry_max。"""
    delay = cfg.retry_base * (2 ** (attempts - 1))
    return min(cfg.retry_max, delay) if cfg.retry_max > 0 else delay


def process_once(cfg: WatcherConfig, state: WatcherState) -> int:
    """扫描一轮 inbox，触发全部「未触发过且不在退避期」的新条目。

    返回本轮「触发失败」的条目数（供 --once 退出码使用）。
    """
    lines = read_complete_lines(cfg.inbox)
    if lines is None:
        return 0

    now = time.time()
    dirty = False
    failed = 0
    file_ids: set[str] = set()
    file_bad: set[str] = set()

    for line in lines:
        rec = _parse_record(line)
        did = _delivery_id(rec) if rec is not None else None
        if did is None:
            # 坏行（非法 JSON / 非对象 / 无 delivery_id）：指纹去重后 WARNING 一次
            h = _line_hash(line)
            file_bad.add(h)
            if h not in state.bad_hashes:
                state.bad_hashes.add(h)
                dirty = True
                _warn(f"malformed inbox line skipped: {line[:120]!r}")
            continue

        file_ids.add(did)
        if did in state.handled_ids:
            continue

        # 机械过滤：非目标 repo / 非白名单 action → 记入 handled（不再关注）
        if rec.get("repo_full_name") != cfg.repo:
            state.handled_ids.add(did)
            dirty = True
            _log(f"ignored: delivery={did} repo={rec.get('repo_full_name')!r} (not {cfg.repo})")
            continue
        action = rec.get("action")
        if action not in cfg.actions:
            state.handled_ids.add(did)
            dirty = True
            _log(f"ignored: delivery={did} action={action!r} (not in allowlist)")
            continue

        # 退避期内的失败条目：本轮跳过（下一轮再看）
        fail = state.failures.get(did)
        if fail is not None and now < float(fail.get("next", 0)):
            continue

        ok, detail = run_trigger(cfg.trigger_argv, line, cfg.trigger_timeout)
        if ok:
            state.handled_ids.add(did)
            state.failures.pop(did, None)
            dirty = True
            _log(
                f"triggered: delivery={did} pr=#{rec.get('pr_number')} "
                f"action={action} head={str(rec.get('pr_head_sha', ''))[:8]}"
            )
        else:
            attempts = int(fail.get("n", 0)) + 1 if fail is not None else 1
            delay = _retry_delay(cfg, attempts)
            state.failures[did] = {"n": attempts, "next": now + delay}
            dirty = True
            failed += 1
            _warn(
                f"trigger failed: delivery={did} attempt={attempts} "
                f"({detail}); retry in {delay:g}s"
            )

    # prune：状态只保留 inbox 中仍存在的条目（rotate 归档后自动收缩）
    new_handled = state.handled_ids & file_ids
    new_bad = state.bad_hashes & file_bad
    new_failures = {k: v for k, v in state.failures.items() if k in file_ids}
    if new_handled != state.handled_ids or new_bad != state.bad_hashes:
        dirty = True
    if new_failures != state.failures:
        dirty = True
    state.handled_ids = new_handled
    state.bad_hashes = new_bad
    state.failures = new_failures

    if dirty:
        state.save(cfg.state_file)
    return failed


# ============================================================
# CLI
# ============================================================
def build_config(args: argparse.Namespace) -> WatcherConfig:
    state_file = (
        Path(args.state_file)
        if args.state_file
        else Path(args.inbox).with_name("inbox_watcher_state.json")
    )
    actions = frozenset(a.strip() for a in args.actions.split(",") if a.strip())
    if not actions:
        raise SystemExit("--actions must not be empty")
    if args.poll_interval <= 0:
        raise SystemExit("--poll-interval must be > 0")
    if args.trigger_timeout <= 0:
        raise SystemExit("--trigger-timeout must be > 0")
    return WatcherConfig(
        inbox=Path(args.inbox),
        state_file=state_file,
        trigger_argv=shlex.split(args.trigger_cmd),
        repo=args.repo,
        actions=actions,
        trigger_timeout=args.trigger_timeout,
        retry_base=args.retry_base,
        retry_max=args.retry_max,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="订阅 inbox.jsonl，新条目出现后触发 merge 决策入口（Issue #47）",
    )
    parser.add_argument("--inbox", required=True, help="inbox.jsonl 路径")
    parser.add_argument(
        "--state-file",
        default=None,
        help="状态文件路径（默认 <inbox 同目录>/inbox_watcher_state.json）",
    )
    parser.add_argument(
        "--trigger-cmd",
        required=True,
        help="触发命令（字符串，shell 风格分词；事件 JSON 走 stdin。"
        "复杂场景请写 wrapper 脚本，参见 deploy/scripts/dc-notify-prod-agent.sh）",
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="只处理该 repo 的事件")
    parser.add_argument("--actions", default=DEFAULT_ACTIONS, help="action 白名单（逗号分隔）")
    parser.add_argument(
        "--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="轮询间隔秒（默认 0.5）"
    )
    parser.add_argument(
        "--trigger-timeout", type=float, default=DEFAULT_TRIGGER_TIMEOUT, help="单次触发的超时秒"
    )
    parser.add_argument("--retry-base", type=float, default=DEFAULT_RETRY_BASE, help="失败退避基数秒")
    parser.add_argument("--retry-max", type=float, default=DEFAULT_RETRY_MAX, help="失败退避封顶秒")
    parser.add_argument(
        "--once",
        action="store_true",
        help="处理一轮当前未触发条目后退出（退出码 1 = 有触发失败）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = build_config(args)
    state = WatcherState.load(cfg.state_file)
    _log(
        f"started: inbox={cfg.inbox} state={cfg.state_file} "
        f"repo={cfg.repo} trigger={' '.join(cfg.trigger_argv)} "
        f"handled={len(state.handled_ids)}"
    )

    if args.once:
        return 1 if process_once(cfg, state) else 0

    last_sig: tuple[int, int, int] | None = None
    while True:
        try:
            try:
                st = cfg.inbox.stat()
                sig: tuple[int, int, int] | None = (st.st_ino, st.st_size, st.st_mtime_ns)
            except FileNotFoundError:
                sig = None  # 等 receiver 创建 inbox（rotate 中间态也会走到这里）
            except OSError as exc:
                _warn(f"stat inbox failed ({exc})")
                sig = last_sig  # 视作无变化，等下一轮
            if sig != last_sig:
                process_once(cfg, state)
                last_sig = sig
            time.sleep(args.poll_interval)
        except KeyboardInterrupt:
            _log("interrupted; exiting")
            return 0


if __name__ == "__main__":
    sys.exit(main())
