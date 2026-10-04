"""Tests for canonical_pure.py — T1 (policy switches) + T23 (Q7 inventory guards).

Spec: docs/2026-10-03-canonical-item-design.md §1.5/§1.6/§2.4/§2.5/§3.5/§7.7
      docs/2026-10-03-canonical-item-design.md §7.7.2 (7 guards)

Test conventions (aligned with tests/conftest.py):
  - Build a temp SQLite db in tmp_path; never touch db/warehouses/.
  - Re-import canonical_pure per test where config patches are needed.
  - Parametrize where it adds coverage without bloating the test file.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────
#  Per-test fixture: build a minimal items table with the 6 dirty
#  float values from the wh_002 dev copy. The same fixture is used by
#  both Q7 guard tests and the assert_inventory_unchanged test — the
#  values come from Spec §7.7.1 (the 7 real-world float-trap values)
#  trimmed to the 6 that exist verbatim in wh_002 at the time of writing.
# ─────────────────────────────────────────────────────────────────────

# Six dirty values (Spec §7.7.1 / task description: wh_002 dev copy).
DIRTY_FLOAT_FIXTURE: tuple[tuple[str, float], ...] = (
    ("冷冻芒果酱",       0.3399999999999998),
    ("草莓风味酱-散",    0.4000000000000005),
    ("榛子巧克力-散",    0.20000000000000212),
    # The task description also lists 0.3599999999999996,
    # 0.12999999999999926, 0.04000000000000048. Those three are not in
    # the current wh_002 dev copy; we insert them as in-test fixtures
    # so all 6 are present and the snapshot/compare logic is exercised
    # against the exact set requested.
    ("测试品项-a",       0.3599999999999996),
    ("测试品项-b",       0.12999999999999926),
    ("测试品项-c",       0.04000000000000048),
)

# Three clean integer rows so we can assert that comparison rejects
# only the changed rows and not the whole table.
INTEGER_FIXTURE: tuple[tuple[str, int], ...] = (
    ("冰激凌勺长", 0),
    ("试吃勺",     2),
    ("夙米",       71),
)


@pytest.fixture
def warehouse_db_with_dirty_floats(tmp_path: Path) -> Path:
    """Build a temp SQLite db mirroring the items schema and seeding
    the 6 dirty float values + 3 clean integer rows + 1 explicit id=42
    row used by the assert_ids_stable test."""
    db_path = tmp_path / "wh_test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE items (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sku           TEXT NOT NULL,
            name          TEXT NOT NULL,
            category_id   INTEGER NOT NULL DEFAULT 0,
            quantity      INTEGER NOT NULL DEFAULT 0,
            safety_stock  INTEGER NOT NULL DEFAULT 0,
            unit_cost     REAL    NOT NULL DEFAULT 0,
            unit          TEXT    NOT NULL DEFAULT '件',
            updated_at    TEXT    NOT NULL,
            gram_per_unit REAL    NOT NULL DEFAULT 0,
            aux_unit      TEXT,
            aux_rate      REAL    NOT NULL DEFAULT 0,
            selling_price REAL    NOT NULL DEFAULT 0
        );
        """
    )
    ts = "2026-10-04 08:00:00"
    next_id = 1
    for name, qty in INTEGER_FIXTURE:
        conn.execute(
            "INSERT INTO items (id, sku, name, quantity, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (next_id, f"T-{next_id}", name, qty, ts),
        )
        next_id += 1
    for name, qty in DIRTY_FLOAT_FIXTURE:
        conn.execute(
            "INSERT INTO items (id, sku, name, quantity, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (next_id, f"T-{next_id}", name, qty, ts),
        )
        next_id += 1
    # Pinned id=42 row for assert_ids_stable tests
    conn.execute(
        "INSERT INTO items (id, sku, name, quantity, updated_at) "
        "VALUES (42, 'T-PINNED', 'pinned-row', 100, ?)",
        (ts,),
    )
    conn.commit()
    conn.close()
    return db_path


# ─────────────────────────────────────────────────────────────────────
#  T1 — Q1 (deny / whitelist), Q3 (deactivate_only), Q4 (freeze)
#  Parametrized per scenario in Spec §1.6.5 判定表 / §2.5 / §3.5.
# ─────────────────────────────────────────────────────────────────────

