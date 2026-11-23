"""Master v10: exact MCP business grants and reversible routing holds."""
from __future__ import annotations

ROUTING_DDL = (
    """
    CREATE TABLE IF NOT EXISTS mcp_policy_routing_principals (
        id TEXT PRIMARY KEY NOT NULL DEFAULT (lower(hex(randomblob(16)))),
        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
        actor_id INTEGER NOT NULL REFERENCES users(id),
        revoked_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS mcp_policy_one_routing_principal
    ON mcp_policy_routing_principals(tenant_id) WHERE revoked_at IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS mcp_policy_routing_approvals (
        id TEXT PRIMARY KEY NOT NULL DEFAULT (lower(hex(randomblob(16)))),
        principal_id TEXT NOT NULL REFERENCES mcp_policy_routing_principals(id),
        canonical_id INTEGER NOT NULL REFERENCES canonical_items(id),
        warehouse_code TEXT NOT NULL REFERENCES warehouses(code),
        revoked_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS mcp_policy_unique_live_routing_approval
    ON mcp_policy_routing_approvals(principal_id, canonical_id, warehouse_code) WHERE revoked_at IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS mcp_policy_routing_scopes (
        token_id INTEGER PRIMARY KEY REFERENCES mcp_policy_bindings(token_id),
        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
        actor_id INTEGER NOT NULL REFERENCES users(id),
        pairs_json TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS mcp_policy_routing_scope_immutable
    BEFORE UPDATE ON mcp_policy_routing_scopes
    BEGIN SELECT RAISE(ABORT, 'MCP routing scope is immutable'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS mcp_policy_routing_admin_events (
        confirmation_id TEXT PRIMARY KEY NOT NULL,
        operation TEXT NOT NULL CHECK (operation IN ('install-preset', 'approve', 'revoke')),
        issuer_id INTEGER NOT NULL REFERENCES users(id),
        decision_ref TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        approval_id TEXT REFERENCES mcp_policy_routing_approvals(id),
        preset_name TEXT,
        preset_version INTEGER,
        FOREIGN KEY (preset_name, preset_version) REFERENCES mcp_policy_presets(name, version),
        UNIQUE (operation, approval_id),
        CHECK ((operation = 'install-preset' AND approval_id IS NULL
                    AND preset_name IS NOT NULL AND preset_version IS NOT NULL)
               OR (operation IN ('approve', 'revoke') AND approval_id IS NOT NULL
                    AND preset_name IS NULL AND preset_version IS NULL))
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS mcp_policy_routing_admin_event_no_replace
    BEFORE INSERT ON mcp_policy_routing_admin_events
    WHEN EXISTS (SELECT 1 FROM mcp_policy_routing_admin_events
                 WHERE confirmation_id=NEW.confirmation_id
                    OR (operation=NEW.operation AND approval_id=NEW.approval_id))
    BEGIN SELECT RAISE(ABORT, 'Routing authorization audit is immutable'); END
    """,
    """
    CREATE TABLE IF NOT EXISTS item_routing_stockout_events (
        id TEXT PRIMARY KEY,
        tenant_id INTEGER NOT NULL,
        canonical_id INTEGER NOT NULL REFERENCES canonical_items(id),
        warehouse_code TEXT NOT NULL REFERENCES warehouses(code),
        token_id INTEGER NOT NULL REFERENCES agent_tokens(id),
        actor_id INTEGER NOT NULL REFERENCES users(id),
        idempotency_key TEXT NOT NULL,
        operation TEXT NOT NULL CHECK (operation IN ('pause', 'restore')),
        pause_id TEXT REFERENCES item_routing_stockout_events(id),
        before_version TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        evidence_kind TEXT NOT NULL,
        evidence_ref TEXT NOT NULL,
        evidence_at TEXT NOT NULL,
        evidence_qty TEXT NOT NULL,
        evidence_unit TEXT NOT NULL,
        request_json TEXT NOT NULL,
        CHECK ((operation = 'pause') = (pause_id IS NULL)),
        UNIQUE (token_id, idempotency_key),
        UNIQUE (tenant_id, canonical_id, warehouse_code, evidence_kind, evidence_ref),
        UNIQUE (pause_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS item_routing_stockout_state (
        tenant_id INTEGER NOT NULL,
        canonical_id INTEGER NOT NULL,
        version TEXT NOT NULL,
        pause_id TEXT REFERENCES item_routing_stockout_events(id),
        PRIMARY KEY (tenant_id, canonical_id)
    )
    """,
)



