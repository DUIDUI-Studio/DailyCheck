"""Registry coverage, preserved wire declarations and real SQLite output contracts."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import json
import secrets
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, validate

from mcp_server.protocol import registry, server
from mcp_server.protocol.tools import get_tools
from mcp_server.service.auth import AuthContext, set_current_auth
from scripts import mcp_contracts
from tests.bot.test_mcp_integration import ARGS
from tests.bot.test_mcp_integration import setup_db as bot_setup_db
from tests.test_mcp_access_log import CapturingServer

registry_db = bot_setup_db

ROOT = Path(__file__).resolve().parents[1]
NAMES = [entry.name for entry in registry.REGISTRY]
CANONICAL_WRITES = {
    "canonical_create", "canonical_update", "canonical_claim", "canonical_unclaim",
    "canonical_fanout", "canonical_resolve_conflict", "canonical_set_binding_active",
}


def test_registry_matches_every_implementation_and_dispatch():
    mcp_contracts.validate_implementation_set()
    assert len(NAMES) == len(set(NAMES)) == 29
    assert {tool.name for tool in get_tools()} == set(server._tool_map) == set(NAMES)
    assert server._tool_map == registry.get_handlers()
    assert {e.name for e in registry.REGISTRY if not e.bot_approved} == CANONICAL_WRITES
    assert sum(e.external_subset for e in registry.REGISTRY) == 16
    assert sum(e.bot_approved for e in registry.REGISTRY) == 22
    assert {e.name for e in registry.REGISTRY if e.mode == 'write'} == CANONICAL_WRITES | {
        'restock_create', 'outbound_create', 'outbound_rollback',
    }
    for entry in registry.REGISTRY:
        Draft202012Validator.check_schema(entry.output_shape)
        assert "outputSchema" not in entry.tool.model_dump(mode="json", exclude_none=True)
        assert entry.tests == (f"tests/test_mcp_registry.py::test_registered_tool_real_serialized_shape[{entry.name}]",)



def test_registry_default_validation_checks_current_registry(monkeypatch):
    monkeypatch.setattr(registry, 'REGISTRY', (*registry.REGISTRY, registry.REGISTRY[0]))
    with pytest.raises(ValueError, match='duplicate tool'):
        registry.validate_registry()


def test_original_all_29_wire_declarations_are_preserved():
    # Captured from origin/main before moving declarations; independent of generator.
    expected = json.loads((ROOT / 'tests/fixtures/mcp-declarations-sha256.json').read_text())
    actual = {
        tool.name: hashlib.sha256(json.dumps(
            tool.model_dump(mode='json', exclude_none=True),
            sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()
        for tool in get_tools()
    }
    assert actual == expected
    first = get_tools()
    first[0].inputSchema['probe'] = True
    first[0].description = 'changed'
    assert get_tools()[0].description != 'changed'
    assert 'probe' not in get_tools()[0].inputSchema


@pytest.mark.parametrize('mutation', ['duplicate', 'missing', 'wrong_handler', 'orphan'])
def test_registry_rejects_missing_duplicate_and_orphan_implementations(monkeypatch, mutation):
    entries = registry.REGISTRY
    if mutation == 'duplicate':
        monkeypatch.setattr(mcp_contracts, 'REGISTRY', entries + (entries[0],))
        # Runtime structural validation also rejects this before building a dict.
        with pytest.raises(ValueError, match='duplicate tool'):
            registry.validate_registry(entries + (entries[0],))
    elif mutation == 'missing':
        monkeypatch.setattr(mcp_contracts, 'REGISTRY', entries[1:])
    elif mutation == 'wrong_handler':
        broken = replace(entries[0], handler=entries[1].handler)
        with pytest.raises(ValueError, match='invalid handler'):
            registry.validate_registry((broken, *entries[1:]))
        monkeypatch.setattr(mcp_contracts, 'REGISTRY', (broken, *entries[1:]))
    else:
        from mcp_server.protocol.tools import inventory
        def orphan_impl(args):
            return []
        orphan_impl.__module__ = inventory.__name__
        monkeypatch.setattr(inventory, 'orphan_impl', orphan_impl, raising=False)
    with pytest.raises(ValueError):
        mcp_contracts.validate_implementation_set()



def copy_maintenance_files(target_root):
    for name in ("AGENT.md", "CLAUDE.md", "tests/test_mcp_registry.py",
                 "docs/integrations/dailycheck-mcp/calculation-logic.md"):
        target = target_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / name).read_text())


def test_generated_artifacts_match_and_check_detects_drift(tmp_path, monkeypatch, capsys):
    for path, expected in mcp_contracts.generated_artifacts().items():
        assert (ROOT / path).read_text() == expected
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(expected)
    copy_maintenance_files(tmp_path)
    monkeypatch.setattr(mcp_contracts, 'ROOT', tmp_path)
    assert mcp_contracts.main(['--check']) == 0
    for path in mcp_contracts.generated_artifacts():
        target = tmp_path / path
        original = target.read_text()
        target.write_text(original + 'drift\n')
        assert mcp_contracts.main(['--check']) == 1
        assert str(path) in capsys.readouterr().out
        target.write_text(original)
    assert mcp_contracts.main(['--write']) == 0
    assert mcp_contracts.main(['--check']) == 0


def test_json_serialization_is_independent_of_nested_object_insertion_order():
    first = {
        'tools': [{'name': '库存', 'metadata': {'z': 1, 'a': {'right': False, 'left': None}}}],
        'counts': {'runtime': 1, 'external': 0},
    }
    reordered = {
        'counts': {'external': 0, 'runtime': 1},
        'tools': [{'metadata': {'a': {'left': None, 'right': False}, 'z': 1}, 'name': '库存'}],
    }
    assert first == reordered
    serialized = mcp_contracts.json_text(first)
    assert json.loads(serialized) == first
    assert serialized == mcp_contracts.json_text(reordered)


@pytest.fixture
def generated_contract_tree(tmp_path, monkeypatch):
    copy_maintenance_files(tmp_path)
    for path, expected in mcp_contracts.generated_artifacts().items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(expected)
    monkeypatch.setattr(mcp_contracts, 'ROOT', tmp_path)
    assert mcp_contracts.main(['--check']) == 0
    return tmp_path


@pytest.mark.parametrize('change', [
    'metadata', 'description', 'input_schema', 'output_shape', 'added_field',
    'array_value', 'array_order', 'array_count', 'declared_count',
])
def test_generated_contract_check_rejects_semantic_json_drift(generated_contract_tree, change):
    target = generated_contract_tree / mcp_contracts.ARTIFACT_DIR / 'registry.json'
    original = target.read_text()
    changed = json.loads(original)
    tool = changed['tools'][0]
    if change == 'metadata':
        tool['access']['path_enforcement'] = 'changed enforcement metadata'
    elif change == 'description':
        tool['description'] += ' changed description'
    elif change == 'input_schema':
        tool['inputSchema']['required'].append('unexpected_argument')
    elif change == 'output_shape':
        tool['output_contract']['schema'] = {'type': 'null'}
    elif change == 'added_field':
        tool['future_metadata'] = {'meaning': 'must not be silently dropped'}
    elif change == 'array_value':
        changed['notes'][0] += ' changed note'
    elif change == 'array_order':
        changed['tools'].reverse()
    elif change == 'array_count':
        changed['tools'].pop()
    else:
        changed['counts']['runtime'] += 1
    assert changed != json.loads(original)
    changed_text = mcp_contracts.json_text(changed)
    assert json.loads(changed_text) == changed
    target.write_text(changed_text)
    assert mcp_contracts.main(['--check']) == 1
    assert target.read_text() == changed_text
    target.write_text(original)
    assert mcp_contracts.main(['--check']) == 0


@pytest.mark.parametrize('change', ['formatting_only', 'malformed', 'trailing_json'])
def test_contract_check_rejects_noncanonical_json_text(generated_contract_tree, change):
    target = generated_contract_tree / mcp_contracts.ARTIFACT_DIR / 'registry.json'
    original = target.read_text()
    if change == 'formatting_only':
        changed = json.dumps(json.loads(original), ensure_ascii=False)
        assert json.loads(changed) == json.loads(original)
    elif change == 'malformed':
        changed = original[:original.rfind('}')]
    else:
        changed = original + '{}\n'
    assert changed != original
    target.write_text(changed)
    assert mcp_contracts.main(['--check']) == 1
    assert target.read_text() == changed


def test_contract_cli_runs_from_repository():
    result = subprocess.run([sys.executable, '-m', 'scripts.mcp_contracts', '--check'],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def contract_env(registry_db, monkeypatch):
    import blueprints.canonical_pure as cp
    import mcp_server.infra.access_log as access_log

    monkeypatch.setattr(cp, 'BACKUP_WAREHOUSE_DIR', registry_db / 'backups')
    monkeypatch.setattr(access_log, '_ACCESS_LOG_PATH', registry_db / 'access.log')
    with sqlite3.connect(registry_db / 'master.db') as conn:
        conn.execute("UPDATE warehouses SET warehouse_type='distribution_center' WHERE code='wh_b'")
    with sqlite3.connect(registry_db / 'wh_a.db') as conn:
        conn.execute("INSERT INTO restock_requests(item_id,requested_quantity,status,created_at) "
                     "VALUES(1,3,'入库',datetime('now'))")
    captured = CapturingServer('registry-contracts')
    monkeypatch.setattr(server, 'Server', lambda _name: captured)
    server.build_server()
    yield registry_db, captured.handler
    set_current_auth(None)


@pytest.mark.parametrize('name', NAMES)
def test_registered_tool_real_serialized_shape(contract_env, name):
    """Reuse #80's real DB scenario; exercise real dispatch/serialization for all29."""
    path, route = contract_env
    entry = next(e for e in registry.REGISTRY if e.name == name)
    ctx = AuthContext(1, ['*'], ['*'], None, read_only=entry.mode == 'read', warehouse_base_dir=str(path))
    set_current_auth(ctx)
    extra_args = {
        'canonical_create': {'name': '新主数据', 'unit': '箱'},
        'canonical_update': {'canonical_id': 1, 'name': '改名'},
        'canonical_claim': {'warehouse_code': 'wh_a', 'local_item_id': 1, 'canonical_id': 1},
        'canonical_unclaim': {'warehouse_code': 'wh_a', 'local_item_id': 1},
        'canonical_fanout': {'canonical_ids': [1], 'warehouse_codes': ['wh_b']},
        'canonical_resolve_conflict': {'conflict_id': 1, 'decision': 'waive'},
        'canonical_set_binding_active': {'warehouse_code': 'wh_a', 'canonical_id': 1, 'active': False},
    }
    if name == 'canonical_claim':
        with sqlite3.connect(path / 'wh_a.db') as conn:
            conn.execute('UPDATE items SET canonical_id=NULL WHERE id=1')
    args = extra_args[name] if name in extra_args else ARGS[name]
    result = asyncio.run(route(name, args))
    assert len(result) == 1 and result[0].type == 'text'
    payload = json.loads(result[0].text)
    assert not isinstance(payload, dict) or 'error' not in payload, payload
    validate(payload, entry.output_shape)
    # Nonempty real rows prevent array contracts from passing vacuously.
    if isinstance(payload, list):
        assert payload
    if isinstance(payload, dict) and name in {'restock_list', 'outbound_list', 'canonical_list'}:
        assert payload['items']
    if name == 'items_detail':
        assert payload[0]['id'] == 1 and payload[0]['current_stock'] == 100
    if name == 'canonical_fanout':
        assert payload['status'] == 'complete' and payload['total_written'] > 0
    if name == 'canonical_claim':
        assert payload['canonical_id'] == 1
    if name == 'canonical_unclaim':
        assert payload['action'] == 'unclaimed'
    if name == 'canonical_update':
        assert payload['name'] == '改名'
    if name == 'canonical_resolve_conflict':
        assert payload['status'] == 'waive'


