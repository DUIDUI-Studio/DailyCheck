"""限速纯逻辑层（无 Flask / ASGI 依赖，易测）。

三个组件（P0-4）：
1. ``SlidingWindowLimiter`` — 线程安全的内存滑动窗口限速器。
   Web 全局按 IP 用；MCP 按 token 用。gunicorn 多 worker 下各自独立
   计数（阈值按 worker 均摊理解），重启清零——v1 约定，切 Redis 再升级。
2. 登录锁定状态机（纯函数）— 计数落 master.db ``login_throttle`` 表，
   跨 worker 生效。时间一律 UTC naive（datetime.utcnow），与进程时区无关，
   规避生产 CST / SQLite UTC 不一致的历史坑。
3. MCP token 配额判定（纯函数）。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta

# --- 阈值约定 ---------------------------------------------------------------
LOGIN_MAX_FAILURES = 5          # 15 分钟窗口内连错 5 次
LOGIN_WINDOW_MINUTES = 15
LOGIN_LOCK_MINUTES = 30         # 锁 30 分钟
GLOBAL_WEB_LIMIT_PER_MIN = 600  # 全局每 IP 每分钟（可被 env 覆盖）
MCP_DEFAULT_RATE_LIMIT = 120    # agent_tokens.rate_limit 为 NULL 时的默认配额/分钟


class SlidingWindowLimiter:
    """内存滑动窗口限速器：每个 key 一个时间戳队列。"""

    def __init__(self, max_events: int, window_seconds: float = 60.0) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, now: float | None = None) -> bool:
        """记录一次事件。返回 True=放行，False=超限（本次不计数）。"""
        if now is None:
            now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            q = self._events.setdefault(key, deque())
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.max_events:
                return False
            q.append(now)
            return True

    def prune(self, now: float | None = None) -> int:
        """清理过期队列（可选维护动作），返回清理的 key 数。"""
        if now is None:
            now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            stale = [k for k, q in self._events.items()
                     if not q or q[-1] <= cutoff]
            for k in stale:
                del self._events[k]
            return len(stale)

    def event_count(self, key: str, now: float | None = None) -> int:
        """当前窗口内某 key 的事件数（测试用）。"""
        if now is None:
            now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            q = self._events.get(key)
            if not q:
                return 0
            return sum(1 for ts in q if ts > cutoff)


# --- 登录锁定（纯函数，时间为 UTC naive 字符串 "YYYY-MM-DD HH:MM:SS"） -------

def _utcnow() -> datetime:
    return datetime.utcnow()


def login_lock_state(
    failed_count: int,
    locked_until: str | None,
    now: datetime | None = None,
) -> tuple[bool, int]:
    """判定当前是否处于锁定。返回 (locked, remaining_seconds)。"""
    if not locked_until:
        return False, 0
    now = now or _utcnow()
    until = datetime.strptime(locked_until, "%Y-%m-%d %H:%M:%S")
    remaining = int((until - now).total_seconds())
    if remaining > 0:
        return True, remaining
    return False, 0


def next_failure_state(
    failed_count: int,
    updated_at: str | None,
    now: datetime | None = None,
) -> tuple[int, str | None]:
    """一次失败后的新状态 (new_failed_count, new_locked_until)。

    - 上次失败超过 15 分钟窗口 → 计数归 1 重来。
    - 计数达 LOGIN_MAX_FAILURES → 置锁定到期时间。
    """
    now = now or _utcnow()
    in_window = False
    if updated_at:
        last = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
        in_window = (now - last) <= timedelta(minutes=LOGIN_WINDOW_MINUTES)
    new_count = failed_count + 1 if in_window else 1
    locked_until = None
    if new_count >= LOGIN_MAX_FAILURES:
        locked_until = (now + timedelta(minutes=LOGIN_LOCK_MINUTES)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    return new_count, locked_until


# --- MCP token 配额（纯函数）-------------------------------------------------

def mcp_quota_limit(rate_limit_column: int | None) -> int:
    """agent_tokens.rate_limit 列值 → 生效配额（NULL/非法值回默认）。"""
    if rate_limit_column is None:
        return MCP_DEFAULT_RATE_LIMIT
    try:
        limit = int(rate_limit_column)
    except (TypeError, ValueError):
        return MCP_DEFAULT_RATE_LIMIT
    return limit if limit > 0 else MCP_DEFAULT_RATE_LIMIT
