"""平台管理员配置页：权限、租户、CSRF 与完整 CRUD。"""
import sqlite3

import pytest

TS = "2026-10-29 12:00:00"
URL = "/admin/item-routing"


@pytest.fixture
def routing_client(logged_client):
    import db

    client, _ = logged_client
    with sqlite3.connect(db.MASTER_DB) as master:
        master.execute("INSERT OR IGNORE INTO tenants VALUES (1, 'existing', 'active', ?)", (TS,))
        master.execute(
            "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
            "SELECT 'A', '配送中心A', db_path, 'distribution_center', ? FROM warehouses LIMIT 1",
            (TS,))
        master.execute(
            "INSERT INTO canonical_items (id, canonical_sku, name, unit, created_at, updated_at) "
            "VALUES (1, 'IC-1', '路由测试品', '件', ?, ?)", (TS, TS))
    return client


def test_admin_list_pending_set_delete(routing_client):
    import db

    client = routing_client
    resp = client.get(URL)
    assert resp.status_code == 200
    assert "路由测试品".encode("unicode_escape").decode() in resp.text
    assert "待配置" in resp.text
    assert 'name="csrf_token"' in resp.text
    resp = client.post(URL + "/1", data={"dc_code": "A"})
    assert resp.status_code == 302
    with sqlite3.connect(db.MASTER_DB) as master:
        row = master.execute(
            "SELECT fulfillment_warehouse_code, updated_by FROM item_routing").fetchone()
        assert row == ("A", 1)
    assert "配送中心A".encode("unicode_escape").decode() in client.get(URL).text
    assert client.get(URL + "/1/delete").status_code == 405
    assert client.post(URL + "/1/delete").status_code == 302
    with sqlite3.connect(db.MASTER_DB) as master:
        assert master.execute("SELECT COUNT(*) FROM item_routing").fetchone()[0] == 0


@pytest.mark.parametrize("suffix,data", [("", {}), ("/1", {"dc_code": "A"}), ("/1/delete", {})])
def test_non_platform_admin_forbidden(nav_client_factory, suffix, data):
    client, _ = nav_client_factory("distribution_center", "admin")
    resp = client.get(URL) if not suffix else client.post(URL + suffix, data=data)
    assert resp.status_code == 403


def test_anonymous_redirects_to_login(routing_client):
    with routing_client.session_transaction() as session:
        session.clear()
    assert "/login" in routing_client.get(URL).location


def test_invalid_target_does_not_write(routing_client):
    import db

    for code in ("wh_test", "unknown"):
        assert routing_client.post(URL + "/1", data={"dc_code": code}).status_code == 400
    with sqlite3.connect(db.MASTER_DB) as master:
        assert master.execute("SELECT COUNT(*) FROM item_routing").fetchone()[0] == 0


def test_other_tenant_platform_admin_fails_closed(routing_client):
    import db

    with sqlite3.connect(db.MASTER_DB) as master:
        master.execute("UPDATE users SET tenant_id=2 WHERE id=1")
    assert routing_client.get(URL).status_code == 403
    assert routing_client.post(URL + "/1", data={"dc_code": "A"}).status_code == 403
    assert routing_client.post(URL + "/1/delete").status_code == 403


def test_post_requires_csrf(routing_client):
    routing_client.application.config["WTF_CSRF_FORCE_ENABLED"] = True
    assert routing_client.post(URL + "/1", data={"dc_code": "A"}).status_code == 400


def test_pending_database_failure_is_visible(routing_client, tmp_path):
    import db

    with sqlite3.connect(db.MASTER_DB) as master:
        master.execute("UPDATE warehouses SET db_path=? WHERE code='A'",
                       (str(tmp_path / "missing.db"),))
    resp = routing_client.get(URL)
    assert resp.status_code == 503
    assert "无法读取" in resp.text


def test_platform_admin_can_discover_page_from_canonical(routing_client):
    resp = routing_client.get("/canonical/list")
    assert resp.status_code == 200
    assert 'href="/admin/item-routing"' in resp.text


def change(cid=1, dc='A', *, old_dc=None, old_time=None):
    return {"canonical_id": cid, "operation": "delete" if dc is None else "set", "dc_code": dc,
            "baseline": {"dc_code": old_dc, "updated_at": old_time}}


