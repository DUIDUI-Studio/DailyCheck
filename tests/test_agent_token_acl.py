"""Synthetic admin forms: default-deny, server review, multiple warehouses, no ACL expansion."""
import html
import json
import re
import sqlite3

import pytest
from werkzeug.datastructures import MultiDict


@pytest.fixture
def admin(logged_client, monkeypatch, tmp_path):
    import blueprints.audit_pure as audit
    import config

    monkeypatch.setattr(audit, 'ADMIN_AUDIT_LOG', tmp_path / 'admin-audit.log')
    client, _ = logged_client
    with sqlite3.connect(config.MASTER_DB) as conn:
        conn.execute(
            "INSERT INTO warehouses (code, name, db_path, created_at) "
            "VALUES ('second', 'Second', 'unused.db', datetime('now'))"
        )
    return client, config.MASTER_DB, tmp_path / 'admin-audit.log'


def _rows(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute('SELECT * FROM agent_tokens ORDER BY id')]


def _preview(client, data):
    response = client.post('/admin/agent-tokens/create', data=data)
    assert response.status_code == 200
    return html.unescape(re.search(r'name="review" value="([^"]+)"', response.text)[1])


def _create(client, data, **confirmation):
    ticket = _preview(client, data)
    return client.post('/admin/agent-tokens/create', data={'review': ticket, **confirmation})


def test_defaults_and_review_do_not_create_or_mutate(admin):
    client, path, log = admin
    page = client.get('/admin/agent-tokens').text
    assert 'name="read_paths"' in page and 'name="write_paths"' in page
    assert 'value="none" selected' in page
    before = _rows(path)
    ticket = _preview(client, {'name': 'deny', 'warehouse_scope': 'none'})
    assert _rows(path) == before
    assert not log.exists()
    assert client.post('/admin/agent-tokens/create', data={'review': ticket}).status_code == 302
    row = _rows(path)[0]
    assert json.loads(row['allowed_read_paths_json']) == []
    assert json.loads(row['allowed_write_paths_json']) == []
    assert json.loads(row['allowed_warehouse_codes_json']) == []
    assert 'read=[] write=[] warehouses=[]' in log.read_text()


def test_multiselect_round_trip_and_existing_acl_unchanged(admin):
    client, path, _ = admin
    assert _create(client, {'name': 'old', 'warehouse_scope': 'none'}).status_code == 302
    before = _rows(path)
    data = MultiDict([
        ('name', 'multi'), ('read_paths', '/api/v1/items,/api/v1/warehouses'),
        ('write_paths', '/api/v1/restock'), ('warehouse_scope', 'selected'),
        ('warehouses', 'wh_test'), ('warehouses', 'second'),
    ])
    assert _create(client, data).status_code == 302
    rows = _rows(path)
    assert rows[:1] == before
    assert json.loads(rows[1]['allowed_warehouse_codes_json']) == ['wh_test', 'second']
    page = client.get('/admin/agent-tokens').text
    assert '/api/v1/restock' in page and '/api/v1/warehouses' in page and 'second' in page
    assert client.post(f"/admin/agent-tokens/{rows[1]['id']}/revoke").status_code == 302
    assert _rows(path)[1]['revoked_at'] is not None
    assert _rows(path)[:1] == before


@pytest.mark.parametrize('fields', [
    {}, {'warehouses': ''}, {'warehouse_scope': 'all'},
])
def test_empty_legacy_or_explicit_all_means_null_requires_confirmation(admin, fields):
    client, path, _ = admin
    ticket = _preview(client, {'name': 'all', **fields})
    assert client.post('/admin/agent-tokens/create', data={'review': ticket}).status_code == 400
    assert _rows(path) == []
    assert client.post('/admin/agent-tokens/create', data={
        'review': ticket, 'confirm_broad': 'yes',
    }).status_code == 302
    assert json.loads(_rows(path)[0]['allowed_warehouse_codes_json']) is None


@pytest.mark.parametrize('fields', [
    {'read_paths': 'items'}, {'write_paths': '/api/v1/../bad'},
    {'read_paths': '/api/v1/it*ems'}, {'warehouses': 'missing'},
    {'warehouse_scope': 'selected'}, {'warehouse_scope': 'all', 'warehouses': 'wh_test'},
    {'warehouse_scope': 'none', 'warehouses': 'wh_test'},
])
def test_invalid_acl_creates_nothing(admin, fields):
    client, path, _ = admin
    response = client.post('/admin/agent-tokens/create', data={'name': 'bad', **fields})
    assert response.status_code == 400
    assert _rows(path) == []


def test_unsigned_fields_cannot_expand_reviewed_acl(admin):
    client, path, _ = admin
    ticket = _preview(client, {'name': 'narrow', 'warehouse_scope': 'none'})
    assert client.post('/admin/agent-tokens/create', data={
        'review': ticket + 'bad', 'read_paths': '*',
    }).status_code == 400
    assert client.post('/admin/agent-tokens/create', data={
        'review': ticket, 'read_paths': '*', 'write_paths': '*', 'warehouse_scope': 'all',
    }).status_code == 302
    row = _rows(path)[0]
    assert row['allowed_read_paths_json'] == '[]'
    assert row['allowed_write_paths_json'] == '[]'
    assert row['allowed_warehouse_codes_json'] == '[]'


def test_wildcard_requires_explicit_confirmation(admin):
    client, path, _ = admin
    ticket = _preview(client, {'name': 'broad', 'warehouse_scope': 'none', 'read_paths': '*'})
    assert client.post('/admin/agent-tokens/create', data={'review': ticket}).status_code == 400
    assert not _rows(path)


def test_create_and_confirm_are_admin_only(staff_client):
    client, _ = staff_client
    assert client.post('/admin/agent-tokens/create', data={'name': 'bad'}).status_code == 403
    assert client.post('/admin/agent-tokens/create', data={'review': 'bad'}).status_code == 403


def test_csrf_required_for_preview_and_confirm(admin):
    client, path, _ = admin
    client.application.config['WTF_CSRF_FORCE_ENABLED'] = True
    assert client.post('/admin/agent-tokens/create', data={'name': 'bad'}).status_code == 400
    assert client.post('/admin/agent-tokens/create', data={'review': 'bad'}).status_code == 400
    assert not _rows(path)


def test_review_expires_and_is_bound_to_admin(admin, monkeypatch):
    import blueprints.agent_tokens as tokens

    client, path, _ = admin
    with client.application.app_context():
        original = tokens._review_signer()
    acl = dict(name='expired', read='[]', write='[]', warehouses='[]', actor=1)
    with monkeypatch.context() as scoped:
        scoped.setattr('itsdangerous.timed.time.time', lambda: 1)
        expired = original.dumps(acl)
    assert client.post('/admin/agent-tokens/create', data={'review': expired}).status_code == 400
    foreign = original.dumps(dict(name='foreign', read='[]', write='[]', warehouses='[]', actor=999))
    assert client.post('/admin/agent-tokens/create', data={'review': foreign}).status_code == 403
    assert not _rows(path)
