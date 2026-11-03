"""只读桥接：外部 WMS（朴宇冷库 / hyWMS on SQL Server）中本货主的数据。

DailyCheck 自己的库是 SQLite；这里接入的是第三方 WMS 的 SQL Server。
边界（改动前先读这段）：

- **只读**：本模块只拼 SELECT，不提供任何写入路径。
- **单货主**：customer_id 由环境变量固定，不接受调用方传入。那个库里
  还有 37 家货主，允许传参等于把别人的库存暴露出去。
- **凭据只从环境变量读**：不落库、不写日志、不进 git。
- **驱动可选**：pymssql 缺失时抛 wms_driver_missing，让 MCP server 其余
  工具照常可用（CI 与未配置的生产实例不该因此起不来）。

配置项（全部可选，缺 host/user/password 视为未配置）：

    DAILYCHECK_WMS_HOST         e.g. 211.149.211.139
    DAILYCHECK_WMS_PORT         default 24331
    DAILYCHECK_WMS_USER
    DAILYCHECK_WMS_PASSWORD
    DAILYCHECK_WMS_DATABASE     default hywms_rentian
    DAILYCHECK_WMS_TDS_VERSION  default 7.0 (SQL Server 2008 R2)
    DAILYCHECK_WMS_CUSTOMER_ID  default 155 (蜜可诗 RTS012)
"""
from __future__ import annotations

import os

from mcp_server.infra.errors import McpError

_DEFAULT_PORT = 24331
_DEFAULT_DATABASE = "hywms_rentian"
_DEFAULT_TDS = "7.0"
_DEFAULT_CUSTOMER_ID = 155

# 上限防止一次调用把整库捞回来。库存总量 ~37 万件/37 货主，单货主最多千级行。
MAX_LIMIT = 500
DEFAULT_LIMIT = 100


class WmsConfig:
    __slots__ = ("host", "port", "user", "password", "database", "tds_version", "customer_id")

    def __init__(self, host: str, port: int, user: str, password: str,
                 database: str, tds_version: str, customer_id: int) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.tds_version = tds_version
        self.customer_id = customer_id

    def __repr__(self) -> str:  # 绝不回显口令
        return f"WmsConfig(host={self.host!r}, port={self.port}, db={self.database!r})"


def load_config() -> WmsConfig:
    """从环境变量读配置；缺关键项抛 wms_not_configured。"""
    host = (os.environ.get("DAILYCHECK_WMS_HOST") or "").strip()
    user = (os.environ.get("DAILYCHECK_WMS_USER") or "").strip()
    password = os.environ.get("DAILYCHECK_WMS_PASSWORD") or ""
    if not (host and user and password):
        raise McpError(
            "wms_not_configured",
            "WMS bridge is not configured (set DAILYCHECK_WMS_HOST/USER/PASSWORD)",
            503,
        )
    try:
        port = int(os.environ.get("DAILYCHECK_WMS_PORT") or _DEFAULT_PORT)
    except ValueError:
        port = _DEFAULT_PORT
    try:
        customer_id = int(os.environ.get("DAILYCHECK_WMS_CUSTOMER_ID") or _DEFAULT_CUSTOMER_ID)
    except ValueError:
        customer_id = _DEFAULT_CUSTOMER_ID
    return WmsConfig(
        host=host,
        port=port,
        user=user,
        password=password,
        database=(os.environ.get("DAILYCHECK_WMS_DATABASE") or _DEFAULT_DATABASE).strip(),
        tds_version=(os.environ.get("DAILYCHECK_WMS_TDS_VERSION") or _DEFAULT_TDS).strip(),
        customer_id=customer_id,
    )


def _connect(cfg: WmsConfig):
    try:
        import pymssql  # noqa: PLC0415  (可选依赖，延迟导入)
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise McpError(
            "wms_driver_missing",
            "pymssql is not installed; pip install pymssql to enable the WMS bridge",
            503,
        ) from exc
    try:
        return pymssql.connect(
            server=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password,
            database=cfg.database, tds_version=cfg.tds_version,
            login_timeout=10, timeout=20, as_dict=True,
        )
    except Exception as exc:  # pymssql raises OperationalError / DatabaseException
        # 连接失败的原因（网络、白名单、口令）不回显细节，避免泄给调用方。
        raise McpError("wms_unavailable", f"WMS upstream unreachable ({type(exc).__name__})", 503) from exc


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise McpError("validation_error", "limit must be an integer", 400) from exc
    if value < 1:
        raise McpError("validation_error", "limit must be >= 1", 400)
    return min(value, MAX_LIMIT)


def _run(sql: str, params: tuple, cfg: WmsConfig | None = None) -> list[dict]:
    """执行一条只读 SQL 并把结果规整成 JSON 友好的 dict 列表。"""
    cfg = cfg or load_config()
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall() or []
    except McpError:
        raise
    except Exception as exc:
        raise McpError("wms_query_failed", f"WMS query failed ({type(exc).__name__})", 502) from exc
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - 关闭失败不应掩盖上面的错误
            pass
    out: list[dict] = []
    for row in rows:
        out.append({k: _jsonable(v) for k, v in dict(row).items()})
    return out


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    return str(value)


# ---------------------------------------------------------------------------
# 查询：只能用 cfg.customer_id，不接受外部传入
# ---------------------------------------------------------------------------

