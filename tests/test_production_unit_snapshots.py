"""Exercise the production writer and its Web, CSV and MCP consumers."""

import csv
import io
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

import db
from blueprints import canonical_pure
from db import warehouse_migrate as migration
from mcp_server.data import unit_of_work
from mcp_server.infra.errors import ForbiddenError, ValidationError
from mcp_server.service import outbound, production_consumption
from mcp_server.service.auth import AuthContext
from mcp_server.service.policy import PRODUCTION_CONSUMPTION_READ, BoundPolicy
from tests.test_production_grams import _seed_item, _seed_product_with_bom, _wh


def _submit(client, product, **actual):
    response = client.post("/production/submit", data={"product_id": product, "output_qty": 2, **actual})
    assert response.status_code == 302
    assert response.location.endswith("/production")


def _stock_state(conn):
    return (
        [row[0] for row in conn.execute("SELECT quantity FROM items ORDER BY id")],
        [tuple(row) for row in conn.execute("SELECT id, rolled_back FROM production_runs ORDER BY id")],
        [tuple(row) for row in conn.execute("SELECT id, rolled_back FROM outbound_requests ORDER BY id")],
        conn.execute("SELECT COUNT(*) FROM production_run_items").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0],
    )


def _revision(conn, item_id):
    return conn.execute(
        "SELECT revision FROM production_item_measurement_revisions WHERE item_id=?", (item_id,),
    ).fetchone()[0]


def _form_basis(client, product):
    html = client.get(f"/production/session?product_id={product}").get_data(as_text=True)
    return re.search(r'name="measurement_basis" value="([0-9a-f]{64})"', html)[1]


def _policy_reader(monkeypatch, master, code):
    monkeypatch.setattr(unit_of_work, "MASTER_DB", master)
    fields = ("summary", "lines")
    policy = BoundPolicy(11, (code,), ((PRODUCTION_CONSUMPTION_READ, fields),))

    def read(view, start_date="2026-11-16", end_date="2026-11-23", **args):
        return production_consumption.read_production_consumption(
            policy, fields, view=view, start_date=start_date, end_date=end_date, **args,
        )

    return read