class TestQ1NewItemPolicy:
    """Q1=deny + 门店友好/相似主数据项判定表 (Spec §1.6.5)."""

    def test_q1_whitelist_no_similar_allows_with_store_exclusive(self, monkeypatch):
        from blueprints import canonical_pure as cp
        from config import CANONICAL_POLICY

        monkeypatch.setitem(
            CANONICAL_POLICY,
            "storefront_new_item_whitelist",
            ("PACKAGING", "CONSUMABLE"),
        )

        result = cp.check_new_item_policy(
            name="一次性手套",
            canonical_id=None,
            category_code="PACKAGING",
            similar_canonicals=[],
        )
        assert result["allowed"] is True
        assert result["requires_store_exclusive"] is True
        assert result["next_action"] == "create"
        assert "白名单" in result["reason"]

    def test_q1_whitelist_with_similar_suggests_but_allows(self, monkeypatch):
        from blueprints import canonical_pure as cp
        from config import CANONICAL_POLICY

        monkeypatch.setitem(
            CANONICAL_POLICY,
            "storefront_new_item_whitelist",
            ("PACKAGING",),
        )
        similar = [{"canonical_id": 7, "name": "一次性手套-M", "score": 0.92}]

        result = cp.check_new_item_policy(
            name="一次性手套-L",
            canonical_id=None,
            category_code="PACKAGING",
            similar_canonicals=similar,
        )
        assert result["allowed"] is True
        assert result["requires_store_exclusive"] is True
        assert result["suggestions"] == similar
        assert result["next_action"] == "create"

    def test_q1_no_whitelist_with_similar_denies_and_suggests(self, monkeypatch):
        from blueprints import canonical_pure as cp
        from config import CANONICAL_POLICY

        monkeypatch.setitem(
            CANONICAL_POLICY, "storefront_new_item_whitelist", ()
        )
        similar = [{"canonical_id": 1, "name": "冰块", "score": 0.88}]

        result = cp.check_new_item_policy(
            name="冰块",
            canonical_id=None,
            category_code="ICE_CATEGORY",
            similar_canonicals=similar,
        )
        assert result["allowed"] is False
        assert result["requires_store_exclusive"] is False
        assert result["suggestions"] == similar
        assert result["next_action"] == "choose"

    def test_q1_no_whitelist_no_similar_denies_and_routes_to_request(self, monkeypatch):
        from blueprints import canonical_pure as cp
        from config import CANONICAL_POLICY

        monkeypatch.setitem(
            CANONICAL_POLICY, "storefront_new_item_whitelist", ()
        )

        result = cp.check_new_item_policy(
            name="新品项X",
            canonical_id=None,
            category_code="NON_WHITELIST",
            similar_canonicals=[],
        )
        assert result["allowed"] is False
        assert result["next_action"] == "request_new"
        assert "提交新增申请" in result["reason"]

    def test_q1_editing_existing_canonical_always_allowed(self, monkeypatch):
        from blueprints import canonical_pure as cp
        from config import CANONICAL_POLICY

        monkeypatch.setitem(
            CANONICAL_POLICY, "storefront_new_item_whitelist", ()
        )

        result = cp.check_new_item_policy(
            name="冰块",
            canonical_id=42,
            category_code=None,
            similar_canonicals=[],
        )
        assert result["allowed"] is True
        assert result["next_action"] == "edit"
        assert result["requires_store_exclusive"] is False

    @pytest.mark.parametrize(
        "category_code,in_whitelist,similar_count,expected_action,expected_allowed",
        [
            # (category_code, in whitelist?, similar count, expected next_action, expected allowed)
            ("PACKAGING",     True,  0, "create",      True),
            ("PACKAGING",     True,  1, "create",      True),   # 允许但提示
            ("ICE_CATEGORY",  False, 1, "choose",      False),
            ("ICE_CATEGORY",  False, 0, "request_new", False),
        ],
    )
    def test_q1_decision_matrix(
        self, monkeypatch, category_code, in_whitelist, similar_count,
        expected_action, expected_allowed,
    ):
        """Spec §1.6.5 判定表完整覆盖（4 个新行 + 编辑行单测）。"""
        from blueprints import canonical_pure as cp
        from config import CANONICAL_POLICY

        whitelist = ("PACKAGING", "CONSUMABLE") if in_whitelist else ()
        monkeypatch.setitem(
            CANONICAL_POLICY, "storefront_new_item_whitelist", whitelist
        )
        similar = (
            [{"canonical_id": 1, "name": "X", "score": 0.9}]
            if similar_count > 0 else []
        )
        result = cp.check_new_item_policy(
            name="t", canonical_id=None,
            category_code=category_code, similar_canonicals=similar,
        )
        assert result["allowed"] is expected_allowed
        assert result["next_action"] == expected_action


