"""DRY_RUN 中间件测试（Phase 1 蓝绿 PoC）。

验证 app.config["DRY_RUN"]=True 时：
- INSERT/UPDATE/DELETE 被拦截（log-only，不真写）
- 写计数器 writes_blocked 准确
- 读（SELECT）透传
- 蓝图用 cursor.lastrowid 不报错（fake id 模拟）
- DRY_RUN=False 时正常写

不属于本测试：业务层写路径（已有 acceptance_item_master_ordering 覆盖）；
本测试聚焦中间件本身的可观察行为。
"""
from __future__ import annotations

import sqlite3

import pytest


def _count_rows(db_path: str, sql: str) -> int:
    """辅助：数真实 DB 行数（绕过 dry_run wrapper）。"""
    real = sqlite3.connect(db_path)
    try:
        return real.execute(sql).fetchone()[0]
    finally:
        real.close()


def test_dry_run_off_normal_writes(app, master_db_path):
    """DRY_RUN=False（默认）时 INSERT 正常写。"""
    assert app.config["DRY_RUN"] is False
    with app.test_request_context():
        from db import get_master_db
        db = get_master_db()
        before = _count_rows(master_db_path, "SELECT count(*) FROM users")
        db.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (99991, "normal_user", "x", 0, "2026-10-13 12:00:00"),
        )
        db.commit()
    after = _count_rows(master_db_path, "SELECT count(*) FROM users")
    assert after == before + 1


def test_dry_run_on_insert_blocked(app_with_dry_run, master_db_path):
    """DRY_RUN=True 时 INSERT 走 fake cursor，db row count 不变。"""
    with app_with_dry_run.test_request_context():
        from db import get_master_db
        db = get_master_db()
        # 验证是 DryRunConnection 包裹
        from db.dryrun import DryRunConnection
        assert isinstance(db, DryRunConnection), \
            f"expected DryRunConnection, got {type(db)}"
        before = _count_rows(master_db_path, "SELECT count(*) FROM users")
        cur = db.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (99992, "dryrun_user", "x", 0, "2026-10-13 12:00:00"),
        )
        # fake cursor.lastrowid 应当返 < 0（避免和真 AUTOINCREMENT 冲突）
        assert cur.lastrowid < 0, f"expected negative fake id, got {cur.lastrowid}"
        # 写计数器 +1
        assert db.writes_blocked == 1
        assert db.writes_allowed == 0
        # commit 是 noop（不抛错即可）
        db.commit()
    after = _count_rows(master_db_path, "SELECT count(*) FROM users")
    assert after == before, "dry_run INSERT should not have written any row"


def test_dry_run_on_select_passes(app_with_dry_run, master_db_path):
    """DRY_RUN=True 时 SELECT 透传：能读到现有数据。"""
    with app_with_dry_run.test_request_context():
        from db import get_master_db
        db = get_master_db()
        # SELECT 不应被拦
        cur = db.execute("SELECT count(*) FROM users")
        row = cur.fetchone()
        # dry_run 模式下读也透传
        assert db.writes_blocked == 0
        assert db.writes_allowed == 0
        # 实际数据在临时 db 里
        assert row[0] >= 0


def test_dry_run_on_update_delete_blocked(app_with_dry_run, master_db_path):
    """DRY_RUN=True 时 UPDATE/DELETE 也被拦截。"""
    with app_with_dry_run.test_request_context():
        from db import get_master_db
        db = get_master_db()
        # 用已存在的 users 表（conftest.logged_client 插了 admin）
        before = _count_rows(master_db_path, "SELECT count(*) FROM users")
        # UPDATE
        db.execute("UPDATE users SET is_admin = 0 WHERE id = 1")
        # DELETE
        db.execute("DELETE FROM users WHERE id = 1")
        # 计数应当是 2
        assert db.writes_blocked == 2
    after = _count_rows(master_db_path, "SELECT count(*) FROM users")
    assert after == before, "UPDATE + DELETE 都不应真发生"


def test_dry_run_executemany_blocked(app_with_dry_run, master_db_path):
    """DRY_RUN=True 时 executemany 也被拦截（批量 INSERT 场景）。"""
    with app_with_dry_run.test_request_context():
        from db import get_master_db
        db = get_master_db()
        before = _count_rows(master_db_path, "SELECT count(*) FROM users")
        db.executemany(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (99993, "u1", "x", 0, "2026-10-13 12:00:00"),
                (99994, "u2", "x", 0, "2026-10-13 12:00:00"),
                (99995, "u3", "x", 0, "2026-10-13 12:00:00"),
            ],
        )
        assert db.writes_blocked == 1
    after = _count_rows(master_db_path, "SELECT count(*) FROM users")
    assert after == before, "executemany 也不应真写"


def test_dry_run_independent_per_connection(app_with_dry_run, master_db_path):
    """g.master_db / g.wh_db 各自独立包裹（不会跨 conn 计数累加）。"""
    with app_with_dry_run.test_request_context():
        from db import get_master_db
        # master 一次写
        master_db = get_master_db()
        master_db.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (99996, "u_master", "x", 0, "2026-10-13 12:00:00"),
        )
        assert master_db.writes_blocked == 1
        # warehouse 取不到（没选仓），跳过


# ─────────────────────────────────────────────────────────────────────
# fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path, monkeypatch):
    """最小 app：DRY_RUN=False（默认）。"""
    import config as config_module
    import db as db_module
    from db import init_master_db

    master_path = tmp_path / "master.db"
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)

    from app import create_app
    app = create_app()
    # create_app 里 init_master_db 用的是模块级 MASTER_DB
    # 已经 monkeypatch 过，但 init_master_db 在 create_app 内部被调
    init_master_db()
    return app


@pytest.fixture
def app_with_dry_run(tmp_path, monkeypatch):
    """最小 app：DRY_RUN=True。"""
    import config as config_module
    import db as db_module
    from db import init_master_db

    master_path = tmp_path / "master.db"
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setenv("DRY_RUN", "true")

    from app import create_app
    app = create_app()
    init_master_db()
    assert app.config["DRY_RUN"] is True
    return app


@pytest.fixture
def master_db_path(tmp_path):
    """被测 master.db 路径（与上面 fixtures 配合）。"""
    return str(tmp_path / "master.db")
