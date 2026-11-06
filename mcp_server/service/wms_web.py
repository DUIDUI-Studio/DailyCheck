"""Cookie-authenticated WMS reads; separate authority from configured-owner MCP reads.

Schema: coordinator's SELECT evidence, 2026-11-03 (Issue #140).
Only resolve_scope creates a Web scope from a requested warehouse selection.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mcp_server.infra.errors import McpError
from mcp_server.service import wms

# Owner confirmed 2026-11-03: 1 = 朴宇冷库; 3 = 朴宇仓.
MULTI_OWNER_WAREHOUSE_IDS: tuple[int, ...] = (1, 3)
MAX_DETAIL_ROWS = 2000
MAX_LOCATION_ROWS = 10000


class Direction(str, Enum):
    IN = "in"
    OUT = "out"


@dataclass(frozen=True)
class DocumentKey:
    direction: Direction
    document_id: int

    def __post_init__(self):
        if not isinstance(self.direction, Direction) or not 1 <= self.document_id <= 2147483647:
            raise ValueError("invalid document identity")


@dataclass(frozen=True)
class WebScope:
    warehouse_ids: tuple[int, ...]
    customer_id: int | None

    def __post_init__(self):
        if (not self.warehouse_ids or any(i < 1 for i in self.warehouse_ids)
                or (self.customer_id is not None and self.customer_id < 1)):
            raise ValueError("warehouse-bound scope required")
        if self.customer_id is None and not set(self.warehouse_ids).issubset(MULTI_OWNER_WAREHOUSE_IDS):
            raise ValueError("multi-owner scope outside approved warehouses")


@dataclass(frozen=True)
class Page:
    rows: list[dict]
    total: int
    number: int
    size: int
    sku_count: int = 0

    @property
    def has_next(self) -> bool:
        return self.number * self.size < self.total

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.size - 1) // self.size)


class DetailState(str, Enum):
    COMPLETE = "complete"
    EMPTY = "empty"
    TRUNCATED = "truncated"


@dataclass(frozen=True)
class DocumentDetail:
    header: dict
    lines: list[dict]
    state: DetailState


def _scope_predicate(scope: WebScope, warehouse_column: str, customer_column: str):
    placeholders = ", ".join("%s" for _ in scope.warehouse_ids)
    sql = f"{warehouse_column} IN ({placeholders})"
    params = list(scope.warehouse_ids)
    if scope.customer_id is not None:
        sql += f" AND {customer_column} = %s"
        params.append(scope.customer_id)
    return sql, params


def list_warehouses(cfg: wms.WmsConfig) -> list[dict]:
    if not MULTI_OWNER_WAREHOUSE_IDS:
        raise McpError("wms_scope_unconfirmed", "Web warehouse scope is not confirmed", 503)
    placeholders = ", ".join("%s" for _ in MULTI_OWNER_WAREHOUSE_IDS)
    return wms._run(f"""
SELECT w.warehouseID AS warehouse_id, w.warehouse AS warehouse
FROM sy_warehouse w
WHERE w.warehouseID IN ({placeholders}) OR (
    EXISTS (SELECT 1 FROM biz_goodsBalance b JOIN sy_goods g ON g.goodsID=b.goodsID
            WHERE b.warehouseID=w.warehouseID AND g.customerID=%s)
    OR EXISTS (SELECT 1 FROM biz_storeIn s WHERE s.warehouseID=w.warehouseID AND s.customerID=%s)
    OR EXISTS (SELECT 1 FROM biz_storeOut s WHERE s.warehouseID=w.warehouseID AND s.customerID=%s)
)
ORDER BY w.warehouseID
""", (*MULTI_OWNER_WAREHOUSE_IDS, cfg.customer_id, cfg.customer_id, cfg.customer_id), cfg)


def resolve_scope(warehouse_id: int | None, warehouses: list[dict], cfg: wms.WmsConfig) -> WebScope:
    available = {int(row["warehouse_id"]) for row in warehouses}
    if warehouse_id is None:
        if not set(MULTI_OWNER_WAREHOUSE_IDS).issubset(available):
            raise McpError("wms_scope_unavailable", "Authorized warehouse missing upstream", 503)
        return WebScope(MULTI_OWNER_WAREHOUSE_IDS, None)
    if warehouse_id not in available:
        raise McpError("validation_error", "warehouse outside available scope", 400)
    return WebScope((warehouse_id,), None if warehouse_id in MULTI_OWNER_WAREHOUSE_IDS else cfg.customer_id)


def list_owners(scope: WebScope, cfg: wms.WmsConfig) -> list[dict]:
    stock_where, stock_params = _scope_predicate(scope, "b.warehouseID", "g.customerID")
    doc_where, doc_params = _scope_predicate(scope, "s.warehouseID", "s.customerID")
    return wms._run(f"""