class TestQ3DeactivateOnly:
    """Q3=deactivate_only (Spec §2.5)."""

    def test_active_is_allowed(self):
        from blueprints import canonical_pure as cp

        assert cp.set_canonical_status(1, "active") == "active"

    def test_disabled_is_allowed(self):
        from blueprints import canonical_pure as cp

        assert cp.set_canonical_status(1, "disabled") == "disabled"

    def test_deleted_is_rejected(self):
        from blueprints import canonical_pure as cp

        with pytest.raises(cp.DeactivateOnlyViolation) as ei:
            cp.set_canonical_status(1, "deleted")
        assert "Q3=deactivate_only" in str(ei.value)

    @pytest.mark.parametrize(
        "status",
        ["DELETED", "remove", "drop", "trash", "", "Active"],  # 包含大小写敏感
    )
    def test_illegal_status_rejected(self, status):
        from blueprints import canonical_pure as cp

        if status == "Active":
            # 大小写敏感: Active 会被当成非法字符串
            with pytest.raises(cp.DeactivateOnlyViolation):
                cp.set_canonical_status(1, status)
            return
        if status == "":
            with pytest.raises(cp.DeactivateOnlyViolation):
                cp.set_canonical_status(1, status)
            return
        with pytest.raises(cp.DeactivateOnlyViolation):
            cp.set_canonical_status(1, status)


class TestQ4FreezePolicy:
    """Q4=freeze 判定 (Spec §3.5)."""

    def test_local_matches_canonical_is_allowed(self):
        from blueprints import canonical_pure as cp

        assert cp.resolve_conflict_policy("A", "A", "A") == "allowed"

    def test_local_unchanged_canonical_changed_is_allowed(self):
        from blueprints import canonical_pure as cp

        # 门店未变（仍=上次下发），canonical 改了 → 安全覆盖
        assert cp.resolve_conflict_policy("A", "A", "B") == "allowed"

    def test_local_changed_but_matches_canonical_is_allowed(self):
        from blueprints import canonical_pure as cp

        # 门店改了，但正好改到 canonical 的新值 → 无冲突
        assert cp.resolve_conflict_policy("B", "A", "B") == "allowed"

    def test_diverged_is_frozen(self):
        from blueprints import canonical_pure as cp

        assert cp.resolve_conflict_policy("C", "A", "B") == "frozen"

    def test_force_required_returns_force_only(self):
        from blueprints import canonical_pure as cp

        assert cp.resolve_conflict_policy("C", "A", "B", force=True) == "force_only"

    @pytest.mark.parametrize(
        "local,last,canonical,force,expected",
        [
            ("x", "x", "x", False, "allowed"),
            ("x", "x", "y", False, "allowed"),
            ("y", "x", "y", False, "allowed"),
            ("z", "x", "y", False, "frozen"),
            ("z", "x", "y", True,  "force_only"),
            (1.0, 1,   1.0, False, "allowed"),    # INTEGER/REAL 兼容
            (None, None, "X", False, "allowed"),
        ],
    )
    def test_q4_matrix(self, local, last, canonical, force, expected):
        from blueprints import canonical_pure as cp

        assert cp.resolve_conflict_policy(local, last, canonical, force) == expected


# ─────────────────────────────────────────────────────────────────────
#  T23 — Q7 库存保护守卫层测试（Spec §7.7.2 七条措施）
# ─────────────────────────────────────────────────────────────────────

