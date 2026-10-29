"""路由播种/管理契约：真实临时 SQLite，不改既有订货行为。"""
import importlib
import sqlite3

import pytest

TS = "2026-10-29 12:00:00"


def routing_api():
    # 模块缺失也表现为明确断言失败，便于保留 TDD 红灯证据。
    assert importlib.util.find_spec("blueprints.item_routing_pure"), "routing pure module missing"
    return importlib.import_module("blueprints.item_routing_pure")


@pytest.fixture
def routing_db(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "MASTER_DB", tmp_path / "master.db")
    db.init_master_db()
    master = sqlite3.connect(tmp_path / "master.db")
    master.execute("PRAGMA foreign_keys=ON")
    master.execute("INSERT OR IGNORE INTO tenants VALUES (1, 'existing', 'active', ?)", (TS,))
    master.execute("INSERT INTO tenants VALUES (2, 'other', 'active', ?)", (TS,))
    master.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'x', 1, ?)", (TS,))
    whs = {}
    for code, kind, tenant in [("A", "distribution_center", 1),
                               ("B", "distribution_center", 1),
                               ("rd", "rd", 1), ("store", "storefront", 1),
                               ("foreign", "distribution_center", 2)]:
        path = tmp_path / f"{code}.db"
        db.init_warehouse_db(path)
        master.execute(
            "INSERT INTO warehouses (code, name, db_path, warehouse_type, tenant_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (code, code, str(path), kind, tenant, TS))
        whs[code] = sqlite3.connect(path)
    master.commit()
    yield master, whs
    master.close()
    for wh in whs.values():
        wh.close()


def canonical(master, cid=1, *, status="active", tenant=1):
    master.execute(
        "INSERT INTO canonical_items (id, canonical_sku, name, unit, status, tenant_id, "
        "created_at, updated_at) VALUES (?, ?, ?, '件', ?, ?, ?, ?)",
        (cid, f"IC-{cid}", f"品项{cid}", status, tenant, TS, TS))


def bind(wh, cid=1, *, active=1, alias=0, exclusive=0):
    category_id = wh.execute("SELECT id FROM categories LIMIT 1").fetchone()[0]
    wh.execute(
        "INSERT INTO items (sku, name, category_id, canonical_id, is_active, is_alias, "
        "is_store_exclusive, updated_at) VALUES (?, '品项', ?, ?, ?, ?, ?, ?)",
        (f"local-{wh.total_changes}", category_id, cid, active, alias, exclusive, TS))
    wh.commit()


@pytest.mark.parametrize("mirror", ["rd", "store", "foreign"])
def test_non_tenant_one_dc_mirrors_are_not_candidates(routing_db, mirror):
    master, whs = routing_db
    canonical(master)
    bind(whs[mirror])
    result = routing_api().seed_routing(master, whs)
    assert result == {"enabled": [], "ambiguous": [], "missing": [1], "skipped_exclusive": []}
    assert routing_api().get_routing(master, 1) is None


def test_unique_dc_enabled_existing_route_untouched_and_repeat_idempotent(routing_db):
    master, whs = routing_db
    canonical(master)
    bind(whs["A"])
    bind(whs["rd"])
    api = routing_api()
    assert api.seed_routing(master, whs)["enabled"] == [(1, "A")]
    api.set_routing(master, 1, "B", actor_id=1)
    before = master.execute("SELECT * FROM item_routing").fetchall()
    assert api.seed_routing(master, whs)["enabled"] == []
    assert master.execute("SELECT * FROM item_routing").fetchall() == before
    assert api.get_routing(master, 1) == "B"


def test_two_dc_ambiguous_and_pending_is_read_only(routing_db):
    master, whs = routing_db
    canonical(master)
    bind(whs["A"])
    bind(whs["B"])
    api = routing_api()
    assert api.seed_routing(master, whs)["ambiguous"] == [1]
    master.commit()
    assert api.routing_pending(master) == {"ambiguous": [1], "missing": []}
    assert master.execute("SELECT COUNT(*) FROM item_routing").fetchone()[0] == 0


@pytest.mark.parametrize("active,alias", [(0, 0), (1, 1)])
def test_inactive_and_alias_bindings_ignored(routing_db, active, alias):
    master, whs = routing_db
    canonical(master)
    bind(whs["A"], active=active, alias=alias)
    assert routing_api().seed_routing(master, whs)["missing"] == [1]


def test_inactive_and_foreign_canonical_not_seeded(routing_db):
    master, whs = routing_db
    canonical(master, status="inactive")
    canonical(master, 2, tenant=2)
    bind(whs["A"])
    bind(whs["A"], 2)
    assert routing_api().seed_routing(master, whs) == {
        "enabled": [], "ambiguous": [], "missing": [], "skipped_exclusive": []}


@pytest.mark.parametrize("code", ["rd", "store", "foreign", "unknown"])
def test_set_rejects_non_tenant_one_dc(routing_db, code):
    master, _ = routing_db
    canonical(master)
    with pytest.raises(ValueError):
        routing_api().set_routing(master, 1, code, actor_id=1)
    assert master.execute("SELECT COUNT(*) FROM item_routing").fetchone()[0] == 0


@pytest.mark.parametrize("cid", [2, 999])
def test_crud_rejects_foreign_or_unknown_canonical(routing_db, cid):
    master, _ = routing_db
    canonical(master, 2, tenant=2)
    api = routing_api()
    for action in (lambda: api.set_routing(master, cid, "A", actor_id=1),
                   lambda: api.delete_routing(master, cid, actor_id=1)):
        with pytest.raises(ValueError):
            action()
    assert api.get_routing(master, cid) is None


def test_list_set_delete_and_caller_transaction(routing_db):
    master, _ = routing_db
    canonical(master)
    master.commit()
    api = routing_api()
    api.set_routing(master, 1, "A", actor_id=1)
    rows = api.list_routing(master)
    assert len(rows) == 1
    assert rows[0]["canonical_id"] == 1
    assert rows[0]["fulfillment_warehouse_code"] == "A"
    assert rows[0]["updated_by"] == 1
    master.rollback()
    assert api.get_routing(master, 1) is None
    api.set_routing(master, 1, "B", actor_id=1)
    api.delete_routing(master, 1, actor_id=1)
    assert api.get_routing(master, 1) is None


def test_registered_seed_is_one_time_auditable_and_repeat_safe(routing_db, tmp_path, caplog):
    from migrations import MIGRATIONS
    from migrations_runner import migrate_up

    master, whs = routing_db
    canonical(master)
    canonical(master, 2)
    bind(whs["A"])
    master.execute("PRAGMA user_version=3")
    master.commit()
    assert any(v == 4 for v, *_ in MIGRATIONS), "seed migration missing"
    path = tmp_path / "master.db"
    assert migrate_up(path, MIGRATIONS, tmp_path / "backups") == [4]
    assert routing_api().get_routing(master, 1) == "A"
    assert "enabled" in caplog.text and "missing" in caplog.text
    # 播种后新增绑定不触发新路由，启动/runner 重跑也不补种。
    bind(whs["A"], 2)
    assert migrate_up(path, MIGRATIONS, tmp_path / "backups") == []
    assert routing_api().get_routing(master, 2) is None
    routing_api().set_routing(master, 1, "B", actor_id=1)
    master.commit()
    registered_up = next(up for v, _, up, _ in MIGRATIONS if v == 4)
    registered_up(master)
    assert routing_api().get_routing(master, 1) == "B"
    master.rollback()


def test_pending_unavailable_db_fails_without_creating_file(routing_db, tmp_path):
    master, whs = routing_db
    canonical(master)
    bind(whs["A"])
    master.execute("UPDATE warehouses SET db_path=? WHERE code='B'",
                   (str(tmp_path / "missing.db"),))
    master.commit()
    with pytest.raises((FileNotFoundError, sqlite3.OperationalError)):
        routing_api().routing_pending(master)
    assert not (tmp_path / "missing.db").exists()


def test_exclusive_dc_binding_skipped_but_store_exclusive_does_not_veto(routing_db):
    master, whs = routing_db
    canonical(master)
    canonical(master, 2)
    bind(whs["A"], exclusive=1)
    bind(whs["A"], 2)
    bind(whs["store"], 2, exclusive=1)
    api = routing_api()
    assert api.seed_routing(master, whs) == {
        "enabled": [(2, "A")], "ambiguous": [], "missing": [], "skipped_exclusive": [1]}
    assert api.routing_pending(master, wh_db_map=whs) == {"ambiguous": [], "missing": []}


def test_exclusive_dc_mirror_does_not_veto_valid_dc(routing_db):
    master, whs = routing_db
    canonical(master)
    bind(whs["A"], exclusive=1)
    bind(whs["B"])
    assert routing_api().seed_routing(master, whs)["enabled"] == [(1, "B")]


def test_migration_materializes_default_tenant_before_seeding(routing_db):
    from migrations.seed_item_routing import registered_up

    master, whs = routing_db
    master.execute("DELETE FROM tenants WHERE id=1")
    canonical(master)
    bind(whs["A"])
    registered_up(master)
    assert master.execute("SELECT name FROM tenants WHERE id=1").fetchone() == ("默认租户",)
    assert routing_api().get_routing(master, 1) == "A"


def test_fresh_install_materializes_default_tenant(tmp_path, monkeypatch):
    import db

    path = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "MASTER_DB", path)
    db.init_master_db()
    with sqlite3.connect(path) as master:
        assert master.execute("SELECT name FROM tenants WHERE id=1").fetchone() == ("默认租户",)