def test_nullable_fanout_event_and_optional_turnover_shapes(contract_env):
    path, route = contract_env
    set_current_auth(AuthContext(1, ['*'], [], None, read_only=True, warehouse_base_dir=str(path)))
    assert json.loads(asyncio.run(route('canonical_fanout_event', {'event_id': 999}))[0].text) is None
    payload = json.loads(asyncio.run(route('item_consumption', {
        'item_id': 1, 'warehouse_code': 'wh_a', 'include_turnover': True,
    }))[0].text)
    assert 'inventory_turnover' in payload
    validate(payload, next(e.output_shape for e in registry.REGISTRY if e.name == 'item_consumption'))


def test_entry_guide_inventory_and_test_references_reject_drift(tmp_path):
    mcp_contracts.validate_maintenance_references()
    copy_maintenance_files(tmp_path)
    for name in ("AGENT.md", "CLAUDE.md"):
        target = tmp_path / name
        original = target.read_text()
        target.write_text(original.replace('`warehouse_list`', '`warehouse_missing`'))
        with pytest.raises(ValueError, match='inventory names drift'):
            mcp_contracts.validate_maintenance_references(tmp_path)
        target.write_text(original)
    test_file = tmp_path / 'tests/test_mcp_registry.py'
    test_file.write_text(test_file.read_text().replace('def test_registered_tool_real_serialized_shape(', 'def missing_shape_test('))
    with pytest.raises(ValueError, match='test reference drift'):
        mcp_contracts.validate_maintenance_references(tmp_path)


