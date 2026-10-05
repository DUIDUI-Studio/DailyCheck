"""CLI commands for platform administration.

Run via:
    flask --app app init-master
    flask --app app migrate-legacy
    flask --app app create-warehouse <code> <name>
    flask --app app create-user <username> <password> [--admin]
    flask --app app assign-role <username> <warehouse_code> <role>
    flask --app app create-agent-token <name> [--read-paths *] [--warehouses wh_001]

Canonical align (issue #11, 2026-10-05):
    flask --app app align-seed
    flask --app app align-detect [--out report.txt]
    flask --app app align-apply --canonical-ids 1,2 --warehouses wh_002 [--action overwrite] [--force]
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import click
from flask import Flask
from werkzeug.security import generate_password_hash

from config import BASE_DIR, MASTER_DB
from db import init_master_db
from db.migrate import migrate_legacy_inventory


def register_cli(app: Flask) -> None:
    app.cli.add_command(init_master_cmd)
    app.cli.add_command(migrate_legacy_cmd)
    app.cli.add_command(create_warehouse_cmd)
    app.cli.add_command(clone_warehouse_cmd)
    app.cli.add_command(create_user_cmd)
    app.cli.add_command(assign_role_cmd)
    app.cli.add_command(list_users_cmd)
    app.cli.add_command(bootstrap_cmd)
    app.cli.add_command(mcp_cmd)
    app.cli.add_command(create_agent_token_cmd)
    app.cli.add_command(align_seed_cmd)
    app.cli.add_command(align_detect_cmd)
    app.cli.add_command(align_apply_cmd)


@click.command("init-master")
def init_master_cmd() -> None:
    """Create master.db schema (idempotent)."""
    init_master_db()
    click.echo(f"Initialized {MASTER_DB}")


@click.command("migrate-legacy")
def migrate_legacy_cmd() -> None:
    """Copy the legacy inventory.db into warehouses/wh_001.db."""
    target = migrate_legacy_inventory()
    click.echo(f"Migrated legacy inventory.db → {target}")


@click.command("create-warehouse")
@click.argument("code")
@click.argument("name")
@click.option(
    "--type",
    "warehouse_type",
    type=click.Choice(["storefront", "rd"], case_sensitive=False),
    default="storefront",
    help="Warehouse type: storefront (default, has inventory ops) or rd (研发中心, recipe-only).",
)
def create_warehouse_cmd(code: str, name: str, warehouse_type: str) -> None:
    """Register a new warehouse. The db file is created if missing."""
    from config import WAREHOUSE_DB_DIR
    from db import init_warehouse_db

    db_path = WAREHOUSE_DB_DIR / f"{code}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_warehouse_db(db_path)

    rel_path = str(db_path.relative_to(BASE_DIR))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        try:
            conn.execute(
                "INSERT INTO warehouses (code, name, db_path, warehouse_type, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (code, name, rel_path, warehouse_type, now),
            )
            conn.commit()
            click.echo(
                f"Created warehouse {code} ({name}) type={warehouse_type}"
            )
        except sqlite3.IntegrityError:
            click.echo(f"Warehouse {code} already exists", err=True)


@click.command("clone-warehouse")
@click.argument("src_code")
@click.argument("new_code")
@click.argument("name")
def clone_warehouse_cmd(src_code: str, new_code: str, name: str) -> None:
    """Create <new_code> by cloning categories, items, products and
    product_bom from <src_code>. Stock quantities are reset to zero.
    """
    from config import WAREHOUSE_DB_DIR
    from db import init_warehouse_db
    from db.clone import clone_warehouse_catalog

    src_path = WAREHOUSE_DB_DIR / f"{src_code}.db"
    if not src_path.exists():
        click.echo(f"Source warehouse {src_code} not found", err=True)
        return

    dst_path = WAREHOUSE_DB_DIR / f"{new_code}.db"
    if dst_path.exists():
        click.echo(f"Destination {new_code} already exists", err=True)
        return

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    init_warehouse_db(dst_path)
    counts = clone_warehouse_catalog(src_path, dst_path)

    rel_path = str(dst_path.relative_to(BASE_DIR))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        try:
            conn.execute(
                "INSERT INTO warehouses (code, name, db_path, created_at) VALUES (?, ?, ?, ?)",
                (new_code, name, rel_path, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            click.echo(f"Warehouse {new_code} already registered", err=True)
            return

    click.echo(
        f"Cloned {src_code} → {new_code} ({name}): "
        f"{counts['categories']} categories, {counts['items']} items, "
        f"{counts['products']} products, {counts['product_bom']} BOM rows. "
        f"Stock reset to 0."
    )


@click.command("create-user")
@click.argument("username")
@click.argument("password")
@click.option("--admin", is_flag=True, help="Grant platform admin (all warehouses).")
def create_user_cmd(username: str, password: str, admin: bool) -> None:
    """Create a user account with the given password."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pwhash = generate_password_hash(password)
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
                (username, pwhash, 1 if admin else 0, now),
            )
            conn.commit()
            click.echo(f"Created user {username}{' (admin)' if admin else ''}")
        except sqlite3.IntegrityError:
            click.echo(f"User {username} already exists", err=True)