class TestAssertNoNeverTouch:
    """Q7 §7.7.2 措施③ 第1层 + 双向断言。"""

    def test_clean_updates_pass(self):
        from blueprints import canonical_pure as cp

        cp.assert_no_never_touch({"name": "冰块", "unit": "kg"})
        cp.assert_no_never_touch({})    # no-op 也合法

    @pytest.mark.parametrize(
        "bad_field",
        ["quantity", "safety_stock", "initial_quantity", "id", "sku",
         "selling_price", "unit_cost", "category_id"],
    )
    def test_each_never_touch_field_is_blocked(self, bad_field):
        from blueprints import canonical_pure as cp

        with pytest.raises(cp.NeverTouchViolation):
            cp.assert_no_never_touch({bad_field: 1})


class TestBuildUpdateSql:
    """Q7 §7.7.2 措施③ 第1层 — 唯一 UPDATE SQL 出口。"""

    def test_happy_path_returns_parameterized_sql(self):
        from blueprints import canonical_pure as cp

        sql, params = cp.build_update_sql(
            conn=None,
            table="items",
            updates={"name": "冰块", "unit": "kg"},
            where_col="id",
            where_val=42,
            set_allowlist=("name", "unit", "gram_per_unit", "aux_unit", "aux_rate"),
        )
        assert sql == "UPDATE items SET name=?, unit=? WHERE id=?"
        assert params == ["冰块", "kg", 42]

    def test_missing_where_col_raises_typeerror(self):
        """R5 防线：where_col 是位置参数，漏传直接 TypeError。"""
        from blueprints import canonical_pure as cp

        with pytest.raises(TypeError):
            cp.build_update_sql(
                conn=None,
                table="items",
                updates={"name": "x"},
                # where_col 缺省 → 必填位置参数报错
                where_val=1,
                set_allowlist=("name",),
            )

    def test_illegal_where_col_is_blocked(self):
        from blueprints import canonical_pure as cp

        with pytest.raises(cp.UpdateSqlViolation):
            cp.build_update_sql(
                conn=None,
                table="items",
                updates={"name": "x"},
                where_col="description",   # 不在 ALLOWED_WHERE_COLUMNS
                where_val=1,
                set_allowlist=("name",),
            )

    def test_set_allowlist_overlapping_never_touch_is_blocked(self):
        from blueprints import canonical_pure as cp

        with pytest.raises(cp.UpdateSqlViolation) as ei:
            cp.build_update_sql(
                conn=None,
                table="items",
                updates={"name": "x"},
                where_col="id",
                where_val=1,
                # 故意把 quantity 放进 set_allowlist —— 必须被拦截
                set_allowlist=("name", "quantity"),
            )
        assert "NEVER_TOUCH_COLUMNS" in str(ei.value)

    def test_updates_outside_set_allowlist_is_blocked(self):
        from blueprints import canonical_pure as cp

        with pytest.raises(cp.UpdateSqlViolation):
            cp.build_update_sql(
                conn=None,
                table="items",
                updates={"name": "x", "fake_field": "y"},
                where_col="id",
                where_val=1,
                set_allowlist=("name",),
            )

    def test_illegal_table_is_blocked(self):
        from blueprints import canonical_pure as cp

        with pytest.raises(cp.UpdateSqlViolation):
            cp.build_update_sql(
                conn=None,
                table="stock_movements",   # 不在 ALLOWED_TABLES
                updates={"quantity_delta": 1},
                where_col="id",
                where_val=1,
                set_allowlist=("quantity_delta",),
            )

    def test_empty_updates_raises(self):
        from blueprints import canonical_pure as cp

        with pytest.raises(cp.UpdateSqlViolation):
            cp.build_update_sql(
                conn=None,
                table="items",
                updates={},
                where_col="id",
                where_val=1,
                set_allowlist=("name",),
            )


