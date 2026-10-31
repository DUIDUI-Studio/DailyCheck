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


def _assert_dc(master_conn, dc_code):
    if master_conn.execute(
        "SELECT 1 FROM warehouses WHERE code=? AND tenant_id=1 "
        "AND warehouse_type='distribution_center'", (dc_code,)
    ).fetchone() is None:
        raise ValueError("履约仓必须为租户 1 的配送中心")


def set_routing(master_conn, canonical_id, dc_code, *, actor_id) -> None:
    _assert_canonical(master_conn, canonical_id)
    _assert_dc(master_conn, dc_code)
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


class BatchRoutingError(ValueError):
    """整批拒绝；errors 使用 canonical_id 的字符串键供界面定位。"""

    def __init__(self, message, *, errors=None, status=400):
        super().__init__(message)
        self.errors = errors or {}
        self.status = status


def parse_batch(payload):
    """只接受明确的差异与原值，不容许 bool 被当成整数 ID。"""
    if not isinstance(payload, dict) or set(payload) != {"changes"}:
        raise BatchRoutingError("请求必须包含 changes 数组")
    changes = payload["changes"]
    if not isinstance(changes, list) or not 1 <= len(changes) <= 200:
        raise BatchRoutingError("每批必须包含 1–200 项变更")
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {
            "canonical_id", "operation", "dc_code", "baseline"
        }:
            raise BatchRoutingError("变更字段不完整或包含未知字段")
        cid = change["canonical_id"]
        if type(cid) is not int or not 0 < cid < 2**63:
            raise BatchRoutingError("品项 ID 必须为正整数")
        if cid in seen:
            raise BatchRoutingError("同一批不可重复提交品项", errors={str(cid): {"message": "重复 ID"}})
        seen.add(cid)
        op, dc, baseline = change["operation"], change["dc_code"], change["baseline"]
        error = None
        if op not in ("set", "delete"):
            error = "操作必须为 set 或 delete"
        elif op == "set" and (not isinstance(dc, str) or not dc.strip()):
            error = "设置路由必须指定配送中心"
        elif op == "delete" and dc is not None:
            error = "清除路由的 dc_code 必须为 null"
        if not isinstance(baseline, dict) or set(baseline) != {"dc_code", "updated_at"}:
            error = "必须提供已保存仓库和更新时间基线"
        else:
            old_dc, old_time = baseline["dc_code"], baseline["updated_at"]
            absent = old_dc is None and old_time is None
            present = (isinstance(old_dc, str) and bool(old_dc.strip())
                       and isinstance(old_time, str) and bool(old_time.strip()))
            if not (absent or present):
                error = "基线必须同时为空，或同时为非空字符串"
        if error:
            raise BatchRoutingError("变更校验失败，整批未写入", errors={str(cid): {"message": error}})
    return changes


def routing_diagnostics(master_conn):
    """一次完整只读扫描；不将 missing 误作无来源，也不为停用品项推断候选。"""
    with routing_warehouse_connections(master_conn) as connections:
        candidates, exclusive = _classify(master_conn, connections)
    return {cid: {"codes": sorted(codes), "exclusive_only": not codes and cid in exclusive}
            for cid, codes in candidates.items()}


def routing_snapshot(master_conn, canonical_id):
    row = master_conn.execute(
        "SELECT fulfillment_warehouse_code, updated_at FROM item_routing "
        "WHERE tenant_id=1 AND canonical_id=?", (canonical_id,)
    ).fetchone()
    return {"dc_code": row[0], "updated_at": row[1]} if row else {
        "dc_code": None, "updated_at": None}


def apply_batch(master_conn, changes, *, actor_id):
    """调用方须先 BEGIN IMMEDIATE；整批校验后写入，不自行 commit。

    仓码 + 秒级时间戳只是覆盖保护，不是严格 ABA 版本。当前值已达目标时不写，
    允许结果未知后的安全重放；其他旧基线冲突导致整批零写入。
    """
    errors, plans = {}, []
    for change in changes:
        cid, desired = change["canonical_id"], change["dc_code"]
        try:
            _assert_canonical(master_conn, cid)
            if change["operation"] == "set":
                _assert_dc(master_conn, desired)
        except ValueError as exc:
            errors[str(cid)] = {"message": str(exc)}
            continue
        current = routing_snapshot(master_conn, cid)
        no_op = current["dc_code"] == desired
        plans.append((change, current, no_op))
    if errors:
        raise BatchRoutingError("变更校验失败，整批未写入", errors=errors)
    for change, current, no_op in plans:
        if not no_op and current != change["baseline"]:
            errors[str(change["canonical_id"])] = {"message": "保存值已被其他操作修改", "current": current}
    if errors:
        raise BatchRoutingError("存在并发冲突，整批未写入；请核对最新值", errors=errors, status=409)
    results, diffs = [], []
    for change, current, no_op in plans:
        cid = change["canonical_id"]
        if not no_op:
            if change["operation"] == "delete":
                delete_routing(master_conn, cid, actor_id=actor_id)
            else:
                set_routing(master_conn, cid, change["dc_code"], actor_id=actor_id)
        saved = routing_snapshot(master_conn, cid)
        results.append({"canonical_id": cid, **saved})
        diffs.append({"canonical_id": cid, "old": current, "new": saved, "changed": not no_op})
    return results, diffs
