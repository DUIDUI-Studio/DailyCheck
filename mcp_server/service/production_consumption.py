"""Read production facts from existing storefront databases."""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal

from itsdangerous import BadData, URLSafeSerializer

import config
from mcp_server.data.unit_of_work import policy_master_connection, policy_read_connection
from mcp_server.infra.errors import ForbiddenError, McpError, ValidationError
from mcp_server.service.policy import PRODUCTION_STORES, BoundPolicy
from production_snapshots import CALCULATION_VERSION, recorded_text, snapshot_select, table_columns
from warehouse_paths import resolve_warehouse_path

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_DAYS = 92
MAX_SOURCE_LINES = 20_000
MAX_CURSOR_LENGTH = 512
VIEW_FIELDS = {"day": "summary", "week": "summary", "lines": "lines"}
_CURSORS = URLSafeSerializer(secrets.token_bytes(32), salt="production-consumption-v1")
_LINE_SNAPSHOT = {
    "unit": "unit_snapshot",
    "item_sku": "sku_snapshot",
    "canonical_item_id": "canonical_id_snapshot",
    "bom_row_id": "bom_row_id_snapshot",
    "bom_qty_per_unit": "bom_qty_per_unit_snapshot",
    "conversion_mode": "conversion_mode_snapshot",
    "grams_per_unit": "grams_per_unit_snapshot",
    "calculation_version": "calculation_version",
    "measurement_revision": "measurement_revision_snapshot",
}
_MEASURE_FIELDS = ("unit", "item_sku", "canonical_item_id", "conversion_mode",
                   "grams_per_unit", "measurement_revision")


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
    with policy_master_connection() as conn:
        rows = conn.execute(
            "SELECT code, db_path, warehouse_type FROM warehouses",
        ).fetchall()
    paths, identities, conflicts = {}, {}, set()
    for row in rows:
        code = row["code"]
        try:
            path = resolve_warehouse_path(row["db_path"], config.BASE_DIR, db_dir=config.DB_DIR).resolve(strict=True)
            info = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        # An outside storefront or distribution center can share a target
        # with an admitted store. Check every registration before any open.
        identity = info.st_dev, info.st_ino
        if identity in identities:
            conflicts.update((code, identities[identity]))
        identities[identity] = code
        if code in codes and row["warehouse_type"] == "storefront":
            paths[code] = path
    if paths.keys() != set(codes) or conflicts.intersection(codes):
        raise ValueError("Invalid production warehouse paths")
    return paths


def _source_expression(conn: sqlite3.Connection, columns: set[str]) -> tuple[str, str]:
    # Pre-v4 schemas have neither provenance column. Read their facts without
    # migrating, and recognize only provenance that the schema can corroborate.
    if "planned_default" not in columns:
        return "'unknown'", ""
    outbound = {col[0] for col in conn.execute("SELECT * FROM outbound_requests LIMIT 0").description}
    if "outbound_req_id" not in columns or "source_type" not in outbound:
        return "CASE WHEN li.planned_default = 0 THEN 'manual' ELSE 'unknown' END", ""
    return (
        """CASE WHEN li.planned_default = 0 THEN 'manual'
                WHEN li.planned_default = 1 AND ob.id IS NOT NULL
                 AND li.planned_qty = li.actual_qty AND li.actual_qty = ob.requested_quantity
                THEN 'system_default'
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
        columns = table_columns(conn, "production_run_items")
        source, join = _source_expression(conn, columns)
        line_snapshot = snapshot_select(columns, "li", _LINE_SNAPSHOT)
        run_snapshot = snapshot_select(table_columns(conn, "production_runs"), "r", {
            "output_unit": "output_unit_snapshot",
        })
        rows = conn.execute(
            f"""SELECT r.created_at, r.id AS run_id, li.id AS line_id, li.item_id,
                       li.planned_qty, li.actual_qty AS booked_qty, {source} AS booked_source,
                       {line_snapshot}, {run_snapshot}
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
        version = record["calculation_version"]
        record["unit"] = recorded_text(record["unit"]) if version == CALCULATION_VERSION else None
        record["snapshot_source"] = "production_write" if version == CALCULATION_VERSION else None
        record["unit_status"] = "snapshotted" if record["unit"] else "historical_unit_unavailable"
        records.append({"warehouse_code": code, **record})
    return records


def _quantities(lines: list[dict]) -> dict:
    planned = Decimal(0)
    booked = {}
    for line in lines:
        planned += Decimal(str(line["planned_qty"]))
        source = line["booked_source"]
        booked[source] = booked.get(source, Decimal(0)) + Decimal(str(line["booked_qty"]))
    return {"planned_qty": float(planned), **{
        f"{source}_qty": float(booked[source]) if source in booked else None
        for source in ("manual", "system_default", "unknown")
    }}


def _summary(lines: list[dict], view: str) -> list[dict]:
    groups = {}
    for line in lines:
        day = date.fromisoformat(line["created_at"][:10])
        period = day - timedelta(days=day.weekday()) if view == "week" else day
        key = (line["warehouse_code"], period.isoformat(), line["item_id"])
        groups.setdefault(key, []).append(line)
    results = []
    for (code, period, item_id), group in sorted(groups.items()):
        counts = Counter(line["booked_source"] for line in group)
        buckets = {}
        for line in group:
            if line["unit"] and line["item_sku"] and line["measurement_revision"] is not None:
                measure = tuple(line[field] for field in _MEASURE_FIELDS)
                buckets.setdefault(measure, []).append(line)
        unit_groups = [
            {**dict(zip(_MEASURE_FIELDS, measure, strict=True)), "line_count": len(bucket),
             **_quantities(bucket)}
            for measure, bucket in buckets.items()
        ]
        unknown = len(group) - sum(bucket["line_count"] for bucket in unit_groups)
        whole = unit_groups[0] if len(unit_groups) == 1 and unknown == 0 else {}
        results.append({
            "warehouse_code": code, "period_start": period, "item_id": item_id, "unit": whole.get("unit"),
            "planned_count": len(group), "manual_count": counts["manual"],
            "system_default_count": counts["system_default"], "unknown_count": counts["unknown"],
            **{field: whole.get(field) for field in ("planned_qty", "manual_qty", "system_default_qty", "unknown_qty")},
            "sample": {name: group[0][name] for name in ("run_id", "line_id", "created_at")},
            "unit_groups": unit_groups, "unknown_unit_count": unknown,
            "unknown_unit_fraction": unknown / len(group),
        })
    return results


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
        "unit_status": (
            ("partially_snapshotted" if any(not line["unit"] for line in lines) else "snapshotted")
            if any(line["unit"] for line in lines) else "historical_unit_unavailable"
        ),
        "cross_store_rollup": "historical_mapping_unavailable",
        "range_empty": not records, "page_empty": not page,
        "limit": limit, "truncated": truncated, "next_cursor": next_cursor,
    }
