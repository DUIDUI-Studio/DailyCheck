"""Target warehouse RBAC must not borrow the current warehouse's permissions."""
import sqlite3

import pytest
from flask import Flask, g

import db
from blueprints import store_ordering_pure as sop
from permissions import current_permissions, has_permission


@pytest.fixture
def rbac(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "MASTER_DB", tmp_path / "master.db")
    db.init_master_db()
    conn = sqlite3.connect(db.MASTER_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        INSERT INTO users (id, username, password_hash, is_admin, created_at)
        VALUES (1, 'manager', 'x', 0, 'now'), (2, 'platform', 'x', 1, 'now'),
               (3, 'staff', 'x', 0, 'now');
        INSERT INTO warehouses (id, code, name, db_path, created_at, tenant_id)
        VALUES (1, 'A', 'A', 'a.db', 'now', 1), (2, 'B', 'B', 'b.db', 'now', 1),
               (3, 'C', 'C', 'c.db', 'now', 1), (4, 'D', 'D', 'd.db', 'now', 2);
        INSERT INTO warehouse_users VALUES (1, 1, 'manager'), (1, 2, 'staff'),
            (3, 1, 'staff'), (1, 4, 'manager');
        INSERT INTO store_orders
            (id, order_no, store_warehouse_code, dc_warehouse_code, requested_by,
             status, created_at, updated_at)
        VALUES (1, 'ORDER-1', 'C', 'A', 1, 'pending', 'now', 'now');
        INSERT INTO canonical_items (id, canonical_sku, name, unit, created_at, updated_at)
        VALUES (1, 'ITEM-1', 'item', '件', 'now', 'now');
        INSERT INTO store_order_items
            (order_id, canonical_id, quantity, unit, source_warehouse_code, created_at)
        VALUES (1, 1, 1, '件', 'A', 'now'), (1, 1, 1, '件', 'B', 'now');
    """)
    conn.commit()
    monkeypatch.setattr(db, "get_master_db", lambda: conn)
    app = Flask(__name__)
    with app.test_request_context():
        g.user = conn.execute("SELECT * FROM users WHERE id=1").fetchone()
        g.warehouse = conn.execute("SELECT * FROM warehouses WHERE id=1").fetchone()
        g.warehouse_db_path = "a.db"
        g.tenant_id = 1
        yield conn
    conn.close()


def test_same_user_roles_resolve_per_warehouse(rbac):
    assert has_permission(1, 'order.approve', warehouse_code='A')
    assert not has_permission(1, 'order.approve', warehouse_code='B')
    assert has_permission(user_id=1, perm='order.approve', warehouse_code='A')


def test_explicit_user_defaults_to_current_warehouse(rbac):
    assert has_permission(1, 'order.approve')
    assert not has_permission(3, 'order.approve')


def test_target_cache_separates_user_warehouse_and_tenant(rbac):
    assert has_permission(1, 'order.approve', warehouse_code='A')
    assert not has_permission(1, 'order.approve', warehouse_code='B')
    assert not has_permission(3, 'order.approve', warehouse_code='A')
    g.tenant_id = 2
    assert not has_permission(1, 'order.approve', warehouse_code='A')
    # Even the same user/warehouse pair must not reuse a different tenant's grant.
    rbac.execute("UPDATE users SET tenant_id=2 WHERE id=1")
    rbac.execute("UPDATE warehouses SET tenant_id=2 WHERE id=1")
    rbac.execute("UPDATE warehouse_users SET role='staff' WHERE user_id=1 AND warehouse_id=1")
    assert not has_permission(1, 'order.approve', warehouse_code='A')
    rbac.execute("UPDATE users SET tenant_id=1 WHERE id=1")
    rbac.execute("UPDATE warehouses SET tenant_id=1 WHERE id=1")
    g.tenant_id = 1
    # The first target result is cached even after the role changes in this request.
    rbac.execute("UPDATE warehouse_users SET role='staff' WHERE user_id=1 AND warehouse_id=1")
    assert has_permission(1, 'order.approve', warehouse_code='A')


@pytest.mark.parametrize('warehouse', ['C', 'D', 'missing'])
def test_unbound_or_foreign_warehouse_fails_closed(rbac, warehouse):
    assert not has_permission(1, 'order.approve', warehouse_code=warehouse)


def test_lookup_error_fails_closed(rbac, monkeypatch):
    def broken_db():
        raise sqlite3.OperationalError('unavailable')

    monkeypatch.setattr(db, 'get_master_db', broken_db)
    assert not has_permission(1, 'order.approve', warehouse_code='A')


def test_tenant_roles_resolve_with_existing_builtin_rule(rbac):
    rbac.execute("""INSERT INTO roles (id, code, tenant_id, name, is_builtin, created_at)
                    VALUES (100, 'custom', 1, 'Custom', 0, 'now'),
                           (101, 'foreign', 2, 'Foreign', 0, 'now')""")
    rbac.executemany("INSERT INTO role_permissions VALUES (?, 'order.approve')", [(100,), (101,)])
    rbac.execute("UPDATE warehouse_users SET role='custom' WHERE user_id=1 AND warehouse_id=1")
    rbac.execute("UPDATE warehouse_users SET role='foreign' WHERE user_id=1 AND warehouse_id=2")
    assert has_permission(1, 'order.approve', warehouse_code='A')
    assert not has_permission(1, 'order.approve', warehouse_code='B')


def test_legacy_calls_and_current_cache_unchanged(rbac):
    expected = current_permissions()
    assert has_permission('order.approve')
    assert has_permission('order.approve', 'order.create')
    assert has_permission()
    assert not has_permission('unknown')
    assert not has_permission('order.approve', warehouse_code='B')
    assert g.perm_set is expected
    assert has_permission('order.approve')


def test_order_action_reports_only_missing_participants(rbac):
    assert sop.can_act_on_order(1, 1) == (False, ['B'])
    assert sop.can_act_on_order(1, 1, perm='order.create') == (True, [])


def test_explicit_admin_always_bypasses_even_without_participants(rbac):
    assert has_permission(2, 'unknown', warehouse_code='missing')
    assert sop.can_act_on_order(2, 1) == (True, [])
    assert sop.can_act_on_order(2, 999) == (True, [])


def test_missing_user_or_order_fails_closed(rbac):
    assert not has_permission(999, 'order.approve', warehouse_code='A')
    assert sop.can_act_on_order(999, 1) == (False, ['A', 'B'])
    assert sop.can_act_on_order(1, 999) == (False, [])


def test_current_admin_does_not_grant_explicit_user_a_bypass(rbac):
    g.user = rbac.execute("SELECT * FROM users WHERE id=2").fetchone()
    assert has_permission('unknown')
    assert not has_permission(3, 'order.approve', warehouse_code='A')


def test_anonymous_legacy_call_fails_closed(rbac):
    g.user = None
    assert not has_permission('order.approve')
    assert not has_permission('order.approve', warehouse_code='A')


def test_explicit_target_needs_no_selected_warehouse(rbac):
    g.warehouse = None
    g.warehouse_db_path = None
    assert has_permission(1, 'order.approve', warehouse_code='A')
    assert not has_permission(1, 'order.approve')
    assert not has_permission('order.approve')
