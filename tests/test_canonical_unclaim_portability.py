"""#108: real SQLite MCP unclaim preserves latest-approved-row semantics."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import blueprints.canonical_pure as canonical
from mcp_server.protocol import server
from mcp_server.service.auth import AuthContext, set_current_auth
from tests.test_mcp_access_log import CapturingServer
from tests.test_mcp_access_log import mcp_env as access_db_fixture

unclaim_db = access_db_fixture
CODE = 'WH001'


def rows(path, table):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]


@pytest.fixture
def unclaim_env(unclaim_db, monkeypatch):
    master = unclaim_db['master_path']
    warehouse = unclaim_db['wh_path']
    monkeypatch.setattr(canonical, 'BACKUP_WAREHOUSE_DIR', master.parent / 'backups')
    with sqlite3.connect(master) as conn:
        conn.execute(
            "INSERT INTO canonical_items(id,canonical_sku,name,unit,created_at,updated_at) "
            "VALUES(1,'C1','测试品项','箱',datetime('now'),datetime('now'))"
        )
    with sqlite3.connect(warehouse) as conn:
        conn.execute("UPDATE items SET canonical_id=1, is_alias=1, canonical_status='active', "
                     "unit='箱', unit_cost=3.5, selling_price=12 WHERE id=1")
    set_current_auth(AuthContext(7, ['*'], ['*'], [CODE]))
    captured = CapturingServer('unclaim-portability')
    monkeypatch.setattr(server, 'Server', lambda _name: captured)
    server.build_server()
    yield master, warehouse, captured.handler
    set_current_auth(None)


def seed_claims(master, scenario):
    # Newer non-approved rows and rows for another warehouse/item must survive.
    claims = [
        (4, CODE, 1, 'pending'),
        (5, CODE, 1, 'rejected'),
        (8, 'OTHER', 1, 'approved'),
        (9, CODE, 2, 'approved'),
    ] if scenario != 'empty' else []
    if scenario == 'multiple':
        claims.extend([(1, CODE, 1, 'approved'), (3, CODE, 1, 'approved')])
    with sqlite3.connect(master) as conn:
        conn.executemany(
            "INSERT INTO canonical_claim_requests "
            "(id,warehouse_code,local_item_id,canonical_id,status,review_note,created_at) "
            "VALUES(?,?,?,1,?,'original',datetime('now'))", claims,
        )


def call(route, reason='cancel note'):
    result = asyncio.run(route('canonical_unclaim', {
        'warehouse_code': CODE, 'local_item_id': 1, 'reason': reason,
    }))
    assert len(result) == 1 and result[0].type == 'text'
    return json.loads(result[0].text)


@pytest.mark.parametrize('scenario', ['multiple', 'no_matching_approved', 'empty'])
def test_unclaim_real_databases_cancel_only_latest_matching_approved(unclaim_env, scenario):
    master, warehouse, route = unclaim_env
    seed_claims(master, scenario)
    before_claims = rows(master, 'canonical_claim_requests')
    before_item = rows(warehouse, 'items')[0]
    assert call(route) == {
        'warehouse_code': CODE, 'local_item_id': 1, 'canonical_id': 1,
        'action': 'unclaimed',
    }
    after_claims = rows(master, 'canonical_claim_requests')
    for before, after in zip(before_claims, after_claims, strict=True):
        if scenario == 'multiple' and before['id'] == 3:
            assert after['status'] == 'cancelled'
            assert after['reviewed_by'] == 7
            assert after['review_note'] == 'cancel note'
            assert after['reviewed_at']
            unchanged = set(before) - {'status', 'reviewed_by', 'review_note', 'reviewed_at'}
            assert {key: after[key] for key in unchanged} == {key: before[key] for key in unchanged}
        else:
            assert after == before
    after_item = rows(warehouse, 'items')[0]
    assert after_item['canonical_id'] is None
    assert after_item['is_alias'] == 0 and after_item['canonical_status'] is None
    assert after_item['quantity'] == before_item['quantity'] == 100
    unchanged = set(before_item) - {'canonical_id', 'is_alias', 'canonical_status', 'updated_at'}
    assert {key: after_item[key] for key in unchanged} == {key: before_item[key] for key in unchanged}
    assert len(list((master.parent / 'backups').glob('*.db'))) == 1


def test_unclaim_default_note_and_repeat_preserve_stock(unclaim_env):
    master, warehouse, route = unclaim_env
    seed_claims(master, 'multiple')
    assert call(route, reason=None)['action'] == 'unclaimed'
    cancelled = next(row for row in rows(master, 'canonical_claim_requests') if row['id'] == 3)
    assert cancelled['review_note'] == 'unclaimed'
    before = rows(master, 'canonical_claim_requests')
    assert call(route)['error'] == 'validation_error'
    assert rows(master, 'canonical_claim_requests') == before
    assert rows(warehouse, 'items')[0]['quantity'] == 100