def test_documented_transport_defaults_match_cli_and_systemd(monkeypatch):
    from click.testing import CliRunner

    from mcp_server import main

    observed = []
    monkeypatch.setattr(main, '_run_stdio', lambda: observed.append('stdio'))
    monkeypatch.setattr(main, '_run_http', lambda port, host: observed.append((host, port)))
    runner = CliRunner()
    assert runner.invoke(main.run, []).exit_code == 0
    assert observed.pop() == 'stdio'
    assert runner.invoke(main.run, ['--transport', 'http']).exit_code == 0
    assert observed.pop() == ('127.0.0.1', 5100)
    unit = (ROOT / 'deploy/systemd/dailycheck-mcp.service').read_text()
    assert '--transport http --host 0.0.0.0 --port 5100' in unit
    guide = (ROOT / 'docs/mcp-configuration.md').read_text()
    assert '--transport http' in guide and 'CLI defaults to **stdio**' in guide
    assert main.STREAMABLE_HTTP_PATH == '/api/mcp/' and main.SSE_PATH == '/sse'
    assert main.MESSAGES_PATH == '/messages/' and main.HEALTH_PATH == '/health'


def test_documented_health_auth_and_db_status_use_actual_app(registry_db, monkeypatch):
    from starlette.testclient import TestClient

    from mcp_server import main

    token = secrets.token_urlsafe(24)
    monkeypatch.setenv('DAILYCHECK_MCP_TOKEN', token)
    with TestClient(main._build_app()) as client:
        assert client.get('/health').status_code == 401
        response = client.get('/health', headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 200
        assert response.json() == {'status': 'ok', 'checks': {
            'mcp_server': 'ok', 'transport': 'ok', 'db': 'ok',
        }}
    # Existing behavior: failed DB probe uses 503 but top-level status stays ok.
    import mcp_server.data.unit_of_work as uow
    monkeypatch.setattr(uow, 'MASTER_DB', registry_db / 'missing-parent' / 'master.db')
    with TestClient(main._build_app()) as client:
        response = client.get('/health', headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 503 and response.json()['status'] == 'ok'
        assert response.json()['checks']['db'].startswith('error:')
    guide = (ROOT / 'docs/mcp-configuration.md').read_text()
    assert 'including `/health`' in guide and 'HTTP 503' in guide


@pytest.mark.parametrize('old,new', [
    ('实时 29 项', '合计 27 项'),
    ('`store_order_list` / `store_order_detail`', '未说明订单范围'),
    ('[完整工具台账](./registry.md)', '没有权威台账链接'),
])
def test_calculation_scope_drift_cannot_pass_check(tmp_path, monkeypatch, capsys, old, new):
    copy_maintenance_files(tmp_path)
    for path, expected in mcp_contracts.generated_artifacts().items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(expected)
    monkeypatch.setattr(mcp_contracts, 'ROOT', tmp_path)
    assert mcp_contracts.main(['--check']) == 0
    guide = tmp_path / 'docs/integrations/dailycheck-mcp/calculation-logic.md'
    original = guide.read_text()
    assert old in original
    changed = original.replace(old, new, 1)
    guide.write_text(changed)
    assert mcp_contracts.main(['--check']) == 1
    assert 'calculation scope drift' in capsys.readouterr().out
    assert guide.read_text() == changed
    guide.write_text(original)
    assert mcp_contracts.main(['--check']) == 0


def test_integrated_consumption_acl_metadata_matches_enforced_paths():
    for entry in registry.REGISTRY:
        if entry.domain != 'consumption':
            continue
        assert entry.access.path_enforcement == 'handler'
        tree = ast.parse(inspect.getsource(entry.handler))
        checks = [call for call in ast.walk(tree) if isinstance(call, ast.Call)
                  and isinstance(call.func, ast.Name) and call.func.id == 'check_path']
        assert any(len(call.args) == 3 and ast.literal_eval(call.args[1]) == entry.access.method
                   and ast.literal_eval(call.args[2]) == entry.access.path for call in checks)
    warehouse = next(entry for entry in registry.REGISTRY if entry.name == 'warehouse_list')
    assert warehouse.access.path_enforcement.startswith('none;')