def post_batch(client, changes):
    return client.post(URL + '/batch', json={"changes": changes})


def saved_rows():
    import db

    with sqlite3.connect(db.MASTER_DB) as master:
        return master.execute(
            'SELECT canonical_id, fulfillment_warehouse_code, updated_at, updated_by '
            'FROM item_routing ORDER BY canonical_id').fetchall()


@pytest.fixture
def batch_client(routing_client):
    import db

    with sqlite3.connect(db.MASTER_DB) as master:
        master.execute(
            "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
            "SELECT 'B', '配送中心B', db_path, 'distribution_center', ? FROM warehouses LIMIT 1", (TS,))
        master.execute("INSERT INTO tenants VALUES (2, 'other', 'active', ?)", (TS,))
        master.execute(
            "INSERT INTO warehouses (code, name, db_path, warehouse_type, tenant_id, created_at) "
            "SELECT 'foreign', '外租户仓', db_path, 'distribution_center', 2, ? FROM warehouses LIMIT 1", (TS,))
        for cid, tenant, status in [(2, 1, 'inactive'), (3, 2, 'active')]:
            master.execute(
                "INSERT INTO canonical_items (id, canonical_sku, name, unit, tenant_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, '件', ?, ?, ?, ?)",
                (cid, f'IC-{cid}', f'品项{cid}', tenant, status, TS, TS))
    return routing_client


def test_batch_sets_deletes_retries_without_touching_unchanged(batch_client, monkeypatch):
    from blueprints import item_routing_pure as pure

    monkeypatch.setattr(pure, '_now', lambda: TS)
    response = post_batch(batch_client, [change(), change(2, 'B')])
    assert response.status_code == 200
    assert response.json['updated_count'] == 2 and response.json['audit_recorded']
    assert response.headers['Cache-Control'] == 'no-store'
    assert saved_rows() == [(1, 'A', TS, 1), (2, 'B', TS, 1)]
    monkeypatch.setattr(pure, '_now', lambda: '2026-11-01 00:00:00')
    replay = post_batch(batch_client, [change(), change(2, 'B')])
    assert replay.status_code == 200 and replay.json['updated_count'] == 0
    assert saved_rows() == [(1, 'A', TS, 1), (2, 'B', TS, 1)]
    deletion = change(1, None, old_dc='A', old_time=TS)
    assert post_batch(batch_client, [deletion]).json['updated_count'] == 1
    assert post_batch(batch_client, [deletion]).json['updated_count'] == 0
    assert saved_rows() == [(2, 'B', TS, 1)]


@pytest.mark.parametrize('payload', [None, [], {}, {"changes": []}, {"changes": 'bad'},
                                    {"changes": [change()] * 201}, {"changes": [change(), change()]},
                                    {"changes": [None]}, {"changes": [1]},
                                    {"changes": [{"canonical_id": 1}]},
                                    {"changes": [change()], "typo": True}])
def test_batch_bad_envelope_has_no_writes(batch_client, payload):
    response = batch_client.post(URL + '/batch', json=payload)
    assert response.status_code == 400 and response.json['ok'] is False
    assert saved_rows() == []


@pytest.mark.parametrize('field,value', [('canonical_id', True), ('canonical_id', '1'),
                                       ('canonical_id', None), ('canonical_id', 0),
                                       ('canonical_id', 2**80), ('operation', 'patch'),
                                       ('operation', []), ('dc_code', True), ('dc_code', ''),
                                       ('dc_code', ['A']), ('baseline', None),
                                       ('baseline', {}),
                                       ('baseline', {'dc_code': None, 'updated_at': TS}),
                                       ('baseline', {'dc_code': 'A', 'updated_at': 1}),
                                       ('baseline', {'dc_code': 1, 'updated_at': TS})])
def test_batch_bad_types_have_no_writes(batch_client, field, value):
    invalid = change()
    invalid[field] = value
    response = post_batch(batch_client, [invalid])
    assert response.status_code == 400 and saved_rows() == []


def test_batch_delete_requires_explicit_null(batch_client):
    invalid = change()
    invalid['operation'] = 'delete'
    assert post_batch(batch_client, [invalid]).status_code == 400
    assert saved_rows() == []


@pytest.mark.parametrize('invalid', [change(3), change(999), change(2, 'foreign'),
                                   change(2, 'wh_test'), change(2, 'unknown')])
