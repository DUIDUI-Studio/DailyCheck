"""Tests for canonical align CLI (issue #11, 2026-10-05).

Covers the three flask commands added to cli.py:
    align-seed    — idempotent seed of canonical_categories + canonical_items
    align-detect  — read-only dry-run report
    align-apply   — real fanout, double-confirmed, scope-fenced

Test conventions (aligned with tests/conftest.py):
  - Temp master.db + warehouse dbs in tmp_path; never touch db/warehouses/.
  - Patch db_module / config_module / cli_module MASTER_DB so every
    call-time import resolves to the temp dbs.
  - CliRunner via app.test_cli_runner() (register_cli runs in create_app).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

# ─────────────────────────────────────────────────────────────────────
#  Fixture: temp master + two in-scope storefront warehouses (wh_002 /
#  wh_003, absolute db paths) + one out-of-scope storefront warehouse
#  (wh_010) used by the scope-fence test.
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def align_env(tmp_path: Path, monkeypatch):
    import blueprints.canonical_pure as cp_module
    import cli as cli_module
    import config as config_module
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cli_module, "MASTER_DB", master_path)
    # align-apply 写前备份落 tmp_path,避免污染真实 backups/ 目录
    monkeypatch.setattr(cp_module, "BACKUP_WAREHOUSE_DIR", tmp_path / "backups")

    init_master_db()

    wh_paths: dict[str, Path] = {}
    for code in ("wh_002", "wh_003", "wh_010"):
        wh_path = wh_dir / f"{code}.db"
        init_warehouse_db(wh_path)
        wh_paths[code] = wh_path

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    for code in ("wh_002", "wh_003", "wh_010"):
        m.execute(
            "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
            "VALUES (?, ?, ?, 'storefront', ?)",
            (code, f"仓{code}", str(wh_paths[code]), ts),
        )
    m.commit()
    m.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True

    return {
        "runner": app.test_cli_runner(),
        "master_path": master_path,
        "wh_paths": wh_paths,
    }


def _count(master_path: Path, table: str) -> int:
    with sqlite3.connect(master_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ─────────────────────────────────────────────────────────────────────
#  align-seed
# ─────────────────────────────────────────────────────────────────────

def test_align_seed_is_idempotent(align_env):
    """调两次,canonical_items 行数不变。"""
    runner = align_env["runner"]
    master_path = align_env["master_path"]

    r1 = runner.invoke(args=["align-seed"])
    assert r1.exit_code == 0, r1.output
    assert "canonical_items: 8" in r1.output
    first = _count(master_path, "canonical_items")

    r2 = runner.invoke(args=["align-seed"])
    assert r2.exit_code == 0, r2.output
    assert "canonical_items: 0" in r2.output  # skip-existing → 0 inserted
    second = _count(master_path, "canonical_items")

    assert first == second == 8
    # canonical_categories 也不重复
    assert _count(master_path, "canonical_categories") > 0


# ─────────────────────────────────────────────────────────────────────
#  align-detect
# ─────────────────────────────────────────────────────────────────────

def test_align_detect_outputs_report(align_env):
    """stdout 含『同物候选』+ 各仓 items 数;且不写任何表。"""
    runner = align_env["runner"]
    wh_paths = align_env["wh_paths"]

    # 给两个在范围内的仓塞 items,制造一个可检测的 SKU 同物组
    ts = "2026-10-05 08:00:00"
    for code in ("wh_002", "wh_003"):
        with sqlite3.connect(wh_paths[code]) as conn:
            cat_id = conn.execute(
                "SELECT id FROM categories ORDER BY id LIMIT 1"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO items (sku, name, category_id, unit, updated_at, quantity) "
                "VALUES (?, ?, ?, '桶', ?, 10)",
                (f"WP0129-{code}-1234", "草莓风味酱", cat_id, ts),
            )
            conn.execute(
                "INSERT INTO items (sku, name, category_id, unit, updated_at, quantity) "
                "VALUES ('X-散装品-9999', '独有品', ?, '件', ?, 1)",
                (cat_id, ts),
            )

    r = runner.invoke(args=["align-detect"])
    assert r.exit_code == 0, r.output
    assert "dry-run" in r.output
    assert "同物候选" in r.output
    assert "wh_002: items=2" in r.output
    assert "wh_003: items=2" in r.output
    # 范围外仓库不出现(wh_010 不在 ALIGN_SCOPE_WAREHOUSES)
    assert "wh_010" not in r.output
    assert "WP0129" in r.output  # 检测到了候选成员

    # 纯只读:items 行数不变
    with sqlite3.connect(wh_paths["wh_002"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2


def test_align_detect_out_file(align_env, tmp_path):
    """--out 落文件,stdout 只回路径摘要。"""
    runner = align_env["runner"]
    out_file = tmp_path / "align-report.txt"
    r = runner.invoke(args=["align-detect", "--out", str(out_file)])
    assert r.exit_code == 0, r.output
    assert "Wrote" in r.output and str(out_file) in r.output
    content = out_file.read_text(encoding="utf-8")
    assert "同物候选" in content


# ─────────────────────────────────────────────────────────────────────
#  align-apply
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def seeded_env(align_env):
    """align-seed 已跑过的环境;并给 wh_002 / wh_003 各塞一个未纳管 items 行,
    其 categories 已按 canonical_code 映射好,供 INSERT 分支扇出。"""
    runner = align_env["runner"]
    master_path = align_env["master_path"]
    wh_paths = align_env["wh_paths"]

    r = runner.invoke(args=["align-seed"])
    assert r.exit_code == 0, r.output

    # 找到乳制品-通用 这条种子的 id + category_code
    with sqlite3.connect(master_path) as conn:
        conn.row_factory = sqlite3.Row
        canon = conn.execute(
            "SELECT id, category_code FROM canonical_items WHERE name='乳制品-通用'"
        ).fetchone()
        cat_name = conn.execute(
            "SELECT name FROM canonical_categories WHERE code=?",
            (canon["category_code"],),
        ).fetchone()["name"]

    ts = "2026-10-05 08:00:00"
    for code in ("wh_002", "wh_003"):
        with sqlite3.connect(wh_paths[code]) as conn:
            # 本仓预置分类里已有同名分类(init_warehouse_db 播 FIXED_CATEGORIES),
            # 直接补 canonical_code 让 _build_category_id_map 能解析到
            row = conn.execute(
                "SELECT id FROM categories WHERE name=?", (cat_name,)
            ).fetchone()
            assert row is not None, f"{code} 缺预置分类 {cat_name}"
            cat_id = int(row[0])
            conn.execute(
                "UPDATE categories SET canonical_code=? WHERE id=?",
                (canon["category_code"], cat_id),
            )
            conn.execute(
                "INSERT INTO items (sku, name, category_id, unit, updated_at, quantity) "
                "VALUES ('A-旧名-0001', '本地牛奶', ?, '盒', ?, 5)",
                (cat_id, ts),
            )

    align_env["canonical"] = {
        "id": int(canon["id"]),
        "category_code": canon["category_code"],
    }
    return align_env


def test_align_apply_without_confirm_aborts(seeded_env):
    """缺 --yes / input 'n' → abort,不写库。"""
    runner = seeded_env["runner"]
    master_path = seeded_env["master_path"]
    wh_paths = seeded_env["wh_paths"]
    cid = seeded_env["canonical"]["id"]

    r = runner.invoke(
        args=["align-apply", "--canonical-ids", str(cid),
              "--warehouses", "wh_002"],
        input="n\n",
    )
    assert r.exit_code != 0  # Abort
    assert "Abort" in r.output

    # wh_002.items 未被写入 canonical_id
    with sqlite3.connect(wh_paths["wh_002"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM items WHERE canonical_id IS NOT NULL"
        ).fetchone()[0] == 0
    # master 侧无 publish event
    assert _count(master_path, "canonical_publish_events") == 0


def test_align_apply_with_force_writes(seeded_env):
    """--yes + --force → fanout 真跑,wh_002 出现 canonical_id 绑定行。"""
    runner = seeded_env["runner"]
    master_path = seeded_env["master_path"]
    wh_paths = seeded_env["wh_paths"]
    cid = seeded_env["canonical"]["id"]

    r = runner.invoke(args=[
        "align-apply", "--canonical-ids", str(cid),
        "--warehouses", "wh_002", "--force", "--yes",
    ])
    assert r.exit_code == 0, r.output
    assert "align-apply done" in r.output

    # INSERT 分支:原行未绑 canonical_id → 新增一条 AUTO-IC-* 行
    with sqlite3.connect(wh_paths["wh_002"]) as conn:
        conn.row_factory = sqlite3.Row
        bound = conn.execute(
            "SELECT sku, name, canonical_id, quantity FROM items "
            "WHERE canonical_id=?",
            (cid,),
        ).fetchone()
        assert bound is not None
        assert bound["sku"].startswith("AUTO-")
        # Q7: quantity=0(INSERT 新行),原行库存未动
        assert bound["quantity"] == 0
        orig = conn.execute(
            "SELECT quantity, name FROM items WHERE sku='A-旧名-0001'"
        ).fetchone()
        assert orig["quantity"] == 5
        assert orig["name"] == "本地牛奶"

    # master 侧 publish event 落库
    assert _count(master_path, "canonical_publish_events") == 1
    assert _count(master_path, "canonical_publish_event_items") >= 1

    # 写前备份(Q7 措施①):备份文件落 tmp 备份目录,事件记录 backup_paths_json
    import blueprints.canonical_pure as cp_module
    backups = list(cp_module.BACKUP_WAREHOUSE_DIR.glob("*.db"))
    assert backups, "align-apply 写前应生成仓库备份"
    with sqlite3.connect(master_path) as conn:
        row = conn.execute(
            "SELECT backup_paths_json FROM canonical_publish_events"
        ).fetchone()
        assert row[0] is not None, "事件应记录备份路径"


def test_align_apply_respects_v4_scope(seeded_env):
    """--warehouses wh_010 → 拒绝(v4 范围外),不写库。"""
    runner = seeded_env["runner"]
    master_path = seeded_env["master_path"]
    wh_paths = seeded_env["wh_paths"]
    cid = seeded_env["canonical"]["id"]

    r = runner.invoke(args=[
        "align-apply", "--canonical-ids", str(cid),
        "--warehouses", "wh_010", "--yes",
    ])
    assert r.exit_code != 0
    assert "v4 对齐范围" in r.output

    with sqlite3.connect(wh_paths["wh_010"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM items WHERE canonical_id IS NOT NULL"
        ).fetchone()[0] == 0
    assert _count(master_path, "canonical_publish_events") == 0


def test_align_apply_rejects_non_numeric_ids(seeded_env):
    """--canonical-ids 含非数字 → 友好 UsageError,不写库。"""
    runner = seeded_env["runner"]
    master_path = seeded_env["master_path"]
    wh_paths = seeded_env["wh_paths"]

    r = runner.invoke(args=[
        "align-apply", "--canonical-ids", "1,abc",
        "--warehouses", "wh_002", "--yes",
    ])
    assert r.exit_code != 0
    assert "非数字" in r.output

    with sqlite3.connect(wh_paths["wh_002"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM items WHERE canonical_id IS NOT NULL"
        ).fetchone()[0] == 0
    assert _count(master_path, "canonical_publish_events") == 0


def test_align_detect_readonly_without_canonical_column(align_env):
    """align-detect 对缺 canonical_id 列的旧仓保持只读:不报错、不补列。"""
    runner = align_env["runner"]
    wh_paths = align_env["wh_paths"]

    # 模拟未迁移旧仓:重建 items 表,去掉 canonical_id 等列
    with sqlite3.connect(wh_paths["wh_002"]) as conn:
        conn.execute("DROP TABLE items")
        conn.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, sku TEXT, "
            "name TEXT, unit TEXT)"
        )
        conn.execute(
            "INSERT INTO items (sku, name, unit) VALUES ('OLD-1', '旧品', '件')"
        )
        conn.commit()

    r = runner.invoke(args=["align-detect"])
    assert r.exit_code == 0, r.output
    assert "wh_002" in r.output

    # 只读契约:items 表未被补 canonical_id 列
    with sqlite3.connect(wh_paths["wh_002"]) as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(items)")]
    assert "canonical_id" not in cols
