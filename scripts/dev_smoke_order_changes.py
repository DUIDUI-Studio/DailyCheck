#!/usr/bin/env python3
"""Repeatable #144 smoke: explicit disposable DB root, loopback HTTP, no external channels.

Requires the test dependencies. --serve retains the isolated HTTP server for browser QA.
Never points at a shared application/database or localhost:8080.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_relative_to(Path('/tmp').resolve()) or not root.name.startswith('dailycheck-order-change-'):
        parser.error('root must be a dedicated /tmp/dailycheck-order-change-* directory')
    if root.exists() and any(root.iterdir()):
        parser.error('root must be empty; choose a fresh disposable directory')
    root.mkdir(parents=True, exist_ok=True)
    os.environ['DAILYCHECK_DB_DIR'] = str(root)
    os.environ['DAILYCHECK_ORDER_CHANGES'] = 'false'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import pytest
    from werkzeug.serving import make_server

    import config
    from blueprints import store_ordering_pure as sop
    from tests import multi_warehouse_env as builder
    patches = pytest.MonkeyPatch()
    env = builder.build(root, patches)
    builder.enable(patches)
    app = env['app']
    app.config['WTF_CSRF_FORCE_ENABLED'] = True
    assert config.MASTER_DB.resolve() == root / 'master.db'
    assert config.WAREHOUSE_DB_DIR.resolve() == root / 'warehouses'
    with builder.connect(env['paths']['dc_alpha']) as wh:
        wh.execute('UPDATE items SET selling_price=.335,unit_cost=99 WHERE canonical_id=201')
    with builder.connect(env['master_path']) as master:
        master.execute("INSERT INTO users(id,username,password_hash,is_admin,created_at) VALUES(4,'smoke_admin','disabled-login',1,?)", (builder.TS,))
        master.commit()
        sop.upsert_shipping_rule(master, base_fee=.005, pct_fee=0)
        cart = sop.get_or_create_cart(master, 1, 'store_one', None)
        for cid, qty in ((201, 3), (203, 2), (202, 5)):
            sop.add_cart_item(master, cart['id'], cid, qty, '件')
        order = sop.submit_order(master, cart['id'], 1, None, '隔离HTTP与移动端验证')
        sop.review_order(master, order['id'], 'approved', 4)
        ids = {line['canonical_id']: line['id'] for line in order['order_items']}
        sop.ship_partial(master, sop.open_warehouse_db, order_id=order['id'], operator_dc_code='dc_alpha', op_no='smoke-pre-ship', line_qty={ids[201]: 1}, shipped_by=2)
        proposal_id = master.execute("""INSERT INTO store_order_change_proposals
            (order_id,sequence,status,base_order_change_revision,proposed_by,reason,snapshot_json,required_warehouses_json,created_at)
            VALUES (?,1,'pending',1,4,'缺货，建议取消未发部分',?, '["dc_alpha","dc_beta"]',?)""",
            (order['id'], json.dumps({'version': 1, 'before': {'goods_amount':'14.51','shipping_fee':'0.01','total_amount':'14.52'},
                                      'after': {'goods_amount':'13.84','shipping_fee':'0.01','total_amount':'13.85'}}), builder.TS)).lastrowid
        master.execute("""INSERT INTO store_order_change_lines
            (proposal_id,order_id,order_item_id,operation,base_line_revision,baseline_quantity,baseline_shipped,remove_quantity)
            VALUES (?,?,?,'cancel',0,3,1,2)""", (proposal_id, order['id'], ids[201]))
        # Genuine old row with no known commercial price; no fake zero backfill.
        old = master.execute("INSERT INTO store_orders(order_no,store_warehouse_code,dc_warehouse_code,status,requested_by,created_at,updated_at) VALUES('SO-LEGACY','store_one','dc_alpha','pending',1,?,?)", (builder.TS, builder.TS)).lastrowid
        master.execute("INSERT INTO store_order_items(order_id,canonical_id,quantity,unit,source_warehouse_code,created_at) VALUES(?,201,2,'件','dc_alpha',?)", (old, builder.TS))
    with builder.connect(env['paths']['dc_alpha']) as wh:
        wh.execute('UPDATE items SET selling_price=80,unit_cost=90,quantity=7 WHERE canonical_id=201')
    serializer = app.session_interface.get_signing_serializer(app)
    cookies = {name: serializer.dumps({'user_id': user, 'warehouse_id': warehouse})
               for name, user, warehouse in [('dc', 4, 1), ('store', 4, 3)]}
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    metadata = {'base_url': base, 'db_root': str(root), 'master_db': str(config.MASTER_DB),
                'order_id': order['id'], 'proposal_id': proposal_id, 'legacy_order_id': old,
                'line_ids': ids, 'cookies': cookies}
    (root / 'browser-seed.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))

    external_attempts = []
    original_urlopen = urllib.request.urlopen

    def loopback_only(req, *positional, **keywords):
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        if not url.startswith(base + '/'):
            external_attempts.append(url)
            raise RuntimeError('isolated smoke refuses external HTTP')
        return original_urlopen(req, *positional, **keywords)
    patches.setattr(urllib.request, 'urlopen', loopback_only)

    def request(path, actor='dc', data=None, ajax=False):
        import urllib.parse
        headers = {'Cookie': 'session=' + cookies[actor]}
        if ajax:
            headers['X-Requested-With'] = 'fetch'
        req = urllib.request.Request(base + path, data=urllib.parse.urlencode(data).encode() if data is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                from http.cookies import SimpleCookie
                for header in response.headers.get_all('Set-Cookie', []):
                    updated = SimpleCookie(header)
                    if 'session' in updated:
                        cookies[actor] = updated['session'].value
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()
    detail = f"/store-ordering/orders/{order['id']}"
    proposal_url = detail + f'/changes/{proposal_id}'
    status, html = request(detail)
    assert status == 200 and '14.52' in html and '待确认' in html
    (root / 'order-detail.html').write_text(html)
    csrf = re.search('name="csrf_token" value="([^"]+)"', html).group(1)
    missing_csrf, _ = request(detail + '/ship', data={'op_no': 'smoke-csrf'})
    assert missing_csrf == 400
    with sqlite3.connect(config.WAREHOUSE_DB_DIR / 'dc_alpha.db') as warehouse:
        stock_before = warehouse.execute('SELECT quantity FROM items WHERE canonical_id=201').fetchone()[0]
    blocked_status, blocked_body = request(detail + '/ship', data={'csrf_token': csrf, 'op_no':'smoke-blocked', f'shipped_items[{ids[201]}]':1, f'shipped_items[{ids[203]}]':1}, ajax=True)
    assert blocked_status == 409 and json.loads(blocked_body)['ok'] is False
    with sqlite3.connect(config.WAREHOUSE_DB_DIR / 'dc_alpha.db') as warehouse:
        stock_after = warehouse.execute('SELECT quantity FROM items WHERE canonical_id=201').fetchone()[0]
    assert stock_before == stock_after == 7
    assert request(proposal_url)[0] == 200
    old_status, old_body = request(f'/store-ordering/orders/{old}')
    assert old_status == 200 and '待核对' in old_body and '当前参考价' in old_body
    (root / 'legacy-detail.html').write_text(old_body)
    summary = {'db_root': str(root), 'base_url': base, 'csrf_missing': missing_csrf,
               'detail_http': status, 'blocked_ship_http': blocked_status,
               'blocked_ship_body': json.loads(blocked_body), 'stock_before': stock_before,
               'stock_after': stock_after, 'snapshot_total': order['total_amount'],
               'proposal_http': 200, 'legacy_unknown_http': old_status, 'external_http_attempts': len(external_attempts)}
    (root / 'http-evidence.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    try:
        if args.serve:
            thread.join()
    finally:
        server.shutdown()
        server.server_close()
        patches.undo()


if __name__ == '__main__':
    main()
