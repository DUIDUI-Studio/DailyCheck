"""Tests for issue #14: ``bulk-publish-canonical`` CLI.

Before the fix: ops had to write ad-hoc /tmp/publish_wh000_to_canonical.py
to bind a DC's real inventory to master canonical_items. Production deploys
should not depend on ops writing ad-hoc SQL.

After the fix: ``flask --app app bulk-publish-canonical wh_XXX [--dry-run]
[--include-bound]`` does it in one shot, with pre-flight backup and a
master + wh transactional commit.

Conventions follow ``tests/test_align_canonical_cli.py`` + ``test_warehouse_type_cli.py``.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

# ─────────────────────────────────────────────────────────────────────
#  Fixture: temp master + 1 DC + categories seeded
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def bp_env(tmp_path: Path, monkeypatch):
    import blueprints.canonical_pure as cp_module
    import cli as cli_module
    import config as config_module
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_000.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path)
    # cli module-level imports of BASE_DIR/MASTER_DB need patching
    monkeypatch.setattr(cli_module, "MASTER_DB", master_path)
    monkeypatch.setattr(cli_module, "BASE_DIR", tmp_path)
    # bulk-publish uses backup_warehouse_db → BACKUP_WAREHOUSE_DIR
    monkeypatch.setattr(cp_module, "BACKUP_WAREHOUSE_DIR", tmp_path / "backups")

    init_master_db()
    init_warehouse_db(wh_path)

    # Register wh_000 as a distribution_center
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(master_path) as m:
        m.execute(
            "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
            "VALUES (?, ?, ?, 'distribution_center', ?)",
            ("wh_000", "DC仓", str(wh_path), ts),
        )
        m.commit()

    # Seed canonical_categories (issue #14 expects wh local category name
    # → canonical_categories.name → code mapping)
    with sqlite3.connect(master_path) as m:
        m.row_factory = sqlite3.Row
        # 9 个 FIXED_CATEGORIES → canonical_categories
        from blueprints.canonical_pure import CATEGORY_CODE_MAP
        from config import FIXED_CATEGORIES
        for name in FIXED_CATEGORIES:
            code = CATEGORY_CODE_MAP.get(name)
            if code is None:
                continue
            m.execute(
                "INSERT INTO canonical_categories (code, name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (code, name, ts, ts),
            )
        m.commit()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True

    return {
        "runner": app.test_cli_runner(),
        "master_path": master_path,
        "wh_path": wh_path,
        "backup_dir": tmp_path / "backups",
    }


def _seed_wh_items(wh_path: Path, items: list[tuple[str, str, str, float, int]]) -> None:
    """Seed wh_XXX.items. Tuple = (sku, name, category_name, qty, canonical_id_or_-1)."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(wh_path) as conn:
        for sku, name, cat_name, qty, cid in items:
            row = conn.execute(
                "SELECT id FROM categories WHERE name=?", (cat_name,),
            ).fetchone()
            assert row is not None, f"category {cat_name!r} missing in test env"
            cat_id = int(row[0])
            cid_val = None if cid < 0 else int(cid)
            conn.execute(
                """INSERT INTO items
                   (sku, name, category_id, unit, quantity, safety_stock,
                    unit_cost, gram_per_unit, aux_unit, aux_rate, canonical_id,
                    updated_at)
                   VALUES (?, ?, ?, '件', ?, 0, 0, 0, NULL, 0, ?, ?)""",
                (sku, name, cat_id, qty, cid_val, ts),
            )
        conn.commit()


