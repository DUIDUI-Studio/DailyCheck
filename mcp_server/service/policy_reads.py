"""Fixed inventory and order projections for an admitted storefront token."""
from __future__ import annotations

import sqlite3

import config
from mcp_server.data.unit_of_work import policy_master_connection, policy_read_connection
from mcp_server.infra.errors import ForbiddenError, McpError, NotFoundError
from mcp_server.service.policy import INVENTORY_COLUMNS, ORDER_COLUMNS, BoundPolicy
from mcp_server.service.store_order import DEFAULT_LIMIT
from warehouse_paths import resolve_warehouse_path


def get_inventory(
    policy: BoundPolicy, fields: tuple[str, ...], *, item_id: int, warehouse_code: str,
) -> list[dict]:
    if warehouse_code not in policy.warehouses:
        raise ForbiddenError("forbidden_warehouse")
    columns = ", ".join(INVENTORY_COLUMNS[field] for field in fields)
    try:
        with policy_master_connection() as conn:
            warehouse = conn.execute(
                "SELECT db_path FROM warehouses WHERE code COLLATE BINARY = ? "
                "AND warehouse_type = 'storefront'", (warehouse_code,),
            ).fetchone()
        if warehouse is None:
            raise NotFoundError()
        path = resolve_warehouse_path(warehouse["db_path"], config.BASE_DIR, db_dir=config.DB_DIR)
        with policy_read_connection(path.resolve()) as conn:
            row = conn.execute(
                f"SELECT {columns} FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        raise McpError("source_unavailable", "source_unavailable", 503) from None
    if row is None:
        raise NotFoundError()
    return [dict(row)]


def _fulfillment(conn: sqlite3.Connection, order_ids: list[int], codes: tuple[str, ...]) -> dict:
    result = {
        order_id: {"warehouses": {}, "unattributed_shipments": [], "unattributed_receipts": []}
        for order_id in order_ids
    }
    if not order_ids:
        return result
    order_values = ", ".join("(?)" for _ in order_ids)
    code_values = ", ".join("(?)" for _ in codes)
    # A corroborated single-source legacy header can block NULL facts.
    # On a mixed order, the primary header reflects the line-count majority.
    rows = conn.execute(
        f"""
        WITH page(id) AS (VALUES {order_values}),
             allowed(code) AS (VALUES {code_values}),
             known_sources(order_id, code) AS (
                SELECT i.order_id, i.source_warehouse_code
                  FROM store_order_items i
                 WHERE i.order_id IN (SELECT id FROM page) AND i.source_warehouse_code <> ''
                UNION ALL
                SELECT d.order_id, d.warehouse_code
                  FROM store_order_deliveries d
                 WHERE d.order_id IN (SELECT id FROM page) AND d.warehouse_code <> ''
                UNION ALL
                SELECT d.order_id, di.warehouse_code
                  FROM store_order_deliveries d
                  JOIN store_order_delivery_items di ON di.delivery_id = d.id
                  JOIN store_order_items i ON i.id = di.order_item_id AND i.order_id = d.order_id
                 WHERE d.order_id IN (SELECT id FROM page) AND di.warehouse_code <> ''
             ),
             orders_allowing_unattributed(id) AS (
                SELECT o.id FROM store_orders o
                 WHERE o.id IN (SELECT id FROM page)
                   AND (o.dc_warehouse_code COLLATE BINARY IN (SELECT code FROM allowed)
                        OR NOT EXISTS (
                            SELECT 1 FROM known_sources s
                             WHERE s.order_id = o.id AND s.code COLLATE BINARY = o.dc_warehouse_code
                        )
                        OR EXISTS (
                            SELECT 1 FROM known_sources s
                             WHERE s.order_id = o.id AND s.code COLLATE BINARY <> o.dc_warehouse_code
                        ))
             ),
             facts AS (
            SELECT DISTINCT i.order_id, i.source_warehouse_code AS code,
                   'participant' AS kind, NULL AS happened_at
              FROM store_order_items i
             WHERE i.order_id IN (SELECT id FROM page)
               AND i.source_warehouse_code COLLATE BINARY IN (SELECT code FROM allowed)
            UNION ALL
            SELECT d.order_id, d.warehouse_code, 'shipment', d.shipped_at
              FROM store_order_deliveries d
             WHERE d.order_id IN (SELECT id FROM page)
               AND (d.warehouse_code COLLATE BINARY IN (SELECT code FROM allowed)
                    OR (d.warehouse_code IS NULL
                        AND d.order_id IN (SELECT id FROM orders_allowing_unattributed) AND NOT EXISTS (
                        SELECT 1 FROM store_order_delivery_items di
                         WHERE di.delivery_id = d.id AND di.warehouse_code IS NOT NULL
                    )))
            UNION ALL
            SELECT r.order_id, i.source_warehouse_code, 'receipt', r.created_at
              FROM store_order_receipts r
              JOIN store_order_items i ON i.id = r.order_item_id AND i.order_id = r.order_id
             WHERE r.order_id IN (SELECT id FROM page) AND r.applied = 1
               AND (i.source_warehouse_code COLLATE BINARY IN (SELECT code FROM allowed)
                    OR (i.source_warehouse_code IS NULL
                        AND r.order_id IN (SELECT id FROM orders_allowing_unattributed) AND NOT EXISTS (
                        SELECT 1 FROM store_order_delivery_items di
                        JOIN store_order_deliveries d ON d.id = di.delivery_id AND d.order_id = r.order_id
                         WHERE di.order_item_id = i.id
                           AND (di.warehouse_code IS NOT NULL OR d.warehouse_code IS NOT NULL)
                    )))
         )
         SELECT f.order_id, f.code, w.name, f.kind, f.happened_at
           FROM facts f
           LEFT JOIN warehouses w ON w.code = f.code COLLATE BINARY
          ORDER BY f.code, f.happened_at
        """, [*order_ids, *codes],
    )
    for row in rows:
        facts = result[row["order_id"]]
        code = row["code"]
        kind = row["kind"]
        if code is None:
            facts[f"unattributed_{kind}s"].append(row["happened_at"])
            continue
        warehouse = facts["warehouses"].setdefault(code, {
            "warehouse_code": code, "warehouse_name": row["name"], "shipments": [], "receipts": [],
        })
        if kind != "participant":
            warehouse[f"{kind}s"].append(row["happened_at"])
    for facts in result.values():
        facts["warehouses"] = list(facts["warehouses"].values())
    return result


def list_orders(
    policy: BoundPolicy, fields: tuple[str, ...], *, warehouse_code: str | None = None,
    status: str | None = None,
    limit: int = DEFAULT_LIMIT, offset: int = 0,
) -> dict:
    codes = policy.warehouses
    if not codes or (warehouse_code is not None and warehouse_code not in codes):
        raise ForbiddenError("forbidden_warehouse")
    params = [warehouse_code] if warehouse_code is not None else list(codes)
    placeholders = ", ".join("?" for _ in params)
    filters = (
        " FROM store_orders WHERE flow_type = 'store_order' "
        f"AND store_warehouse_code COLLATE BINARY IN ({placeholders})"
    )
    if status is not None:
        filters += " AND status = ?"
        params.append(status)
    columns = ["id", *(ORDER_COLUMNS[field] for field in fields if field != "fulfillment")]
    try:
        with policy_master_connection() as conn:
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*)" + filters, params).fetchone()[0]
            orders = conn.execute(
                f"SELECT {', '.join(columns)}"
                + filters + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            fulfillment = (
                _fulfillment(conn, [row["id"] for row in orders], codes) if "fulfillment" in fields else None
            )
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        raise McpError("source_unavailable", "source_unavailable", 503) from None
    page = []
    for row in orders:
        order = dict(row)
        order_id = order.pop("id")
        if fulfillment is not None:
            order["fulfillment"] = fulfillment[order_id]
        page.append(order)
    return {
        "orders": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "truncated": total > offset + len(orders),
    }