def test_claim_and_fanout_leave_routes_unchanged(routing_db, tmp_path, monkeypatch):
    from blueprints import canonical_pure as cp

    master, whs = routing_db
    monkeypatch.setattr(cp, "BACKUP_WAREHOUSE_DIR", tmp_path / "backups")
    canonical(master)
    canonical(master, 2)
    bind(whs["A"])
    bind(whs["B"], 2)
    master.commit()
    api = routing_api()
    api.seed_routing(master, whs)
    # 删除 cid2 路由后再认领/扇出，不得被自动恢复。
    api.delete_routing(master, 2, actor_id=1)
    master.commit()
    before = master.execute("SELECT * FROM item_routing ORDER BY canonical_id").fetchall()
    whs["A"].execute(
        "UPDATE items SET canonical_id=NULL WHERE canonical_id=1")
    whs["A"].commit()
    local_id = whs["A"].execute("SELECT id FROM items LIMIT 1").fetchone()[0]
    cp.claim_item(master, whs["A"], warehouse_code="A", local_item_id=local_id,
                  canonical_id=2, submitted_by=1)
    cp.fanout_canonical_items(master, whs, canonical_ids=[2], warehouse_codes=["A", "B"])
    after = [tuple(row) for row in master.execute(
        "SELECT * FROM item_routing ORDER BY canonical_id")]
    assert after == before
    assert api.get_routing(master, 2) is None


