"""租户 1 的显式履约路由；连接事务由调用方管理，播种仅由迁移显式执行。"""
from __future__ import annotations

import sqlite3
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime
from pathlib import Path


def _dc_rows(master_conn):
    return master_conn.execute(
        "SELECT code, db_path FROM warehouses "
        "WHERE tenant_id=1 AND warehouse_type='distribution_center' ORDER BY code"
    ).fetchall()


@contextmanager
def routing_warehouse_connections(master_conn):
    """只读打开所有合格 DC；缺库/坏库须显式失败，不能把不完整扫描当唯一候选。"""
    from config import BASE_DIR

    with ExitStack() as stack:
        result = {}
        for code, db_path in _dc_rows(master_conn):
            path = Path(db_path)
            if not path.is_absolute():
                path = BASE_DIR / path
            conn = stack.enter_context(closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                                             uri=True)))
            result[code] = conn
        yield result


def _classify(master_conn, wh_db_map):
    canonical_ids = [row[0] for row in master_conn.execute(
        "SELECT id FROM canonical_items WHERE tenant_id=1 AND status='active' ORDER BY id")]
    candidates = {cid: set() for cid in canonical_ids}
    exclusive = set()
    for code, _ in _dc_rows(master_conn):
        if code not in wh_db_map:
            raise ValueError(f"缺少配送中心数据库：{code}")
        for cid, is_exclusive in wh_db_map[code].execute(
            "SELECT canonical_id, is_store_exclusive FROM items "
            "WHERE canonical_id IS NOT NULL AND is_active=1 AND is_alias=0"
        ):
            if cid not in candidates:
                continue
            if is_exclusive:
                exclusive.add(cid)
            else:
                candidates[cid].add(code)
    return candidates, exclusive


def seed_routing(master_conn, wh_db_map: dict[str, sqlite3.Connection]) -> dict:
    """一次性播种；已有配置原样保留，返回本次启用及待人工处理的审计清单。"""
    result = {"enabled": [], "ambiguous": [], "missing": [], "skipped_exclusive": []}
    candidates, exclusive = _classify(master_conn, wh_db_map)
    existing = {row[0] for row in master_conn.execute(
        "SELECT canonical_id FROM item_routing WHERE tenant_id=1")}
    for cid, codes in candidates.items():
        if cid in existing:
            continue
        if len(codes) == 1:
            code = next(iter(codes))
            changed = master_conn.execute(
                "INSERT OR IGNORE INTO item_routing "
                "(tenant_id, canonical_id, fulfillment_warehouse_code, updated_at, updated_by) "
                "VALUES (1, ?, ?, ?, NULL)", (cid, code, _now())
            ).rowcount
            if changed:
                result["enabled"].append((cid, code))
        elif len(codes) > 1:
            result["ambiguous"].append(cid)
        elif cid in exclusive:
            result["skipped_exclusive"].append(cid)
        else:
            result["missing"].append(cid)
    return result


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _assert_canonical(master_conn, canonical_id):
    if master_conn.execute(
        "SELECT 1 FROM canonical_items WHERE id=? AND tenant_id=1", (canonical_id,)
    ).fetchone() is None:
        raise ValueError("品项不存在或不属于租户 1")


def list_routing(master_conn) -> list[dict]:
    cursor = master_conn.execute(
        "SELECT r.*, c.canonical_sku, c.name, c.status, w.name AS warehouse_name "
        "FROM item_routing r JOIN canonical_items c ON c.id=r.canonical_id AND c.tenant_id=1 "
        "JOIN warehouses w ON w.code=r.fulfillment_warehouse_code "
        "WHERE r.tenant_id=1 ORDER BY r.canonical_id")
    keys = [col[0] for col in cursor.description]
    return [dict(zip(keys, row, strict=True)) for row in cursor]


def set_routing(master_conn, canonical_id, dc_code, *, actor_id) -> None:
    _assert_canonical(master_conn, canonical_id)
    if master_conn.execute(
        "SELECT 1 FROM warehouses WHERE code=? AND tenant_id=1 "
        "AND warehouse_type='distribution_center'", (dc_code,)
    ).fetchone() is None:
        raise ValueError("履约仓必须为租户 1 的配送中心")
    master_conn.execute(
        "INSERT INTO item_routing "
        "(tenant_id, canonical_id, fulfillment_warehouse_code, updated_at, updated_by) "
        "VALUES (1, ?, ?, ?, ?) ON CONFLICT(tenant_id, canonical_id) DO UPDATE SET "
        "fulfillment_warehouse_code=excluded.fulfillment_warehouse_code, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (canonical_id, dc_code, _now(), actor_id))


def delete_routing(master_conn, canonical_id, *, actor_id):
    """删除显式路由（不修改历史订单）；操作者审计由 HTTP 层现有日志承载。"""
    _assert_canonical(master_conn, canonical_id)
    master_conn.execute("DELETE FROM item_routing WHERE tenant_id=1 AND canonical_id=?",
                        (canonical_id,))


def get_routing(master_conn, canonical_id) -> str | None:
    row = master_conn.execute(
        "SELECT r.fulfillment_warehouse_code FROM item_routing r "
        "JOIN canonical_items c ON c.id=r.canonical_id AND c.tenant_id=1 "
        "WHERE r.tenant_id=1 AND r.canonical_id=?", (canonical_id,)).fetchone()
    return row[0] if row else None


def routing_pending(master_conn, *, wh_db_map: dict | None = None) -> dict:
    """动态只读诊断；新增唯一绑定仍待管理员显式配置，绝不补种。"""
    if wh_db_map is None:
        with routing_warehouse_connections(master_conn) as connections:
            return routing_pending(master_conn, wh_db_map=connections)
    candidates, exclusive = _classify(master_conn, wh_db_map)
    existing = {row[0] for row in master_conn.execute(
        "SELECT canonical_id FROM item_routing WHERE tenant_id=1")}
    result = {"ambiguous": [], "missing": []}
    for cid, codes in candidates.items():
        if cid in existing or (not codes and cid in exclusive):
            continue
        result["ambiguous" if len(codes) > 1 else "missing"].append(cid)
    return result
