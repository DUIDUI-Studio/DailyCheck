"""v4：仅迁移运行时播种，审计清单进入迁移日志；不在启动/认领/扇出补种。"""
import logging
from datetime import datetime


def registered_up(conn):
    from blueprints.item_routing_pure import routing_warehouse_connections, seed_routing

    required = {
        "warehouses": {"code", "db_path", "warehouse_type", "tenant_id"},
        "canonical_items": {"id", "tenant_id", "status"},
        "tenants": {"id", "name", "status", "created_at"},
    }
    missing = {table: sorted(columns - {row[1] for row in conn.execute(
        f"PRAGMA table_info({table})")}) for table, columns in required.items()}
    missing = {table: columns for table, columns in missing.items() if columns}
    if missing:
        logging.getLogger(__name__).warning("item_routing 播种跳过：旧库缺业务表/列 %s", missing)
        return
    conn.execute(
        "INSERT OR IGNORE INTO tenants (id, name, status, created_at) "
        "VALUES (1, '默认租户', 'active', ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
    )
    with routing_warehouse_connections(conn) as warehouses:
        result = seed_routing(conn, warehouses)
    logging.getLogger(__name__).warning("item_routing 播种审计清单: %s", result)


def registered_down(conn):
    raise RuntimeError("路由含管理员配置，不可自动 down；请还原备份")
