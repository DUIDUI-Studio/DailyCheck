"""Issue24: real SDK/HTTP error flags and procurement cache isolation, synthetic DBs only."""
import json
import sqlite3

import pytest

from mcp_server.service.auth import authenticate
from tests.mcp_server.test_protocol.test_consumption_http import (
    _call,
    _snapshot,
    _token,
)
from tests.mcp_server.test_protocol.test_consumption_http import (
    http_mcp as http_mcp,
)


@pytest.mark.parametrize('name,args,code', [
    ('restock_create', {'warehouse_code': 'allowed', 'item_id': 1, 'quantity': 1}, 'forbidden'),
    ('items_detail', {'warehouse_code': 'allowed', 'item_id': 99999}, 'not_found'),
])
def test_business_error_flag(http_mcp, name, args, code):
    env, client = http_mcp
    raw, _ = _token(env, ['*'], ['allowed'])
    response = client.post('/api/mcp/', headers={
        'Authorization': f'Bearer {raw}', 'Accept': 'application/json, text/event-stream',
    }, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
             'params': {'name': name, 'arguments': args}})
    assert response.status_code == 200
    body = response.json()
    assert 'error' not in body
    assert body['result']['isError'] is True
    assert json.loads(body['result']['content'][0]['text'])['error'] == code


@pytest.mark.parametrize('name', ['procurement_store', 'procurement_hub'])
@pytest.mark.parametrize('cache_state', ['cold', 'stale', 'invalid'])
@pytest.mark.parametrize('write_paths', [[], ['*']])
def test_procurement_get_is_cache_safe(http_mcp, name, cache_state, write_paths):
    env, client = http_mcp
    raw, _ = _token(env, ['/api/v1/procurement'], ['allowed'], write_paths=write_paths)
    # This is a normally authenticated token, not a manually read_only=True context.
    ctx = authenticate(f'Bearer {raw}')
    assert ctx.read_only is False
    with sqlite3.connect(env['warehouses'] / 'allowed.db') as conn:
        conn.execute('UPDATE items SET quantity=0')
        for day in range(2, 10):
            conn.execute(
                "INSERT INTO outbound_requests (item_id, requested_quantity, reason, created_at) "
                "VALUES (1, 10, 'fixture', datetime('now', ?))", (f'-{day} day',),
            )
    if cache_state != 'cold':
        with sqlite3.connect(env['master']) as conn:
            conn.execute(
                'INSERT INTO procurement_cache '
                '(item_id, warehouse_code, computed_at, daily_avg, current_qty, '
                'in_transit_qty, safety_stock, suggested_qty, invalid) '
                "VALUES (1, 'allowed', '2000-01-01T00:00:00Z', 0, 999, 0, 0, 0, ?)",
                (int(cache_state == 'invalid'),),
            )
    before = _snapshot(env['root'])
    result = _call(client, raw, name)
    if name == 'procurement_store':
        assert result['warehouse_code'] == 'allowed'
        assert result['items'][0]['suggested_qty'] > 0
    else:
        assert result[0]['total_suggested_qty'] > 0
    assert _snapshot(env['root']) == before


@pytest.mark.parametrize('error', [
    ('unauthorized', 'no auth context', 401),
    ('forbidden', 'forbidden_path', 403),
    ('not_found', 'not_found', 404),
    ('validation_error', 'invalid_quantity', 400),
    ('database_busy', 'database_busy', 503),
])
def test_shared_sdk_contract_for_http_sse_and_stdio(monkeypatch, error):
    import asyncio

    from mcp.types import CallToolRequest

    import mcp_server.protocol.server as protocol
    from mcp_server.infra.errors import McpError

    def fail(_args):
        raise McpError(*error)

    monkeypatch.setitem(protocol._tool_map, 'warehouse_list', fail)
    server = protocol.build_server()
    result = asyncio.run(server.request_handlers[CallToolRequest](
        CallToolRequest(method='tools/call', params={'name': 'warehouse_list', 'arguments': {}})
    )).root
    assert result.isError is True
    assert json.loads(result.content[0].text) == {'error': error[0], 'message': error[1]}


def test_sdk_preserves_write_reconciliation_fields(monkeypatch):
    import asyncio

    from mcp.types import CallToolRequest

    import mcp_server.protocol.server as protocol
    from mcp_server.infra.errors import WriteError

    def fail(_args):
        raise WriteError('cache_invalidation_failed', 'cache_invalidation_failed', 503,
                         committed=True, request_id=42, retry_safe=False)

    monkeypatch.setitem(protocol._tool_map, 'warehouse_list', fail)
    server = protocol.build_server()
    result = asyncio.run(server.request_handlers[CallToolRequest](
        CallToolRequest(method='tools/call', params={'name': 'warehouse_list', 'arguments': {}})
    )).root
    assert result.isError is True
    assert json.loads(result.content[0].text) == {
        'error': 'cache_invalidation_failed', 'message': 'cache_invalidation_failed',
        'committed': True, 'request_id': 42, 'retry_safe': False,
    }
