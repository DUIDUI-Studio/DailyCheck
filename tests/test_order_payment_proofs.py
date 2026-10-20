"""付款凭证（Issue #135）：纯函数边界 + 落库 + 下载越权。

覆盖：
- validate_upload：扩展名白名单 / 空文件 / 8MB 上限
- store_upload：uuid 重命名、原名保留、路径穿越被切断
- proof_path：库被改脏时拒绝跳出订单目录
- insert/list/count/get：按订单隔离
- HTTP：本店可下载、他店 403、admin 可下载、跨单 proof_id 404
"""
from __future__ import annotations

import io
import sqlite3
from datetime import datetime

import pytest

from blueprints import payment_proofs_pure as ppp
from db import MASTER_SCHEMA


class TestValidateUpload:
    def test_accepts_supported_types(self):
        for name in ("a.png", "b.JPG", "c.webp", "d.Pdf"):
            assert ppp.validate_upload(name, 1024) is None

    def test_rejects_other_types(self):
        err = ppp.validate_upload("shell.exe", 10)
        assert err and "仅支持" in err

    def test_rejects_empty(self):
        assert ppp.validate_upload("a.png", 0) == "文件为空"

    def test_rejects_oversize(self):
        err = ppp.validate_upload("a.png", ppp.MAX_FILE_BYTES + 1)
        assert err and "8MB" in err


class TestStoreUpload:
    def test_renames_and_keeps_original(self, tmp_path):
        meta = ppp.store_upload(io.BytesIO(b"payload"), "../../凭证.png", tmp_path, 7)
        assert meta["size_bytes"] == 7
        assert meta["mime_type"] == "image/png"
        assert meta["original_name"] == "凭证.png"
        # 落盘名必须是生成的 uuid，不能带用户给的目录片段
        assert "/" not in meta["stored_name"] and ".." not in meta["stored_name"]
        assert (tmp_path / "7" / meta["stored_name"]).read_bytes() == b"payload"

    def test_rejects_traversal_in_proof_path(self, tmp_path):
        assert ppp.proof_path(tmp_path, 7, "x.png").name == "x.png"
        with pytest.raises(ValueError):
            ppp.proof_path(tmp_path, 7, "../../etc/passwd")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(MASTER_SCHEMA)
    yield c
    c.close()


class TestProofRows:
    def test_insert_list_count_get(self, conn):
        meta = {
            "stored_name": "a.png", "original_name": "a.png",
            "mime_type": "image/png", "size_bytes": 3,
        }
        pid = ppp.insert_proof(conn, 1, meta, 9, "2026-10-20 20:00:00")
        assert ppp.count_proofs(conn, 1) == 1
        rows = ppp.list_proofs(conn, 1)
        assert rows[0]["id"] == pid and rows[0]["size_bytes"] == 3
        assert ppp.get_proof(conn, 1, pid)["stored_name"] == "a.png"
        # 跨订单取同一个 proof_id 不得命中
        assert ppp.get_proof(conn, 2, pid) is None


class TestViewHelpers:
    """视图侧 helper：确认 multipart 字段名接线正确、数量与落盘闭环。"""

    def test_multipart_field_is_collected_and_validated(self):
        from flask import Flask

        from blueprints.store_ordering import (
            _collect_payment_proofs,
            _validate_payment_proofs,
        )

        app = Flask(__name__)
        with app.test_request_context(
            "/store-ordering/cart/submit", method="POST",
            data={"payment_proofs": (io.BytesIO(b"png-bytes"), "凭证.png")},
            content_type="multipart/form-data",
        ):
            files = _collect_payment_proofs()
            assert [f.filename for f in files] == ["凭证.png"]
            assert _validate_payment_proofs(files) is None

    def test_more_than_three_rejected(self):
        from flask import Flask

        from blueprints.store_ordering import (
            _collect_payment_proofs,
            _validate_payment_proofs,
        )

        app = Flask(__name__)
        data = {"payment_proofs": [(io.BytesIO(b"x"), f"p{i}.png") for i in range(4)]}
        with app.test_request_context(
            "/x", method="POST", data=data, content_type="multipart/form-data",
        ):
            err = _validate_payment_proofs(_collect_payment_proofs())
            assert err and "最多上传 3 个" in err

    def test_save_writes_file_and_row(self, conn, tmp_path, monkeypatch):
        from flask import Flask, g

        import config as config_module
        from blueprints.store_ordering import (
            _collect_payment_proofs,
            _save_payment_proofs,
        )

        monkeypatch.setattr(config_module, "PAYMENT_PROOF_DIR", tmp_path / "proofs")
        app = Flask(__name__)
        with app.test_request_context(
            "/x", method="POST",
            data={"payment_proofs": (io.BytesIO(b"abc"), "a.png")},
            content_type="multipart/form-data",
        ):
            g.user = {"id": 5}
            _save_payment_proofs(conn, 42, _collect_payment_proofs())

        rows = ppp.list_proofs(conn, 42)
        assert len(rows) == 1 and rows[0]["original_name"] == "a.png"
        stored = tmp_path / "proofs" / "42" / rows[0]["stored_name"]
        assert stored.read_bytes() == b"abc"