def test_batch_validates_whole_batch_before_writing(batch_client, invalid):
    response = post_batch(batch_client, [change(), invalid])
    assert response.status_code == 400
    assert response.json['errors'][str(invalid['canonical_id'])]['message']
    assert saved_rows() == []


def test_batch_conflict_rolls_back_all_and_reports_current_value(batch_client, monkeypatch):
    from blueprints import item_routing_pure as pure

    monkeypatch.setattr(pure, '_now', lambda: TS)
    assert post_batch(batch_client, [change()]).status_code == 200
    response = post_batch(batch_client, [change(2), change(1, 'B')])
    assert response.status_code == 409
    assert response.json['errors']['1']['current'] == {'dc_code': 'A', 'updated_at': TS}
    assert saved_rows() == [(1, 'A', TS, 1)]
    # Same warehouse but a changed timestamp is stale if the desired warehouse differs.
    response = post_batch(batch_client, [change(1, 'B', old_dc='A', old_time='2000-01-01 00:00:00')])
    assert response.status_code == 409 and saved_rows() == [(1, 'A', TS, 1)]


def test_batch_mid_write_database_failure_rolls_back_without_success_audit(batch_client, monkeypatch):
    import db
    from blueprints import item_routing

    with sqlite3.connect(db.MASTER_DB) as master:
        master.execute("CREATE TRIGGER fail_second BEFORE INSERT ON item_routing "
                       "WHEN NEW.canonical_id=2 BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    audits = []
    monkeypatch.setattr(item_routing, 'audit', lambda *args: audits.append(args))
    response = post_batch(batch_client, [change(), change(2)])
    assert response.status_code == 503 and not response.json['ok']
    assert saved_rows() == [] and audits == []


def test_batch_commit_failure_rolls_back(batch_client, monkeypatch):
    from blueprints import item_routing

    get_master = item_routing.get_master_db

    class CommitFailure:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            raise sqlite3.OperationalError('injected commit failure')

    monkeypatch.setattr(item_routing, 'get_master_db', lambda: CommitFailure(get_master()))
    assert post_batch(batch_client, [change(), change(2)]).status_code == 503
    assert saved_rows() == []


def test_batch_audit_runs_after_visible_master_commit_and_failure_stays_success(batch_client, monkeypatch, caplog):
    from blueprints import item_routing

    def failing_audit(*args):
        assert [(row[0], row[1]) for row in saved_rows()] == [(1, 'A'), (2, 'B')]
        raise sqlite3.OperationalError('warehouse audit unavailable')

    monkeypatch.setattr(item_routing, 'audit', failing_audit)
    response = post_batch(batch_client, [change(), change(2, 'B')])
    assert response.status_code == 200 and response.json['ok']
    assert response.json['audit_recorded'] is False
    assert response.json['batch_id'] in caplog.text and 'actor=1' in caplog.text
    assert len(saved_rows()) == 2


def test_batch_warehouse_audit_contains_actor_old_new_batch(batch_client):
    import json

    import db

    response = post_batch(batch_client, [change(), change(2, 'B')])
    with sqlite3.connect(db.MASTER_DB) as master:
        path = master.execute("SELECT db_path FROM warehouses WHERE code='wh_test'").fetchone()[0]
    with sqlite3.connect(path) as wh:
        row = wh.execute("SELECT user_id, action, detail FROM audit_log WHERE action='item_routing.batch'").fetchone()
    detail = json.loads(row[2])
    assert row[:2] == (1, 'item_routing.batch')
    assert detail['actor_id'] == 1 and detail['batch_id'] == response.json['batch_id']
    assert detail['changes'][0]['old'] == {'dc_code': None, 'updated_at': None}
    assert detail['changes'][0]['new']['dc_code'] == 'A'
    assert detail['changes'][1]['canonical_id'] == 2


def test_batch_security_tenant_platform_csrf_and_auth(batch_client):
    import db

    batch_client.application.config['WTF_CSRF_FORCE_ENABLED'] = True
    assert post_batch(batch_client, [change()]).status_code == 400
    import re
    token = re.search(r'id="routing-csrf" value="([^"]+)"', batch_client.get(URL).text)[1]
    valid = batch_client.post(URL + '/batch', json={'changes': [change()]}, headers={'X-CSRFToken': token})
    assert valid.status_code == 200
    batch_client.application.config['WTF_CSRF_FORCE_ENABLED'] = False
    with sqlite3.connect(db.MASTER_DB) as master:
        master.execute('UPDATE users SET tenant_id=2 WHERE id=1')
    assert post_batch(batch_client, [change(2)]).status_code == 403
    with sqlite3.connect(db.MASTER_DB) as master:
        master.execute('UPDATE users SET tenant_id=1,is_admin=0 WHERE id=1')
    assert post_batch(batch_client, [change(2)]).status_code == 403
    with batch_client.session_transaction() as session:
        session.clear()
    response = batch_client.post(URL + '/batch', json={'changes': [change(2)]}, headers={'Accept': 'application/json'})
    assert response.status_code == 401 and response.json['ok'] is False
    assert len(saved_rows()) == 1


def test_batch_two_independent_requests_serialize_and_conflict(batch_client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    app = batch_client.application

    def save(code):
        with app.test_client() as client:
            with client.session_transaction() as session:
                session['user_id'] = 1
                session['warehouse_id'] = 1
            barrier.wait(timeout=5)
            return post_batch(client, [change(1, code)]).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ['A', 'B']))
    assert sorted(results) == [200, 409]
    assert len(saved_rows()) == 1