@click.command("assign-role")
@click.argument("username")
@click.argument("warehouse_code")
@click.argument("role", type=click.Choice(["staff", "manager", "admin"]))
def assign_role_cmd(username: str, warehouse_code: str, role: str) -> None:
    """Grant <role> on <warehouse_code> to <username>."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        wh = conn.execute("SELECT id FROM warehouses WHERE code=?", (warehouse_code,)).fetchone()
        if user is None:
            click.echo(f"No user {username}", err=True)
            return
        if wh is None:
            click.echo(f"No warehouse {warehouse_code}", err=True)
            return
        conn.execute(
            "INSERT OR REPLACE INTO warehouse_users (user_id, warehouse_id, role) VALUES (?, ?, ?)",
            (user["id"], wh["id"], role),
        )
        conn.commit()
        click.echo(f"{username} → {warehouse_code} as {role}")


@click.command("list-users")
def list_users_cmd() -> None:
    """Show all users and their warehouse roles."""
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT id, username, is_admin FROM users").fetchall()
        for u in users:
            tag = " [admin]" if u["is_admin"] else ""
            click.echo(f"  {u['username']}{tag}")
            roles = conn.execute(
                """SELECT w.code, wu.role FROM warehouse_users wu
                   JOIN warehouses w ON w.id = wu.warehouse_id
                   WHERE wu.user_id=?""",
                (u["id"],),
            ).fetchall()
            for r in roles:
                click.echo(f"    {r['code']}: {r['role']}")


@click.command("bootstrap")
@click.option("--admin-username", default="admin", show_default=True)
@click.option("--admin-password", default="admin123", show_default=True)
@click.option(
    "--warehouse-code", default="wh_001", show_default=True,
    help="First warehouse code to create (only used if no warehouses exist yet)."
)
@click.option(
    "--warehouse-name", default="中央仓", show_default=True,
    help="Display name for the first warehouse."
)
def bootstrap_cmd(
    admin_username: str, admin_password: str,
    warehouse_code: str, warehouse_name: str,
) -> None:
    """One-shot first-run setup: master.db + admin user + first warehouse.

    Run this on a fresh clone (no db files committed). Idempotent — if
    master.db / users / warehouses already exist, those steps are
    skipped. Always runnable; never destroys data.
    """
    from datetime import datetime

    from werkzeug.security import generate_password_hash

    # 1) master.db
    init_master_db()
    click.echo(f"  master.db schema ready at {MASTER_DB}")

    # 2) admin user
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        existing_admin = conn.execute(
            "SELECT id FROM users WHERE username=?", (admin_username,)
        ).fetchone()
        if existing_admin is None:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """INSERT INTO users
                   (username, password_hash, is_admin, created_at)
                   VALUES (?, ?, 1, ?)""",
                (admin_username, generate_password_hash(admin_password), now),
            )
            conn.commit()
            click.echo(f"  admin user '{admin_username}' created")
        else:
            click.echo(f"  admin user '{admin_username}' already exists, skipped")

    # 3) first warehouse (only if none exist)
    with closing(sqlite3.connect(MASTER_DB)) as conn:
        any_wh = conn.execute("SELECT 1 FROM warehouses LIMIT 1").fetchone()
    if any_wh is None:
        # Inline create-warehouse logic so we don't double-import.
        from config import WAREHOUSE_DB_DIR
        from db import init_warehouse_db
        db_path = WAREHOUSE_DB_DIR / f"{warehouse_code}.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        init_warehouse_db(db_path)
        rel_path = str(db_path.relative_to(BASE_DIR))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with closing(sqlite3.connect(MASTER_DB)) as conn:
            conn.execute(
                """INSERT INTO warehouses (code, name, db_path, created_at)
                   VALUES (?, ?, ?, ?)""",
                (warehouse_code, warehouse_name, rel_path, now),
            )
            conn.commit()
        click.echo(f"  warehouse '{warehouse_code}' ({warehouse_name}) created")
    else:
        click.echo("  warehouses already exist, skipped")

    click.echo(
        f"\nBootstrap complete. Login: {admin_username} / {admin_password}"
    )


@click.command("mcp")
@click.option("--port", default=5100, help="MCP server port (stdio mode only binds to stdio)")
def mcp_cmd(port: int) -> None:
    """Start the MCP server (stdio transport)."""
    import asyncio

    from mcp.server.stdio import stdio_server

    from mcp_server.protocol.server import build_server

    async def run_server():
        server = build_server()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run_server())


@click.command("create-agent-token")
@click.argument("name")
@click.option("--read-paths", default="*", help="Allowed read paths (comma-separated, * = all)")
@click.option("--write-paths", default="", help="Allowed write paths (comma-separated, * = all)")
@click.option("--warehouses", default="", help="Allowed warehouse codes (comma-separated, empty = all)")
def create_agent_token_cmd(name: str, read_paths: str, write_paths: str, warehouses: str) -> None:
    """Create an agent token and print the raw secret.

    Store the printed secret as DAILYCHECK_MCP_TOKEN in your environment.
    Example: flask --app app create-agent-token my-agent --read-paths "*"
    """
    import secrets

    from werkzeug.security import generate_password_hash

    raw_token = secrets.token_urlsafe(32)
    token_hash = generate_password_hash(raw_token, method="pbkdf2:sha256")

    import json
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _parse_paths(s: str) -> str:
        if s == "*":
            return json.dumps(["*"])
        return json.dumps([p.strip() for p in s.split(",") if p.strip()])

    def _parse_warehouses(s: str) -> str:
        if not s.strip():
            return "null"
        return json.dumps([w.strip() for w in s.split(",") if w.strip()])

    with closing(sqlite3.connect(MASTER_DB)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id FROM users WHERE is_admin = 1 LIMIT 1"
        ).fetchone()
        created_by = row["id"] if row else 1

        conn.execute(
            """INSERT INTO agent_tokens
               (name, token_hash, created_by, created_at,
                allowed_read_paths_json, allowed_write_paths_json, allowed_warehouse_codes_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                token_hash,
                created_by,
                now,
                _parse_paths(read_paths),
                _parse_paths(write_paths),
                _parse_warehouses(warehouses),
            ),
        )
        conn.commit()

    click.echo(f"Token '{name}' created.")
    click.echo(f"  DAILYCHECK_MCP_TOKEN={raw_token}")