class TestSelectItemColumns:
    """Q7 §7.7.2 措施④ — 跨仓动态 SELECT 列名。"""

    def test_returns_columns_for_items_table(
        self, warehouse_db_with_dirty_floats,
    ):
        from blueprints import canonical_pure as cp

        conn = sqlite3.connect(warehouse_db_with_dirty_floats)
        cols = cp.select_item_columns(conn)
        conn.close()
        # Spec §7.7.1: initial_quantity NOT present in this fixture
        # (mirrors wh_002/wh_003/rd_001)
        assert "id" in cols
        assert "name" in cols
        assert "quantity" in cols
        assert "initial_quantity" not in cols

    def test_works_with_no_initial_quantity_column(
        self, warehouse_db_with_dirty_floats,
    ):
        """R4 防线：fixture 已证明 initial_quantity 不存在；该函数仍正常返回。"""
        from blueprints import canonical_pure as cp

        conn = sqlite3.connect(warehouse_db_with_dirty_floats)
        conn.row_factory = sqlite3.Row
        try:
            info = conn.execute("PRAGMA table_info(items)").fetchall()
            assert all(r[1] != "initial_quantity" for r in info)
            # select_item_columns 必须能在没有 initial_quantity 的库里正常返回
            cols = cp.select_item_columns(conn)
            assert "quantity" in cols
        finally:
            conn.close()