@pytest.fixture
def proof_env(tmp_path, monkeypatch):
    """master + 门店/他店/DC 三仓 + 一张订单 + 一条凭证。"""
    import config as config_module
    import db as db_module

    master = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    store_db = wh_dir / "store.db"
    other_db = wh_dir / "other.db"
    dc_db = wh_dir / "dc.db"
    proof_dir = tmp_path / "proofs"

    monkeypatch.setattr(db_module, "MASTER_DB", master)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "PAYMENT_PROOF_DIR", proof_dir)

    from db import init_master_db, init_warehouse_db

    init_master_db()
    for path in (store_db, other_db, dc_db):
        init_warehouse_db(path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master)
    m.row_factory = sqlite3.Row
    # user 1 = 下单门店 staff；user 2 = 另一门店的平台管理员
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'store1', 'x', 0, ?)", (ts,))
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (2, 'boss', 'x', 1, ?)", (ts,))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (1, 'store_a', '门店A', ?, 'storefront', ?)", (str(store_db), ts))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (2, 'store_b', '门店B', ?, 'storefront', ?)", (str(other_db), ts))
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (3, 'dc_a', '配送中心', ?, 'distribution_center', ?)", (str(dc_db), ts))
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 1, 'staff')")
    # 门店A 的 staff 同时被授权进门店B —— 用于验证「仓库切换后仍不能看别店订单」
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1, 2, 'staff')")
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (2, 2, 'admin')")
    cur = m.execute(
        """INSERT INTO store_orders
           (order_no, store_warehouse_code, dc_warehouse_code, status,
            requested_by, created_at, updated_at)
           VALUES ('SO-TEST-1', 'store_a', 'dc_a', 'pending', 1, ?, ?)""",
        (ts, ts),
    )
    order_id = int(cur.lastrowid)
    meta = ppp.store_upload(io.BytesIO(b"proof-bytes"), "付款.png", proof_dir, order_id)
    proof_id = ppp.insert_proof(m, order_id, meta, 1, ts)
    m.commit()
    m.close()

    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app.test_client(), order_id, proof_id


def _login(client, user_id: int, warehouse_id: int):
    with client.session_transaction() as s:
        s["user_id"] = user_id
        s["warehouse_id"] = warehouse_id


class TestDownloadRoute:
    def test_owner_store_can_download(self, proof_env):
        client, order_id, proof_id = proof_env
        _login(client, 1, 1)
        r = client.get(f"/store-ordering/orders/{order_id}/payment-proofs/{proof_id}")
        assert r.status_code == 200
        assert r.data == b"proof-bytes"
        assert r.mimetype == "image/png"

    def test_other_store_forbidden(self, proof_env):
        client, order_id, proof_id = proof_env
        _login(client, 1, 2)  # 门店B 的会话，非本单门店
        r = client.get(f"/store-ordering/orders/{order_id}/payment-proofs/{proof_id}")
        assert r.status_code == 403

    def test_admin_from_other_store_allowed(self, proof_env):
        client, order_id, proof_id = proof_env
        _login(client, 2, 2)
        r = client.get(f"/store-ordering/orders/{order_id}/payment-proofs/{proof_id}")
        assert r.status_code == 200

    def test_unknown_proof_is_404(self, proof_env):
        client, order_id, _ = proof_env
        _login(client, 1, 1)
        r = client.get(f"/store-ordering/orders/{order_id}/payment-proofs/9999")
        assert r.status_code == 404

    def test_anonymous_redirected(self, proof_env):
        client, order_id, proof_id = proof_env
        r = client.get(f"/store-ordering/orders/{order_id}/payment-proofs/{proof_id}")
        assert r.status_code == 302
