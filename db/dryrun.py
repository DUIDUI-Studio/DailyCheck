"""DRY_RUN 中间件：写操作拦截。

Phase 1 蓝绿部署需要让绿环境在「不动生产 DB」的前提下完整跑通业务逻辑。
本模块提供 DryRunConnection：包裹真实 sqlite3.Connection，在
app.config["DRY_RUN"]=True 时把 INSERT/UPDATE/DELETE/REPLACE/DDL
全部 log-only，返 fake cursor；读操作（SELECT）和事务（commit/rollback）
透传或无害化。

设计要点
---------
1. **最小侵入**：只拦截 `execute()` / `executemany()`，其它属性（row_factory、
   PRAGMA、close）通过 `__getattr__` 透传，对蓝图代码完全无感。
2. **fake lastrowid 模拟**：很多蓝图依赖 `cursor.lastrowid` 拿刚插入行的 id
   做后续 INSERT/外键关联。dry_run 模式下我们用递增计数器模拟，让蓝图
   "看似成功"继续跑，避免 FK 报错。**dry_run 计数器与真实 DB 计数器独立**，
   不保证 id 在生产里真存在。
3. **取证计数**：每次拦截的写操作累加到 `writes_blocked`，供
   `prod_dryrun_evidence.sh` 取证脚本比对"绿环境写的次数 = 0"。
   拦截日志为 **WARNING** 级、带 `[dry_run] BLOCKED` 前缀——曾因 INFO 级被
   生产日志配置吞掉，导致 2026-10-15 事故（DRY_RUN 实例接管流量 44 分钟）
   的取证只能靠实例 access log 反推被吞写操作清单。拦截属于必须留痕的
   写安全事件：不得降级为 INFO/DEBUG。

不在范围
---------
- DDL 拦截（CREATE/ALTER/DROP）：理论上 dry_run 也不该跑 DDL，但写
  blueprint 当前没有 DDL 路径，v0.1 先把 INSERT/UPDATE/DELETE/REPLACE
  拦截稳妥；DDL 留作 Phase 2 扩展。
- SELECT 性能影响：无（直接透传）。
- 跨连接的 dry_run 一致性：g.wh_db / g.master_db 各自包裹独立计数器，
  跨 request 不共享。
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("dryrun")

# 写操作识别：开头的关键字（大小写不敏感，允许多空白）
_WRITE_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)


class _FakeCursor:
    """dry_run 写操作的 cursor 替身。"""

    def __init__(self, fake_id: int = 0):
        self._fake_id = fake_id

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size: int | None = None):
        return []

    @property
    def lastrowid(self) -> int:
        return self._fake_id

    @property
    def rowcount(self) -> int:
        return 0

    def close(self) -> None:
        pass

    def __iter__(self):
        return iter([])

    def __enter__(self):
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class DryRunConnection:
    """SQLite conn 包装：写操作 log-only + fake cursor，读透传。

    写计数器（writes_blocked / writes_allowed）供 prod_dryrun_evidence.sh
    取证：绿环境跑 5 项写操作后，writes_blocked == 5 且真实 DB row count
    不变 → 证明 dry_run 隔离生效。
    """

    def __init__(self, real_conn: Any):
        self._real = real_conn
        self._writes_blocked = 0
        self._writes_allowed = 0
        # fake lastrowid 起始值；用 id=0 不行（蓝图期待 id>0 后续 INSERT 引用），
        # 用 id=-1 表示 dry_run 假 id，与真实 AUTOINCREMENT 不冲突。
        self._next_fake_id = -1

    @property
    def writes_blocked(self) -> int:
        """dry_run 拦截的写操作次数。"""
        return self._writes_blocked

    @property
    def writes_allowed(self) -> int:
        """dry_run 期间通过的写操作次数（应始终为 0）。"""
        return self._writes_allowed

    def execute(self, sql: str, parameters: Any = ()):
        if _WRITE_RE.match(sql):
            self._writes_blocked += 1
            self._next_fake_id -= 1
            # WARNING 级（非 INFO）：app 未配置 logging → 生产 root logger 默认
            # 只放行 WARNING+；INFO 级拦截日志会被静默吞掉（2026-10-15 事故
            # 取证困难根因之一）。前缀 [dry_run] BLOCKED 供日志检索。
            logger.warning(
                "[dry_run] BLOCKED write #%d (log-only, DB not written): "
                "sql=%r params=%r",
                self._writes_blocked,
                sql[:120],
                parameters,
            )
            return _FakeCursor(fake_id=self._next_fake_id)
        # 读 / 事务控制语句：透传
        return self._real.execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Any):
        if _WRITE_RE.match(sql):
            self._writes_blocked += 1
            self._next_fake_id -= 1
            logger.warning(
                "[dry_run] BLOCKED executemany #%d (log-only, DB not written): "
                "sql=%r count=%d",
                self._writes_blocked,
                sql[:120],
                len(seq_of_parameters),
            )
            return _FakeCursor(fake_id=self._next_fake_id)
        return self._real.executemany(sql, seq_of_parameters)

    def commit(self) -> None:
        # dry_run 不 commit（写没真发生，commit 无意义）
        logger.debug("[dry_run] skip commit (no writes to flush)")

    def rollback(self) -> None:
        # rollback 放行（无害；可能 _real 也没事务）
        try:
            return self._real.rollback()
        except Exception:  # noqa: BLE001 — dry_run rollback 必须永不抛
            pass

    def __getattr__(self, name: str) -> Any:
        """透传 row_factory、PRAGMA、close、isolation_level 等未拦截属性。"""
        return getattr(self._real, name)


def wrap_if_dry_run(conn: Any) -> Any:
    """根据 app.config["DRY_RUN"] 决定是否包裹 conn。

    单独抽函数方便单测（mock current_app）。
    """
    try:
        from flask import current_app
        if current_app.config.get("DRY_RUN", False):
            return DryRunConnection(conn)
    except RuntimeError:
        # 没有 application context（CLI / 脚本）→ 不包裹
        pass
    return conn
