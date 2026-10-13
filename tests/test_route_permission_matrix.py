"""权限精细化（Phase 3）—— 路由 × 角色 矩阵测试。

覆盖三类（对应 Spec §7 AC-10/AC-11/AC-13）：

  Test A  路由覆盖：`app.url_map` 的每个 endpoint 都在 `ROUTE_MATRIX` 登记，
          且 manifest 无已删路由的残留条目。
  Test B  deny/allow 矩阵：按角色发真实请求，断言关键端点的放行/拦截/
          成本脱敏行为。
  Test C  反倒挂不变式：阶梯包嵌套、旁支 rank=NULL、权限数契约、平台级点
          不进任何仓库角色包、viewer/finance 无写点。

单一事实源：`permissions_manifest`（守卫 / 本测试 / UI 共用）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from permissions_manifest import (
    BUILTIN_PACKAGE_SIZES,
    BUILTIN_ROLE_CODES,
    LADDER_ROLE_CODES,
    PERMISSION_CATALOG,
    PLATFORM_ONLY_PERMS,
    ROLE_META,
    ROLE_PACKAGES,
    ROUTE_MATRIX,
    is_any,
    required_permissions,
)

WRITE_PERMS = {
    "order.create", "order.approve", "order.ship", "order.receive", "order.cancel.any",
    "stock.outbound", "stock.outbound.rollback", "stock.stocktake",
    "stock.stocktake.approve", "stock.adjust", "stock.restock",
    "item.edit", "item.create", "item.delete", "item.price.edit",
}


# ---------------------------------------------------------------------------
# 环境：单 storefront 仓 + 5 个角色用户（admin 为平台超管）
# ---------------------------------------------------------------------------

def _seed_user(m, uid, name, is_admin):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (?, ?, 'x', ?, ?)", (uid, name, is_admin, ts))


@pytest.fixture
def rbac_env(tmp_path, monkeypatch):
    """Return (app, client, wh_path). Users: 1=admin 2=staff 3=manager 4=viewer 5=finance."""
    import config as config_module
    import db as db_module
    from db import init_master_db, init_warehouse_db

    master_path = tmp_path / "master.db"
    wh_dir = tmp_path / "warehouses"
    wh_dir.mkdir()
    wh_path = wh_dir / "wh_test.db"

    monkeypatch.setattr(db_module, "MASTER_DB", master_path)
    monkeypatch.setattr(db_module, "WAREHOUSE_DB_DIR", wh_dir)
    monkeypatch.setattr(config_module, "MASTER_DB", master_path)
    monkeypatch.setattr(config_module, "WAREHOUSE_DB_DIR", wh_dir)

    init_master_db()
    init_warehouse_db(wh_path)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m = sqlite3.connect(master_path)
    _seed_user(m, 1, "admin", 1)
    for uid, role in ((2, "staff"), (3, "manager"), (4, "viewer"), (5, "finance")):
        _seed_user(m, uid, f"{role}_user", 0)
    m.execute(
        "INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
        "VALUES (1, 'wh_test', '测试门店', ?, 'storefront', ?)", (str(wh_path), ts))
    m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1,1,'admin')")
    for uid, role in ((2, "staff"), (3, "manager"), (4, "viewer"), (5, "finance")):
        m.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (?,1,?)",
                  (uid, role))
    m.commit()
    m.close()

    # 一个品项 + 一条出库流水，供成本脱敏测试有数据行。
    w = sqlite3.connect(wh_path)
    w.row_factory = sqlite3.Row
    cat = w.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    w.execute(
        "INSERT INTO items (sku,name,category_id,quantity,safety_stock,unit_cost,unit,updated_at) "
        "VALUES ('T-1','测试品',?,0,0,12.5,'件',?)", (cat, ts))
    iid = w.execute("SELECT id FROM items WHERE sku='T-1'").fetchone()["id"]
    w.execute(
        "INSERT INTO stock_movements (item_id,action,delta,note,created_at) "
        "VALUES (?,'出库',-3,'',?)", (iid, ts))
    w.commit()
    w.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app, app.test_client(), wh_path


def _login(client, uid):
    with client.session_transaction() as s:
        s["user_id"] = uid
        s["warehouse_id"] = 1


def _request(client, app, endpoint, uid, method="GET", url_args=None, **kw):
    from flask import url_for
    _login(client, uid)
    with app.test_request_context():
        url = url_for(endpoint, **(url_args or {}))
    return client.open(url, method=method, **kw)


# ---------------------------------------------------------------------------
# Test A — 路由覆盖（AC-10）
# ---------------------------------------------------------------------------

def _registered_endpoints(app):
    return {r.endpoint for r in app.url_map.iter_rules()}


def test_every_route_is_classified(app_rbac):
    missing = sorted(_registered_endpoints(app_rbac) - set(ROUTE_MATRIX))
    assert not missing, f"以下路由未登记到 ROUTE_MATRIX：{missing}"


def test_matrix_has_no_stale_entries(app_rbac):
    stale = sorted(set(ROUTE_MATRIX) - _registered_endpoints(app_rbac))
    assert not stale, f"ROUTE_MATRIX 含已删除路由：{stale}"


@pytest.fixture
def app_rbac(rbac_env):
    return rbac_env[0]


# ---------------------------------------------------------------------------
# Test B — deny / allow 矩阵（AC-11）
# ---------------------------------------------------------------------------

# (endpoint, method, uid, expect_denied, url_args)
_MATRIX_CASES = [
    # —— 平台级（PLATFORM）：非超管一律 403 ——
    ("items.items_list", "GET", 2, True, None),           # 终裁③ 维持平台级
    ("items.items_list", "GET", 3, True, None),
    ("items.delete_item", "POST", 3, True, {"item_id": 1}),  # 终裁② 维持平台级
    ("items.edit_item", "GET", 2, True, {"item_id": 1}),
    ("agent_tokens.list_tokens", "GET", 2, True, None),
    ("users.list_users", "GET", 2, True, None),
    ("canonical.canonical_edit", "GET", 3, True, None),
    # —— 权限点收紧 ——
    ("stocktake.approve", "POST", 2, True, {"batch_id": 1}),   # 终裁①：staff 失去审批
    ("stocktake.approve", "POST", 3, False, {"batch_id": 1}),  # manager 保留
    # —— viewer 无写点 ——
    ("outbound.outbound_submit", "POST", 4, True, None),
    ("restock.restock_submit", "POST", 4, True, None),
    ("store_ordering.submit_order_route", "POST", 4, True, None),
    ("store_ordering.cancel_order_route", "POST", 4, True, {"order_id": 1}),
    ("reports.export_consumption", "GET", 4, False, None),   # viewer 有 report.export
    # —— 显式开放 / 等价迁移：staff 放行 ——
    ("outbound.outbound_submit", "POST", 2, False, None),
    ("reports.outbound", "GET", 2, False, None),
    ("reports.export_consumption", "GET", 2, False, None),
    ("reports.export_summary", "GET", 5, False, None),
    ("items.new_item_redirect", "GET", 3, False, None),
    ("store_ordering.cancel_order_route", "POST", 2, False, {"order_id": 1}),  # owner 自撤
    # —— 平台超管直通 ——
    ("items.items_list", "GET", 1, False, None),
]


@pytest.mark.parametrize("endpoint,method,uid,expect_denied,url_args", _MATRIX_CASES)
def test_route_permission_matrix(rbac_env, endpoint, method, uid, expect_denied, url_args):
    app, client, _ = rbac_env
    resp = _request(client, app, endpoint, uid, method=method, url_args=url_args)
    if expect_denied:
        assert resp.status_code == 403, (
            f"{endpoint} [{method}] uid={uid} 期望 403，实得 {resp.status_code}")
    else:
        assert resp.status_code < 500 and resp.status_code != 403, (
            f"{endpoint} [{method}] uid={uid} 不应被拦，实得 {resp.status_code}")


def test_viewer_all_writes_403(rbac_env):
    """viewer 对全部「写点」端点应 403（AC-07）。"""
    app, client, _ = rbac_env
    viewer_perms = ROLE_PACKAGES["viewer"]
    assert not (viewer_perms & WRITE_PERMS)
    write_cases = [
        ("outbound.outbound_submit", "POST", None),
        ("restock.restock_submit", "POST", None),
        ("restock.update_status", "POST", {"req_id": 1}),
        ("stocktake.stocktake_submit", "POST", None),
        ("store_ordering.submit_order_route", "POST", None),
    ]
    for endpoint, method, url_args in write_cases:
        resp = _request(client, app, endpoint, 4, method=method, url_args=url_args)
        assert resp.status_code == 403, f"viewer 不应写入 {endpoint}"


def test_cancel_allows_order_owner(rbac_env):
    """order owner（staff，持 order.create）撤自己的单不得被守卫拦（AC-11）。"""
    app, client, _ = rbac_env
    resp = _request(client, app, "store_ordering.cancel_order_route", 2,
                    method="POST", url_args={"order_id": 1})
    assert resp.status_code != 403


def test_staff_cost_masked_finance_sees_cost(rbac_env):
    """成本脱敏：staff 无 report.cost.view → CSV 成本列留空；finance 可见（AC-04/AC-07）。"""
    app, client, _ = rbac_env

    _login(client, 2)  # staff
    resp = client.get("/export/consumption")
    assert resp.status_code == 200
    lines = resp.get_data(as_text=True).splitlines()
    assert len(lines) >= 2
    staff_row = lines[1].split(",")
    assert staff_row[4] == "" and staff_row[5] == "", f"staff 成本列应留空：{staff_row}"

    _login(client, 5)  # finance
    resp = client.get("/export/consumption")
    assert resp.status_code == 200
    fin_row = resp.get_data(as_text=True).splitlines()[1].split(",")
    assert fin_row[4] != "" and fin_row[5] != "", f"finance 应见成本：{fin_row}"


def test_admin_perms_gate_staff(rbac_env):
    """admin.* 名义权限：staff 触及的平台级端点一律 403。"""
    app, client, _ = rbac_env
    for endpoint in ("items.items_list", "items.delete_item", "users.list_users"):
        url_args = {"item_id": 1} if endpoint == "items.delete_item" else None
        method = "POST" if endpoint == "items.delete_item" else "GET"
        resp = _request(client, app, endpoint, 2, method=method, url_args=url_args)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test C — 反倒挂不变式（AC-13）
# ---------------------------------------------------------------------------

def test_ladder_packages_nested():
    """仅阶梯三包 staff ⊆ manager ⊆ admin。"""
    assert ROLE_PACKAGES["staff"] <= ROLE_PACKAGES["manager"] <= ROLE_PACKAGES["admin"]


def test_viewer_finance_are_side_branches():
    for code in ("viewer", "finance"):
        assert code not in LADDER_ROLE_CODES
        assert ROLE_META[code][1] is None, f"{code} 的 rank 应为 None（旁支）"


def test_builtin_package_sizes():
    actual = {code: len(ROLE_PACKAGES[code]) for code in BUILTIN_ROLE_CODES}
    assert actual == BUILTIN_PACKAGE_SIZES
    assert BUILTIN_PACKAGE_SIZES == {
        "staff": 10, "manager": 17, "admin": 18, "viewer": 3, "finance": 4}


def test_no_empty_builtin_package():
    for code in BUILTIN_ROLE_CODES:
        assert ROLE_PACKAGES[code], f"空权限包：{code}"


def test_admin_perms_not_in_any_package():
    for code in BUILTIN_ROLE_CODES:
        assert not (ROLE_PACKAGES[code] & PLATFORM_ONLY_PERMS), f"{code} 含平台级点"
    # order.cancel.any / item.delete 亦不属任何仓库角色包
    for code in BUILTIN_ROLE_CODES:
        assert "order.cancel.any" not in ROLE_PACKAGES[code]
        assert "item.delete" not in ROLE_PACKAGES[code]


def test_sensitive_perms_not_in_staff():
    sensitive = {
        "item.delete", "item.price.edit", "admin.user.manage", "admin.token.manage",
        "report.cost.view", "stock.stocktake.approve",
    }
    assert not (sensitive & ROLE_PACKAGES["staff"])


def test_viewer_finance_have_no_write_perms():
    for code in ("viewer", "finance"):
        assert not (ROLE_PACKAGES[code] & WRITE_PERMS), f"{code} 不应含写点"


def test_catalog_covers_all_packages():
    """包内每个 code 都在权限点目录中（无孤儿）。"""
    catalog = set(PERMISSION_CATALOG)
    for code in BUILTIN_ROLE_CODES:
        orphan = ROLE_PACKAGES[code] - catalog
        assert not orphan, f"{code} 含未登记 code：{orphan}"


def test_required_permissions_parsing():
    assert required_permissions("perm:order.create") == frozenset({"order.create"})
    assert required_permissions("any:a|b") == frozenset({"a", "b"})
    assert required_permissions("dc>perm:order.approve") == frozenset({"order.approve"})
    assert required_permissions("login") == frozenset()
    assert is_any("any:a|b")
    assert not is_any("perm:a")