class TestInventoryGuardsWithDirtyFloats:
    """Q7 §7.7.2 措施③ 第3层 + R3b 防 round() 掩盖。"""

    def test_snapshot_inventory_captures_all_6_dirty_values_exact_repr(
        self, warehouse_db_with_dirty_floats,
    ):
        from blueprints import canonical_pure as cp

        conn = sqlite3.connect(warehouse_db_with_dirty_floats)
        conn.row_factory = sqlite3.Row
        try:
            snap = cp.snapshot_inventory(conn)
            # 6 个脏值必须以原始 repr 出现在快照中 —— 任何 round() 都会把它们
            # 「正常化」到 0.34 / 0.40 / 0.20 等，等价于漂移但 assert 看不见。
            assert snap[4] == 0.3399999999999998   # 冷冻芒果酱
            assert snap[5] == 0.4000000000000005   # 草莓风味酱-散
            assert snap[6] == 0.20000000000000212  # 榛子巧克力-散
            assert snap[7] == 0.3599999999999996   # 测试品项-a
            assert snap[8] == 0.12999999999999926  # 测试品项-b
            assert snap[9] == 0.04000000000000048  # 测试品项-c
        finally:
            conn.close()

    def test_assert_inventory_unchanged_passes_for_identical_snapshot(
        self, warehouse_db_with_dirty_floats,
    ):
        from blueprints import canonical_pure as cp

        conn = sqlite3.connect(warehouse_db_with_dirty_floats)
        conn.row_factory = sqlite3.Row
        try:
            before = cp.snapshot_inventory(conn)
            after = cp.snapshot_inventory(conn)
            cp.assert_inventory_unchanged(before, after)   # 不抛即过
        finally:
            conn.close()

    def test_assert_inventory_unchanged_catches_single_digit_drift(
        self, warehouse_db_with_dirty_floats,
    ):
        """模拟「有人误把 quantity+1」—— 必须被抓到（具体到行号 + repr 前后值）。"""
        from blueprints import canonical_pure as cp

        conn = sqlite3.connect(warehouse_db_with_dirty_floats)
        conn.row_factory = sqlite3.Row
        try:
            before = cp.snapshot_inventory(conn)
            # id=5 = 草莓风味酱-散, 原 quantity = 0.4000000000000005 (脏值)
            conn.execute("UPDATE items SET quantity = quantity + 1 WHERE id = 5")
            conn.commit()
            after = cp.snapshot_inventory(conn)
            with pytest.raises(cp.InventoryMutated) as ei:
                cp.assert_inventory_unchanged(before, after)
            msg = str(ei.value)
            assert "id=5" in msg
            assert "0.4000000000000005" in msg or "1.4000000000000005" in msg
        finally:
            conn.close()

    def test_assert_inventory_unchanged_catches_disappeared_row(
        self, warehouse_db_with_dirty_floats,
    ):
        from blueprints import canonical_pure as cp

        conn = sqlite3.connect(warehouse_db_with_dirty_floats)
        conn.row_factory = sqlite3.Row
        try:
            before = cp.snapshot_inventory(conn)
            conn.execute("DELETE FROM items WHERE id = 5")
            conn.commit()
            after = cp.snapshot_inventory(conn)
            with pytest.raises(cp.InventoryMutated) as ei:
                cp.assert_inventory_unchanged(before, after)
            assert "消失" in str(ei.value)
            assert "id=5" in str(ei.value)
        finally:
            conn.close()

    def test_r3b_round_then_compare_would_hide_drift(
        self, warehouse_db_with_dirty_floats,
    ):
        """反向证据 —— 证明「先 round() 再比较」**会**把漂移放过去。

        这是 R3b 的活教材：本测试用 round(x, 2) 做对比，结果两边都 = 0.34
        从而「以为没事」。assert_inventory_unchanged 绝不允许用这种比较方式。
        """
        conn = sqlite3.connect(warehouse_db_with_dirty_floats)
        conn.row_factory = sqlite3.Row
        try:
            before = conn.execute(
                "SELECT id, quantity FROM items WHERE id = 4"
            ).fetchone()
            # 故意把 0.3399999999999998 改成 0.34（肉眼看不出来）
            conn.execute("UPDATE items SET quantity = 0.34 WHERE id = 4")
            conn.commit()
            after = conn.execute(
                "SELECT id, quantity FROM items WHERE id = 4"
            ).fetchone()

            # 错误做法：round() 后两边都是 0.34，对比通过 → 漂移被掩盖
            bad_before = round(before["quantity"], 2)
            bad_after = round(after["quantity"], 2)
            assert bad_before == bad_after   # 漂移被吞掉

            # 正确做法：原始值逐行 ==，0.3399999999999998 != 0.34
            assert before["quantity"] != after["quantity"]
        finally:
            conn.close()

    def test_assert_ids_stable_detects_missing_id(
        self, warehouse_db_with_dirty_floats,
    ):
        from blueprints import canonical_pure as cp

        conn = sqlite3.connect(warehouse_db_with_dirty_floats)
        conn.row_factory = sqlite3.Row
        try:
            before = {r["id"] for r in conn.execute("SELECT id FROM items")}
            conn.execute("DELETE FROM items WHERE id = 42")
            conn.commit()
            after = {r["id"] for r in conn.execute("SELECT id FROM items")}
            with pytest.raises(cp.IdSetChanged) as ei:
                cp.assert_ids_stable(before, after)
            assert "42" in str(ei.value)
        finally:
            conn.close()

    def test_assert_ids_stable_passes_when_no_change(
        self, warehouse_db_with_dirty_floats,
    ):
        from blueprints import canonical_pure as cp

        conn = sqlite3.connect(warehouse_db_with_dirty_floats)
        conn.row_factory = sqlite3.Row
        try:
            ids = {r["id"] for r in conn.execute("SELECT id FROM items")}
            cp.assert_ids_stable(ids, set(ids))
        finally:
            conn.close()

    def test_assert_row_count_conserved_detects_change(
        self, warehouse_db_with_dirty_floats,
    ):
        from blueprints import canonical_pure as cp

        conn = sqlite3.connect(warehouse_db_with_dirty_floats)
        conn.row_factory = sqlite3.Row
        try:
            before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            conn.execute("DELETE FROM items WHERE id = 1")
            conn.commit()
            with pytest.raises(cp.RowCountChanged) as ei:
                cp.assert_row_count_conserved(conn, "items", before)
            assert "items" in str(ei.value)
            assert str(before) in str(ei.value)
        finally:
            conn.close()


