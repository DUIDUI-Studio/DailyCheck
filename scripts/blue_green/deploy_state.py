#!/usr/bin/env python3
"""deploy_state.py —— 蓝绿部署「单飞」（single-flight）状态原语。

背景：Prod Agent 的 merge + 部署是「一个会话跑 40-60 分钟」的长流程
（up -> verify -> 转正 -> switch -> 观察窗）。同项目并发事件（两个 PR
同时全绿 / 同一 PR 多个 check_suite 事件）可能让两个部署会话在同一套
蓝绿设施上重叠 —— 本原语提供「部署单飞」所需的全部状态机：

  组件一：会话级部署锁（心跳式，TTL 30 分钟）
    - acquire 抢锁（被活跃锁占用时拒绝）；heartbeat 续约；release 收官。
    - 持有者崩溃 -> 心跳过期 -> 锁自动失效，后来者可抢占（无死锁）。
  组件二：事实锚点「已部署 sha」+ 追平检测（catchup）
    - release success 记录 deployed.sha；catchup 对比 origin/main：
        rc 0 = 已追平 / rc 1 = 有欠账（可部署）/ rc 2 = 同 sha 上次失败
        （不自动重试）/ rc 3 = 未初始化 / rc 65 = 无法确定 main。
    - 正确性不依赖任何会话「记得排队」：任何后来者（含兜底轮询）都能
      从事实独立算出「要不要部署」。

退出码约定（供 shell 守卫与轮询消费）：
    0   正常（check=无活跃锁 / acquire 成功 / release 成功 / catchup 已追平）
    1   catchup：有欠账（main != deployed，可部署）
    2   catchup：有欠账但上次同 sha 部署失败 —— 不自动重试（防坏 main 死循环）
    3   catchup：未初始化（deployed 基线缺失，先跑 init）
    10  被活跃锁占用（acquire / check）
    11  token 不匹配或锁不存在（heartbeat / release）
    12  check --pr：锁活跃但属于另一个 PR（交叉校验拒绝）
    64  参数/用法错误
    65  无法确定 main sha（git 调用失败且未显式 --main-sha）

环境变量：
    DAILYCHECK_DEPLOY_STATE_DIR       状态目录（默认 /var/lib/dailycheck-deploy）
    DAILYCHECK_DEPLOY_HEARTBEAT_TTL   心跳过期阈值秒（默认 1800 —— 必须明显长于
                                      单步最长耗时：verify 实测 12-14 分钟，
                                      900s 会让锁在 verify 中途就过期被合法抢占）
    DAILYCHECK_PROD_REPO              catchup 默认取 main 的仓库（默认 /root/DailyCheck）

输入/输出：
    - 除 status 外均为单行 JSON（stdout）；错误信息走 stderr。
    - **token 走文件，不走 stdout**：acquire 除 JSON 外还写 `<state_dir>/lock.token`
      （0600），heartbeat / release 缺省从该文件取 token。原因：会话层（Hermes /
      飞书）会把 stdout 里形似密钥的值脱敏成 `***`，token 一旦只走 stdout，
      调用方就只能去 state.json 手抠 —— 那一步会撞上「管道进解释器」审批门禁。
stdlib only（fcntl 限 POSIX —— 本项目运行时只有 macOS dev / Linux prod）。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_STATE_DIR = "/var/lib/dailycheck-deploy"
DEFAULT_TTL = 1800
DEFAULT_PROD_REPO = "/root/DailyCheck"

STATE_FILENAME = "state.json"
FLOCK_FILENAME = ".state.lock"
TOKEN_FILENAME = "lock.token"

EXIT_OK = 0
EXIT_BEHIND = 1
EXIT_FAILED_PENDING = 2
EXIT_UNINITIALIZED = 3
EXIT_LOCK_BUSY = 10
EXIT_TOKEN_MISMATCH = 11
EXIT_PR_MISMATCH = 12
EXIT_USAGE = 64
EXIT_NO_MAIN = 65


def _state_dir() -> Path:
    return Path(os.environ.get("DAILYCHECK_DEPLOY_STATE_DIR", DEFAULT_STATE_DIR))


def _ttl() -> int:
    try:
        return int(os.environ.get("DAILYCHECK_DEPLOY_HEARTBEAT_TTL", DEFAULT_TTL))
    except ValueError:
        return DEFAULT_TTL


def _now() -> int:
    return int(time.time())


def _empty_state() -> dict:
    return {
        "version": 1,
        "lock": None,
        "deployed": None,
        "last_attempt": None,
        "last_evicted": None,
    }


def _load_state(directory: Path, heal: bool = False) -> dict:
    """读取 state.json。

    文件缺失 -> 空状态。JSON 损坏 -> 返回空状态；heal=True（写命令）时把
    损坏文件改名为 *.corrupt-<ts> 留证据，避免静默丢历史（只读命令不落盘）。
    """
    path = directory / STATE_FILENAME
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
        for key, default in _empty_state().items():
            data.setdefault(key, default)
        return data
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"[deploy_state] WARN: state 文件损坏（{exc}）—— 按空状态处理", file=sys.stderr)
        if heal:
            bak = path.with_name(f"{STATE_FILENAME}.corrupt-{_now()}")
            try:
                path.replace(bak)
                print(f"[deploy_state] 已保留损坏文件: {bak}", file=sys.stderr)
            except OSError:
                pass
        return _empty_state()


def _save_state(directory: Path, state: dict) -> None:
    tmp = directory / f".{STATE_FILENAME}.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # state 里有锁 token，按密钥对待；umask 不可靠，显式收紧
    os.chmod(tmp, 0o600)
    os.replace(tmp, directory / STATE_FILENAME)


def _token_path(directory: Path) -> Path:
    return directory / TOKEN_FILENAME


def _write_token_file(directory: Path, token: str) -> Path:
    """把锁 token 落到 0600 文件。

    为什么要有文件：Hermes / 飞书等会话层会把形似密钥的值脱敏成 `***`，
    会话侧从 stdout 拿不到 token（2026-10-17 PR #96 实测：agent 只好去
    cat state.json 手抠，撞上「管道进解释器」审批门禁卡 5 分钟）。文件是
    唯一稳定的交接通道：写入方是脚本，读取方是同机 root 会话。
    """
    path = _token_path(directory)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    os.chmod(path, 0o600)  # O_CREAT 的 mode 对已存在文件不生效
    return path


def _remove_token_file(directory: Path) -> None:
    try:
        _token_path(directory).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[deploy_state] WARN: 清理 token 文件失败（{exc}）", file=sys.stderr)


def _resolve_token(args, directory: Path) -> str:
    """--token 优先，其次 --token-file，都省略则读 <state_dir>/lock.token。"""
    explicit = (getattr(args, "token", "") or "").strip()
    if explicit:
        return explicit
    raw = (getattr(args, "token_file", "") or "").strip()
    path = Path(raw) if raw else _token_path(directory)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@contextmanager
def _write_lock(directory: Path):
    """写命令的读-改-写全程排他互斥（防两个进程同时改 state）。"""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)  # 目录里有 token，别留 0755
    except OSError:
        pass
    with open(directory / FLOCK_FILENAME, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _lock_active(lock, now: int, ttl: int) -> bool:
    if not isinstance(lock, dict):
        return False
    try:
        beat = int(lock.get("heartbeat_at") or 0)
    except (TypeError, ValueError):
        return False
    return (now - beat) < ttl


def _fmt_ts(epoch) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(epoch)))
    except (TypeError, ValueError, OSError):
        return "-"


def _lock_brief(lock) -> dict:
    if not isinstance(lock, dict):
        return {}
    return {
        "pr": lock.get("pr"),
        "sha": lock.get("sha"),
        "session": lock.get("session"),
        "phase": lock.get("phase"),
        "heartbeat_at": lock.get("heartbeat_at"),
        "heartbeat_human": _fmt_ts(lock.get("heartbeat_at")),
        "acquired_at": lock.get("acquired_at"),
    }


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _fail(message: str, code: int) -> int:
    print(f"[deploy_state] {message}", file=sys.stderr)
    return code


# ----------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------

def cmd_check(args) -> int:
    """锁活跃性检查（只读）。

    默认 rc：0 = 无活跃锁 / 10 = 活跃锁。
    带 --pr N 时交叉校验：锁 pr 为空或在匹配 -> 10；属于别的 PR -> 12。
    """
    directory = _state_dir()
    dir_exists = directory.is_dir()
    state = _load_state(directory) if dir_exists else _empty_state()
    lock = state.get("lock")
    active = _lock_active(lock, _now(), _ttl())

    if args.pr is not None and active:
        lock_pr = (lock or {}).get("pr")
        if lock_pr not in (None, "", args.pr):
            _emit({
                "active": True,
                "activated": True,
                "pr_mismatch": True,
                "pr": args.pr,
                "lock": _lock_brief(lock),
            })
            return EXIT_PR_MISMATCH

    _emit({"active": active, "activated": dir_exists, "lock": _lock_brief(lock) if active else None})
    return EXIT_LOCK_BUSY if active else EXIT_OK


def cmd_acquire(args) -> int:
    """抢锁。无活跃锁（或过期可抢占）时成功；被占用 -> rc 10。"""
    directory = _state_dir()
    with _write_lock(directory):
        state = _load_state(directory, heal=True)
        now = _now()
        lock = state.get("lock")
        if _lock_active(lock, now, _ttl()):
            _emit({"ok": False, "reason": "lock-busy", "lock": _lock_brief(lock)})
            return EXIT_LOCK_BUSY

        if isinstance(lock, dict):
            # 过期锁被抢占：留证据（诊断「谁死在半路」）
            evicted = dict(lock)
            evicted["evicted_at"] = now
            state["last_evicted"] = evicted

        token = secrets.token_hex(8)
        state["lock"] = {
            "token": token,
            "session": args.session or "",
            "pr": args.pr,
            "sha": args.sha or "",
            "phase": args.phase or "acquire",
            "acquired_at": now,
            "heartbeat_at": now,
        }
        _save_state(directory, state)
        token_file = _write_token_file(directory, token)
        _emit({
            "ok": True,
            "token": token,
            "token_file": str(token_file),
            "pr": args.pr,
            "sha": args.sha or "",
            "ttl": _ttl(),
            "state_dir": str(directory),
        })
        return EXIT_OK


def cmd_heartbeat(args) -> int:
    """续约。token 必须匹配当前锁；过期锁若未被抢占仍可救活。"""
    directory = _state_dir()
    token = _resolve_token(args, directory)
    with _write_lock(directory):
        state = _load_state(directory, heal=True)
        lock = state.get("lock")
        if not isinstance(lock, dict) or not token or lock.get("token") != token:
            _emit({"ok": False, "reason": "token-mismatch-or-no-lock"})
            return EXIT_TOKEN_MISMATCH
        now = _now()
        expired = not _lock_active(lock, now, _ttl())
        lock["heartbeat_at"] = now
        if args.phase:
            lock["phase"] = args.phase
        _save_state(directory, state)
        _emit({"ok": True, "revived": expired, "phase": lock.get("phase")})
        return EXIT_OK


def cmd_release(args) -> int:
    """收官。success -> 更新 deployed 锚点并清 last_attempt；failed -> 记 last_attempt。"""
    directory = _state_dir()
    token = _resolve_token(args, directory)
    with _write_lock(directory):
        state = _load_state(directory, heal=True)
        lock = state.get("lock")
        if not isinstance(lock, dict) or not token or lock.get("token") != token:
            _emit({"ok": False, "reason": "token-mismatch-or-no-lock"})
            return EXIT_TOKEN_MISMATCH

        now = _now()
        sha = (args.sha or lock.get("sha") or "").strip()
        pr = lock.get("pr")

        if args.result == "success":
            if not sha:
                return _fail("release success 需要 sha（锁内为空且未给 --sha）", EXIT_USAGE)
            state["deployed"] = {"sha": sha, "pr": pr, "at": now}
            state["last_attempt"] = None
        else:
            state["last_attempt"] = {
                "sha": sha,
                "pr": pr,
                "result": "failed",
                "note": args.note or "",
                "at": now,
            }

        state["lock"] = None
        _save_state(directory, state)
        _remove_token_file(directory)  # 锁已放手，token 文件一律不留在盘上
        _emit({"ok": True, "result": args.result, "sha": sha, "pr": pr})
        return EXIT_OK


def _resolve_main_sha(args) -> str:
    if args.main_sha:
        return args.main_sha.strip()
    repo = os.environ.get("DAILYCHECK_PROD_REPO", DEFAULT_PROD_REPO)
    try:
        out = subprocess.run(
            ["git", "-C", repo, "rev-parse", "origin/main"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[deploy_state] WARN: 无法从 {repo} 取 origin/main（{exc}）", file=sys.stderr)
        return ""


def cmd_catchup(args) -> int:
    """追平检测：main vs deployed（+ 失败防重试）。"""
    directory = _state_dir()
    state = _load_state(directory)
    main = _resolve_main_sha(args)
    if not main:
        _emit({"status": "no-main", "state_dir": str(directory)})
        return EXIT_NO_MAIN

    deployed = state.get("deployed") or {}
    deployed_sha = (deployed.get("sha") or "").strip()
    if not deployed_sha:
        _emit({"status": "uninitialized", "main": main, "state_dir": str(directory)})
        return EXIT_UNINITIALIZED

    if deployed_sha == main:
        _emit({"status": "caught-up", "main": main, "deployed": deployed_sha})
        return EXIT_OK

    last = state.get("last_attempt") or {}
    if (last.get("result") == "failed") and ((last.get("sha") or "").strip() == main):
        _emit({
            "status": "failed-pending",
            "main": main,
            "deployed": deployed_sha,
            "last_attempt": {"pr": last.get("pr"), "note": last.get("note")},
        })
        return EXIT_FAILED_PENDING

    _emit({"status": "behind", "main": main, "deployed": deployed_sha})
    return EXIT_BEHIND


def cmd_init(args) -> int:
    """一次性激活：初始化状态目录并把 deployed 基线设为当前 origin/main。

    之后所有 live 脚本调用进入强制校验（状态目录存在 = 机制激活）。
    """
    directory = _state_dir()
    with _write_lock(directory):
        state = _load_state(directory, heal=True)
        now = _now()
        lock = state.get("lock")
        if _lock_active(lock, now, _ttl()):
            _emit({"ok": False, "reason": "lock-busy", "lock": _lock_brief(lock)})
            return EXIT_LOCK_BUSY
        if (state.get("deployed") or {}).get("sha"):
            _emit({"ok": True, "already": True, "deployed": state["deployed"]})
            return EXIT_OK
        sha = (args.sha or "").strip()
        if not sha:
            sha = _resolve_main_sha(args)
        if not sha:
            return _fail("init 需要 sha（--sha 或可解析的 origin/main）", EXIT_NO_MAIN)
        state["deployed"] = {"sha": sha, "pr": None, "at": now}
        _save_state(directory, state)
        _emit({"ok": True, "already": False, "deployed": state["deployed"], "state_dir": str(directory)})
        return EXIT_OK


def cmd_clear_lock(args) -> int:
    """break-glass：人工清锁（需 --yes）。"""
    if not args.yes:
        return _fail("clear-lock 需要 --yes 明确确认", EXIT_USAGE)
    directory = _state_dir()
    with _write_lock(directory):
        state = _load_state(directory, heal=True)
        old = state.get("lock")
        state["lock"] = None
        _save_state(directory, state)
        _remove_token_file(directory)
        print("[deploy_state] WARN: 锁已被人工清除（clear-lock --yes）", file=sys.stderr)
        _emit({"ok": True, "cleared": _lock_brief(old) if old else None})
        return EXIT_OK


def cmd_status(args) -> int:
    """人类可读全景（--json 可选）。"""
    directory = _state_dir()
    state = _load_state(directory)
    lock = state.get("lock")
    now = _now()
    active = _lock_active(lock, now, _ttl())

    if args.json:
        _emit({
            "state_dir": str(directory),
            "activated": directory.is_dir(),
            "token_file": {"path": str(_token_path(directory)), "present": _token_path(directory).exists()},
            "lock_active": active,
            "lock": _lock_brief(lock),
            "deployed": state.get("deployed"),
            "last_attempt": state.get("last_attempt"),
            "last_evicted": _lock_brief(state.get("last_evicted")) if state.get("last_evicted") else None,
        })
        return EXIT_OK

    print(f"state_dir : {directory}  ({'activated' if directory.is_dir() else 'NOT activated'})")
    print(f"ttl       : {_ttl()}s")
    tok = _token_path(directory)
    print(f"token_file: {tok}  ({'present' if tok.exists() else 'absent'})")
    if active:
        print(f"lock      : ACTIVE  pr={lock.get('pr')} phase={lock.get('phase')} "
              f"session={lock.get('session') or '-'} beat={_fmt_ts(lock.get('heartbeat_at'))}")
    else:
        stale = ""
        if isinstance(lock, dict):
            stale = f" (stale: beat={_fmt_ts(lock.get('heartbeat_at'))} — 可被抢占)"
        print(f"lock      : none{stale}")
    dep = state.get("deployed")
    if dep:
        print(f"deployed  : {dep.get('sha', '')[:12]}  pr={dep.get('pr')}  at={_fmt_ts(dep.get('at'))}")
    else:
        print("deployed  : (未初始化 —— 跑 init 建立基线)")
    last = state.get("last_attempt")
    if last:
        print(f"last_att. : FAILED {str(last.get('sha', ''))[:12]} pr={last.get('pr')} note={last.get('note')}")
    ev = state.get("last_evicted")
    if ev:
        print(f"evicted   : pr={ev.get('pr')} at={_fmt_ts(ev.get('evicted_at'))}（心跳过期被抢占）")
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="deploy_state.py", description="蓝绿部署单飞状态原语")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("check", help="锁活跃性检查（rc 0=无锁 / 10=活跃 / 12=PR 不匹配）")
    p.add_argument("--pr", type=int, default=None, help="交叉校验：锁须属于该 PR（或锁未绑定 PR）")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("acquire", help="抢锁（rc 10=被占用）")
    p.add_argument("--pr", type=int, default=None)
    p.add_argument("--sha", default="")
    p.add_argument("--session", default="")
    p.add_argument("--phase", default="")
    p.set_defaults(func=cmd_acquire)

    p = sub.add_parser("heartbeat", help="续约（rc 11=token 不匹配）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--token", default="", help="锁 token（缺省读 <state_dir>/lock.token）")
    g.add_argument("--token-file", default="", help="从指定文件读 token")
    p.add_argument("--phase", default="")
    p.set_defaults(func=cmd_heartbeat)

    p = sub.add_parser("release", help="收官（success 更新 deployed 锚点）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--token", default="", help="锁 token（缺省读 <state_dir>/lock.token）")
    g.add_argument("--token-file", default="", help="从指定文件读 token")
    p.add_argument("--result", required=True, choices=["success", "failed"])
    p.add_argument("--sha", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("catchup", help="追平检测（rc 0/1/2/3，65=取不到 main）")
    p.add_argument("--main-sha", default="", help="显式 main sha（缺省从 DAILYCHECK_PROD_REPO 取 origin/main）")
    p.set_defaults(func=cmd_catchup)

    p = sub.add_parser("init", help="一次性激活：初始化 deployed 基线")
    p.add_argument("--sha", default="")
    p.add_argument("--main-sha", default="")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("clear-lock", help="break-glass：人工清锁（需 --yes）")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_clear_lock)

    p = sub.add_parser("status", help="人类可读全景")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
