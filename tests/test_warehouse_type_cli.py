"""Tests for issue #13: ``create-warehouse --type`` and ``set-warehouse-type``.

Before the fix:
  - ``create-warehouse`` Choice was ``['storefront', 'rd']`` — no
    ``distribution_center``.
  - Pre-v3.1 wh_000「配送中心仓库」landed with ``warehouse_type='storefront'``
    (schema default) and ops had to UPDATE master.db by hand.
After the fix:
  - ``create-warehouse <code> <name> --type distribution_center`` works.
  - ``set-warehouse-type <code> <type>`` fixes a legacy warehouse in one
    step (idempotent: re-running with the same type is a no-op).

Conventions follow ``tests/test_align_canonical_cli.py``: temp master.db,
temp WAREHOUSE_DB_DIR, CliRunner via ``app.test_cli_runner()``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# ─────────────────────────────────────────────────────────────────────
#  Fixture: temp master + temp warehouse dir
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def wh_type_env(tmp_path: Path, monkeypatch):
    import cli as cli_module
    import config as config_module
    import db as db_module
    from db import init_master_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path)
    # cli.py uses module-level ``from config import BASE_DIR, MASTER_DB``,
    # so we must patch the cli module's own references too — otherwise
    # ``create-warehouse`` does ``db_path.relative_to(BASE_DIR)`` against
    # the real DailyCheck repo path and blows up on tmp_path.
    # (cli.py only references WAREHOUSE_DB_DIR via call-time import inside
    # the command body, so patching config is enough for that one.)
    monkeypatch.setattr(cli_module, "MASTER_DB", master_path)
    monkeypatch.setattr(cli_module, "BASE_DIR", tmp_path)

    init_master_db()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True

    return {
        "runner": app.test_cli_runner(),
        "master_path": master_path,
        "wh_dir": wh_dir,
    }


def _wh_type(master_path: Path, code: str) -> str | None:
    with sqlite3.connect(master_path) as conn:
        row = conn.execute(
            "SELECT warehouse_type FROM warehouses WHERE code=?", (code,),
        ).fetchone()
    return None if row is None else str(row[0])


# ─────────────────────────────────────────────────────────────────────
#  create-warehouse --type
# ─────────────────────────────────────────────────────────────────────

def test_create_warehouse_default_is_storefront(wh_type_env):
    """不传 --type → 仍是 storefront(向后兼容老调用)。"""
    runner = wh_type_env["runner"]
    master_path = wh_type_env["master_path"]

    r = runner.invoke(args=["create-warehouse", "wh_test_a", "测试门店A"])
    assert r.exit_code == 0, r.output
    assert "type=storefront" in r.output
    assert _wh_type(master_path, "wh_test_a") == "storefront"


def test_create_warehouse_with_distribution_center(wh_type_env):
    """--type distribution_center 真的能落库(issue #13 复现路径)。"""
    runner = wh_type_env["runner"]
    master_path = wh_type_env["master_path"]

    r = runner.invoke(args=[
        "create-warehouse", "wh_dc_0", "测试DC仓",
        "--type", "distribution_center",
    ])
    assert r.exit_code == 0, r.output
    assert "type=distribution_center" in r.output
    assert _wh_type(master_path, "wh_dc_0") == "distribution_center"


def test_create_warehouse_with_rd(wh_type_env):
    """--type rd 仍可用(老 Choice 没被破坏)。"""
    runner = wh_type_env["runner"]
    master_path = wh_type_env["master_path"]

    r = runner.invoke(args=[
        "create-warehouse", "wh_rd_0", "研发中心",
        "--type", "rd",
    ])
    assert r.exit_code == 0, r.output
    assert _wh_type(master_path, "wh_rd_0") == "rd"


def test_create_warehouse_rejects_unknown_type(wh_type_env):
    """--type 拼错 → click 友好 UsageError,不写库。"""
    runner = wh_type_env["runner"]
    master_path = wh_type_env["master_path"]

    r = runner.invoke(args=[
        "create-warehouse", "wh_bad", "错",
        "--type", "central_hub",
    ])
    assert r.exit_code != 0
    # click 的 Choice 校验会带 "Invalid value"
    assert "Invalid value" in r.output
    assert _wh_type(master_path, "wh_bad") is None


# ─────────────────────────────────────────────────────────────────────
#  set-warehouse-type
# ─────────────────────────────────────────────────────────────────────

def test_set_warehouse_type_fixes_legacy_dc(wh_type_env):
    """复现 issue #13 evidence 路径:legacy wh_000 type=storefront → set 成 DC。"""
    runner = wh_type_env["runner"]
    master_path = wh_type_env["master_path"]

    # 1. 先建一个 storefront 仓(模拟老 wh_000「配送中心仓库」错误落库)
    r = runner.invoke(args=["create-warehouse", "wh_000", "配送中心仓库"])
    assert r.exit_code == 0, r.output
    assert _wh_type(master_path, "wh_000") == "storefront"

    # 2. set-warehouse-type 补救
    r = runner.invoke(args=[
        "set-warehouse-type", "wh_000", "distribution_center",
    ])
    assert r.exit_code == 0, r.output
    assert "storefront" in r.output and "distribution_center" in r.output
    assert _wh_type(master_path, "wh_000") == "distribution_center"


def test_set_warehouse_type_is_idempotent(wh_type_env):
    """同 type 重跑 → 提示 'already ... no-op',不写库。"""
    runner = wh_type_env["runner"]
    master_path = wh_type_env["master_path"]

    runner.invoke(args=["create-warehouse", "wh_001", "仓"])
    runner.invoke(args=["set-warehouse-type", "wh_001", "rd"])

    r = runner.invoke(args=["set-warehouse-type", "wh_001", "rd"])
    assert r.exit_code == 0, r.output
    assert "no-op" in r.output
    assert _wh_type(master_path, "wh_001") == "rd"


def test_set_warehouse_type_missing_warehouse(wh_type_env):
    """不存在 code → 友好报错,不抛 traceback。"""
    runner = wh_type_env["runner"]
    r = runner.invoke(args=["set-warehouse-type", "wh_nope", "rd"])
    assert r.exit_code != 0
    assert "No warehouse wh_nope" in r.output


def test_set_warehouse_type_rejects_unknown_type(wh_type_env):
    """type 拼错 → click 友好 UsageError。"""
    runner = wh_type_env["runner"]
    r = runner.invoke(args=[
        "set-warehouse-type", "wh_x", "central_hub",
    ])
    assert r.exit_code != 0
    assert "Invalid value" in r.output


# ─────────────────────────────────────────────────────────────────────
#  跨命令一致性:issue #13 提到的根本痛点——
#  storefront user 看到 dcs=[] 是因为 _list_dcs 过滤 warehouse_type=
#  distribution_center。如果 set-warehouse-type 不生效,_list_dcs 就返空。
# ─────────────────────────────────────────────────────────────────────

def test_legacy_dc_appears_in_list_dcs_after_fix(wh_type_env):
    """set-warehouse-type 之后,_list_dcs 能看见 wh_000(回归 issue #13 evidence 第 2 条)。"""
    from blueprints import store_ordering as so

    runner = wh_type_env["runner"]
    master_path = wh_type_env["master_path"]

    # 复现 evidence:wh_000 默认 storefront → _list_dcs 返空
    runner.invoke(args=["create-warehouse", "wh_000", "配送中心仓库"])
    with sqlite3.connect(master_path) as conn:
        dcs_before = so._list_dcs(conn)
    assert "wh_000" not in [d["code"] for d in dcs_before], (
        f"pre-condition broken: wh_000 已经是 DC ({dcs_before})"
    )

    # Run set-warehouse-type
    runner.invoke(args=["set-warehouse-type", "wh_000", "distribution_center"])

    with sqlite3.connect(master_path) as conn:
        dcs_after = so._list_dcs(conn)
    assert "wh_000" in [d["code"] for d in dcs_after], (
        f"set-warehouse-type 没让 wh_000 出现在 _list_dcs 里: {dcs_after}"
    )
