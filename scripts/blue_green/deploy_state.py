#!/usr/bin/env python3
"""deploy_state.py —— 蓝绿部署「单飞」（single-flight）状态原语。

背景：Prod Agent 的 merge + 部署是「一个会话跑 40-60 分钟」的长流程
（up -> verify -> 转正 -> switch -> 观察窗）。同项目并发事件（两个 PR
同时全绿 / 同一 PR 多个 check_suite 事件）可能让两个部署会话在同一套
蓝绿设施上重叠 —— 本原语提供「部署单飞」所需的全部状态机：

  组件一：会话级部署锁（心跳式，TTL 30 分钟）
    - acquire 抢锁（被活跃锁占用时拒绝）；heartbeat 续约；release 收官。
    - heartbeat --loop 为编排脚本提供常驻续约：一个进程持续打心跳，
      把「阶段耗时超过 TTL」从结构上消掉（up/verify/promote/switch 内部
      只 check 不 beat，靠调用方自觉打点是不可靠的）。
    - 普通 pipeline 另持 .attempt.lock 的 OS flock，覆盖完整进程生命周期；
      过期心跳不是退出证明，活着/暂停的 pipeline 不可被另一普通 attempt 抢占。
      独立 CLI 的旧租约仍兼容 TTL 抢占，但 acquire 也须先通过该 OS 锁。
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
    - 状态命令（除 status）输出单行 JSON；run exec pipeline，所有权断言只返回 rc。
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
import re
import secrets
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_DIR = "/var/lib/dailycheck-deploy"
DEFAULT_TTL = 1800
DEFAULT_PROD_REPO = "/root/DailyCheck"

STATE_FILENAME = "state.json"
FLOCK_FILENAME = ".state.lock"
TOKEN_FILENAME = "lock.token"
ATTEMPT_LOCK_FILENAME = ".attempt.lock"
ATTEMPT_ENV = "DAILYCHECK_DEPLOY_ATTEMPT"
ATTEMPT_FD_ENV = "DAILYCHECK_DEPLOY_ATTEMPT_FD"
NATIVE_GUARD_FILENAME = "native-queue.guard"

# Issue163: the public code sink is a fixed 162 identity, never a fact the
# reporter may take from a PR body, context JSON or caller argument.
PUBLIC_CODE_HOSTING = {
    "provider": "github",
    "host": "github.com",
    "repository_id": 1448986421,
    "repository": "DUIDUI-Studio/DailyCheck",
}
OUTCOME_RESULT_SUCCESS = "success"
OUTCOME_RESULT_FAILED = "failed"
DEPLOYMENT_PROVIDERS = {"github", "gitea"}
FULL_SHA = re.compile(r"[0-9a-f]{40}")
ATTEMPT_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
VERIFICATION = re.compile(r"[A-Za-z0-9._+-]{1,60}")
HOST_NAME = re.compile(r"[A-Za-z0-9.:-]+")
REPOSITORY_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
OUTCOME_STAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")


EXIT_OK = 0
EXIT_BEHIND = 1
EXIT_FAILED_PENDING = 2
EXIT_UNINITIALIZED = 3
EXIT_LOCK_BUSY = 10
EXIT_TOKEN_MISMATCH = 11
EXIT_PR_MISMATCH = 12
EXIT_USAGE = 64
EXIT_NO_MAIN = 65


@dataclass(frozen=True)
class AttemptIdentity:
    token: str
    pr: int | None
    sha: str
    session: str

    @classmethod
    def from_env(cls):
        raw = os.environ.get(ATTEMPT_ENV)
        if raw is None:
            return None
        data = json.loads(raw)
        identity = cls(*(data[key] for key in ("token", "pr", "sha", "session")))
        if not identity.token or not identity.sha:
            raise ValueError("incomplete attempt identity")
        return identity

    def owns(self, lock) -> bool:
        return isinstance(lock, dict) and all(
            lock.get(key) == getattr(self, key) for key in ("token", "pr", "sha", "session")
        )


def _process_lock_fd():
    """The inherited descriptor, not an environment flag, proves process exclusion."""
    try:
        fd = int(os.environ.get(ATTEMPT_FD_ENV, ""))
        if fd < 3:
            return None
        held = os.fstat(fd)
        expected = (_state_dir() / ATTEMPT_LOCK_FILENAME).stat()
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            return None
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (ValueError, OSError):
        return None


@contextmanager
def _attempt_lock(directory):
    inherited = _process_lock_fd()
    if inherited is not None:
        yield inherited
        return
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(directory / ATTEMPT_LOCK_FILENAME, "a+") as fh:
        os.chmod(fh.name, 0o600)
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Do not LOCK_UN explicitly: exec descendants may still own this descriptor.
        yield fh.fileno()


def cmd_run(args) -> int:
    """Exec the normal pipeline with one OS lock held through EXIT finalization."""
    if not args.exec or os.environ.get(ATTEMPT_ENV) is not None:
        return _fail("run requires a command and a fresh attempt", EXIT_USAGE)
    command = args.exec[1:] if args.exec[0] == "--" else args.exec
    if not command:
        return EXIT_USAGE
    try:
        with _attempt_lock(_state_dir()) as fd:
            if native_blocked(_state_dir(), _load_state(_state_dir())):
                return _fail("native grant requires reconciliation before another pipeline", EXIT_LOCK_BUSY)
            os.set_inheritable(fd, True)
            env = dict(os.environ, **{ATTEMPT_FD_ENV: str(fd)})
            os.execvpe(command[0], command, env)
    except BlockingIOError:
        return _fail("normal pipeline process still owns deployment", EXIT_LOCK_BUSY)


def cmd_process_owned(args) -> int:
    return EXIT_OK if _process_lock_fd() is not None else EXIT_TOKEN_MISMATCH


def _attempt_owns(lock) -> bool:
    try:
        identity = AttemptIdentity.from_env()
    except (ValueError, KeyError, TypeError):
        return False
    return identity is None or (_process_lock_fd() is not None and identity.owns(lock))


def cmd_assert_owner(args) -> int:
    try:
        identity = AttemptIdentity.from_env()
    except (ValueError, KeyError, TypeError):
        return _fail("invalid attempt identity", EXIT_TOKEN_MISMATCH)
    lock = _load_state(_state_dir()).get("lock")
    if identity is None or not _attempt_owns(lock):
        return _fail("attempt ownership lost", EXIT_TOKEN_MISMATCH)
    if args.pr is not None and args.pr != identity.pr:
        return EXIT_PR_MISMATCH
    if args.sha and args.sha != identity.sha:
        return EXIT_TOKEN_MISMATCH
    return EXIT_OK


def cmd_owned_checkout(args) -> int:
    """Order a local checkout with supported revocation; never hold across fetch."""
    with _write_lock(_state_dir()) as boundary_fd:
        lock = _load_state(_state_dir()).get("lock")
        if ATTEMPT_ENV not in os.environ or not _attempt_owns(lock):
            return _fail("attempt ownership lost before checkout", EXIT_TOKEN_MISMATCH)
        if args.candidate and args.candidate != lock.get("sha"):
            return _fail("checkout sha differs from attempt", EXIT_TOKEN_MISMATCH)
        command = ["git", "-C", args.repo, "checkout", "--quiet"]
        if args.main:
            command.extend(["-B", "main", "origin/main"])
        else:
            command.extend(["--detach", args.candidate])
        # The mutator retains both fences if its waiting Python parent is killed.
        process_fd = int(os.environ[ATTEMPT_FD_ENV])
        return subprocess.call(command, pass_fds=(boundary_fd, process_fd))


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
        # Issue163 additive domain. Old states keep working; missing keys are
        # filled on read and never upgrade a legacy state into an outcome.
        "generation": 0,
        "outcomes": {},
        "publications": {},
    }


def _state_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("ambiguous deployment state")
        value[key] = item
    return value


def _state_constant(_value):
    raise ValueError("invalid deployment state number")


def _load_state(directory: Path, heal: bool = False, *, strict: bool = False) -> dict:
    """读取 state.json。

    文件缺失 -> 空状态。JSON 损坏 -> 返回空状态；heal=True（写命令）时把
    损坏文件改名为 *.corrupt-<ts> 留证据，避免静默丢历史（只读命令不落盘）。
    """
    path = directory / STATE_FILENAME
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"),
                          object_pairs_hook=_state_object if strict else dict,
                          **({"parse_constant": _state_constant} if strict else {}))
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
        for key, default in _empty_state().items():
            data.setdefault(key, default)
        if strict and (type(data["version"]) is not int or data["version"] != 1
                       or type(data["generation"]) is not int or data["generation"] < 0
                       or not isinstance(data["outcomes"], dict)
                       or any(not isinstance(value, dict) for value in data["outcomes"].values())
                       or data["deployed"] is not None and not isinstance(data["deployed"], dict)
                       or data["last_attempt"] is not None and not isinstance(data["last_attempt"], dict)):
            raise ValueError("invalid deployment state shape")
        return data
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        if strict:
            raise ValueError("deployment state requires reconciliation") from exc
        print(f"[deploy_state] WARN: state 文件损坏（{exc}）—— 按空状态处理", file=sys.stderr)
        if heal and not (directory / NATIVE_GUARD_FILENAME).exists():
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
    with tmp.open("rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, directory / STATE_FILENAME)
    _sync_directory(directory)


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def native_guard(directory: Path, marker: dict) -> None:
    """Fail-closed marker, never positive authority; same directory and locks."""
    path = directory / NATIVE_GUARD_FILENAME
    tmp = directory / (NATIVE_GUARD_FILENAME + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        os.chmod(tmp, 0o600)
        json.dump(marker, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _sync_directory(directory)


def native_consistent(directory: Path, state: dict) -> bool:
    """Once initialized, absent/partial state or marker must never look like first use."""
    native = state.get("native_queue")
    path = directory / NATIVE_GUARD_FILENAME
    if native is None:
        return not path.exists()
    try:
        marker = json.loads(path.read_text())
        if (not isinstance(native, dict) or native.get("version") != 1
                or not isinstance(native.get("initialization_id"), str)
                or marker.get("initialization_id") != native["initialization_id"]
                or marker.get("version") != 1 or "active" not in native):
            return False
        active = native["active"]
        if active is None:
            return marker == {"version": 1, "initialization_id": native["initialization_id"],
                              "intent_id": None}
        if (not isinstance(active, dict) or not isinstance(active.get("candidate"), dict)
                or active.get("phase") not in {"deploy", "verified", "grant_intent", "grant_uncertain",
                                               "grant_accepted", "incident"}
                or not isinstance(active.get("origin_attempt_id"), str)
                or type(active.get("origin_generation")) is not int):
            return False
        grant = active.get("release_grant")
        return marker == {"version": 1, "initialization_id": native["initialization_id"],
                          "intent_id": grant["intent_id"] if isinstance(grant, dict) else None}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def native_blocked(directory: Path, state: dict) -> bool:
    return (not native_consistent(directory, state)
            or bool((state.get("native_queue") or {}).get("active")))


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
            yield fh.fileno()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# Public state-transaction primitives shared with the Issue163 publication
# adapter. They are the same objects the CLI uses; no second lock family or
# storage domain is introduced.
state_dir = _state_dir
load_state = _load_state
save_state = _save_state
state_transaction = _write_lock
attempt_lock = _attempt_lock
process_lock_fd = _process_lock_fd
now_epoch = _now


@contextmanager
def publication_config_transaction(directory: Path):
    """Order a cooperating private-mapping write against publication HTTP.

    The future off-repo installer updates `bindings.json` through this helper:
    it takes the same `.attempt.lock` process exclusion a normal pipeline or a
    post-exit publication retry holds, then the ordinary `.state.lock`
    transaction. A writer that wins first therefore changes the binding before
    the claim reads it (the stale context then fails closed); a writer that
    loses waits until the publication and its actual HTTP mutator finish, so
    the accepted POST is ordered before the binding change. This is not a new
    lock family, queue or service.
    """
    if _process_lock_fd() is not None:
        raise RuntimeError(
            "publication_config_transaction cannot nest inside a deployment attempt")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(directory / ATTEMPT_LOCK_FILENAME, "a+") as fh:
        os.chmod(fh.name, 0o600)
        fcntl.flock(fh, fcntl.LOCK_EX)  # blocking: wait for an in-flight publication
        try:
            with _write_lock(directory) as boundary:
                if native_blocked(directory, _load_state(directory)):
                    raise RuntimeError("native unresolved barrier requires reconciliation before config writes")
                yield boundary
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


def _deployment_identity(provider, host, repository, pr) -> dict | None:
    """Immutable actual deployment provenance; never default a missing provider."""
    provider = (provider or "").strip().lower()
    host = (host or "").strip()
    repository = (repository or "").strip()
    if not provider and not host and not repository:
        return None
    if (provider not in DEPLOYMENT_PROVIDERS
            or HOST_NAME.fullmatch(host) is None
            or REPOSITORY_NAME.fullmatch(repository) is None
            or type(pr) is not int or pr <= 0):
        return None
    return {"provider": provider, "host": host, "repository": repository, "pr": pr}


def _outcome_deployment(value) -> dict | None:
    if (not isinstance(value, dict)
            or set(value) != {"provider", "host", "repository", "pr"}
            or any(not isinstance(value[k], str) for k in ("provider", "host", "repository"))):
        return None
    identity = _deployment_identity(value["provider"], value["host"], value["repository"], value["pr"])
    return identity if identity == value else None


def _valid_head_outcome(key, outcome, generation) -> bool:
    try:
        return (isinstance(key, str) and ATTEMPT_ID.fullmatch(key) is not None
                and outcome.get("attempt_id") == key
                and type(generation) is int
                and type(outcome.get("generation")) is int
                and 1 <= outcome["generation"] <= generation
                and type(outcome.get("pr")) is int and outcome["pr"] > 0
                and outcome.get("hosting") == PUBLIC_CODE_HOSTING
                and type(outcome["hosting"]["repository_id"]) is int
                and _outcome_deployment(outcome.get("deployment")) is not None
                and outcome["deployment"]["pr"] == outcome["pr"]
                and outcome.get("candidate") is None
                and outcome.get("result") in {OUTCOME_RESULT_SUCCESS, OUTCOME_RESULT_FAILED}
                and type(outcome.get("rc")) is int and 0 <= outcome["rc"] <= 255
                and (outcome["rc"] == 0) == (outcome["result"] == OUTCOME_RESULT_SUCCESS)
                and isinstance(outcome.get("stage"), str)
                and OUTCOME_STAGE.fullmatch(outcome["stage"]) is not None
                and isinstance(outcome.get("verification"), str)
                and VERIFICATION.fullmatch(outcome["verification"]) is not None
                and str(uuid.UUID(outcome["public_receipt"])) == outcome["public_receipt"]
                and isinstance(outcome.get("outcome_at"), str)
                and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", outcome["outcome_at"]) is not None
                and type(outcome.get("created_at")) is int and outcome["created_at"] >= 0)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def failed_head_status(state: dict, pr: int, sha: str, deployment: dict | None = None) -> str | None:
    """Pure ordinary-GitHub admission check; publication is never clearance.

    Explicit foreign provenance stays separate. An unqualified legacy caller
    only collides with a typed GitHub deployment, never an unproven old attempt.
    Missing provenance for the protected GitHub target requires reconciliation;
    the public reporting sink cannot identify a foreign deployment for it.
    """
    public = _deployment_identity("github", "github.com", PUBLIC_CODE_HOSTING["repository"], pr)
    target = deployment if deployment is not None else public
    if (target is None or target.get("provider") != "github"
            or not isinstance(sha, str) or FULL_SHA.fullmatch(sha) is None):
        return None
    unknown_blocks = deployment is not None and target == public
    outcomes = state.get("outcomes", {})
    if not isinstance(outcomes, dict):
        return "reconcile-needed" if unknown_blocks else None
    matches = {}
    for key, outcome in outcomes.items():
        if not isinstance(outcome, dict) or outcome.get("pr") != pr or outcome.get("sha") != sha:
            continue
        actual = _outcome_deployment(outcome.get("deployment"))
        if actual is None:
            if unknown_blocks:
                return "reconcile-needed"
            continue
        valid = _valid_head_outcome(key, outcome, state.get("generation"))
        if actual["provider"] != "github" or deployment is not None and actual != target:
            if unknown_blocks and not valid:
                return "reconcile-needed"
            continue
        if not valid:
            return "reconcile-needed"
        matches[key] = outcome
    last = state.get("last_attempt")
    if isinstance(last, dict) and last.get("pr") == pr and last.get("sha") == sha:
        key = last.get("outcome_id")
        outcome = matches.get(key) if isinstance(key, str) else None
        referenced = outcomes.get(key) if isinstance(key, str) else None
        actual = _outcome_deployment(referenced.get("deployment")) if isinstance(referenced, dict) else None
        if outcome is not None:
            if (type(last.get("pr")) is not int or type(last.get("generation")) is not int
                    or last["generation"] != outcome["generation"]
                    or last.get("result") != outcome["result"]):
                return "reconcile-needed"
        elif unknown_blocks and (actual is None or actual == target
                                 or referenced.get("pr") != pr or referenced.get("sha") != sha):
            return "reconcile-needed"
    elif last is not None and not isinstance(last, dict) and unknown_blocks:
        return "reconcile-needed"
    if not matches:
        return None
    identities = {tuple(outcome["deployment"][key] for key in ("provider", "host", "repository", "pr"))
                  for outcome in matches.values()}
    if deployment is None and len(identities) != 1:
        return "reconcile-needed"
    latest = max(outcome["generation"] for outcome in matches.values())
    current = [outcome for outcome in matches.values() if outcome["generation"] == latest]
    if len(current) != 1:
        return "reconcile-needed"
    return "failed-head" if current[0]["result"] == OUTCOME_RESULT_FAILED else None


def cmd_acquire(args) -> int:
    if os.environ.get(ATTEMPT_ENV) is not None:
        return _fail("cannot acquire another lease inside an attempt", EXIT_TOKEN_MISMATCH)
    try:
        with _attempt_lock(_state_dir()):
            return _acquire(args)
    except BlockingIOError:
        _emit({"ok": False, "reason": "pipeline-process-busy"})
        return EXIT_LOCK_BUSY


def _acquire(args) -> int:
    """抢锁。无活跃锁（或过期可抢占）时成功；被占用 -> rc 10。"""
    directory = _state_dir()
    with _write_lock(directory):
        deployment = _deployment_identity(
            getattr(args, "deployment_provider", ""),
            getattr(args, "deployment_host", ""),
            getattr(args, "deployment_repository", ""), args.pr)
        if (getattr(args, "deployment_provider", "")
                or getattr(args, "deployment_host", "")
                or getattr(args, "deployment_repository", "")) and deployment is None:
            return _fail("invalid deployment identity", EXIT_USAGE)
        strict = deployment is not None and deployment["provider"] == "github"
        try:
            state = _load_state(directory, heal=not strict, strict=strict)
        except ValueError:
            _emit({"ok": False, "reason": "reconcile-needed", "status": "failed-pending"})
            return EXIT_FAILED_PENDING
        if native_blocked(directory, state):
            return _fail("native grant/admission requires reconciliation", EXIT_LOCK_BUSY)
        now = _now()
        lock = state.get("lock")
        if _lock_active(lock, now, _ttl()):
            _emit({"ok": False, "reason": "lock-busy", "lock": _lock_brief(lock)})
            return EXIT_LOCK_BUSY

        blocked = failed_head_status(state, args.pr, args.sha or "", deployment)
        if blocked:
            _emit({"ok": False, "reason": blocked, "status": "failed-pending",
                   "pr": args.pr, "sha": args.sha})
            return EXIT_FAILED_PENDING

        if isinstance(lock, dict):
            # 过期锁被抢占：留证据（诊断「谁死在半路」）
            evicted = dict(lock)
            evicted["evicted_at"] = now
            state["last_evicted"] = evicted

        token = secrets.token_hex(8)
        try:
            previous_generation = int(state.get("generation") or 0)
        except (TypeError, ValueError):
            previous_generation = 0
        generation = previous_generation + 1
        state["generation"] = generation
        state["lock"] = {
            "token": token,
            "session": args.session or "",
            "pr": args.pr,
            "sha": args.sha or "",
            "phase": args.phase or "acquire",
            "acquired_at": now,
            "heartbeat_at": now,
            "generation": generation,
            "attempt_id": secrets.token_hex(16),
            "deployment": deployment,
        }
        _save_state(directory, state)
        token_file = _write_token_file(directory, token)
        _emit({
            "ok": True,
            "token": token,
            "token_file": str(token_file),
            "pr": args.pr,
            "sha": args.sha or "",
            "session": args.session or "",
            "ttl": _ttl(),
            "state_dir": str(directory),
        })
        return EXIT_OK


def _beat(args, apply_phase: bool, token=None) -> tuple[int, dict]:
    """一次续约。返回 (rc, payload)，不做 I/O 输出，供单发与循环共用。"""
    directory = _state_dir()
    token = _resolve_token(args, directory) if token is None else token
    with _write_lock(directory):
        state = _load_state(directory, heal=True)
        lock = state.get("lock")
        if (not isinstance(lock, dict) or not token or lock.get("token") != token
                or not _attempt_owns(lock)):
            return EXIT_TOKEN_MISMATCH, {"ok": False, "reason": "token-mismatch-or-no-lock"}
        now = _now()
        expired = not _lock_active(lock, now, _ttl())
        lock["heartbeat_at"] = now
        if apply_phase and args.phase:
            lock["phase"] = args.phase
        _save_state(directory, state)
        return EXIT_OK, {"ok": True, "revived": expired, "phase": lock.get("phase")}


def _heartbeat_loop(args) -> int:
    """周期续约循环（供编排脚本后台常驻）。

    与单发心跳的区别：一次进程存活期内持续续约，把「阶段耗时超过 TTL」这
    一类故障从结构上消掉 —— 2026-10-17 实测 verify 10 分钟、promote 7.5 分钟
    都不打心跳（up/verify/promote/switch 内部只 check 不 beat），只能靠放大
    TTL 硬兜。

    - phase 只在第一拍应用：主流程后续可用单发 heartbeat --phase 推进阶段，
      不会被后台循环盖回去。
    - token 不匹配（锁被抢占 / 已被 release）→ 立即退出 rc 11，不制造噪音。
    - SIGTERM / SIGINT 优雅退出 rc 0，便于编排脚本 cleanup 时收割。
    """
    try:
        interval = int(args.interval)
    except (TypeError, ValueError):
        return _fail("--interval 需要整数秒", EXIT_USAGE)
    if interval < 1:
        return _fail("--interval 必须 >= 1", EXIT_USAGE)

    # Resolve the shared handoff file exactly once for this process lifetime.
    token = _resolve_token(args, _state_dir())
    stop = {"flag": False}

    def _on_signal(_signum, _frame):
        stop["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass  # 非主线程（被 import 复用）时只能靠 terminate 强杀

    beats = 0
    while not stop["flag"]:
        rc, payload = _beat(args, apply_phase=(beats == 0), token=token)
        payload["beat"] = beats + 1
        _emit(payload)
        if rc != EXIT_OK:
            print("[deploy_state] heartbeat loop 终止：锁已不属于本会话", file=sys.stderr)
            return rc
        beats += 1
        if args.max_beats and beats >= args.max_beats:
            return EXIT_OK
        # 切片 sleep：SIGTERM 最多延迟 0.5s 生效，不用等满整个 interval
        slept = 0.0
        while slept < interval and not stop["flag"]:
            time.sleep(min(0.5, interval - slept))
            slept += 0.5

    print(f"[deploy_state] heartbeat loop 收到终止信号，共 {beats} 拍", file=sys.stderr)
    return EXIT_OK


def cmd_heartbeat(args) -> int:
    """续约。token 必须匹配当前锁；过期锁若未被抢占仍可救活。"""
    if getattr(args, "loop", False):
        return _heartbeat_loop(args)
    rc, payload = _beat(args, apply_phase=True)
    _emit(payload)
    return rc


def _mint_outcome(state: dict, lock: dict, sha: str, pr, now: int,
                  result: str, rc: int, stage: str, verification: str) -> dict | None:
    """Create the immutable outcome for a successful, owned normal release.

    Issuance requires the live pipeline descriptor AND an identity-bound lock
    that captured the actual deployment provenance. A legacy/manual CLI
    release, a failed release operation, a no-op/dry-run path, a short SHA, a
    non-numeric PR, a missing deployment identity or a generation mismatch
    returns None. Both a valid success and a valid failed deployment result are
    recorded with their actual rc/stage; the caller commits this record in the
    same atomic state write that closes the attempt.
    """
    if os.environ.get(ATTEMPT_ENV) is None or _process_lock_fd() is None:
        return None
    if type(pr) is not int or pr <= 0 or not FULL_SHA.fullmatch(sha or ""):
        return None
    generation = lock.get("generation")
    if type(generation) is not int or generation < 1 or state.get("generation") != generation:
        return None
    outcomes = state.get("outcomes")
    if not isinstance(outcomes, dict):
        return None
    deployment = lock.get("deployment")
    if (not isinstance(deployment, dict)
            or _deployment_identity(deployment.get("provider"), deployment.get("host"),
                                    deployment.get("repository"), deployment.get("pr"))
            != deployment):
        return None
    if result not in {OUTCOME_RESULT_SUCCESS, OUTCOME_RESULT_FAILED}:
        return None
    if type(rc) is not int or not 0 <= rc <= 255:
        return None
    if (rc == 0) != (result == OUTCOME_RESULT_SUCCESS):
        return None
    if result == OUTCOME_RESULT_SUCCESS:
        stage = "complete"
    else:
        stage = (stage or "").strip()
        if not OUTCOME_STAGE.fullmatch(stage):
            stage = "unknown"
    attempt_id = lock.get("attempt_id")
    if not isinstance(attempt_id, str) or not ATTEMPT_ID.fullmatch(attempt_id):
        attempt_id = secrets.token_hex(16)
    if attempt_id in outcomes:
        return None
    notice = (verification or "normal").strip()
    if not VERIFICATION.fullmatch(notice):
        notice = "normal"
    outcome = {
        "attempt_id": attempt_id,
        "generation": generation,
        "pr": pr,
        "sha": sha,
        "hosting": dict(PUBLIC_CODE_HOSTING),
        "deployment": dict(deployment),
        "result": result,
        "rc": rc,
        "stage": stage,
        "verification": notice,
        "outcome_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "public_receipt": str(uuid.uuid4()),
        "attempt_token": lock.get("token") or "",
        "session": lock.get("session") or "",
        "created_at": now,
        "facts": None,
        "payload_sha256": None,
    }
    outcomes[attempt_id] = outcome
    return outcome


def cmd_release(args) -> int:
    """收官。success -> 更新 deployed 锚点并清 last_attempt；failed -> 记 last_attempt。"""
    directory = _state_dir()
    token = _resolve_token(args, directory)
    with _write_lock(directory):
        state = _load_state(directory, heal=True)
        if native_blocked(directory, state):
            return _fail("native candidate cannot use ordinary release", EXIT_LOCK_BUSY)
        lock = state.get("lock")
        if (not isinstance(lock, dict) or not token or lock.get("token") != token
                or not _attempt_owns(lock)):
            _emit({"ok": False, "reason": "token-mismatch-or-no-lock"})
            return EXIT_TOKEN_MISMATCH

        now = _now()
        if os.environ.get(ATTEMPT_ENV) is not None and args.sha != lock.get("sha"):
            return _fail("release sha differs from attempt", EXIT_TOKEN_MISMATCH)
        sha = (args.sha or lock.get("sha") or "").strip()
        pr = lock.get("pr")

        if args.result == "success":
            if not sha:
                return _fail("release success 需要 sha（锁内为空且未给 --sha）", EXIT_USAGE)
            deployed = {"sha": sha, "pr": pr, "at": now}
            # Same atomic commit carries both the release anchor and the
            # immutable outcome reference. No outcome -> no GitHub authority.
            outcome = _mint_outcome(
                state, lock, sha, pr, now, OUTCOME_RESULT_SUCCESS, 0, "complete",
                args.verification)
            if outcome is not None:
                deployed["generation"] = outcome["generation"]
                deployed["outcome_id"] = outcome["attempt_id"]
            state["deployed"] = deployed
            state["last_attempt"] = None
        else:
            state["last_attempt"] = {
                "sha": sha,
                "pr": pr,
                "result": "failed",
                "note": args.note or "",
                "at": now,
            }
            # A successfully released FAILED deployment keeps its actual typed
            # rc/stage as a durable outcome. A failed release operation or an
            # unowned caller still mints nothing.
            outcome = _mint_outcome(
                state, lock, sha, pr, now, OUTCOME_RESULT_FAILED, args.rc,
                args.stage, args.verification)
            if outcome is not None:
                state["last_attempt"]["generation"] = outcome["generation"]
                state["last_attempt"]["outcome_id"] = outcome["attempt_id"]

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
    if native_blocked(directory, state):
        _emit({"status": "reconcile_required"})
        return EXIT_LOCK_BUSY
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
        if native_blocked(directory, state):
            return _fail("native state cannot be initialized/healed", EXIT_LOCK_BUSY)
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
        if native_blocked(directory, state):
            return _fail("native unresolved barrier cannot be cleared", EXIT_LOCK_BUSY)
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
            "generation": state.get("generation", 0),
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


NATIVE_STAGES = ("candidate", "up", "verify", "promote", "switch", "observe")


def _native_owned(state: dict) -> dict:
    native = state.get("native_queue")
    active = native.get("active") if isinstance(native, dict) else None
    lock = state.get("lock")
    if (not native_consistent(_state_dir(), state) or not isinstance(active, dict)
            or not isinstance(lock, dict) or ATTEMPT_ENV not in os.environ
            or not _attempt_owns(lock) or active["origin_attempt_id"] != lock.get("attempt_id")
            or active["origin_generation"] != state.get("generation")
            or lock.get("sha") != active["candidate"].get("manifest", {}).get("group_sha")):
        raise ValueError("native attempt ownership lost")
    return active


def cmd_native_candidate(args) -> int:
    try:
        active = _native_owned(_load_state(_state_dir()))
        if active["phase"] != "deploy":
            raise ValueError("native candidate is not deploying")
        _emit(active["candidate"]["manifest"])
        return EXIT_OK
    except (ValueError, KeyError, TypeError):
        return EXIT_TOKEN_MISMATCH


def cmd_native_stage(args) -> int:
    try:
        with _write_lock(_state_dir()):
            state = _load_state(_state_dir())
            active = _native_owned(state)
            if active["phase"] != "deploy":
                raise ValueError("native stage is already sealed")
            journal = active["journal"]
            completed = [row["stage"] for row in journal if row.get("completed") is True]
            if args.action == "start":
                if (completed != list(NATIVE_STAGES[:len(completed)])
                        or len(journal) != len(completed)
                        or len(completed) >= len(NATIVE_STAGES)
                        or args.stage != NATIVE_STAGES[len(completed)]):
                    raise ValueError("native stage out of order")
                journal.append({"stage": args.stage, "started_monotonic": time.monotonic(),
                                "started_at": _now(), "completed": False})
            else:
                if not journal or journal[-1]["stage"] != args.stage or journal[-1]["completed"]:
                    raise ValueError("native stage missing or repeated")
                journal[-1].update(completed=True, rc=0, completed_monotonic=time.monotonic(),
                                   completed_at=_now())
            _save_state(_state_dir(), state)
        return EXIT_OK
    except (ValueError, KeyError, TypeError):
        return EXIT_TOKEN_MISMATCH


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="deploy_state.py", description="蓝绿部署单飞状态原语")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("native-candidate", help="owned native candidate; never grants release")
    p.set_defaults(func=cmd_native_candidate)
    p = sub.add_parser("native-stage", help="record owned real pipeline stage boundaries")
    p.add_argument("--stage", required=True, choices=NATIVE_STAGES)
    p.add_argument("--action", required=True, choices=["start", "done"])
    p.set_defaults(func=cmd_native_stage)

    p = sub.add_parser("run", help="hold the normal pipeline process lock through exec")
    p.add_argument("exec", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("process-owned", help="validate the inherited pipeline lock descriptor")
    p.set_defaults(func=cmd_process_owned)

    p = sub.add_parser("assert-owner", help="require the immutable pipeline attempt identity")
    p.add_argument("--pr", type=int, default=None)
    p.add_argument("--sha", default="")
    p.set_defaults(func=cmd_assert_owner)

    p = sub.add_parser("owned-checkout", help="order local candidate/main checkout with revocation")
    p.add_argument("--repo", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--candidate", default="")
    g.add_argument("--main", action="store_true")
    p.set_defaults(func=cmd_owned_checkout)

    p = sub.add_parser("check", help="锁活跃性检查（rc 0=无锁 / 10=活跃 / 12=PR 不匹配）")
    p.add_argument("--pr", type=int, default=None, help="交叉校验：锁须属于该 PR（或锁未绑定 PR）")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("acquire", help="抢锁（rc 10=被占用）")
    p.add_argument("--pr", type=int, default=None)
    p.add_argument("--sha", default="")
    p.add_argument("--session", default="")
    p.add_argument("--phase", default="")
    p.add_argument("--deployment-provider", default="",
                   help="actual deployment hosting provider: github|gitea")
    p.add_argument("--deployment-host", default="")
    p.add_argument("--deployment-repository", default="")
    p.set_defaults(func=cmd_acquire)

    p = sub.add_parser("heartbeat", help="续约（rc 11=token 不匹配）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--token", default="", help="锁 token（缺省读 <state_dir>/lock.token）")
    g.add_argument("--token-file", default="", help="从指定文件读 token")
    p.add_argument("--phase", default="")
    p.add_argument("--loop", action="store_true",
                   help="常驻循环续约（供编排脚本后台跑；收到 SIGTERM/SIGINT 优雅退出）")
    p.add_argument("--interval", type=int, default=120,
                   help="--loop 的续约间隔秒（默认 120，须 < TTL/2）")
    p.add_argument("--max-beats", type=int, default=0,
                   help="--loop 最多拍数（默认 0 = 无限；测试与短任务用）")
    p.set_defaults(func=cmd_heartbeat)

    p = sub.add_parser("release", help="收官（success 更新 deployed 锚点）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--token", default="", help="锁 token（缺省读 <state_dir>/lock.token）")
    g.add_argument("--token-file", default="", help="从指定文件读 token")
    p.add_argument("--result", required=True, choices=["success", "failed"])
    p.add_argument("--sha", default="")
    p.add_argument("--note", default="")
    p.add_argument("--rc", type=int, default=-1,
                   help="actual pipeline exit code for a failed deployment result")
    p.add_argument("--stage", default="", help="actual failing stage for a failed result")
    p.add_argument("--verification", default="normal",
                   help="private honest notice: normal|skip-verify|short-observe|…")
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
