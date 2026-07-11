"""User management (platform admin only).

- Create users (with optional per-warehouse role bindings at creation time)
- Reset passwords
- Toggle platform admin (is_admin)
- Bind / unbind / change role on a per-warehouse basis
- Create warehouses (optionally cloning items / products / product_bom
  from an existing one)
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from config import BASE_DIR, MASTER_DB, ROLE_RANK, WAREHOUSE_DB_DIR
from db import get_master_db, init_warehouse_db
from permissions import require_login


bp = Blueprint("users", __name__)

VALID_ROLES = set(ROLE_RANK.keys())  # staff / manager / admin


def _require_admin():
    from flask import g
    if not g.user or not g.user["is_admin"]:
        abort(403)


def _bindings_for(user_id: int) -> dict[int, str]:
    """Return {warehouse_id: role} for a user."""
    db = get_master_db()
    rows = db.execute(
        "SELECT warehouse_id, role FROM warehouse_users WHERE user_id=?",
        (user_id,),
    ).fetchall()
    return {r["warehouse_id"]: r["role"] for r in rows}


@bp.route("/users", methods=["GET"])
@require_login
def list_users():
    _require_admin()
    from flask import g
    db = get_master_db()
    users = db.execute(
        """SELECT u.id, u.username, u.is_admin, u.last_login_at, u.created_at,
                  GROUP_CONCAT(w.code || ':' || wu.role, ', ') AS bindings
           FROM users u
           LEFT JOIN warehouse_users wu ON wu.user_id = u.id
           LEFT JOIN warehouses w ON w.id = wu.warehouse_id
           GROUP BY u.id
           ORDER BY u.id"""
    ).fetchall()
    warehouses = db.execute("SELECT id, code, name FROM warehouses ORDER BY id").fetchall()
    # Per-user full binding detail so the template can render editable rows.
    detail: dict[int, dict[int, str]] = {}
    for u in users:
        detail[u["id"]] = _bindings_for(u["id"])
    return render_template(
        "users.html",
        users=users,
        warehouses=warehouses,
        bindings_by_user=detail,
        current_user_id=g.user["id"],
    )


@bp.route("/users/create", methods=["POST"])
@require_login
def create_user():
    _require_admin()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    is_admin = request.form.get("is_admin") == "1"
    if not username or not password:
        flash("账号和密码必填")
        return redirect(url_for("users.list_users"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db = get_master_db()
    try:
        db.execute(
            """INSERT INTO users (username, password_hash, is_admin, created_at)
               VALUES (?, ?, ?, ?)""",
            (username, generate_password_hash(password, method="pbkdf2:sha256"), 1 if is_admin else 0, now),
        )
        new_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        db.commit()
        _apply_bindings_from_form(new_id, request.form, db)
        _log_admin_action(
            f"create user #{new_id} {username} admin={is_admin} "
            f"bindings={_bindings_summary(new_id, db)}"
        )
        flash(f"已创建账号 {username}")
    except sqlite3.IntegrityError:
        flash(f"账号 {username} 已存在")
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/bind", methods=["POST"])
@require_login
def bind_role(user_id: int):
    """Add a binding (warehouse_id + role) for an existing user."""
    _require_admin()
    db = get_master_db()
    if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
        abort(404)
    wh_id, role, err = _parse_binding(request.form, db)
    if err:
        flash(err)
        return redirect(url_for("users.list_users"))
    try:
        db.execute(
            """INSERT INTO warehouse_users (user_id, warehouse_id, role)
               VALUES (?, ?, ?)""",
            (user_id, wh_id, role),
        )
        db.commit()
        _log_admin_action(f"bind user #{user_id} wh={wh_id} role={role}")
        flash(f"已绑定仓库 {wh_id} 角色 {role}")
    except sqlite3.IntegrityError:
        flash("该仓库已绑定(请用修改角色)")
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/unbind", methods=["POST"])
@require_login
def unbind_role(user_id: int):
    """Remove a binding for an existing user."""
    _require_admin()
    db = get_master_db()
    if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
        abort(404)
    wh_id_raw = request.form.get("warehouse_id", "").strip()
    if not wh_id_raw.isdigit():
        flash("仓库 id 非法")
        return redirect(url_for("users.list_users"))
    wh_id = int(wh_id_raw)
    db.execute(
        "DELETE FROM warehouse_users WHERE user_id=? AND warehouse_id=?",
        (user_id, wh_id),
    )
    db.commit()
    _log_admin_action(f"unbind user #{user_id} wh={wh_id}")
    flash("已解除绑定")
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/change-role", methods=["POST"])
@require_login
def change_role(user_id: int):
    """Change the role on an existing binding."""
    _require_admin()
    db = get_master_db()
    if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
        abort(404)
    wh_id, role, err = _parse_binding(request.form, db)
    if err:
        flash(err)
        return redirect(url_for("users.list_users"))
    existing = db.execute(
        "SELECT role FROM warehouse_users WHERE user_id=? AND warehouse_id=?",
        (user_id, wh_id),
    ).fetchone()
    if existing is None:
        flash("该仓库未绑定,请用绑定")
        return redirect(url_for("users.list_users"))
    db.execute(
        "UPDATE warehouse_users SET role=? WHERE user_id=? AND warehouse_id=?",
        (role, user_id, wh_id),
    )
    db.commit()
    _log_admin_action(f"change-role user #{user_id} wh={wh_id} -> {role}")
    flash(f"角色已更新为 {role}")
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@require_login
def reset_password(user_id: int):
    _require_admin()
    password = request.form.get("password", "")
    if not password:
        flash("新密码必填")
        return redirect(url_for("users.list_users"))
    db = get_master_db()
    target = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    if target is None:
        abort(404)
    db.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(password, method="pbkdf2:sha256"), user_id),
    )
    db.commit()
    _log_admin_action(f"reset password for user #{user_id} {target['username']}")
    flash(f"已重置 {target['username']} 的密码")
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/toggle-admin", methods=["POST"])
@require_login
def toggle_admin(user_id: int):
    _require_admin()
    from flask import g
    if g.user["id"] == user_id:
        flash("不能修改自己的平台管理员状态")
        return redirect(url_for("users.list_users"))
    db = get_master_db()
    target = db.execute(
        "SELECT username, is_admin FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if target is None:
        abort(404)
    new_state = 0 if target["is_admin"] else 1
    db.execute("UPDATE users SET is_admin=? WHERE id=?", (new_state, user_id))
    db.commit()
    _log_admin_action(
        f"toggle admin user #{user_id} {target['username']} -> {bool(new_state)}"
    )
    flash(
        f"{target['username']} {'已设为' if new_state else '已取消'}平台管理员"
    )
    return redirect(url_for("users.list_users"))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@require_login
def delete_user(user_id: int):
    """Permanently delete a user.

    Guard rails:
    - Cannot delete self (would lock out the only admin).
    - Cannot delete the last platform admin (must keep ≥1 admin).
    - warehouse_users bindings cascade-delete with the user.
    """
    _require_admin()
    from flask import g
    if g.user["id"] == user_id:
        flash("不能删除自己的账号")
        return redirect(url_for("users.list_users"))
    db = get_master_db()
    target = db.execute(
        "SELECT username, is_admin FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if target is None:
        abort(404)
    if target["is_admin"]:
        admin_count = db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE is_admin=1"
        ).fetchone()["c"]
        if admin_count <= 1:
            flash("至少保留一个平台管理员,无法删除")
            return redirect(url_for("users.list_users"))
    db.execute("DELETE FROM warehouse_users WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    _log_admin_action(f"delete user #{user_id} {target['username']}")
    flash(f"已删除账号 {target['username']}")
    return redirect(url_for("users.list_users"))


@bp.route("/warehouses/create", methods=["POST"])
@require_login
def create_warehouse():
    """Create a new warehouse. Optionally clone the items / products /
    product_bom from an existing warehouse (inventory zeroed, SKUs kept).
    """
    _require_admin()

    code = request.form.get("code", "").strip().lower()
    name = request.form.get("name", "").strip()
    clone_from_code = request.form.get("clone_from_code", "").strip()

    if not re.fullmatch(r"wh_\w+", code):
        flash("仓库编码必须以 wh_ 开头,后面接字母/数字/下划线")
        return redirect(url_for("users.list_users"))
    if not name:
        flash("门店名称必填")
        return redirect(url_for("users.list_users"))

    db_path = WAREHOUSE_DB_DIR / f"{code}.db"
    if db_path.exists():
        flash(f"仓库编码 {code} 的数据库文件已存在")
        return redirect(url_for("users.list_users"))

    db = get_master_db()
    if db.execute("SELECT 1 FROM warehouses WHERE code=?", (code,)).fetchone() is not None:
        flash(f"仓库编码 {code} 已注册")
        return redirect(url_for("users.list_users"))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_warehouse_db(db_path)
    counts = {"items": 0, "products": 0, "bom": 0}
    if clone_from_code:
        if clone_from_code == code:
            flash("克隆源不能是自己")
            return redirect(url_for("users.list_users"))
        if db.execute("SELECT 1 FROM warehouses WHERE code=?", (clone_from_code,)).fetchone() is None:
            flash(f"克隆源仓库 {clone_from_code} 不存在")
            return redirect(url_for("users.list_users"))
        src_path = WAREHOUSE_DB_DIR / f"{clone_from_code}.db"
        if not src_path.exists():
            flash(f"克隆源数据库 {src_path} 缺失")
            return redirect(url_for("users.list_users"))
        counts = _clone_catalog_from(src_path, db_path)

    rel_path = str(db_path.relative_to(BASE_DIR))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        """INSERT INTO warehouses (code, name, db_path, created_at)
           VALUES (?, ?, ?, ?)""",
        (code, name, rel_path, now),
    )
    db.commit()
    _log_admin_action(
        f"create warehouse {code} ({name})"
        + (f" cloned {counts['items']} items / {counts['products']} products / "
           f"{counts['bom']} bom from {clone_from_code}"
           if clone_from_code else "")
    )
    flash(
        f"已创建仓库 {code} ({name})"
        + (f",从 {clone_from_code} 复制了 {counts['items']} 个品项"
           if clone_from_code else "")
    )
    return redirect(url_for("users.list_users"))


@bp.route("/warehouses/<int:warehouse_id>/init", methods=["POST"])
@require_login
def init_warehouse(warehouse_id: int):
    """Wipe business data for one warehouse, preserving the catalog.

    What gets cleared:
      - stock_movements, stocktakes, stocktake_batches
      - restock_requests, outbound_requests, adjustment_requests
      - daily_revenue, audit_log
      - items.quantity / items.safety_stock reset to 0

    What gets preserved:
      - categories (system-fixed seeds)
      - items (the catalog: sku, name, unit, unit_cost) — only
        quantity and safety_stock are zeroed

    Two-step confirmation: the form sends a hidden confirm_token that
    must equal the warehouse's code. Plain-text confirmation in the
    button label.
    """
    _require_admin()
    from flask import g
    from config import BASE_DIR

    db = get_master_db()
    wh = db.execute(
        "SELECT code, name, db_path FROM warehouses WHERE id=?", (warehouse_id,)
    ).fetchone()
    if wh is None:
        abort(404)

    expected = request.form.get("confirm_token", "")
    if expected.strip() != wh["code"]:
        flash(f"确认口令不匹配:请输入仓库编码 '{wh['code']}' 二次确认")
        return redirect(url_for("users.list_users"))

    db_path = BASE_DIR / wh["db_path"]
    target_tables = [
        "stock_movements",
        "stocktakes",
        "stocktake_batches",
        "restock_requests",
        "outbound_requests",
        "adjustment_requests",
        "daily_revenue",
        "audit_log",
    ]
    with closing(sqlite3.connect(db_path)) as wh_db:
        cur = wh_db.cursor()
        cur.execute("PRAGMA foreign_keys = OFF")
        for tbl in target_tables:
            cur.execute(f"DELETE FROM {tbl}")
        cur.execute("UPDATE items SET quantity = 0, safety_stock = 0")
        wh_db.commit()

    _log_admin_action(
        f"init warehouse #{warehouse_id} {wh['code']} ({wh['name']}) "
        f"cleared {len(target_tables)} tables + zeroed item quantities"
    )
    flash(f"已初始化仓库 {wh['name']}({wh['code']}):业务数据已清空,品类/品项保留")
    return redirect(url_for("users.list_users"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_binding(form, db) -> tuple[int | None, str | None, str | None]:
    """Parse and validate warehouse_id + role from a form. Returns
    (wh_id, role, error_msg). error_msg is non-None when validation fails."""
    wh_id_raw = form.get("warehouse_id", "").strip()
    role = form.get("role", "").strip()
    if not wh_id_raw.isdigit():
        return None, None, "仓库 id 必填"
    wh_id = int(wh_id_raw)
    if role not in VALID_ROLES:
        return None, None, f"角色必须是 {sorted(VALID_ROLES)} 之一"
    if db.execute("SELECT 1 FROM warehouses WHERE id=?", (wh_id,)).fetchone() is None:
        return None, None, "仓库不存在"
    return wh_id, role, None


def _apply_bindings_from_form(user_id: int, form, db) -> None:
    """For the create form: pull all `wh_<id>` role select fields and bind."""
    for key, value in form.items():
        if not key.startswith("wh_"):
            continue
        raw_id = key[3:]
        if not raw_id.isdigit():
            continue
        wh_id = int(raw_id)
        if value in VALID_ROLES:
            db.execute(
                """INSERT OR IGNORE INTO warehouse_users (user_id, warehouse_id, role)
                   VALUES (?, ?, ?)""",
                (user_id, wh_id, value),
            )
    db.commit()


def _bindings_summary(user_id: int, db) -> str:
    rows = db.execute(
        """SELECT w.code, wu.role FROM warehouse_users wu
           JOIN warehouses w ON w.id = wu.warehouse_id
           WHERE wu.user_id=?""",
        (user_id,),
    ).fetchall()
    return ",".join(f"{r['code']}:{r['role']}" for r in rows)


def _log_admin_action(detail: str) -> None:
    """Append an admin action line to a simple flat file. Master.db has no
    audit_log table; the per-warehouse audit_log only covers business writes."""
    import json
    from pathlib import Path
    from flask import g
    log = Path(__file__).resolve().parent.parent / "admin_audit.log"
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "actor": g.user["username"] if g.user else None,
            "detail": detail,
        }, ensure_ascii=False) + "\n")


def _clone_catalog_from(src_path: Path, dst_path: Path) -> dict[str, int]:
    """Copy items / products / product_bom from src to dst.

    Preserves SKUs and per-item fields (safety_stock, unit_cost, unit,
    gram_per_unit, category_id, name) but resets quantity to 0 so the
    new warehouse starts empty. The fixed categories must already be
    seeded in dst by init_warehouse_db before calling this.

    Returns a {items, products, bom} count dict for the audit log.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(src_path)) as src, \
         closing(sqlite3.connect(dst_path)) as dst:
        src.row_factory = sqlite3.Row
        items = src.execute(
            """SELECT name, category_id, sku, safety_stock, unit_cost, unit,
                      gram_per_unit
               FROM items"""
        ).fetchall()
        dst.executemany(
            """INSERT INTO items
               (name, category_id, sku, quantity, safety_stock, unit_cost, unit,
                gram_per_unit, updated_at)
               VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)""",
            [(r["name"], r["category_id"], r["sku"], r["safety_stock"],
              r["unit_cost"], r["unit"], r["gram_per_unit"], now) for r in items],
        )

        products = src.execute(
            "SELECT name, unit, note, created_at FROM products"
        ).fetchall()
        dst.executemany(
            """INSERT INTO products (name, unit, note, created_at)
               VALUES (?, ?, ?, ?)""",
            [(r["name"], r["unit"], r["note"], now) for r in products],
        )

        # BOM preserves product_id / item_id from src. This relies on both
        # dbs inserting items and products in the same order so the
        # auto-increment ids line up — true because we just inserted them
        # in that order above. Cloning a fully-built warehouse this way
        # is the supported entry point; ad-hoc per-row clones aren't.
        bom = src.execute(
            "SELECT product_id, item_id, qty_per_unit FROM product_bom"
        ).fetchall()
        dst.executemany(
            """INSERT INTO product_bom (product_id, item_id, qty_per_unit)
               VALUES (?, ?, ?)""",
            [(r["product_id"], r["item_id"], r["qty_per_unit"]) for r in bom],
        )
        dst.commit()
    return {"items": len(items), "products": len(products), "bom": len(bom)}
