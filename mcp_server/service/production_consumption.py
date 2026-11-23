"""Read production facts from existing storefront databases."""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta

from itsdangerous import BadData, URLSafeSerializer

import config
from mcp_server.data.unit_of_work import policy_master_connection, policy_read_connection
from mcp_server.infra.errors import ForbiddenError, McpError, ValidationError
from mcp_server.service.policy import PRODUCTION_STORES, BoundPolicy
from warehouse_paths import resolve_warehouse_path

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_DAYS = 92
MAX_SOURCE_LINES = 20_000
MAX_CURSOR_LENGTH = 512
VIEW_FIELDS = {"day": "summary", "week": "summary", "lines": "lines"}
_CURSORS = URLSafeSerializer(secrets.token_bytes(32), salt="production-consumption-v1")


def _json_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _date_range(start_date: str, end_date: str, view: str) -> None:
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError:
        raise ValidationError("invalid_date_range") from None
    if (
        start.isoformat() != start_date or end.isoformat() != end_date
        or not 0 < (end - start).days <= MAX_DAYS
        or (view == "week" and (start.weekday() or end.weekday()))
    ):
        raise ValidationError("invalid_date_range")


def _warehouse_paths(codes: tuple[str, ...]) -> dict:
    placeholders = ", ".join("?" for _ in codes)
    with policy_master_connection() as conn:
        rows = conn.execute(
            f"SELECT code, db_path FROM warehouses WHERE code COLLATE BINARY IN ({placeholders}) "
            "AND warehouse_type = 'storefront'", codes,
        )
        paths = {
            row["code"]: resolve_warehouse_path(row["db_path"], config.BASE_DIR, db_dir=config.DB_DIR).resolve()
            for row in rows
        }
    if paths.keys() != set(codes) or len(set(paths.values())) != len(paths):
        raise ValueError("Invalid production warehouse paths")
    return paths


def _source_expression(conn: sqlite3.Connection) -> tuple[str, str]:
    # Pre-v4 schemas have neither provenance column. Read their facts without
    # migrating, and recognize only provenance that the schema can corroborate.
    columns = {col[0] for col in conn.execute("SELECT * FROM production_run_items LIMIT 0").description}
    if "planned_default" not in columns:
        return "'unknown'", ""
    outbound = {col[0] for col in conn.execute("SELECT * FROM outbound_requests LIMIT 0").description}
    if "outbound_req_id" not in columns or "source_type" not in outbound:
        return "CASE WHEN li.planned_default = 0 THEN 'manual' ELSE 'unknown' END", ""
    return (
        """CASE WHEN li.planned_default = 0 THEN 'manual'
                WHEN li.planned_default = 1 AND ob.id IS NOT NULL THEN 'system_default'
                ELSE 'unknown' END""",
        """LEFT JOIN outbound_requests ob
                  ON ob.id = li.outbound_req_id AND ob.item_id = li.item_id
                 AND ob.source_type = 'production' AND ob.status = '出库' AND ob.rolled_back = 0
                 AND ob.reason = '生产领料(run=#' || r.id || ')'
                 AND NOT EXISTS (
                     SELECT 1 FROM production_run_items other
                     WHERE other.outbound_req_id = ob.id AND other.id <> li.id
                 )""",
    )


def _read_lines(path, code, start, end, filters, budget) -> list[dict]:
    with policy_read_connection(path) as conn:
        conn.execute("BEGIN")
        source, join = _source_expression(conn)
        rows = conn.execute(
            f"""SELECT r.created_at, r.id AS run_id, li.id AS line_id, li.item_id,
                       li.planned_qty, li.actual_qty AS booked_qty, {source} AS booked_source
                  FROM production_runs r
                  JOIN production_run_items li ON li.run_id = r.id
                  {join}
                 WHERE r.rolled_back = 0 AND r.created_at >= ? AND r.created_at < ?
                 {"".join(f" AND {column} = ?" for column in filters)}
                 ORDER BY r.created_at, r.id, li.id LIMIT ?""",
            (start, end, *filters.values(), budget + 1),
        ).fetchall()
    if len(rows) > budget:
        raise McpError("range_too_dense", "range_too_dense", 422)
    records = []
    for row in rows:
        timestamp = row["created_at"]
        if datetime.fromisoformat(timestamp).isoformat(sep=" ") != timestamp or len(timestamp) != 19:
            raise ValueError("Invalid production timestamp")
        if not all(type(row[key]) in (int, float) and math.isfinite(row[key])
                   for key in ("planned_qty", "booked_qty")):
            raise ValueError("Invalid production quantity")
        record = dict(row)
        record["created_at"] = timestamp.replace(" ", "T") + "+08:00"
        records.append({"warehouse_code": code, "unit": None, **record})
    return records