def test_snapshots_round_trip_and_mixed_groups(logged_client, monkeypatch):
    client, path = logged_client
    item = _seed_item(path, "快照原料", "桶", 100, 200)
    product = _seed_product_with_bom(path, "快照产品", [(item, 100)])
    master = path.parent.parent / "master.db"
    with sqlite3.connect(master) as conn:
        conn.execute("UPDATE warehouses SET code='wh_002', warehouse_type='storefront'")
    read = _policy_reader(monkeypatch, master, "wh_002")
    monkeypatch.setattr("blueprints.production.now", lambda: "2026-11-16 12:00:00")
    _submit(client, product, **{f"actual_{item}": "0"})
    _submit(client, product)

    with closing(_wh(path)) as conn, conn:
        category = conn.execute("SELECT category_id FROM items WHERE id=?", (item,)).fetchone()[0]
        keys = list(conn.execute("PRAGMA index_list(production_run_items)"))
        assert next(row for row in keys if row["name"] == "idx_pruni_outbound")["unique"] == 0
    edited = client.post(f"/items/{item}/edit", data={
        "name": "快照原料", "category_id": category, "unit": "罐", "aux_unit": "克", "aux_rate": 100,
    })
    assert edited.status_code == 302
    with closing(_wh(path)) as conn, conn:
        conn.execute("UPDATE products SET unit='份' WHERE id=?", (product,))
        conn.execute("UPDATE product_bom SET qty_per_unit=150 WHERE product_id=?", (product,))
    monkeypatch.setattr("blueprints.production.now", lambda: "2026-11-17 12:00:00")
    _submit(client, product, **{f"actual_{item}": "2"})

    with closing(_wh(path)) as conn, conn:
        first, second, third = conn.execute("SELECT * FROM production_run_items ORDER BY id").fetchall()
        for line, expected in zip((first, second, third), (
            ("桶", 100, 200, 1, 0, 0), ("桶", 100, 200, 1, 1, 1), ("罐", 150, 100, 3, 2, 0),
        ), strict=True):
            keys = ("unit_snapshot", "bom_qty_per_unit_snapshot", "grams_per_unit_snapshot",
                    "planned_qty", "actual_qty", "planned_default")
            assert tuple(line[key] for key in keys) == expected
            assert line["calculation_version"] == 1 and line["conversion_mode_snapshot"] == "grams"
            count, quantity = conn.execute("SELECT COUNT(*), requested_quantity FROM outbound_requests WHERE id=?",
                                           (line["outbound_req_id"],)).fetchone()
            assert (count, quantity) == (1, line["actual_qty"])
        assert _stock_state(conn) == ([97], [(1, 0), (2, 0), (3, 0)], [(1, 0), (2, 0), (3, 0)], 3, 3)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE production_run_items SET unit_snapshot='wrong' WHERE id=1")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE production_runs SET output_unit_snapshot='wrong' WHERE id=1")
        # Mimic a row written by the prior binary. An unvalidated legacy
        # source value remains unknown and its original quantity stays raw.
        conn.execute("INSERT INTO production_runs(product_id, output_qty, created_at) VALUES (?, 2, ?)",
                     (product, "2026-11-17 13:00:00"))
        conn.execute("INSERT INTO outbound_requests(item_id, requested_quantity, reason, status, created_at, source_type) "
                     "VALUES (?, 2, '生产领料(run=#4)', '出库', '2026-11-17 13:00:00', 'production')", (item,))
        conn.execute("INSERT INTO production_run_items(run_id, item_id, planned_qty, actual_qty) VALUES (4, ?, 2, 2)",
                     (item,))
        conn.execute("INSERT INTO stock_movements(item_id, action, delta, note, created_at) "
                     "VALUES (?, '生产消耗', -2, '生产记录#4领料', '2026-11-17 13:00:00')", (item,))

    html = client.get("/production/runs?scope=day&date=2026-11-16").get_data(as_text=True)
    assert "0 桶" in html and "2 件" in html
    html = client.get("/outbound").get_data(as_text=True)
    assert "0 桶" in html and "2 罐" in html and "2 单位未留存" in html
    table = list(csv.DictReader(io.StringIO(client.get("/production/runs.csv").data.decode("utf-8-sig"))))
    assert [(row["output_unit"], row["item_unit"]) for row in table] == [
        ("单位未留存", "单位未留存"), ("份", "罐"), ("件", "桶"), ("件", "桶"),
    ]
    report = client.get("/report/outbound?scope=all").get_data(as_text=True)
    assert "可能混有不同或未留存的历史单位" in report
    assert "含单位未留存的生产领料，当前参考单位 罐" in report

    monkeypatch.setattr(outbound, "resolve_warehouse", lambda code: {"db_path": str(path)})
    auth = AuthContext(1, [], [], None, read_only=True)
    page = outbound.list_outbound("wh_002", auth, 2)
    assert page["total"] == 4 and page["truncated"]
    assert [(row["unit"], row["snapshot_source"]) for row in page["items"]] == [(None, None), ("罐", "production_write")]
    assert outbound.list_outbound("wh_002", auth, 2, 2)["items"][1]["unit"] == "桶"

    day = read("day", limit=1)
    assert day["truncated"] and day["unit_status"] == "partially_snapshotted"
    group, = day["summary"]
    assert (group["unit"], group["manual_qty"], group["system_default_qty"]) == ("桶", 0, 1)
    next_day, = read("day", limit=1, cursor=day["next_cursor"])["summary"]
    assert next_day["unit"] is None and next_day["manual_qty"] is None and next_day["unknown_unit_fraction"] == 0.5
    week, = read("week")["summary"]
    assert week["planned_count"] == 4 and week["unknown_unit_count"] == 1
    assert week["planned_qty"] is None and week["unit"] is None
    assert [(g["unit"], g["planned_qty"]) for g in week["unit_groups"]] == [("桶", 2), ("罐", 3)]
    lines = read("lines")["lines"]
    assert [(line["unit"], line["booked_qty"], line["booked_source"]) for line in lines] == [
        ("桶", 0, "manual"), ("桶", 1, "system_default"), ("罐", 2, "manual"), (None, 2, "unknown"),
    ]
    assert read("lines", "2026-11-18", "2026-11-19")["range_empty"]
    with pytest.raises(ForbiddenError, match="forbidden_warehouse"):
        read("week", warehouse_code="wh_003")