SELECT DISTINCT owners.owner_id, c.customerCode AS owner_code, c.customerName AS owner_name
FROM (
    SELECT g.customerID AS owner_id FROM biz_goodsBalance b
    JOIN sy_goods g ON g.goodsID=b.goodsID
    WHERE {stock_where} AND ISNULL(b.inactive,0)=0 AND b.qty>0
    UNION SELECT s.customerID FROM biz_storeIn s WHERE {doc_where}
    UNION SELECT s.customerID FROM biz_storeOut s WHERE {doc_where}
) owners
LEFT JOIN sy_customer c ON c.customerID=owners.owner_id
ORDER BY owners.owner_id
""", tuple(stock_params + doc_params + doc_params), cfg)


def _page(select: str, source: str, where: str, params: list, order: str,
          number: int, size: int, cfg: wms.WmsConfig, *, stock: bool = False) -> Page:
    if not 1 <= number <= 2147483647 or not 1 <= size <= wms.MAX_LIMIT:
        raise ValueError("invalid page")
    sku = "COUNT(DISTINCT goods_id)" if stock else "0"
    rows = wms._run(f"""
WITH filtered AS (
    SELECT {select} FROM {source} WHERE {where}
), numbered AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY {order}) AS row_number FROM filtered
), totals AS (
    SELECT COUNT_BIG(*) AS total, {sku} AS sku_count FROM filtered
)
SELECT totals.total, totals.sku_count, numbered.*
FROM totals LEFT JOIN numbered ON numbered.row_number BETWEEN %s AND %s
ORDER BY numbered.row_number
""", (*params, (number - 1) * size + 1, number * size), cfg)
    if not rows:
        raise McpError("wms_query_failed", "Missing page totals", 502)
    return Page([row for row in rows if row["row_number"] is not None],
                int(rows[0]["total"]), number, size, int(rows[0]["sku_count"]))


def inventory_page(scope: WebScope, cfg: wms.WmsConfig, *, owner_id: int | None = None,
                   goods_code: str = "", number: int = 1, size: int = 100) -> Page:
    where, params = _scope_predicate(scope, "b.warehouseID", "g.customerID")
    where += " AND ISNULL(b.inactive,0)=0 AND b.qty>0"
    if owner_id is not None:
        where += " AND g.customerID=%s"
        params.append(owner_id)
    if goods_code:
        where += " AND g.goodsCode=%s"
        params.append(goods_code)
    return _page("""
b.goodsBalanceID AS balance_id, g.goodsID AS goods_id,
g.customerID AS owner_id, c.customerCode AS owner_code, c.customerName AS owner_name,
g.goodsCode AS goods_code, g.goodsName AS goods_name, g.unit,
CAST(b.qty AS DECIMAL(18,4)) AS qty, w.warehouse,
p.zone, p.position, b.lotNo AS lot_no
""", """biz_goodsBalance b JOIN sy_goods g ON g.goodsID=b.goodsID
LEFT JOIN sy_customer c ON c.customerID=g.customerID
LEFT JOIN sy_warehouse w ON w.warehouseID=b.warehouseID
LEFT JOIN sy_position p ON p.positionID=b.positionID AND p.warehouseID=b.warehouseID
""", where, params, "balance_id", number, size, cfg, stock=True)


@dataclass(frozen=True)
class DocumentTables:
    header: str
    key: str
    number: str
    detail: str
    line_key: str
    positions: str
    position_key: str
    party: str


_DOCUMENTS = {
    Direction.IN: DocumentTables("biz_storeIn", "storeInID", "storeInNo", "biz_storeInDetail",
                                 "storeInDetailID", "biz_storeInPosition", "storeInPositionID", "supplierName"),
    Direction.OUT: DocumentTables("biz_storeOut", "storeOutID", "storeOutNo", "biz_storeOutDetail",
                                  "storeOutDetailID", "biz_storeOutPosition", "storeOutPositionID", "receiverName"),
}


def _header_select(t: DocumentTables) -> str:
    return f"""s.{t.key} AS doc_id, s.{t.number} AS doc_no, s.bizDate AS sort_date,