def test_missing_dc_connection_cannot_look_like_unique_candidate(routing_db):
    master, whs = routing_db
    canonical(master)
    bind(whs["A"])
    with pytest.raises(ValueError):
        routing_api().seed_routing(master, {"A": whs["A"]})
    assert routing_api().get_routing(master, 1) is None


def test_migration_missing_dc_rolls_back_tenant_seed_and_version(routing_db, tmp_path):
    from migrations import MIGRATIONS
    from migrations_runner import migrate_up

    master, _ = routing_db
    master.execute("DELETE FROM tenants WHERE id=1")
    master.execute("UPDATE warehouses SET db_path=? WHERE code='B'",
                   (str(tmp_path / "missing.db"),))
    master.execute("PRAGMA user_version=3")
    master.commit()
    with pytest.raises(sqlite3.OperationalError):
        migrate_up(tmp_path / "master.db", MIGRATIONS, tmp_path / "backups")
    assert master.execute("PRAGMA user_version").fetchone()[0] == 3
    assert master.execute("SELECT id FROM tenants WHERE id=1").fetchone() is None
    assert master.execute("SELECT COUNT(*) FROM item_routing").fetchone()[0] == 0


@pytest.mark.parametrize("missing", ["warehouses", "warehouse_type", "status", "tenant_columns"])
def test_minimal_legacy_schema_explicitly_skips_without_writes(routing_db, caplog, missing):
    from migrations.seed_item_routing import registered_up

    master, _ = routing_db
    if missing == "warehouses":
        master.execute("DROP TABLE warehouses")
    elif missing == "warehouse_type":
        master.execute("ALTER TABLE warehouses DROP COLUMN warehouse_type")
    elif missing == "status":
        master.execute("DROP INDEX idx_canonical_items_status")
        master.execute("ALTER TABLE canonical_items DROP COLUMN status")
    else:
        master.execute("ALTER TABLE tenants DROP COLUMN created_at")
    before = master.total_changes
    registered_up(master)
    assert "跳过" in caplog.text
    assert master.total_changes == before