@pytest.mark.parametrize("operation", ["rollback", "delete"])
def test_write_failures_and_retry_are_atomic(logged_client, operation):
    client, path = logged_client
    a = _seed_item(path, "A", "桶", 20, 0)
    b = _seed_item(path, "B", "罐", 20, 0)
    product = _seed_product_with_bom(path, "生产", [(a, 2), (b, 3)])
    with closing(_wh(path)) as conn, conn:
        conn.execute(f"""CREATE TRIGGER injected_submit BEFORE INSERT ON stock_movements WHEN NEW.item_id={b}
                         BEGIN SELECT RAISE(ABORT, 'injected submit'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected submit"):
        _submit(client, product)
    with closing(_wh(path)) as conn, conn:
        assert _stock_state(conn) == ([20, 20], [], [], 0, 0)
        conn.execute("DROP TRIGGER injected_submit")
    _submit(client, product)
    with closing(_wh(path)) as conn, conn:
        booked = _stock_state(conn)
        assert booked == ([16, 14], [(1, 0)], [(1, 0), (2, 0)], 2, 2)
        assert conn.execute("SELECT conversion_mode_snapshot, grams_per_unit_snapshot FROM production_run_items") \
            .fetchone()[:] == ("stock_unit", None)
        conn.execute(f"""CREATE TRIGGER injected_reverse BEFORE INSERT ON stock_movements WHEN NEW.item_id={b}
                         BEGIN SELECT RAISE(ABORT, 'injected reversal'); END""")
    url = f"/production/runs/1/{operation}"
    with pytest.raises(sqlite3.IntegrityError, match="injected reversal"):
        client.post(url)
    with closing(_wh(path)) as conn, conn:
        assert _stock_state(conn) == booked
        conn.execute("DROP TRIGGER injected_reverse")
    for _ in range(2):
        assert client.post(url).status_code == 302
    with closing(_wh(path)) as conn:
        expected = ([(1, 1)], [(1, 1), (2, 1)], 2) if operation == "rollback" else ([], [], 0)
        assert _stock_state(conn) == ([20, 20], *expected, 4)


@pytest.mark.parametrize("edit", ["unit", "factor", "identity", "round_trip", "duplicate_link", "legacy"])
def test_unprovable_reversal_guards_all_entrypoints(logged_client, monkeypatch, edit):
    client, path = logged_client
    item = _seed_item(path, "测试", "桶", 20, 200)
    product = _seed_product_with_bom(path, "生产", [(item, 200)])
    _submit(client, product)
    with closing(_wh(path)) as conn, conn:
        if edit in {"unit", "round_trip"}:
            conn.execute("UPDATE items SET unit='罐'")
            if edit == "round_trip":
                conn.execute("UPDATE items SET unit='桶'")
        elif edit == "factor":
            conn.execute("UPDATE items SET gram_per_unit=100")
        elif edit == "identity":
            conn.execute("UPDATE items SET canonical_id=101")
        else:
            line = 1 if edit == "duplicate_link" else 2
            conn.execute("INSERT INTO production_runs(id, product_id, output_qty, created_at) VALUES (2, ?, 2, '2026-11-16')",
                         (product,))
            conn.execute("INSERT INTO production_run_items(run_id, item_id, planned_qty, actual_qty, outbound_req_id) "
                         "VALUES (2, ?, 2, 2, ?)", (item, 1 if line == 1 else None))
        state = _stock_state(conn)
    run = 2 if edit == "legacy" else 1
    for operation in ("rollback", "delete"):
        assert client.post(f"/production/runs/{run}/{operation}").status_code == 302
        assert client.post(f"/outbound/1/{operation}").status_code == 302
    monkeypatch.setattr(outbound, "resolve_warehouse", lambda _: {"db_path": str(path)})
    with pytest.raises(ValidationError, match="production_requires_run_reversal"):
        outbound.rollback_outbound(1, "wh_test", AuthContext(1, [], [], None))
    with closing(_wh(path)) as conn:
        assert _stock_state(conn) == state
        assert outbound._list_outbound(conn)[0]["unit"] == (None if edit == "duplicate_link" else "桶")


def test_migrate_restore_and_legacy_code_compatibility(logged_client, tmp_path):
    from migrations.warehouse import up_document_lines

    # Real v7 schema from the registered baseline, with old facts and two references to one outbound.
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(db.WAREHOUSE_SCHEMA)
        conn.execute("INSERT INTO categories(name, created_at) VALUES ('fixture', '2026-11-16 00:00:00')")
    db._migrate_warehouse_baseline(path)
    item = _seed_item(path, "原料", "桶", 20, 0)
    product = _seed_product_with_bom(path, "配方", [(item, 2)])
    with closing(_wh(path)) as conn, conn:
        up_document_lines(conn)
        conn.execute("PRAGMA user_version=7")
        conn.execute("INSERT INTO production_runs(product_id, output_qty, created_at) VALUES (?, 2, '2026-11-16 10:00:00')",
                     (product,))
        conn.execute("INSERT INTO outbound_requests(item_id, requested_quantity, reason, status, created_at, source_type) "
                     "VALUES (?, 4, '生产领料(run=#1)', '出库', '2026-11-16 10:00:00', 'production')", (item,))
        conn.executemany("INSERT INTO production_run_items(run_id, item_id, planned_qty, actual_qty, outbound_req_id) "
                         "VALUES (1, ?, ?, ?, 1)", [(item, 4, 4), (item, 4, 4)])
        original = _stock_state(conn)
        original_item = dict(conn.execute("SELECT * FROM items").fetchone())
    assert production_consumption._read_lines(path.resolve(), "wh_002", "2026-11-16", "2026-11-17", {}, 8)[0]["unit"] is None
    migration.upgrade(path)
    migration.upgrade(path)
    backup, = (tmp_path / "backups/migrations").glob("legacy.db-v7-to-8-*.db")
    restored = tmp_path / "restored.db"
    with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(restored)) as target:
        source.backup(target)
        assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert _stock_state(target) == original
    assert migration.validate(restored) == 7
    with closing(_wh(path)) as conn:
        assert _stock_state(conn) == original
        assert conn.execute("SELECT unit_snapshot, calculation_version FROM production_run_items").fetchone()[:] == (None, None)
        assert dict(conn.execute("SELECT * FROM items").fetchone()) == original_item
        assert _revision(conn, item) == 0

    client, candidate_path = logged_client
    current = _seed_item(candidate_path, "现行原料", "罐", 20, 0)
    recipe = _seed_product_with_bom(candidate_path, "现行配方", [(current, 2)])
    _submit(client, recipe)
    migration.downgrade(candidate_path, 7)
    with closing(sqlite3.connect(":memory:")) as attached:
        attached.execute("PRAGMA user_version=8")
        attached.execute("ATTACH DATABASE ? AS store", (str(candidate_path),))
        attached.execute("CREATE TABLE main.production_item_measurement_revisions(item_id, revision)")
        attached.execute("UPDATE store.items SET quantity=quantity+40")
        with pytest.raises(sqlite3.IntegrityError, match="snapshot-aware software"):
            attached.execute("UPDATE store.outbound_requests SET rolled_back=1")
        assert not attached.in_transaction
        assert attached.execute("SELECT quantity FROM store.items").fetchone() == (16,)
        attached.execute("UPDATE store.items SET unit='桶', sku='changed', gram_per_unit=100")
        attached.execute("UPDATE store.items SET unit='罐', sku='T-现行原料', gram_per_unit=0")
        assert attached.execute("SELECT COUNT(*) FROM main.production_item_measurement_revisions").fetchone() == (0,)
        attached.commit()
    with closing(_wh(candidate_path)) as conn, conn:
        assert migration.validate(candidate_path) == 7
        before = _stock_state(conn)
        conn.execute("UPDATE items SET quantity=quantity+4")
        with pytest.raises(sqlite3.IntegrityError, match="snapshot-aware software"):
            conn.execute("UPDATE outbound_requests SET rolled_back=1")
        assert _stock_state(conn) == before
        # Prior runtime's INSERT shape remains accepted, with every snapshot nullable.
        conn.execute("INSERT INTO production_runs(product_id, output_qty, created_at) VALUES (?, 2, '2026-11-17 10:00:00')",
                     (recipe,))
        conn.execute("INSERT INTO production_run_items(run_id, item_id, planned_qty, actual_qty) VALUES (2, ?, 4, 4)",
                     (current,))
    migration.upgrade(candidate_path)
    with closing(_wh(candidate_path)) as conn:
        assert [tuple(r) for r in conn.execute("SELECT unit_snapshot, calculation_version FROM production_run_items")] == [
            ("罐", 1), (None, None),
        ]
        assert _revision(conn, current) == 2
        before = _stock_state(conn)
    assert client.post("/production/runs/1/rollback").status_code == 302
    with closing(_wh(candidate_path)) as conn:
        assert _stock_state(conn) == before
    _submit(client, recipe)
    assert client.post("/production/runs/3/rollback").status_code == 302
    with closing(_wh(candidate_path)) as conn:
        assert conn.execute("SELECT quantity FROM items").fetchone()[0] == 16
        assert conn.execute("SELECT rolled_back FROM production_runs WHERE id=3").fetchone()[0] == 1

    # A withdrawn preview stamped v8 with the revision on items is outside the
    # supported migration path. The original v7 backup can still be upgraded.
    with closing(_wh(restored)) as preview, preview:
        preview.execute("ALTER TABLE items ADD COLUMN production_measurement_revision INTEGER NOT NULL DEFAULT 0")
        preview.execute("PRAGMA user_version=8")
    with pytest.raises(RuntimeError, match="unreleased preview"):
        migration.upgrade(restored)
    with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(restored)) as target:
        source.backup(target)
    migration.upgrade(restored)
    with closing(_wh(restored)) as conn:
        assert migration.validate(restored) == 8
        assert _stock_state(conn) == original and _revision(conn, item) == 0


def test_stale_production_form_cannot_relabel_displayed_quantity(logged_client):
    client, path = logged_client
    item = _seed_item(path, "测试", "桶", 20, 200)
    product = _seed_product_with_bom(path, "生产", [(item, 200)])

    before = _form_basis(client, product)
    with closing(_wh(path)) as conn, conn:
        conn.execute("UPDATE items SET unit='罐', gram_per_unit=100")
    stale = client.post("/production/submit", data={
        "product_id": product, "output_qty": 2, "measurement_basis": before, f"actual_{item}": 2,
    })
    assert stale.status_code == 302 and stale.location.startswith("/production/session?")
    with closing(_wh(path)) as conn:
        assert _stock_state(conn) == ([20], [], [], 0, 0)
    _submit(client, product, measurement_basis=_form_basis(client, product), **{f"actual_{item}": 4})
    with closing(_wh(path)) as conn:
        assert conn.execute("SELECT unit_snapshot, actual_qty FROM production_run_items").fetchone()[:] == ("罐", 4)


def test_production_preview_cannot_book_into_a_different_warehouse(logged_client):
    client, path = logged_client
    item = _seed_item(path, "原料", "桶", 20, 0)
    product = _seed_product_with_bom(path, "配方", [(item, 2)])
    basis = _form_basis(client, product)
    other = path.with_name("synthetic-other.db")
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(other)) as target:
        source.backup(target)
    with closing(sqlite3.connect(path.parent.parent / "master.db")) as master, master:
        master.execute("INSERT INTO warehouses(id, code, name, db_path, created_at) "
                       "VALUES(2, 'other', 'Fixture', ?, datetime('now'))", (str(other),))
    with client.session_transaction() as session:
        session["warehouse_id"] = 2
    stale = client.post("/production/submit", data={
        "product_id": product, "output_qty": 2, "measurement_basis": basis,
    })
    assert stale.status_code == 302 and stale.location.startswith("/production/session?")
    with closing(_wh(other)) as conn:
        assert _stock_state(conn) == ([20], [], [], 0, 0)
    _submit(client, product, measurement_basis=_form_basis(client, product))
    with closing(_wh(path)) as conn:
        assert _stock_state(conn) == ([20], [], [], 0, 0)


def test_unclaim_and_reclaim_advance_only_private_revision(logged_client, monkeypatch):
    client, path = logged_client
    item = _seed_item(path, "绑定原料", "桶", 20, 200)
    product = _seed_product_with_bom(path, "绑定配方", [(item, 200)])
    master_path = path.parent.parent / "master.db"
    monkeypatch.setattr(canonical_pure, "BACKUP_WAREHOUSE_DIR", master_path.parent / "canonical-backups")
    claim = dict(warehouse_code="wh_test", local_item_id=item, canonical_id=1, submitted_by=1)
    with closing(sqlite3.connect(master_path)) as master, closing(_wh(path)) as conn:
        master.execute("INSERT INTO canonical_items(id, canonical_sku, name, unit, created_at, updated_at) "
                       "VALUES (1, 'fixture', '绑定原料', '桶', datetime('now'), datetime('now'))")
        master.commit()
        canonical_pure.claim_item(master, conn, **claim)
        assert _revision(conn, item) == 1
        _submit(client, product)
        basis = _form_basis(client, product)
        before = dict(conn.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone())
        canonical_pure.unclaim_item(master, conn, warehouse_code="wh_test", local_item_id=item, reviewed_by=1)
        assert _revision(conn, item) == 2
        canonical_pure.claim_item(master, conn, **claim)
        after = dict(conn.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone())
        assert before.keys() == after.keys() and "production_measurement_revision" not in after
        assert {k: v for k, v in before.items() if k != "updated_at"} == {
            k: v for k, v in after.items() if k != "updated_at"
        }
        assert _revision(conn, item) == 3
        state = _stock_state(conn)
        assert client.post("/production/runs/1/rollback").status_code == 302
        stale = client.post("/production/submit", data={
            "product_id": product, "output_qty": 2, "measurement_basis": basis,
        })
        assert stale.status_code == 302 and stale.location.startswith("/production/session?")
        assert _stock_state(conn) == state


@pytest.mark.parametrize("action", ["delete_and_insert", "replace"])
def test_reusing_item_id_cannot_reuse_its_snapshot_revision(logged_client, action):
    client, path = logged_client
    item = _seed_item(path, "重建原料", "桶", 20, 0)
    product = _seed_product_with_bom(path, "重建配方", [(item, 2)])
    _submit(client, product)
    basis = _form_basis(client, product)
    with closing(_wh(path)) as conn, conn:
        data = tuple(conn.execute("SELECT * FROM items WHERE id=?", (item,)).fetchone())
        if action == "delete_and_insert":
            conn.execute("DELETE FROM items WHERE id=?", (item,))
        conn.execute(f"INSERT OR REPLACE INTO items VALUES ({','.join('?' for _ in data)})", data)
        assert _revision(conn, item) == 1
        state = _stock_state(conn)
    assert client.post("/production/runs/1/rollback").status_code == 302
    assert _form_basis(client, product) != basis
    with closing(_wh(path)) as conn:
        assert _stock_state(conn) == state


def test_zero_return_after_unit_change_preserves_balance(logged_client):
    client, path = logged_client
    item = _seed_item(path, "零用量", "桶", 20, 100)
    product = _seed_product_with_bom(path, "零用量配方", [(item, 100)])
    _submit(client, product, **{f"actual_{item}": "0"})
    with closing(_wh(path)) as conn, conn:
        conn.execute("UPDATE items SET unit='罐', gram_per_unit=50")
    assert client.post("/production/runs/1/rollback").status_code == 302
    with closing(_wh(path)) as conn:
        assert _stock_state(conn) == ([20], [(1, 1)], [(1, 1)], 1, 2)


def test_adjustment_displays_unknown_and_blocks_direct_reversals(logged_client, monkeypatch):
    client, path = logged_client
    item = _seed_item(path, "历史原料", "桶", 20, 100)
    with closing(_wh(path)) as conn, conn:
        conn.execute("""INSERT INTO outbound_requests
            (item_id, requested_quantity, reason, status, created_at, source_type)
            VALUES (?, 1, '生产补录', '出库', '2026-11-17 10:00:00', 'production')""", (item,))
        state = _stock_state(conn)
    body = client.get("/outbound").get_data(as_text=True)
    assert "生产补录" in body and "单位未留存" in body and "生产领料" not in body
    assert "/outbound/1/delete" not in body
    for operation in ("rollback", "delete"):
        assert client.post(f"/outbound/1/{operation}").status_code == 302
    monkeypatch.setattr(outbound, "resolve_warehouse", lambda _: {"db_path": str(path)})
    with pytest.raises(ValidationError, match="production_requires_run_reversal"):
        outbound.rollback_outbound(1, "wh_test", AuthContext(1, [], [], None))
    with pytest.raises(ForbiddenError):
        outbound.rollback_outbound(1, "wh_test", AuthContext(1, [], [], ["other"]))
    with closing(_wh(path)) as conn:
        assert _stock_state(conn) == state


def test_concurrent_production_reversal_and_manual_outbound(logged_client):
    client, path = logged_client
    item = _seed_item(path, "A", "桶", 20, 0)
    product = _seed_product_with_bom(path, "配方", [(item, 2)])
    _submit(client, product)
    # The identical reason string does not change an explicitly manual row.
    assert client.post("/outbound/submit", data={"reason": "生产领料(run=#1)", f"outbound_{item}": "1"}).status_code == 302
    barrier = Barrier(2)

    def rollback():
        with client.application.test_client() as actor:
            with actor.session_transaction() as session:
                session.update(user_id=1, warehouse_id=1)
            barrier.wait(timeout=5)
            return actor.post("/production/runs/1/rollback").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(rollback), pool.submit(rollback)
        assert (first.result(timeout=10), second.result(timeout=10)) == (302, 302)
    with closing(_wh(path)) as conn:
        assert _stock_state(conn) == ([19], [(1, 1)], [(1, 1), (2, 0)], 1, 3)
        first, second = outbound._list_outbound(conn)
        assert first["id"] == 2 and "unit" not in first
        assert second["id"] == 1 and second["unit"] == "桶"
    assert client.post("/outbound/2/rollback").status_code == 302
    with closing(_wh(path)) as conn:
        assert _stock_state(conn) == ([20], [(1, 1)], [(1, 1), (2, 1)], 1, 4)
