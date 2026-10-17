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
#      prune，防状态无限膨胀；failures 额外排除已 handled 的 id（否则该类条目
#      永不被重试却让 retry_due() 恒 True → 主循环每轮空跑）
#   7. 状态里的失败条目在 **load() 边界一次性归一化**（`next` 非数值 / 非有限 /
#      溢出 / 负数 / 超出「配置封顶 + 时钟余量」（`sane_ahead_for`），`n` 非数值 /
#      非正整数 / 越界 → 一律钳制为合法值；非 dict 的值丢弃；`failures` /
#      `handled_ids` / `bad_hashes` 的**容器级**损坏（不是 dict / list、含非字符串
#      元素）同样归一为合法形态），**且每次实际钳制 / 丢弃都打 WARNING 留痕**
#      （不静默 —— 否则运维无法从 journal 区分「状态正常」与「损坏但被静默降级」）。
#      两个「绝不允许」：
#        · 损坏的 state 绝不能让 listener 崩溃（systemd Restart=always 会把崩溃
#          掩盖成「重启」）—— 异常网须覆盖 OSError + ValueError（含
#          JSONDecodeError / UnicodeDecodeError / json 对 >4300 位整数与 inbox 行
#          解析的 ValueError）+ RecursionError（超深嵌套）；日志里一切来自状态文件
#          或 inbox 的键值都必须过 `_short()`（孤立代理字符原样 print 会抛
#          UnicodeEncodeError，那又是同一条崩溃路径）—— **`delivery_id` 之外的
#          字段（`pr_number` / `pr_head_sha` / `repo_full_name` / `action` 等）
#          同受本规则约束**，不得只在 id 字段上做转义
#        · 损坏的条目绝不能被静默锁死 —— 归一化后只剩「立即重试」或「合法等待」
#          两种归宿，两者都在 journal 有痕
#      归一化只用 _sane_next / _normalize_failure / _str_set，上界存在状态对象上
#      （`state.sane_ahead`），使用点（retry_due / process_once）与归一化点共用
#      同一口径 —— 不存在「一处判损坏、另一处照采信」（Issue #51 第 5 轮 M-1 教训）
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
import math
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

# 退避表合理性上界（load() 边界归一化 / 纵深防御共用）：
#   程序自身**只可能**写出 `next ≤ now + retry_max`（写点唯一，见 process_once），
#   因此「比配置封顶还晚」的 next 只可能来自损坏 state（时钟被回拨 / 手改 / 版本
#   错位）。上界 = 配置封顶 + 时钟余量，余量用于容忍宿主时钟小幅回拨 —— 判错的
#   代价只是「提前重试一次 + 一条 WARNING」，无副作用，故宁可收紧不放松。
RETRY_NEXT_CLOCK_SLACK_S = 300.0
MAX_SANE_ATTEMPTS = 1024  # n 的位数上界：远超真实需要（60s 封顶下 6 次即到顶）


def sane_ahead_for(retry_max: float) -> float:
    """由配置的退避封顶推出「next 的合理性上界」（秒）。"""
    base = retry_max if retry_max > 0 else DEFAULT_RETRY_MAX
    return base + RETRY_NEXT_CLOCK_SLACK_S


DEFAULT_SANE_AHEAD = DEFAULT_RETRY_MAX + RETRY_NEXT_CLOCK_SLACK_S


def _log(msg: str) -> None:
    print(f"[inbox_watcher] {msg}", flush=True)


def _warn(msg: str) -> None:
    # 拦截 / 失败属必须留痕事件，一律 WARNING 级（与 [dry_run] BLOCKED 同规）
    print(f"[inbox_watcher] WARNING: {msg}", flush=True)


def _short(value: object, limit: int = 160) -> str:
    """把任意值 repr 成可安全打印的短串（截断 + repr 自身转义代理字符）。"""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


# ============================================================
# 损坏状态归一化（全模块唯一口径，Issue #66 / #51）
# ============================================================
def _sane_next(
    value: object, now: float, sane_ahead: float = DEFAULT_SANE_AHEAD
) -> float | None:
    """把任意 `next` 值归一为合法的「下次可重试时刻」（unix 秒）；None = 损坏。

    合法 = 有限、非负、且不超过 `now + sane_ahead` 的实数（上界由配置封顶推出，
    见 `sane_ahead_for`）。

    ⚠️ `float()` 对 ≥309 位整数抛的是 **OverflowError**（ArithmeticError 子类，
    **不是** ValueError），必须显式列入 except —— 否则损坏 state 会直穿 main()
    （只捕 KeyboardInterrupt）→ systemd `Restart=always` 崩溃循环。
    """
    try:
        value_f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value_f) or value_f < 0:
        return None
    return value_f if value_f <= now + sane_ahead else None