# ─────────────────────────────────────────────────────────────────────
#  Canonical align (issue #11, 2026-10-05)
#
#  align-seed / align-detect / align-apply —— canonical item 对齐 CLI。
#  策略与范围常量全部在 blueprints/canonical_pure.py (T25)。
# ─────────────────────────────────────────────────────────────────────

@click.command("align-seed")
def align_seed_cmd() -> None:
    """Seed default canonical categories + items (幂等,无破坏性).

    只写 master.canonical_categories / master.canonical_items,
    不动任何 wh_xxx.items。
    """
    from blueprints import canonical_pure as cp

    with closing(sqlite3.connect(MASTER_DB)) as conn:
        cats = cp.seed_canonical_categories(conn)
        items = cp.seed_default_canonical_items(conn)
    click.echo(
        f"Seeded canonical_categories: {len(cats)}, "
        f"canonical_items: {len(items)} (skip-existing 幂等)"
    )


@click.command("align-detect")
@click.option(
    "--out", "out_path", type=click.Path(), default=None,
    help="报告输出到文件;不传则打到 stdout。",
)
def align_detect_cmd(out_path: str | None) -> None:
    """跨仓 detect 同物候选,生成 dry-run 报告(纯只读,不写任何表)."""
    from blueprints import canonical_pure as cp

    with closing(sqlite3.connect(MASTER_DB)) as conn:
        report = cp.dry_run_report(conn)
    if out_path:
        Path(out_path).write_text(report, encoding="utf-8")
        click.echo(f"Wrote {len(report)} chars to {out_path}")
    else:
        click.echo(report)


