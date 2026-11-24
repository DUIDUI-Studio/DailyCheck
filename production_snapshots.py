"""Shared production snapshot interpretation for Web and MCP readers."""

CALCULATION_VERSION = 1
UNKNOWN_UNIT = "单位未留存"


def recorded_text(value):
    return value if isinstance(value, str) and value.strip() else None


def table_columns(conn, table):
    return {row[0] for row in conn.execute(f"SELECT * FROM {table} LIMIT 0").description}


def snapshot_select(columns, alias, fields):
    # A read-only connection can encounter an unmigrated warehouse. Each
    # projected column remains nullable, with no fallback to current items.
    return ", ".join(
        f"{alias}.{column} AS {name}" if column in columns else f"NULL AS {name}"
        for name, column in fields.items()
    )


def outbound_production_expression(conn):
    """SQL for an outbound row aliased as o, including ambiguous legacy links."""
    columns = table_columns(conn, "outbound_requests")
    if "source_type" not in columns:
        return "(o.reason GLOB '生产领料(run=#[0-9]*)')"
    if "outbound_req_id" in table_columns(conn, "production_run_items"):
        return """COALESCE(o.source_type = 'production', EXISTS (
            SELECT 1 FROM production_run_items linked WHERE linked.outbound_req_id = o.id
        ))"""
    return "COALESCE(o.source_type = 'production', 0)"


def outbound_snapshot_query(conn):
    """Return projection and a non-fanout join for unique, corroborated links."""
    fields = {
        "unit_snapshot": "unit_snapshot",
        "calculation_version": "calculation_version",
    }
    lines = table_columns(conn, "production_run_items")
    join = ""
    if (set(fields.values()) | {"outbound_req_id"}) <= lines and (
        "source_type" in table_columns(conn, "outbound_requests")
    ):
        join = """LEFT JOIN production_run_items ps
            ON ps.id = (SELECT li.id FROM production_run_items li
                        WHERE li.outbound_req_id = o.id
                        GROUP BY li.outbound_req_id HAVING COUNT(*) = 1)
           AND ps.item_id = o.item_id AND ps.actual_qty = o.requested_quantity
           AND o.source_type = 'production' AND o.status = '出库'
           AND EXISTS (SELECT 1 FROM production_runs r WHERE r.id = ps.run_id
                        AND r.rolled_back = o.rolled_back
                        AND o.reason = '生产领料(run=#' || r.id || ')')"""
    return f"{outbound_production_expression(conn)} AS is_production, " + snapshot_select(
        lines if join else set(), "ps", fields,
    ), join


def outbound_unit_metadata(row):
    written = row["calculation_version"] == CALCULATION_VERSION
    unit = recorded_text(row["unit_snapshot"]) if written else None
    return {
        "unit": unit,
        "unit_status": "snapshotted" if unit else "historical_unit_unavailable",
        "snapshot_source": "production_write" if written else None,
    }