def _normalize_failure(
    entry: dict, now: float, sane_ahead: float = DEFAULT_SANE_AHEAD
) -> dict:
    """把一条失败条目归一为合法形态 `{"n": int ∈ [0, MAX_SANE_ATTEMPTS], "next": float ≥ 0}`。

    损坏值一律按下述**保守**语义钳制（绝不丢弃整条 = 绝不静默放弃重试）：
    - `next` 损坏 → `0.0`（等价「已到期」）：下一轮重试一次，随后被成功 / 失败
      分支重写成合法值 —— 自愈。
    - `n` 损坏 → `0`：退避从首次失败重新累计（绝不允许负次数或天文位数）。

    本函数是 **load() 边界的唯一归一化点**：把「损坏形态」的枚举收口到一处，
    而不是在每个使用点各补一次 except（Issue #51 审核连续 4 轮 FAIL 的根因）。
    """
    try:
        n = int(entry.get("n", 0))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        n = 0
    if n < 0 or n > MAX_SANE_ATTEMPTS:
        n = 0
    nxt = _sane_next(entry.get("next", 0), now, sane_ahead)
    return {"n": n, "next": 0.0 if nxt is None else nxt}


def _str_set(value: object, field: str) -> set[str]:
    """把状态里的字符串集合字段（`handled_ids` / `bad_hashes`）归一为 `set[str]`。

    字段缺失（`None`）= 首次写入前的正常态，不告警；**形状损坏**（不是 list）或
    列表内混入非字符串元素一律留痕 —— 前者静默丢弃会让去重集无声清空（后果是
    同一 delivery 被重复触发），后者静默丢弃会让坏行指纹去重失效。
    """
    if value is None:
        return set()
    if not isinstance(value, list):
        _warn(f"state 损坏: {field} 不是列表（{_short(value)}）→ 该集合按空处理")
        return set()
    items = {x for x in value if isinstance(x, str)}
    dropped = len(value) - len(items)
    if dropped:
        _warn(
            f"state 损坏: {field} 含 {dropped} 个非字符串元素 → 丢弃"
            f"（保留 {len(items)} 条）"
        )
    return items


def _next_at(
    entry: object, now: float, sane_ahead: float = DEFAULT_SANE_AHEAD
) -> float | None:
    """取失败条目的「下次可重试时刻」；None = 损坏（调用方一律按「已到期」处理）。

    与 load() 共用 `_sane_next` 的同一口径（纵深防御：覆盖内存态与未来新增写入点，
    不会出现「一处判损坏、另一处照采信」的不一致）。缺 `next` 归为 0.0
    （同样等价「已到期」，故不单列为损坏形态）。
    """
    if not isinstance(entry, dict):
        return None
    return _sane_next(entry.get("next", 0), now, sane_ahead)


def _attempts(entry: object) -> int:
    """取失败条目的「已失败次数」；非 dict / 非数值 / 越界一律当 0。

    与 load() 的归一化同口径。`int(nan)` 抛 ValueError、`int(±inf)` 抛
    **OverflowError**，两者都必须显式兜住 —— 否则损坏的 `n` 会直穿 main()。
    """
    if not isinstance(entry, dict):
        return 0
    try:
        n = int(entry.get("n", 0))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if 0 < n <= MAX_SANE_ATTEMPTS else 0