class TestBackupAndGuardRealDb:
    """Q7 §7.7.2 措施① + 措施⑦ — backup 与真实库保护。"""

    def test_backup_creates_file_with_correct_naming(
        self, warehouse_db_with_dirty_floats, monkeypatch, tmp_path,
    ):
        from blueprints import canonical_pure as cp
        from config import BACKUP_WAREHOUSE_DIR

        monkeypatch.setattr(cp, "BACKUP_WAREHOUSE_DIR", tmp_path / "backups")

        dest = cp.backup_warehouse_db(warehouse_db_with_dirty_floats, tag="pre-fanout")
        assert dest.exists()
        assert dest.suffix == ".db"
        # 命名规则: {stem}-{YYYYMMDD-HHMMSS}-{tag}.db
        assert "pre-fanout" in dest.name
        assert dest.name.startswith("wh_test-")
        # copy2 保留 mtime
        assert dest.stat().st_mtime_ns == Path(warehouse_db_with_dirty_floats).stat().st_mtime_ns

    def test_backup_propagates_failure_does_not_swallow(
        self, warehouse_db_with_dirty_floats, monkeypatch, tmp_path,
    ):
        """§7.7.2 措施① 第1条硬要求：失败即中止。"""
        from blueprints import canonical_pure as cp

        # 把 BACKUP_WAREHOUSE_DIR 改成只读目录 → copy2 抛 PermissionError
        ro_dir = tmp_path / "ro"
        ro_dir.mkdir()
        ro_dir.chmod(0o555)
        monkeypatch.setattr(cp, "BACKUP_WAREHOUSE_DIR", ro_dir)

        with pytest.raises(cp.BackupFailed):
            cp.backup_warehouse_db(warehouse_db_with_dirty_floats, tag="x")

        ro_dir.chmod(0o755)  # 还原,便于 tmp_path 清理

    def test_guard_real_db_blocks_path_under_db_warehouses(self, tmp_path, monkeypatch):
        from blueprints import canonical_pure as cp
        from config import BASE_DIR, CANONICAL_POLICY

        # 默认 allow_real_db_write=False → 应拒绝真实路径
        monkeypatch.setattr(cp, "BASE_DIR", BASE_DIR)
        monkeypatch.setitem(CANONICAL_POLICY, "allow_real_db_write", False)
        # 选 wh_002 真实库路径作为目标
        real_path = BASE_DIR / "db" / "warehouses" / "wh_002.db"
        if not real_path.exists():
            real_path.parent.mkdir(parents=True, exist_ok=True)
            real_path.touch()
        try:
            with pytest.raises(cp.RealDatabaseForbidden) as ei:
                cp.guard_real_db(real_path)
            assert "真实仓库" in str(ei.value) or "dc_dryrun" in str(ei.value)
        finally:
            if real_path.exists() and real_path.stat().st_size == 0:
                real_path.unlink()

    def test_guard_real_db_allows_path_under_dryrun(self, tmp_path):
        from blueprints import canonical_pure as cp

        copy_path = tmp_path / "wh_002-copy.db"
        copy_path.touch()
        cp.guard_real_db(copy_path)  # 不抛即过

    def test_guard_real_db_can_be_temporarily_allowed(self, monkeypatch):
        from blueprints import canonical_pure as cp
        from config import BASE_DIR, CANONICAL_POLICY

        monkeypatch.setitem(CANONICAL_POLICY, "allow_real_db_write", True)
        real_path = BASE_DIR / "db" / "warehouses" / "wh_002.db"
        # 当 allow_real_db_write=True → 不抛
        cp.guard_real_db(real_path)


# ─────────────────────────────────────────────────────────────────────
#  T1 附加：is_syncable_field + CANONICAL_FIELD_POLICY 单元（Spec §2.4）
# ─────────────────────────────────────────────────────────────────────

class TestIsSyncableField:
    """§2.4 「字段是否进 canonical_synced_json」的唯一真相源。"""

    @pytest.mark.parametrize(
        "field,expected",
        [
            ("name",          True),    # overwritable
            ("unit",          True),
            ("gram_per_unit", True),
            ("aux_unit",      True),
            ("aux_rate",      True),
            ("status",        True),    # overwritable (writes canonical_status)
            ("category_code", False),   # mapping_only
            ("barcode",       False),   # not_synced (P0)
            ("selling_price", False),   # Q6 默认不在 → 不下发
            ("unit_cost",     False),   # Q6 默认不在 → 不下发
            ("quantity",      False),   # 门禁字段 → 必须 False
            ("safety_stock",  False),
            ("initial_quantity", False),
            ("id",            False),
            ("sku",           False),
            ("category_id",   False),
            ("nonexistent",   False),
        ],
    )
    def test_is_syncable_field_truth_table(self, field, expected):
        from blueprints import canonical_pure as cp

        assert cp.is_syncable_field(field) is expected