CONVERT(varchar(10),s.bizDate,120) AS biz_date, w.warehouse,
s.customerID AS owner_id, c.customerCode AS owner_code, c.customerName AS owner_name,
CAST(s.qty AS DECIMAL(18,4)) AS qty, s.{t.party} AS party, s.issusedBy AS issued_by"""


def _header_source(t: DocumentTables) -> str:
    return f"""{t.header} s LEFT JOIN sy_warehouse w ON w.warehouseID=s.warehouseID
LEFT JOIN sy_customer c ON c.customerID=s.customerID"""


def documents_page(scope: WebScope, cfg: wms.WmsConfig, direction: Direction, *,
                   owner_id: int | None = None, date_from: str = "",
                   number: int = 1, size: int = 100) -> Page:
    t = _DOCUMENTS[direction]
    where, params = _scope_predicate(scope, "s.warehouseID", "s.customerID")
    if owner_id is not None:
        where += " AND s.customerID=%s"
        params.append(owner_id)
    if date_from:
        where += " AND s.bizDate>=%s"
        params.append(date_from)
    return _page(_header_select(t), _header_source(t), where, params,
                 "sort_date DESC, doc_id DESC", number, size, cfg)


def document_detail(scope: WebScope, cfg: wms.WmsConfig, key: DocumentKey) -> DocumentDetail | None:
    t = _DOCUMENTS[key.direction]
    where, params = _scope_predicate(scope, "s.warehouseID", "s.customerID")
    where += f" AND s.{t.key}=%s"
    params.append(key.document_id)
    headers = wms._run(f"SELECT {_header_select(t)} FROM {_header_source(t)} WHERE {where}",
                       tuple(params), cfg)
    if not headers:
        return None
    lines = wms._run(f"""
SELECT TOP {MAX_DETAIL_ROWS + 1} d.{t.line_key} AS line_id,
g.goodsCode AS goods_code, g.goodsName AS goods_name, g.unit,
CAST(d.qty AS DECIMAL(18,4)) AS qty, d.lotNo AS lot_no, d.storeLotNo AS store_lot_no
FROM {t.detail} d JOIN {t.header} s ON s.{t.key}=d.{t.key}
LEFT JOIN sy_goods g ON g.goodsID=d.goodsID
WHERE {where} ORDER BY d.{t.line_key}
""", tuple(params), cfg)
    locations = wms._run(f"""
SELECT TOP {MAX_LOCATION_ROWS + 1} d.{t.line_key} AS line_id,
p.zone, p.position, CAST(a.qty AS DECIMAL(18,4)) AS qty, a.lotNo AS lot_no
FROM {t.positions} a JOIN {t.detail} d ON d.{t.line_key}=a.{t.line_key}
JOIN {t.header} s ON s.{t.key}=d.{t.key}
LEFT JOIN sy_position p ON p.positionID=a.positionID AND p.warehouseID=s.warehouseID
WHERE {where} ORDER BY d.{t.line_key}, a.{t.position_key}
""", tuple(params), cfg)
    truncated = len(lines) > MAX_DETAIL_ROWS or len(locations) > MAX_LOCATION_ROWS
    lines = lines[:MAX_DETAIL_ROWS]
    by_id = {row["line_id"]: row for row in lines}
    for row in lines:
        row["locations"] = []
    for location in locations[:MAX_LOCATION_ROWS]:
        if location["line_id"] in by_id:
            by_id[location["line_id"]]["locations"].append(location)
    state = DetailState.TRUNCATED if truncated else DetailState.COMPLETE if lines else DetailState.EMPTY
    return DocumentDetail(headers[0], lines, state)