def _revocation_triggers():
    resources = {
        "principals": "id, tenant_id, actor_id",
        "approvals": "id, principal_id, canonical_id, warehouse_code",
    }
    for name, immutable in resources.items():
        table = f"mcp_policy_routing_{name}"
        for event, clause in (
            ("reuse_id", f"BEFORE INSERT ON {table} WHEN EXISTS (SELECT 1 FROM {table} WHERE id=NEW.id)"),
            ("identity", f"BEFORE UPDATE OF {immutable} ON {table}"),
            ("delete", f"BEFORE DELETE ON {table}"),
            ("revival", f"BEFORE UPDATE OF revoked_at ON {table} WHEN OLD.revoked_at IS NOT NULL"),
        ):
            yield f"""
            CREATE TRIGGER IF NOT EXISTS {table}_protect_{event} {clause}
            BEGIN SELECT RAISE(ABORT, 'Routing authorization history is immutable'); END
            """


def _route_triggers():
    for operation, row in (("INSERT", "NEW"), ("UPDATE", "OLD"), ("DELETE", "OLD")):
        key = f"tenant_id={row}.tenant_id AND canonical_id={row}.canonical_id"
        yield f"""
        CREATE TRIGGER IF NOT EXISTS item_routing_stockout_protect_{operation}
        BEFORE {operation} ON item_routing
        WHEN EXISTS (SELECT 1 FROM item_routing_stockout_state WHERE {key} AND pause_id IS NOT NULL)
        BEGIN SELECT RAISE(ABORT, 'active stockout pause'); END
        """
        yield f"""
        CREATE TRIGGER IF NOT EXISTS item_routing_stockout_version_{operation}
        AFTER {operation} ON item_routing
        BEGIN
            INSERT INTO item_routing_stockout_state (tenant_id, canonical_id, version)
            VALUES ({row}.tenant_id, {row}.canonical_id, lower(hex(randomblob(16))))
            ON CONFLICT (tenant_id, canonical_id) DO UPDATE SET version=excluded.version;
        END
        """


def _revoked_token_trigger(table, key):
    return f"""CREATE TRIGGER IF NOT EXISTS {table}_routing_no_unrevoke
        BEFORE UPDATE OF revoked_at ON {table}
        WHEN OLD.revoked_at IS NOT NULL
           AND EXISTS (SELECT 1 FROM mcp_policy_routing_scopes WHERE token_id=OLD.{key})
        BEGIN SELECT RAISE(ABORT, 'Revoked routing credential cannot be reactivated'); END"""


_AGENT_TOKEN_TRIGGER = _revoked_token_trigger("agent_tokens", "id")
ROUTING_DDL += tuple(_revocation_triggers()) + tuple(_route_triggers()) + (
    _AGENT_TOKEN_TRIGGER, _revoked_token_trigger("mcp_policy_bindings", "token_id"),
) + tuple(
    f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation}
        BEFORE {operation} ON {table}
        BEGIN SELECT RAISE(ABORT, 'Routing history is immutable'); END"""
    for table in ("item_routing_stockout_events", "mcp_policy_routing_admin_events")
    for operation in ("UPDATE", "DELETE")
)


def registered_up(conn):
    token_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_tokens'",
    ).fetchone()
    for ddl in ROUTING_DDL:
        # Reduced legacy masters can lack the optional identity table. Full Web
        # initialization creates it before installing the same trigger.
        if ddl != _AGENT_TOKEN_TRIGGER or token_table:
            conn.execute(ddl)


def registered_down(conn):
    raise RuntimeError("Stockout holds and audit must be retained; see the routing rollback runbook")