def test_editor_dataset_is_complete_canonical_category_and_diagnostic(batch_client):
    import json
    import re

    import db

    with sqlite3.connect(db.MASTER_DB) as master:
        master.execute("INSERT INTO canonical_categories(code,name,created_at,updated_at) VALUES('CANON','canonical类别',?,?)", (TS, TS))
        master.execute("UPDATE canonical_items SET category_code='CANON' WHERE id=1")
        master.executemany(
            "INSERT INTO canonical_items(id,canonical_sku,name,unit,created_at,updated_at) VALUES(?,?,?,'件',?,?)",
            [(cid, f'IC-{cid}', f'品项{cid}', TS, TS) for cid in range(4, 209)])
    response = batch_client.get(URL)
    dataset = json.loads(re.search(r'<script type="application/json" id="routing-data">(.*?)</script>', response.text, re.S)[1])
    assert len(dataset['items']) == 207  # No mutation-limit truncation; tenant 2 is excluded.
    first = dataset['items'][0]
    assert first['category_name'] == 'canonical类别'
    assert first['saved'] == {'dc_code': None, 'updated_at': None}
    assert first['diagnostic']['codes'] == []
    assert dataset['items'][1]['status'] == 'inactive'
    assert dataset['items'][1]['diagnostic'] is None
    assert response.headers['Cache-Control'] == 'no-store'


def test_batch_lock_contention_is_503_without_writes(batch_client, monkeypatch):
    import db
    from blueprints import item_routing

    original = item_routing.get_master_db

    def quick_timeout():
        master = original()
        master.execute("PRAGMA busy_timeout=1")
        return master

    monkeypatch.setattr(item_routing, "get_master_db", quick_timeout)
    with sqlite3.connect(db.MASTER_DB) as contender:
        contender.execute("BEGIN IMMEDIATE")
        response = post_batch(batch_client, [change()])
        assert response.status_code == 503 and response.json["ok"] is False
    assert saved_rows() == []


def test_batch_200_changes_uses_one_master_commit(batch_client, monkeypatch):
    import db
    from blueprints import item_routing

    ids = [1, *range(4, 203)]
    with sqlite3.connect(db.MASTER_DB) as master:
        master.executemany(
            "INSERT INTO canonical_items(id,canonical_sku,name,unit,created_at,updated_at) "
            "VALUES(?,?,?, '件',?,?)",
            [(cid, f"IC-{cid}", f"品项{cid}", TS, TS) for cid in ids[1:]])
    trace = []
    original = item_routing.get_master_db

    def traced():
        master = original()
        master.set_trace_callback(trace.append)
        return master

    monkeypatch.setattr(item_routing, "get_master_db", traced)
    response = post_batch(batch_client, [change(cid) for cid in ids])
    assert response.status_code == 200 and response.json["updated_count"] == 200
    assert len(saved_rows()) == 200
    assert trace.count("BEGIN IMMEDIATE") == 1 and trace.count("COMMIT") == 1