def _count_canonical(master_path: Path) -> int:
    with sqlite3.connect(master_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM canonical_items").fetchone()[0]


def _count_bound(wh_path: Path) -> int:
    with sqlite3.connect(wh_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM items WHERE canonical_id IS NOT NULL"
        ).fetchone()[0]


# ─────────────────────────────────────────────────────────────────────
#  bulk-publish-canonical: dry-run path
# ─────────────────────────────────────────────────────────────────────

def test_bulk_publish_dry_run_does_not_write(bp_env):
    """--dry-run 只报数,不创 master canonical_items,也不回写 wh.items.canonical_id。"""
    _seed_wh_items(bp_env["wh_path"], [
        ("SKU-1", "冷冻芒果酱", "调味酱", 50, -1),
        ("SKU-2", "草莓风味酱", "调味酱", 30, -1),
    ])
    initial_canon = _count_canonical(bp_env["master_path"])
    initial_bound = _count_bound(bp_env["wh_path"])
    assert initial_canon == 0
    assert initial_bound == 0

    r = bp_env["runner"].invoke(args=[
        "bulk-publish-canonical", "wh_000", "--dry-run",
    ], input="y\n")
    assert r.exit_code == 0, r.output
    assert "[dry-run]" in r.output
    assert "'scanned': 2" in r.output
    assert "'dry_run': True" in r.output
    assert "'linked': 0" in r.output  # 不应回写

    # 关键:不写
    assert _count_canonical(bp_env["master_path"]) == 0
    assert _count_bound(bp_env["wh_path"]) == 0
    # dry-run 不备份
    assert not list(bp_env["backup_dir"].glob("*.db"))


def test_bulk_publish_dry_run_scans_even_unbound_categories(bp_env):
    """dry-run 走通不报错,即便有 items 拿不到 category_code。"""
    _seed_wh_items(bp_env["wh_path"], [
        ("SKU-X", "未知分类的品项", "调味酱", 5, -1),
    ])
    r = bp_env["runner"].invoke(args=[
        "bulk-publish-canonical", "wh_000", "--dry-run",
    ], input="y\n")
    assert r.exit_code == 0, r.output


# ─────────────────────────────────────────────────────────────────────
#  bulk-publish-canonical: real path
# ─────────────────────────────────────────────────────────────────────

def test_bulk_publish_creates_and_links(bp_env):
    """真实跑:扫 wh.items → 创 master.canonical_items → 回写 wh.items.canonical_id。"""
    _seed_wh_items(bp_env["wh_path"], [
        ("SKU-1", "冷冻芒果酱", "调味酱", 50, -1),
        ("SKU-2", "草莓风味酱", "调味酱", 30, -1),
        ("SKU-3", "华夫蛋筒", "包材", 200, -1),
    ])

    r = bp_env["runner"].invoke(args=[
        "bulk-publish-canonical", "wh_000",
    ], input="y\n")
    assert r.exit_code == 0, r.output
    assert "bulk-publish-canonical done" in r.output
    assert "scanned:           3" in r.output
    assert "created (master):  3" in r.output
    assert "linked (wh.items): 3" in r.output

    # 写后:3 个 canonical_items + 3 个 wh.items bound
    assert _count_canonical(bp_env["master_path"]) == 3
    assert _count_bound(bp_env["wh_path"]) == 3

    # SKU → canonical_sku 映射对得上
    with sqlite3.connect(bp_env["wh_path"]) as conn:
        rows = conn.execute(
            "SELECT sku, canonical_id FROM items ORDER BY sku"
        ).fetchall()
    with sqlite3.connect(bp_env["master_path"]) as conn:
        conn.row_factory = sqlite3.Row
        canon = {
            int(r["id"]): str(r["canonical_sku"])
            for r in conn.execute("SELECT id, canonical_sku FROM canonical_items").fetchall()
        }
    for sku, cid in rows:
        assert cid is not None, f"{sku} still unbound"
        assert canon[cid].startswith("IC-"), f"{sku} bound to bad sku: {canon[cid]}"


def test_bulk_publish_reuses_existing_canonical(bp_env):
    """已有 canonical_items (name, unit) 匹配时复用,不重复创建。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 预置一个 canonical_items
    with sqlite3.connect(bp_env["master_path"]) as m:
        m.execute(
            "INSERT INTO canonical_items (canonical_sku, name, unit, category_code, "
            "status, created_from, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', 'rd_manual', ?, ?)",
            ("IC-000100", "冷冻芒果酱", "件", "SAUCE", ts, ts),
        )
        m.commit()
        preseed_id = m.execute(
            "SELECT id FROM canonical_items WHERE canonical_sku='IC-000100'"
        ).fetchone()[0]
    initial_count = _count_canonical(bp_env["master_path"])

    _seed_wh_items(bp_env["wh_path"], [
        ("SKU-1", "冷冻芒果酱", "调味酱", 50, -1),  # 匹配已有
        ("SKU-2", "草莓风味酱", "调味酱", 30, -1),  # 全新
    ])

    r = bp_env["runner"].invoke(args=[
        "bulk-publish-canonical", "wh_000",
    ], input="y\n")
    assert r.exit_code == 0, r.output
    assert "created (master):  1" in r.output  # 只创 1 个
    assert "reused (master):   1" in r.output

    final = _count_canonical(bp_env["master_path"])
    assert final == initial_count + 1
    # SKU-1 应绑到预置的 id
    with sqlite3.connect(bp_env["wh_path"]) as conn:
        row = conn.execute(
            "SELECT canonical_id FROM items WHERE sku='SKU-1'"
        ).fetchone()
    assert row[0] == preseed_id


def test_bulk_publish_only_no_canonical_default(bp_env):
    """默认 --only-no-canonical: 已绑定的行不被重复处理(避免误覆盖)。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(bp_env["master_path"]) as m:
        m.execute(
            "INSERT INTO canonical_items (canonical_sku, name, unit, category_code, "
            "status, created_from, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', 'rd_manual', ?, ?)",
            ("IC-000200", "已绑品项", "件", "SAUCE", ts, ts),
        )
        m.commit()
        preseed_id = m.execute(
            "SELECT id FROM canonical_items WHERE canonical_sku='IC-000200'"
        ).fetchone()[0]
    # 预置已绑 + 未绑
    _seed_wh_items(bp_env["wh_path"], [
        ("SKU-OK", "已绑品项", "调味酱", 50, preseed_id),
        ("SKU-NEW", "未绑品项", "调味酱", 30, -1),
    ])

    r = bp_env["runner"].invoke(args=[
        "bulk-publish-canonical", "wh_000",
    ], input="y\n")
    assert r.exit_code == 0, r.output
    assert "scanned:           1" in r.output  # 只扫未绑
    assert "created (master):  1" in r.output
    assert "linked (wh.items): 1" in r.output

    # SKU-OK 仍绑 preseed_id
    with sqlite3.connect(bp_env["wh_path"]) as conn:
        row = conn.execute(
            "SELECT canonical_id FROM items WHERE sku='SKU-OK'"
        ).fetchone()
    assert row[0] == preseed_id


def test_bulk_publish_include_bound_idempotent(bp_env):
    """--include-bound 扫全部行;已绑的不会被重新 link(幂等)。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(bp_env["master_path"]) as m:
        m.execute(
            "INSERT INTO canonical_items (canonical_sku, name, unit, category_code, "
            "status, created_from, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', 'rd_manual', ?, ?)",
            ("IC-000300", "已绑", "件", "SAUCE", ts, ts),
        )
        m.commit()
        preseed_id = m.execute(
            "SELECT id FROM canonical_items WHERE canonical_sku='IC-000300'"
        ).fetchone()[0]
    _seed_wh_items(bp_env["wh_path"], [
        ("A", "已绑", "调味酱", 50, preseed_id),
        ("B", "未绑", "调味酱", 30, -1),
    ])

    r = bp_env["runner"].invoke(args=[
        "bulk-publish-canonical", "wh_000", "--include-bound",
    ], input="y\n")
    assert r.exit_code == 0, r.output
    assert "scanned:           2" in r.output
    assert "created (master):  1" in r.output
    assert "linked (wh.items): 1" in r.output  # 只 link 未绑那个
    # A 行被扫但 skipped(B 已绑的 UPDATE canonical_id IS NULL 命中 0 行)
    with sqlite3.connect(bp_env["wh_path"]) as conn:
        a = conn.execute(
            "SELECT canonical_id FROM items WHERE sku='A'"
        ).fetchone()[0]
        b = conn.execute(
            "SELECT canonical_id FROM items WHERE sku='B'"
        ).fetchone()[0]
    assert a == preseed_id
    assert b is not None


def test_bulk_publish_creates_pre_flight_backup(bp_env):
    """真实跑写 wh 前会生成备份到 BACKUP_WAREHOUSE_DIR。"""
    _seed_wh_items(bp_env["wh_path"], [
        ("SKU-1", "冷冻芒果酱", "调味酱", 50, -1),
    ])

    assert not list(bp_env["backup_dir"].glob("*.db"))
    r = bp_env["runner"].invoke(args=[
        "bulk-publish-canonical", "wh_000",
    ], input="y\n")
    assert r.exit_code == 0, r.output
    assert "pre-flight backups:" in r.output

    backups = list(bp_env["backup_dir"].glob("*.db"))
    assert len(backups) == 1
    assert "pre-bulk-publish" in backups[0].name
    # 备份是 wh_XXX 当时的完整快照(应含 seed 前的空 items + 之后 bulk 之前的 1 行)
    # 这里只验证备份是 sqlite3 db 且能打开
    with sqlite3.connect(backups[0]) as b:
        cols = [c[1] for c in b.execute("PRAGMA table_info(items)").fetchall()]
    assert "canonical_id" in cols


def test_bulk_publish_missing_warehouse(bp_env):
    """dc_code 不存在 → 非 0 exit, 友好报错。"""
    r = bp_env["runner"].invoke(args=[
        "bulk-publish-canonical", "wh_nope",
    ], input="y\n")
    assert r.exit_code != 0
    assert "No warehouse wh_nope" in r.output


def test_bulk_publish_requires_confirmation(bp_env):
    """不输 'y' → abort,不写库。"""
    _seed_wh_items(bp_env["wh_path"], [
        ("SKU-1", "冷冻芒果酱", "调味酱", 50, -1),
    ])
    initial_canon = _count_canonical(bp_env["master_path"])

    r = bp_env["runner"].invoke(args=[
        "bulk-publish-canonical", "wh_000",
    ], input="n\n")
    assert r.exit_code != 0
    assert "Abort" in r.output

    # 未写
    assert _count_canonical(bp_env["master_path"]) == initial_canon
    assert _count_bound(bp_env["wh_path"]) == 0