def _summary(lines: list[dict], view: str) -> list[dict]:
    groups = {}
    for line in lines:
        day = date.fromisoformat(line["created_at"][:10])
        period = day - timedelta(days=day.weekday()) if view == "week" else day
        key = (line["warehouse_code"], period.isoformat(), line["item_id"])
        if key not in groups:
            reference = {name: line[name] for name in ("run_id", "line_id", "created_at")}
            groups[key] = (Counter(), reference)
        groups[key][0][line["booked_source"]] += 1
    return [
        {
            "warehouse_code": code, "period_start": period, "item_id": item_id, "unit": None,
            "planned_count": sum(counts.values()), "manual_count": counts["manual"],
            "system_default_count": counts["system_default"], "unknown_count": counts["unknown"],
            "planned_qty": None, "manual_qty": None, "system_default_qty": None, "unknown_qty": None,
            "sample": reference,
        }
        for (code, period, item_id), (counts, reference) in sorted(groups.items())
    ]


def read_production_consumption(
    policy: BoundPolicy, fields: tuple[str, ...], *, start_date: str, end_date: str,
    view: str, warehouse_code: str | None = None, item_id: int | None = None,
    run_id: int | None = None, line_id: int | None = None,
    limit: int = DEFAULT_LIMIT, cursor: str | None = None,
) -> dict:
    field = VIEW_FIELDS[view]
    if field not in fields:
        raise ForbiddenError("forbidden_view")
    codes = (warehouse_code,) if warehouse_code is not None else policy.warehouses
    if not codes or not set(codes) <= PRODUCTION_STORES.intersection(policy.warehouses):
        raise ForbiddenError("forbidden_warehouse")
    _date_range(start_date, end_date, view)
    if view != "lines" and (run_id is not None or line_id is not None):
        raise ValidationError("invalid_line_filter")
    filters = {column: value for column, value in (
        ("li.item_id", item_id), ("r.id", run_id), ("li.id", line_id),
    ) if value is not None}
    request_digest = _json_digest([
        policy.token_id, policy.warehouses, fields, codes, start_date, end_date, view, filters, limit,
    ])
    offset, expected = 0, None
    if cursor is not None:
        try:
            scope, expected, offset = _CURSORS.loads(cursor)
        except BadData:
            raise ValidationError("invalid_cursor") from None
        if scope != request_digest:
            raise ValidationError("invalid_cursor")

    lines = []
    try:
        paths = _warehouse_paths(codes)
        for code in codes:
            lines.extend(_read_lines(
                paths[code], code, start_date, end_date, filters, MAX_SOURCE_LINES - len(lines),
            ))
    except (OSError, sqlite3.Error, TypeError, ValueError, RuntimeError, OverflowError):
        raise McpError("source_unavailable", "source_unavailable", 503) from None
    records = _summary(lines, view) if field == "summary" else lines
    page = records[offset:offset + limit]
    truncated = offset + len(page) < len(records)
    next_cursor = None
    if cursor is not None or truncated:
        snapshot = _json_digest(lines)
        if expected is not None and expected != snapshot:
            raise McpError("stale_cursor", "stale_cursor", 409)
        if truncated:
            next_cursor = _CURSORS.dumps([request_digest, snapshot, offset + len(page)])
    return {
        field: page, "view": view, "warehouses": list(codes),
        "start_date": start_date, "end_date": end_date, "timezone": "Asia/Shanghai",
        "unit_status": "historical_unit_unavailable",
        "cross_store_rollup": "historical_mapping_unavailable",
        "range_empty": not records, "page_empty": not page,
        "limit": limit, "truncated": truncated, "next_cursor": next_cursor,
    }