_STOCK_SQL = """
SELECT g.goodsCode AS goods_code,
       g.goodsName AS goods_name,
       g.unit AS unit,
       CAST(b.qty AS DECIMAL(18, 2)) AS qty,
       w.warehouse AS warehouse,
       p.zone AS zone,
       p.position AS position,
       b.lotNo AS lot_no
FROM biz_goodsBalance b
JOIN sy_goods g ON g.goodsID = b.goodsID
LEFT JOIN sy_warehouse w ON w.warehouseID = b.warehouseID
LEFT JOIN sy_position p ON p.positionID = b.positionID
WHERE g.customerID = %s AND ISNULL(b.inactive, 0) = 0 AND b.qty > 0
ORDER BY b.qty DESC
"""

_SUMMARY_SQL = """
SELECT w.warehouse AS warehouse,
       COUNT(DISTINCT g.goodsID) AS sku_count,
       CAST(SUM(b.qty) AS DECIMAL(18, 2)) AS total_qty
FROM biz_goodsBalance b
JOIN sy_goods g ON g.goodsID = b.goodsID
LEFT JOIN sy_warehouse w ON w.warehouseID = b.warehouseID
WHERE g.customerID = %s AND ISNULL(b.inactive, 0) = 0 AND b.qty > 0
GROUP BY w.warehouse
ORDER BY SUM(b.qty) DESC
"""

_IN_SQL = """
SELECT s.storeInNo AS doc_no,
       CONVERT(varchar(10), s.bizDate, 120) AS biz_date,
       w.warehouse AS warehouse,
       CAST(s.qty AS DECIMAL(18, 2)) AS qty,
       s.supplierName AS supplier,
       s.issusedBy AS issued_by
FROM biz_storeIn s
LEFT JOIN sy_warehouse w ON w.warehouseID = s.warehouseID
WHERE s.customerID = %s
  AND (%s IS NULL OR s.bizDate >= %s)
ORDER BY s.bizDate DESC, s.storeInID DESC
"""

_OUT_SQL = """
SELECT s.storeOutNo AS doc_no,
       CONVERT(varchar(10), s.bizDate, 120) AS biz_date,
       w.warehouse AS warehouse,
       CAST(s.qty AS DECIMAL(18, 2)) AS qty,
       s.receiverName AS receiver,
       s.issusedBy AS issued_by
FROM biz_storeOut s
LEFT JOIN sy_warehouse w ON w.warehouseID = s.warehouseID
WHERE s.customerID = %s
  AND (%s IS NULL OR s.bizDate >= %s)
ORDER BY s.bizDate DESC, s.storeOutID DESC
"""


def list_stock(limit: int | None = None, goods_code: str | None = None,
               *, warehouse: str | None = None) -> list[dict]:
    """当前在库明细（含货位与批次）。"""
    cfg = load_config()
    n = _clamp_limit(limit)
    sql = _STOCK_SQL
    params: list = [cfg.customer_id]
    code = (goods_code or "").strip()
    if code:
        sql = sql.replace("ORDER BY b.qty DESC", "AND g.goodsCode = %s\nORDER BY b.qty DESC")
        params.append(code)
    if warehouse:
        sql = sql.replace("ORDER BY b.qty DESC", "AND w.warehouse = %s\nORDER BY b.qty DESC")
        params.append(warehouse)
    # TOP 由 SQL Server 语法承担，避免在 Python 侧截断后丢失 total 语义
    sql = sql.replace("SELECT g.goodsCode", f"SELECT TOP {n} g.goodsCode", 1)
    return _run(sql, tuple(params), cfg)


def summarize_stock(*, warehouse: str | None = None) -> list[dict]:
    """按仓库汇总：SKU 数与总件数。"""
    cfg = load_config()
    sql = _SUMMARY_SQL
    params: tuple = (cfg.customer_id,)
    if warehouse:
        sql = sql.replace("GROUP BY w.warehouse", "AND w.warehouse = %s\nGROUP BY w.warehouse")
        params += (warehouse,)
    return _run(sql, params, cfg)


def list_documents(direction: str = "in", date_from: str | None = None,
                   limit: int | None = None, *, warehouse: str | None = None) -> list[dict]:
    """出入库单据列表。direction 只接受 in / out。"""
    cfg = load_config()
    n = _clamp_limit(limit)
    d = (direction or "in").strip().lower()
    if d not in ("in", "out"):
        raise McpError("validation_error", "direction must be 'in' or 'out'", 400)
    sql = _IN_SQL if d == "in" else _OUT_SQL
    sql = sql.replace("SELECT s.storeInNo", f"SELECT TOP {n} s.storeInNo", 1)
    sql = sql.replace("SELECT s.storeOutNo", f"SELECT TOP {n} s.storeOutNo", 1)
    since = (date_from or "").strip() or None
    params: tuple = (cfg.customer_id, since, since)
    if warehouse:
        sql = sql.replace("ORDER BY s.bizDate", "AND w.warehouse = %s\nORDER BY s.bizDate")
        params += (warehouse,)
    return _run(sql, params, cfg)


def list_warehouses() -> list[dict]:
    """本货主涉及的外仓；含已清空库存但仍有历史单据的仓库。"""
    cfg = load_config()
    return _run("""
SELECT DISTINCT w.warehouse AS warehouse
FROM sy_warehouse w
WHERE w.warehouse IS NOT NULL AND w.warehouse <> ''
  AND (
    EXISTS (SELECT 1 FROM biz_goodsBalance b
            JOIN sy_goods g ON g.goodsID = b.goodsID
            WHERE b.warehouseID = w.warehouseID AND g.customerID = %s)
    OR EXISTS (SELECT 1 FROM biz_storeIn s
               WHERE s.warehouseID = w.warehouseID AND s.customerID = %s)
    OR EXISTS (SELECT 1 FROM biz_storeOut s
               WHERE s.warehouseID = w.warehouseID AND s.customerID = %s)
  )
ORDER BY w.warehouse
""", (cfg.customer_id, cfg.customer_id, cfg.customer_id), cfg)
