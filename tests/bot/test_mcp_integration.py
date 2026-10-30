"""PR2 real temporary-DB calls; collected by production CI outside tests/mcp_server."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from flask import has_app_context, has_request_context

from bot import agent
from bot.access import SenderAccess, load_sender_access
from bot.agent_config import load_effective_config
from bot.tool_contract import EXTRA_READS, GLOBAL_READS, MCP_NAMES, WRITE_PATHS, mcp_schemas
from bot.tool_schema import TOOL_NAMES


@pytest.fixture
def setup_db(tmp_path, monkeypatch):
    import blueprints.audit_pure as audit
    import config
    import db
    import mcp_server.data.unit_of_work as uow

    master = tmp_path / "master.db"
    monkeypatch.setattr(db, "MASTER_DB", master)
    monkeypatch.setattr(config, "MASTER_DB", master)
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(uow, "MASTER_DB", master)
    monkeypatch.setenv("DAILYCHECK_DB_DIR", str(tmp_path))
    monkeypatch.setenv("BOT_OWNER_USERID", "verified-owner")
    monkeypatch.delenv("BOT_AUTH_POLICY", raising=False)
    monkeypatch.setattr(audit, "ADMIN_AUDIT_LOG", tmp_path / "admin_audit.log")
    db.init_master_db()
    with sqlite3.connect(master) as conn:
        conn.execute("INSERT OR REPLACE INTO procurement_config (id, cover_days, min_absolute) VALUES (1, 14, 200)")
        conn.execute("INSERT INTO canonical_items (id, canonical_sku, name, unit, created_at, updated_at) VALUES (1, 'C1', '面粉', '箱', datetime('now'), datetime('now'))")
        conn.execute("INSERT INTO canonical_publish_events (id, summary, started_at, target_warehouse_codes_json) VALUES (1, '测试事件', datetime('now'), '[\"wh_a\",\"wh_b\"]')")
        for i, code in enumerate(("wh_a", "wh_b"), 1):
            path = tmp_path / (code + ".db")
            db.init_warehouse_db(path)
            conn.execute("INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) VALUES (?, ?, ?, ?, 'storefront', datetime('now'))", (i, code, code, str(path)))
            conn.execute("INSERT INTO canonical_conflicts (canonical_id, warehouse_code, local_item_id, field, conflict_type, created_at) VALUES (1, ?, 1, 'name', 'value', datetime('now'))", (code,))
            conn.execute("INSERT INTO canonical_publish_event_warehouses (publish_event_id, warehouse_code, status) VALUES (1, ?, 'success')", (code,))
            conn.execute("INSERT INTO canonical_publish_event_items (publish_event_id, canonical_id, target_warehouse_code, local_item_id, status) VALUES (1, 1, ?, 1, 'success')", (code,))
            with sqlite3.connect(path) as wh:
                wh.execute("INSERT INTO items (id, category_id, sku, name, unit, quantity, selling_price, canonical_id, updated_at) VALUES (1, 1, 'F1', '面粉', '箱', 100, 12, 1, datetime('now'))")
                # Seven real business documents force procurement's compute branch.
                for _ in range(7):
                    wh.execute("INSERT INTO outbound_requests (item_id, requested_quantity, status, rolled_back, created_at) VALUES (1, 2, '出库', 0, datetime('now','-1 day'))")
                wh.execute("INSERT INTO stock_movements (item_id, action, delta, created_at) VALUES (1, '出库', -20, datetime('now','-1 day'))")
            conn.execute("INSERT INTO store_orders (order_no, store_warehouse_code, dc_warehouse_code, requested_by, status, created_at, updated_at) VALUES (?, ?, ?, 1, 'pending', datetime('now'), datetime('now'))", (f'O{i}', code, code))
    return tmp_path


def cfg(path, **updates):
    with sqlite3.connect(path / "master.db") as conn:
        for key, value in updates.items():
            conn.execute("INSERT OR REPLACE INTO bot_agent_config (key, value) VALUES (?, ?)", (key, str(value)))
    return load_effective_config(path / "master.db")


def sender(userid="verified-owner", *, warehouses=("wh_a",), global_read=False, paths=("*",)):
    return SenderAccess(userid, ("*",), paths, warehouses, global_read, True)


def call(name, args, config, access):
    text, payload = agent._run_tool(name, json.dumps(args), config=config, sender=access)
    return json.loads(text), payload


def snapshot(path):
    return {p.name: p.read_bytes() for p in path.glob("*.db")}


ARGS = {
    "warehouse_list": {}, "items_list": {"warehouse_code": "wh_a"},
    "items_detail": {"warehouse_code": "wh_a", "item_id": 1},
    "movements_list": {"warehouse_code": "wh_a"},
    "restock_list": {"warehouse_code": "wh_a"}, "outbound_list": {"warehouse_code": "wh_a"},
    "item_forecast": {"warehouse_code": "wh_a", "item_id": 1},
    "procurement_store": {"warehouse_code": "wh_a"}, "procurement_hub": {},
    "warehouse_consumption": {"warehouse_code": "wh_a"},
    "item_consumption": {"warehouse_code": "wh_a", "item_id": 1},
    "canonical_list": {}, "canonical_detail": {"canonical_id": 1},
    "canonical_categories": {}, "canonical_fanout_targets": {},
    "canonical_conflicts": {"canonical_id": 1}, "canonical_fanout_event": {"event_id": 1},
    "store_order_list": {}, "store_order_detail": {"order_id": 1},
    "restock_create": {"warehouse_code": "wh_a", "item_id": 1, "quantity": 3},
    "outbound_create": {"warehouse_code": "wh_a", "item_id": 1, "quantity": 4},
    "outbound_rollback": {"warehouse_code": "wh_a", "request_id": 1},
}


@pytest.mark.parametrize("name", sorted(MCP_NAMES))
def test_approved_schema_dispatch_real_result_without_context(setup_db, name, monkeypatch):
    from bot.mcp_tools import handlers
    from mcp_server.service import auth

    path = setup_db
    config = cfg(path, **{f"tool.{n}": 1 for n in EXTRA_READS})
    assert set(ARGS) == set(handlers()) == MCP_NAMES
    assert {s['function']['name'] for s in mcp_schemas()} == MCP_NAMES
    assert not has_app_context() and not has_request_context()
    # Poison both ambient identities: adapters must pass their own context.
    auth.set_current_auth(auth.AuthContext(999, ["*"], ["*"], None))
    monkeypatch.setenv("DAILYCHECK_MCP_TOKEN", "must-never-be-used")
    before = snapshot(path)
    result, payload = call(name, ARGS[name], config, sender(warehouses=None, global_read=True))
    assert payload is not None, result
    assert result.get("ok") is not False, result
    if name not in WRITE_PATHS:
        assert snapshot(path) == before
    if name == "items_list":
        assert payload['items'][0]['name'] == '面粉'
    if name == "canonical_detail":
        assert payload['bound_warehouse_count'] == 2
    if name == "store_order_list":
        assert payload['total'] == 2
    if name == "canonical_categories":
        assert payload['items'][0]['item_count'] == 1
    if name == "canonical_conflicts":
        assert {row['warehouse_code'] for row in payload['items']} == {'wh_a', 'wh_b'}
        assert all(row['status'] == 'open' for row in payload['items'])
    if name == "canonical_fanout_targets":
        assert {row['code'] for row in payload['items']} == {'wh_a', 'wh_b'}
    if name == "canonical_fanout_event":
        assert payload['event']['id'] == 1 and len(payload['warehouses']) == len(payload['items']) == 2
    if name == "canonical_list":
        assert payload['total'] == 1 and payload['items'][0]['canonical_sku'] == 'C1'
    if name == "procurement_store":
        assert payload['items'][0]['suggested_qty'] == 100
    if name == "procurement_hub":
        assert payload['items'][0]['total_suggested_qty'] == 200
    if name == "item_consumption":
        assert payload['consume_7d']['qty'] == 14 and payload['consume_monthly']['window_days'] == 28
    if name == "warehouse_consumption":
        assert payload['items'][0]['consume_qty'] == 14
    if name == "item_forecast":
        assert payload['forecast_total'] == 28 and payload['daily_avg'] == 2
    if name == "outbound_list":
        assert payload['total'] == 7 and sum(row['quantity'] for row in payload['items']) == 14
    if name == "movements_list":
        assert len(payload['items']) == 8
    if name == "items_detail":
        assert payload['items'][0]['current_stock'] == 100
    if name == "warehouse_list":
        assert {r['code'] for r in payload['items']} == {'wh_a', 'wh_b'}
    if name in WRITE_PATHS:
        assert result['quantity'] > 0
    auth.set_current_auth(None)


@pytest.mark.parametrize("name", sorted(WRITE_PATHS))
@pytest.mark.parametrize("rejection", ["unknown", "missing", "off", "warehouse", "path", "forged"])
def test_write_rejection_no_database_change(setup_db, name, rejection):
    config = cfg(setup_db)
    access = sender()
    args = dict(ARGS[name])
    expected = {"unknown": "forbidden_userid", "missing": "forbidden_userid", "off": "unknown_tool", "warehouse": "forbidden_warehouse", "path": "forbidden_path", "forged": "bad_arguments"}[rejection]
    if rejection in ("unknown", "missing"):
        access = sender("outsider" if rejection == "unknown" else "")
    elif rejection == "off":
        config = cfg(setup_db, **{f"tool.{name}": 0})
    elif rejection == "warehouse":
        args["warehouse_code"] = "wh_b"
    elif rejection == "path":
        access = sender(paths=("/api/v1/other",))
    elif rejection == "forged":
        args["userid"] = "verified-owner"
        access = sender("outsider")
    before = snapshot(setup_db)
    result, payload = call(name, args, config, access)
    assert payload is None and expected in json.dumps(result), result
    assert snapshot(setup_db) == before
    audit = [json.loads(line) for line in (setup_db / "admin_audit.log").read_text().splitlines()]
    detail = json.loads(audit[-1]["detail"])
    assert detail['tool'] == name and detail['args'] == args
    assert detail['who'] == (access.userid or None) and detail['duration_ms'] >= 0
    assert detail['result'] == result


def test_three_writes_expected_effects_and_audit(setup_db):
    config = cfg(setup_db)
    def qty():
        with sqlite3.connect(setup_db / "wh_a.db") as c:
            return c.execute("SELECT quantity FROM items WHERE id=1").fetchone()[0]
    restock, _ = call("restock_create", ARGS['restock_create'], config, sender())
    assert qty() == 103 and restock['quantity'] == 3 and restock['id'] == 1
    listed, _ = call('restock_list', {'warehouse_code': 'wh_a'}, config, sender())
    assert listed['total'] == 1 and listed['items'][0]['quantity'] == 3
    outbound, _ = call("outbound_create", ARGS['outbound_create'], config, sender())
    assert qty() == 99 and isinstance(outbound['id'], int)
    rolled, _ = call("outbound_rollback", {"warehouse_code": "wh_a", "request_id": outbound['id']}, config, sender())
    assert qty() == 103 and rolled['quantity'] == 4
    before = snapshot(setup_db)
    failed, payload = call("outbound_rollback", {"warehouse_code": "wh_a", "request_id": outbound['id']}, config, sender())
    assert payload is None and 'already_rolled_back' in failed['message']
    assert snapshot(setup_db) == before
    with sqlite3.connect(setup_db / 'wh_a.db') as c:
        assert c.execute('SELECT count(*) FROM restock_requests').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM outbound_requests').fetchone()[0] == 8
        assert c.execute('SELECT count(*) FROM stock_movements').fetchone()[0] == 4


@pytest.mark.parametrize('name', sorted(GLOBAL_READS))
def test_global_reads_require_explicit_unrestricted_grant(setup_db, name):
    config = cfg(setup_db, **{f'tool.{name}': 1})
    before = snapshot(setup_db)
    for access in (sender(global_read=True), sender(warehouses=None)):
        result, payload = call(name, ARGS[name], config, access)
        assert payload is None and 'forbidden_global_read' in result['message']
    assert snapshot(setup_db) == before


def test_scope_rows_totals_pagination_and_legacy_queries(setup_db):
    config = cfg(setup_db, **{f'tool.{n}': 1 for n in EXTRA_READS})
    access = sender()
    orders, _ = call('store_order_list', {'limit': 1}, config, access)
    assert orders['total'] == 1 and len(orders['orders']) == 1
    assert orders['orders'][0]['store_warehouse_code'] == 'wh_a'
    denied, _ = call('store_order_detail', {'order_id': 2}, config, access)
    missing, _ = call('store_order_detail', {'order_id': 999}, config, access)
    assert denied == missing
    hub, _ = call('procurement_hub', {}, config, access)
    assert all(s['warehouse_code'] == 'wh_a' for i in hub['items'] for s in i['stores_detail'])
    old, _ = call('list_orders', {}, config, access)
    assert old['count'] == 1 and old['orders'][0]['order_no'] == 'O1'
    assert sum(old['status_summary'].values()) == 1
    detail, _ = call('get_item_detail', {'sku_or_name': 'F1'}, config, access)
    assert detail['warehouse_count'] == 1 and detail['total_quantity'] == 100
    bad, _ = call('get_item_consumption', {'sku_or_name': 'F1', 'warehouse_code': 'no-such'}, config, access)
    assert bad['ok'] is False
    legacy, _ = call('get_item_consumption', {'sku_or_name': 'F1', 'days': 90}, config, access)
    mcp, _ = call('item_consumption', ARGS['item_consumption'], config, access)
    assert legacy['total_consumed'] == 20
    assert mcp['consume_7d']['qty'] == mcp['consume_30d']['qty'] == 14
    assert mcp['consume_monthly']['window_days'] == 28


def test_config_next_message_and_concurrent_userids(setup_db):
    first = cfg(setup_db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda who: call('restock_create', ARGS['restock_create'], first, sender(who)), ['verified-owner', 'outsider']))
    assert results[0][1] is not None and results[1][1] is None
    second = cfg(setup_db, **{'write.allowed_userids': 'outsider'})
    assert call('restock_create', ARGS['restock_create'], second, sender())[1] is None
    assert call('restock_create', ARGS['restock_create'], second, sender('outsider'))[1] is not None
    assert first.write_allowed_userids == ('verified-owner',)


def test_parent_switches_and_fixed_failure_fallback(setup_db):
    for child, parent in __import__('bot.tool_contract', fromlist=['LEGACY_PARENTS']).LEGACY_PARENTS.items():
        config = cfg(setup_db, **{f'tool.{parent}': 0, f'tool.{child}': 1})
        assert child not in config.enabled_tools
    with sqlite3.connect(setup_db / 'master.db') as c:
        c.execute('DROP TABLE bot_agent_config')
    config = load_effective_config(setup_db / 'master.db')
    assert config.degraded and config.enabled_tools == TOOL_NAMES and not config.write_allowed_userids
    assert call('restock_create', ARGS['restock_create'], config, sender())[1] is None


def test_unknown_discovery_and_seven_canonical_writes_never_authorize(setup_db, monkeypatch):
    from mcp.types import Tool

    from mcp_server.protocol import tools
    original = tools.get_tools
    monkeypatch.setattr(tools, 'get_tools', lambda: original() + [Tool(name='new_write', description='probe', inputSchema={'type':'object'})])
    forbidden = {t.name for t in original()} - MCP_NAMES
    config = cfg(setup_db, **{f'tool.{n}': 1 for n in forbidden | {'new_write', 'get_price'}})
    before = snapshot(setup_db)
    for name in forbidden | {'new_write', 'get_price'}:
        assert call(name, {}, config, sender(warehouses=None, global_read=True))[0]['error'] == 'unknown_tool'
    assert snapshot(setup_db) == before


def test_policy_is_trusted_reloaded_and_fail_closed(setup_db, monkeypatch):
    file = setup_db / 'policy.json'
    monkeypatch.setenv('BOT_AUTH_POLICY', str(file))
    file.write_text(json.dumps({'verified-owner': {'read_paths': ['*'], 'write_paths': ['/api/v1/restock'], 'warehouses': ['wh_a']}}))
    access = load_sender_access('verified-owner')
    assert access.warehouses == ('wh_a',) and access.write_paths == ('/api/v1/restock',)
    assert load_sender_access('outsider').read_paths == ()
    file.write_text('broken')
    assert load_sender_access('verified-owner').write_paths == ()
    assert access.write_paths == ('/api/v1/restock',)  # Existing message immutable.


def test_unverified_identity_blocks_historical_default_even_with_policy(setup_db, monkeypatch):
    monkeypatch.delenv('BOT_OWNER_USERID')
    config = cfg(setup_db)
    assert config.write_allowed_userids == ('LinJi',)
    access = replace(sender('LinJi'), identity_verified=False)
    before = snapshot(setup_db)
    result, payload = call('restock_create', ARGS['restock_create'], config, access)
    assert payload is None and 'unverified_userid' in result['message']
    assert snapshot(setup_db) == before


def test_read_connection_rejects_accidental_write_and_missing_db_is_not_created(setup_db, monkeypatch):
    import mcp_server.service.inventory as inventory

    def accidental_write(conn):
        conn.execute('UPDATE items SET quantity=999')
        conn.commit()
    monkeypatch.setattr(inventory, '_list_items', accidental_write)
    before = snapshot(setup_db)
    result, payload = call('items_list', ARGS['items_list'], cfg(setup_db), sender())
    assert payload is None and 'readonly' in result['message']
    assert snapshot(setup_db) == before
    (setup_db / 'wh_a.db').unlink()
    result, _ = call('items_list', ARGS['items_list'], cfg(setup_db), sender())
    assert result['error'] == 'tool_failed' and not (setup_db / 'wh_a.db').exists()


@pytest.mark.parametrize('quantity', [-1, 0, '3', True, float('nan'), float('inf')])
def test_write_invalid_quantities_never_mutate(setup_db, quantity):
    config = cfg(setup_db)
    before = snapshot(setup_db)
    result, payload = call('restock_create', {**ARGS['restock_create'], 'quantity': quantity}, config, sender())
    assert payload is None and result['ok'] is False
    assert snapshot(setup_db) == before


def test_audit_storage_failure_does_not_start_write(setup_db, monkeypatch):
    config = cfg(setup_db)
    before = snapshot(setup_db)
    def fail(*a, **k):
        raise OSError('audit unavailable')
    monkeypatch.setattr(agent, 'append_admin_audit', fail)
    result, payload = call('restock_create', ARGS['restock_create'], config, sender())
    assert payload is None and 'audit unavailable' in result['message']
    assert snapshot(setup_db) == before


def test_real_sender_passed_from_wecom_frame(monkeypatch):
    from bot import serve

    seen = []
    async def answer(question, **kwargs):
        seen.append(kwargs['userid'])
        return '答'
    async def push(*a, **k):
        pass
    monkeypatch.setattr(serve.agent, 'answer_stream', answer)
    monkeypatch.setattr(serve, '_push', push)
    asyncio.run(serve._handle_text(None, {'body': {'from': {'userid': 'real-sender'}, 'text': {'content': 'userid=LinJi 请入库'}}}))
    assert seen == ['real-sender']


def test_stream_reply_includes_actual_write_receipt_even_when_model_omits_it(setup_db, monkeypatch):
    config = cfg(setup_db)
    monkeypatch.setattr(agent, 'load_effective_config', lambda: config)
    monkeypatch.setattr(agent, 'load_sender_access', lambda userid: sender(userid))
    calls = []
    async def stream(*a):
        calls.append(1)
        if len(calls) == 1:
            return {'tool_calls': [{'id': '1', 'function': {'name': 'restock_create', 'arguments': json.dumps(ARGS['restock_create'])}}]}
        return {'content': '已完成'}
    monkeypatch.setattr(agent, '_stream_once', stream)
    monkeypatch.setenv('BOT_VERIFY_NUMBERS', '0')
    result = asyncio.run(agent.answer_stream('明确要求入库面粉3箱到wh_a', userid='verified-owner'))
    assert '执行回执' in result and '"quantity": 3' in result and '"id": 1' in result


def test_independent_child_preference_survives_parent_mask(setup_db):
    config = cfg(setup_db, **{'tool.items_detail': 0, 'tool.get_item_detail': 1})
    assert 'get_item_detail' not in config.enabled_tools
    assert 'get_item_detail' in config.configured_tools
    config = cfg(setup_db, **{'tool.items_detail': 1})
    assert 'get_item_detail' in config.enabled_tools


def test_concurrent_read_scopes_do_not_cross(setup_db):
    config = cfg(setup_db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda code: call('get_item_detail', {'sku_or_name': 'F1'}, config, sender(code, warehouses=(code,)))[0], ['wh_a', 'wh_b']))
    assert [r['details'][0]['warehouse_code'] for r in result] == ['wh_a', 'wh_b']
    assert all(r['warehouse_count'] == 1 and r['total_quantity'] == 100 for r in result)


@pytest.mark.parametrize('name', sorted(MCP_NAMES - WRITE_PATHS.keys()))
def test_every_read_tool_enforces_path_acl(setup_db, name):
    config = cfg(setup_db, **{f'tool.{n}': 1 for n in EXTRA_READS})
    access = replace(sender(warehouses=None, global_read=True), read_paths=())
    before = snapshot(setup_db)
    result, payload = call(name, ARGS[name], config, access)
    assert payload is None and 'forbidden_path' in result['message']
    assert snapshot(setup_db) == before


def test_prompt_does_not_leak_scopes_or_denied_warehouse_path(setup_db):
    assert 'wh_b' not in agent.build_system_prompt(sender())
    assert 'wh_a' in agent.build_system_prompt(sender())
    denied = replace(sender(warehouses=None), read_paths=())
    assert 'wh_b' not in agent.build_system_prompt(denied)


def test_legacy_and_mcp_consumption_keep_distinct_document_and_window_semantics(setup_db):
    config = cfg(setup_db)
    with sqlite3.connect(setup_db / 'wh_a.db') as wh:
        for action, delta, ago in [('门店订货出库', -3, '-1 day'), ('总仓调货出库', -4, '-1 day'), ('出库', -5, '-60 day'), ('出库回退', 99, '-1 day'), ('生产领料', -6, '-1 day')]:
            wh.execute("INSERT INTO stock_movements (item_id, action, delta, created_at) VALUES (1, ?, ?, datetime('now',?))", (action, delta, ago))
        wh.execute("INSERT INTO outbound_requests (item_id, requested_quantity, rolled_back, created_at) VALUES (1, 77, 1, datetime('now','-1 day'))")
        wh.execute("INSERT INTO outbound_requests (item_id, requested_quantity, rolled_back, reason, created_at) VALUES (1, 6, 0, '生产领料(run=#1)', datetime('now','-1 day'))")
        wh.execute("INSERT INTO production_runs (id, product_id, output_qty, rolled_back, created_at) VALUES (1, 1, 1, 0, datetime('now','-1 day'))")
        wh.execute("INSERT INTO production_run_items (run_id, item_id, planned_qty, actual_qty) VALUES (1,1,6,6)")
        wh.execute("INSERT INTO production_runs (id, product_id, output_qty, rolled_back, created_at) VALUES (2, 1, 1, 1, datetime('now','-1 day'))")
        wh.execute("INSERT INTO production_run_items (run_id, item_id, planned_qty, actual_qty) VALUES (2,1,88,88)")
    old, _ = call('get_item_consumption', {'sku_or_name': 'F1', 'days': 90}, config, sender())
    recent, _ = call('get_item_consumption', {'sku_or_name': 'F1', 'days': 30}, config, sender())
    mcp, _ = call('item_consumption', ARGS['item_consumption'], config, sender())
    assert old['total_consumed'] == 32 and old['days'] == 90
    assert recent['total_consumed'] == 27  # Action rows, without production/rollback.
    assert mcp['consume_7d']['qty'] == 20  # 14 business outbound + 6 production, no double count.


def test_legacy_date_cutoff_keeps_midnight_boundary_without_negative_delta_filter(setup_db):
    config = cfg(setup_db)
    with sqlite3.connect(setup_db / 'wh_a.db') as wh:
        wh.execute("INSERT INTO stock_movements (item_id, action, delta, created_at) VALUES (1, '出库', -5, date('now', '-30 day') || ' 00:00:00')")
        wh.execute("INSERT INTO stock_movements (item_id, action, delta, created_at) VALUES (1, '出库', -99, date('now', '-31 day') || ' 23:59:59')")
        wh.execute("INSERT INTO stock_movements (item_id, action, delta, created_at) VALUES (1, '出库', 1, datetime('now','-1 day'))")
    old, _ = call('get_item_consumption', {'sku_or_name': 'F1', 'days': 30}, config, sender())
    assert old['total_consumed'] == 24  # 20 + cutoff 5 - positive action delta 1.
    assert old['by_warehouse'][0]['movement_count'] == 3