def retry_due(state: WatcherState, now: float | None = None) -> bool:
    """是否存在「退避期已过、等待重试」的失败条目（Issue #51 / #66）。

    main() 主循环默认只在 inbox 文件签名（inode / size / mtime_ns）变化时才
    调 process_once；而重试**不写 inbox**（inbox 是 receiver 单向 append-only
    的），所以「退避到期」这个事件永远不会改变签名。若主循环只看签名，失败条目
    就只在「下一条新事件到来」时才被重试 —— 触发失败 + 无后续事件 = 永不重试。

    故主循环必须把本条件与签名变化**并列**作为 process_once 的触发条件。
    损坏条目（`_next_at` 返回 None）同样返回 True：宁可多跑一轮触发自愈，也不
    让一个无法解释的状态把条目永久锁死。上界取 `state.sane_ahead`（= load() 时由
    配置封顶推出），保证与归一化点**同一口径**。
    """
    if now is None:
        now = time.time()
    for entry in state.failures.values():
        next_at = _next_at(entry, now, state.sane_ahead)
        if next_at is None or now >= next_at:
            return True
    return False


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
    # `next` 的合理性上界（秒，相对 now）——由配置退避封顶推出（sane_ahead_for），
    # 运行期不变式，**不落盘**（save() 的 payload 是显式构造的）。放在状态对象上
    # 是为了让归一化点（load）与所有使用点（retry_due / process_once）共用同一口径。
    sane_ahead: float = DEFAULT_SANE_AHEAD

    @classmethod
    def load(
        cls, path: Path, *, sane_ahead: float = DEFAULT_SANE_AHEAD
    ) -> WatcherState:
        """读状态文件并把**全部**损坏形态在边界归一化（本模块唯一的归一化点）。

        异常网必须覆盖三种「损坏 state 不崩」的形态，否则直穿 main() → systemd
        `Restart=always` 崩溃循环（正是本 PR 要修的缺陷同型）：
        - `OSError`：文件不可读
        - `ValueError`：`json.JSONDecodeError`（语法），`UnicodeDecodeError`（非 UTF-8
          字节），以及 `json.loads` 对 **> 4300 位整数字面量**抛的 ValueError
          （CPython `sys.get_int_max_str_digits()` 默认 4300 —— 该形态比
          `float()` 的 309 位溢出阈值更早触发，且异常类型不同）
        - `RecursionError`：超深嵌套的 JSON（C 扫描器对 `[[[…]]]` 递归过深）
        """
        if not path.exists():
            return cls(sane_ahead=sane_ahead)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            _warn(f"state file unreadable ({_short(exc)}); starting with empty state")
            return cls(sane_ahead=sane_ahead)
        if not isinstance(raw, dict):
            _warn("state file malformed (not an object); starting with empty state")
            return cls(sane_ahead=sane_ahead)
        handled = raw.get("handled_ids")
        bad = raw.get("bad_hashes")
        raw_failures = raw.get("failures")
        now = time.time()

        # 归一化 + **留痕**（Issue #51 审核 M-1：只修「永久锁死」不修「无 WARNING」
        # 等于把静默丢弃缩量保留）。留痕覆盖**条目级与容器级**两类损坏：任一形态的
        # 钳制 / 丢弃都必须有痕，否则运维无法从 journal 区分「状态正常」与「状态
        # 已损坏但被静默降级」。合法条目走快速路径不打扰。
        # ⚠️ 日志里一切来自状态文件的**键与值**都必须过 `_short()`（= `repr` 截断）：
        # 孤立代理字符键（JSON `"\ud800"` 可作对象键）原样插进 f-string 会让 `print`
        # 抛 UnicodeEncodeError —— 那就是又一条「损坏 state 直穿 main()」的崩溃路径。
        failures: dict[str, dict] = {}
        if raw_failures is None:
            pass  # 键缺失 = 首次写入前的正常态
        elif not isinstance(raw_failures, dict):
            _warn(
                f"state 损坏: failures 不是对象（{_short(raw_failures)}）→ "
                "整张退避表按空处理（所有条目下一轮重试一次）"
            )
        else:
            for key, entry in raw_failures.items():
                shown = _short(key)
                if not isinstance(key, str):
                    _warn(f"state 损坏: failures 的键 {shown} 不是字符串 → 丢弃")
                    continue
                if not isinstance(entry, dict):
                    _warn(
                        f"state 损坏: failures[{shown}] = {_short(entry)} 不是对象 → "
                        "丢弃该条目（下一轮按「未失败」重试一次）"
                    )
                    continue
                fixed = _normalize_failure(entry, now, sane_ahead)
                if fixed != entry:
                    _warn(
                        f"state 损坏: failures[{shown}] = {_short(entry)} → "
                        f"归一为 {_short(fixed)}（next 归 0 = 立即可重试）；"
                        "重试一轮后自动重写为合法值"
                    )
                failures[key] = fixed

        return cls(
            handled_ids=_str_set(handled, "handled_ids"),
            failures=failures,
            bad_hashes=_str_set(bad, "bad_hashes"),
            sane_ahead=sane_ahead,
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
    """解析 inbox 行；返回 None = 坏行（调用方按既有指纹去重路径处理）。

    异常网必须覆盖 `ValueError`（含 `JSONDecodeError`，以及 `json.loads` 对 **>4300
    位整数字面量**抛的 ValueError）与 `RecursionError`（超深嵌套）—— 只捕
    `JSONDecodeError` 时，这样一行会让 `process_once` 抛出 → `main()`（只捕
    KeyboardInterrupt）直接退出 → `Restart=always` 反复崩在同一行（该行永远不会被
    记入 `bad_hashes`，故永不跳过）。与孪生副本 `dailycheck_dashboard_on_pr.py` 同口径。
    """
    try:
        rec = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return rec if isinstance(rec, dict) else None


def _delivery_id(rec: dict) -> str | None:
    """取 delivery_id；不可用（非字符串 / 空 / **无法 UTF-8 编码**）返回 None。

    孤立代理字符（inbox 行里写 `"\\ud800"` 这类 JSON 转义）能被 `json.loads` 解出，
    但既无法用 `print` 打印（`UnicodeEncodeError`），也无法作为 state 文件的键写盘
    —— 故在此一律按「坏行」处理（坏行路径走指纹去重 + `!r` 转义打印，不崩）。
    Issue #66 审核 L-6：与孪生副本 `deploy/scripts/dailycheck_dashboard_on_pr.py`
    保持同一口径。
    """
    did = rec.get("delivery_id")
    if not isinstance(did, str) or not did:
        return None
    try:
        did.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return did


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
    """第 attempts 次失败后的退避时长：base * 2^(n-1)，封顶 retry_max。

    用 `math.ldexp`（= base * 2**exp，浮点指数运算）而非 `base * 2 ** (n-1)`：
    后者在 attempts 异常大时（只可能来自被损坏的 state.json）先构造天文位数的
    大整数再转 float —— 实测 n=10**18 挂死 >8s、n=int(1e300) 挂死 >5 分钟
    （Issue #66 复现记录）；ldexp 立刻以 OverflowError 报溢出，由下面的兜底
    收口为封顶值。
    """
    try:
        delay = math.ldexp(float(cfg.retry_base), attempts - 1)
    except OverflowError:
        delay = math.inf  # 溢出 ⇒ 必然超过封顶 ⇒ 交由 retry_max 收口
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
        # M-1-r4：action 为 list / dict 等**不可哈希**值时，`x in frozenset` 抛
        # TypeError（不返回 False）→ 直穿 main() → Restart=always 永久崩溃循环。
        # 先判类型，非字符串一律按「非白名单」走既有 ignored 分支（!r 留痕）。
        if not isinstance(action, str) or action not in cfg.actions:
            state.handled_ids.add(did)
            dirty = True
            _log(f"ignored: delivery={did} action={action!r} (not in allowlist)")
            continue

        # 退避期内的失败条目：本轮跳过（下一轮再看）
        fail = state.failures.get(did)
        next_at = _next_at(fail, now, state.sane_ahead)
        if next_at is not None and now < next_at:
            continue

        ok, detail = run_trigger(cfg.trigger_argv, line, cfg.trigger_timeout)
        if ok:
            state.handled_ids.add(did)
            state.failures.pop(did, None)
            dirty = True
            _log(
                f"triggered: delivery={did} pr={_short(rec.get('pr_number'))} "
                f"action={action} head={_short(str(rec.get('pr_head_sha', ''))[:8])}"
            )
        else:
            attempts = _attempts(fail) + 1
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
    # failures 额外排除已 handled 的 id：process_once 开头的 handled 短路使这类条目
    # 永不重试（且**无任何日志**），留在表里只会让 retry_due() 恒 True → 主循环空转
    new_failures = {
        k: v for k, v in state.failures.items() if k in file_ids and k not in new_handled
    }
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
    state = WatcherState.load(
        cfg.state_file, sane_ahead=sane_ahead_for(cfg.retry_max)
    )
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
            if sig != last_sig or retry_due(state):
                # 除「文件变化」外，退避到期的重试也必须驱动一轮处理（Issue #51）：
                # 重试本身不写 inbox → 签名不变 → 只等签名就永不重试。
                process_once(cfg, state)
                last_sig = sig
            time.sleep(args.poll_interval)
        except KeyboardInterrupt:
            _log("interrupted; exiting")
            return 0


if __name__ == "__main__":
    sys.exit(main())