@click.command("align-apply")
@click.option(
    "--canonical-ids", "canonical_ids", type=str, required=True,
    help="逗号分隔的 canonical_items.id, e.g. '140,141,142'",
)
@click.option(
    "--warehouses", "warehouse_codes", type=str, required=True,
    help="逗号分隔的目标仓代码, e.g. 'wh_002,wh_003'",
)
@click.option(
    "--action", type=click.Choice(["overwrite", "merge", "keep"]),
    default="overwrite", show_default=True,
    help="下发动作语义 (Spec §3.5)。",
)
@click.option(
    "--force", is_flag=True,
    help="跳过冻结字段(local != last_synced)保护 (Spec §3.5 force_only)。",
)
@click.confirmation_option(
    prompt="真要 apply?这会写目标仓 items.canonical_id / canonical_synced_json"
)
def align_apply_cmd(
    canonical_ids: str, warehouse_codes: str, action: str, force: bool,
) -> None:
    """真扇出 canonical → 各仓 items 表 (Q7 库存零丢失:不动 quantity/sku/name).

    范围硬约束:目标仓必须在 ALIGN_SCOPE_WAREHOUSES 内,否则拒绝。
    """
    from blueprints import canonical_pure as cp

    ids = [int(x) for x in canonical_ids.split(",") if x.strip()]
    whs = [w.strip() for w in warehouse_codes.split(",") if w.strip()]
    if not ids:
        raise click.UsageError("--canonical-ids 不能为空")
    if not whs:
        raise click.UsageError("--warehouses 不能为空")

    # v4 范围白名单(设计文档 §6 冻结;测试 test_align_apply_respects_v4_scope)
    out_of_scope = [w for w in whs if w not in cp.ALIGN_SCOPE_WAREHOUSES]
    if out_of_scope:
        raise click.UsageError(
            f"目标仓 {out_of_scope} 不在 v4 对齐范围 "
            f"{cp.ALIGN_SCOPE_WAREHOUSES} 内,已拒绝。"
            f"如需扩范围请先改 canonical_pure.ALIGN_SCOPE_WAREHOUSES 并同步设计文档。"
        )

    from config import BASE_DIR  # call-time import:测试 monkeypatch 生效

    with closing(sqlite3.connect(MASTER_DB)) as master_conn:
        master_conn.row_factory = sqlite3.Row
        # 解析各仓 db 路径(master.warehouses.db_path,相对路径按 BASE_DIR)
        wh_conns: dict[str, sqlite3.Connection] = {}
        for code in whs:
            row = master_conn.execute(
                "SELECT db_path FROM warehouses WHERE code=?", (code,)
            ).fetchone()
            if row is None:
                raise click.UsageError(f"仓库 {code} 未注册于 master.db")
            db_path = Path(row["db_path"])
            if not db_path.is_absolute():
                db_path = BASE_DIR / db_path
            if not db_path.exists():
                raise click.UsageError(f"仓库 {code} 的 db 不存在: {db_path}")
            wh_conns[code] = sqlite3.connect(db_path)

        try:
            result = cp.fanout_canonical_items(
                master_conn, wh_conns,
                canonical_ids=ids, warehouse_codes=whs,
                action=action, force=force, dry_run=False,
                summary=f"cli align-apply {canonical_ids} -> {warehouse_codes}",
            )
        finally:
            for conn in wh_conns.values():
                conn.close()

    click.echo(
        f"align-apply done: status={result['status']}, "
        f"event_id={result['event_id']}, "
        f"total_written={result['total_written']}, "
        f"total_frozen={result['total_frozen']}"
    )
    for entry in result["per_canonical"]:
        lines = [
            f"  canonical_id={entry['canonical_id']} "
            f"({entry.get('canonical_sku', '?')})"
        ]
        for wh_result in entry["per_warehouse"]:
            if "error" in wh_result:
                lines.append(
                    f"    {wh_result['warehouse_code']}: ERROR {wh_result['error']}"
                )
                continue
            parts = []
            if wh_result.get("inserted"):
                parts.append("inserted")
            if wh_result.get("written"):
                parts.append(f"written={list(wh_result['written'].keys())}")
            if wh_result.get("frozen"):
                parts.append(f"frozen={list(wh_result['frozen'].keys())}")
            if wh_result.get("skipped_reason"):
                parts.append(f"skipped({wh_result['skipped_reason']})")
            lines.append(
                f"    {wh_result['warehouse_code']}: "
                f"{'; '.join(parts) if parts else 'no-op'}"
            )
        click.echo("\n".join(lines))
