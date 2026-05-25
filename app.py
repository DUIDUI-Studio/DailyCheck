from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
import os
from typing import Any

from flask import Flask, flash, g, redirect, render_template, request, send_from_directory, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "inventory.db"
FIXED_CATEGORIES = ("包材", "原料", "工具", "成品")

app = Flask(__name__)
app.config["SECRET_KEY"] = "dev-key-change-me"


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_: Any) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def gen_sku() -> str:
    return f"AUTO-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"


def init_db() -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        category_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 0,
        safety_stock INTEGER NOT NULL DEFAULT 0,
        unit TEXT NOT NULL DEFAULT '件',
        updated_at TEXT NOT NULL,
        FOREIGN KEY (category_id) REFERENCES categories(id)
    );

    CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        delta INTEGER NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (item_id) REFERENCES items(id)
    );

    CREATE TABLE IF NOT EXISTS stocktakes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        previous_quantity INTEGER NOT NULL,
        actual_quantity INTEGER NOT NULL,
        diff INTEGER NOT NULL,
        batch_id INTEGER,
        created_at TEXT NOT NULL,
        note TEXT,
        FOREIGN KEY (item_id) REFERENCES items(id)
    );

    CREATE TABLE IF NOT EXISTS stocktake_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        rolled_back INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS restock_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        requested_quantity INTEGER NOT NULL,
        reason TEXT,
        status TEXT NOT NULL DEFAULT '提交',
        created_at TEXT NOT NULL,
        FOREIGN KEY (item_id) REFERENCES items(id)
    );

    CREATE TABLE IF NOT EXISTS outbound_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        requested_quantity INTEGER NOT NULL,
        reason TEXT,
        status TEXT NOT NULL DEFAULT '提交',
        rolled_back INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (item_id) REFERENCES items(id)
    );

    CREATE TABLE IF NOT EXISTS adjustment_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        adjusted_quantity INTEGER NOT NULL,
        reason TEXT,
        rolled_back INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (item_id) REFERENCES items(id)
    );

    CREATE TABLE IF NOT EXISTS daily_revenue (
        date TEXT NOT NULL PRIMARY KEY,
        amount REAL NOT NULL,
        created_at TEXT NOT NULL
    );
    """
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executescript(schema)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(stocktakes)").fetchall()]
        if "batch_id" not in cols:
            conn.execute("ALTER TABLE stocktakes ADD COLUMN batch_id INTEGER")
        outbound_cols = [
            r[1] for r in conn.execute("PRAGMA table_info(outbound_requests)").fetchall()
        ]
        if "rolled_back" not in outbound_cols:
            conn.execute(
                "ALTER TABLE outbound_requests ADD COLUMN rolled_back INTEGER NOT NULL DEFAULT 0"
            )

        items_cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
        if "unit_cost" not in items_cols:
            conn.execute("ALTER TABLE items ADD COLUMN unit_cost REAL NOT NULL DEFAULT 0")

        existing = {r[0] for r in conn.execute("SELECT name FROM categories").fetchall()}
        for name in FIXED_CATEGORIES:
            if name not in existing:
                conn.execute(
                    "INSERT INTO categories (name, description, created_at) VALUES (?, ?, ?)",
                    (name, "系统固定品类", now()),
                )
        conn.commit()


@app.route("/")
def dashboard():
    db = get_db()
    total_items = db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    total_categories = db.execute("SELECT COUNT(*) AS c FROM categories").fetchone()["c"]
    low_stock = db.execute(
        "SELECT COUNT(*) AS c FROM items WHERE quantity <= safety_stock"
    ).fetchone()["c"]
    pending_requests = db.execute(
        "SELECT COUNT(*) AS c FROM restock_requests WHERE status = '提交'"
    ).fetchone()["c"]

    today = datetime.now().strftime("%Y-%m-%d")
    outbound_today = db.execute(
        "SELECT COUNT(*) AS c FROM outbound_requests WHERE created_at LIKE ? || '%'",
        (today,),
    ).fetchone()["c"]
    inbound_today = db.execute(
        "SELECT COUNT(*) AS c FROM stock_movements WHERE action = '补货入库' AND created_at LIKE ? || '%'",
        (today,),
    ).fetchone()["c"]

    latest_movements = db.execute(
        """
        SELECT m.created_at, m.action, m.delta, i.name AS item_name
        FROM stock_movements m
        JOIN items i ON i.id = m.item_id
        ORDER BY m.id DESC
        LIMIT 8
        """
    ).fetchall()

    return render_template(
        "dashboard.html",
        total_items=total_items,
        total_categories=total_categories,
        low_stock=low_stock,
        pending_requests=pending_requests,
        outbound_today=outbound_today,
        inbound_today=inbound_today,
        latest_movements=latest_movements,
    )


@app.route("/summary")
def summary():
    db = get_db()

    # 进货金额 = 补货入库金额（initial_quantity 历史遗留字段已移除）
    total_inbound_value = db.execute(
        """
        SELECT COALESCE(SUM(i.unit_cost * sub.inbound_qty), 0) AS c
        FROM items i
        LEFT JOIN (
            SELECT m.item_id, SUM(m.delta) AS inbound_qty
            FROM stock_movements m WHERE m.action = '补货入库' GROUP BY m.item_id
        ) sub ON sub.item_id = i.id
        """
    ).fetchone()["c"]

    total_consumed_value = db.execute(
        """
        SELECT COALESCE(SUM(ABS(m.delta) * i.unit_cost), 0) AS c
        FROM stock_movements m
        JOIN items i ON i.id = m.item_id
        WHERE m.action = '出库'
        """
    ).fetchone()["c"]

    total_stock_value = db.execute(
        "SELECT COALESCE(SUM(quantity * unit_cost), 0) AS c FROM items"
    ).fetchone()["c"]

    # 营业额：读取 daily_revenue 表
    total_revenue = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS c FROM daily_revenue"
    ).fetchone()["c"]

    # 按品类统计：库存结余用实际当前值（items.quantity × unit_cost）
    # 进货/消耗保持历史流量累计
    cat_data = db.execute(
        """
        SELECT
            c.name AS category_name,
            COALESCE(SUM(item_vals.init_value), 0) AS init_value,
            COALESCE(SUM(item_vals.restock_value), 0) AS restock_value,
            COALESCE(SUM(item_vals.consumed_value), 0) AS consumed_value,
            COALESCE(SUM(item_vals.current_stock_value), 0) AS stock_value
        FROM categories c
        LEFT JOIN (
            SELECT
                i.category_id,
                0 AS init_value,
                COALESCE(sub.inbound_qty, 0) * i.unit_cost AS restock_value,
                COALESCE(out.consumed_qty, 0) * i.unit_cost AS consumed_value,
                i.quantity * i.unit_cost AS current_stock_value
            FROM items i
            LEFT JOIN (
                SELECT item_id, SUM(delta) AS inbound_qty
                FROM stock_movements WHERE action = '补货入库' GROUP BY item_id
            ) sub ON sub.item_id = i.id
            LEFT JOIN (
                SELECT item_id, SUM(ABS(delta)) AS consumed_qty
                FROM stock_movements WHERE action = '出库' GROUP BY item_id
            ) out ON out.item_id = i.id
        ) item_vals ON item_vals.category_id = c.id
        GROUP BY c.id, c.name
        ORDER BY c.id
        """
    ).fetchall()

    enriched_stats = []
    for row in cat_data:
        stock_value = round(row["stock_value"], 2)
        consumed_value = round(row["consumed_value"], 2)
        inbound_value = round(row["init_value"] + row["restock_value"], 2)
        enriched_stats.append({
            "category_name": row["category_name"],
            "inbound_value": inbound_value,
            "consumed_value": consumed_value,
            "stock_value": stock_value,
        })

    top_consumed = db.execute(
        """
        SELECT i.name AS item_name, c.name AS category_name,
               ABS(SUM(m.delta)) AS consumed_qty,
               i.unit,
               ROUND(ABS(SUM(m.delta)) * i.unit_cost, 2) AS consumed_value
        FROM stock_movements m
        JOIN items i ON i.id = m.item_id
        JOIN categories c ON c.id = i.category_id
        WHERE m.action = '出库'
        GROUP BY m.item_id
        ORDER BY consumed_qty DESC
        """
    ).fetchall()

    return render_template(
        "summary.html",
        total_inbound_value=round(total_inbound_value, 2),
        total_consumed_value=round(total_consumed_value, 2),
        total_stock_value=round(total_stock_value, 2),
        total_revenue=round(total_revenue, 2),
        category_stats=enriched_stats,
        top_consumed=top_consumed,
    )


@app.route("/export/consumption")
def export_consumption():
    db = get_db()
    rows = db.execute(
        """
        SELECT i.name AS item_name, c.name AS category_name,
               ABS(SUM(m.delta)) AS consumed_qty,
               i.unit,
               ROUND(i.unit_cost, 3) AS unit_cost,
               ROUND(ABS(SUM(m.delta)) * i.unit_cost, 2) AS consumed_value
        FROM stock_movements m
        JOIN items i ON i.id = m.item_id
        JOIN categories c ON c.id = i.category_id
        WHERE m.action = '出库'
        GROUP BY m.item_id
        ORDER BY c.name, i.name
        """
    ).fetchall()

    import csv, io

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["品类", "品项", "消耗数量", "单位", "单价", "消耗金额"])
    for r in rows:
        writer.writerow([
            r["category_name"],
            r["item_name"],
            r["consumed_qty"],
            r["unit"],
            r["unit_cost"],
            r["consumed_value"],
        ])

    response = app.response_class(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=consumption.csv"},
    )
    return response


@app.route("/categories", methods=["GET"])
def categories():
    db = get_db()
    rows = db.execute(
        f"SELECT * FROM categories WHERE name IN ({','.join('?' for _ in FIXED_CATEGORIES)}) ORDER BY id ASC",
        FIXED_CATEGORIES,
    ).fetchall()
    return render_template("categories.html", categories=rows)


@app.route("/categories/<int:category_id>/delete", methods=["POST"])
def delete_category(category_id: int):
    flash("品类为系统固定项，不支持删除")
    return redirect(url_for("categories"))


@app.route("/items", methods=["GET", "POST"])
def items():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category_id = request.form.get("category_id", "").strip()
        quantity = int(request.form.get("quantity", "0") or 0)
        safety_stock = int(request.form.get("safety_stock", "0") or 0)
        unit_cost = float(request.form.get("unit_cost", "0") or 0)
        unit = request.form.get("unit", "件").strip() or "件"

        if not name or not category_id:
            flash("名称、品类为必填")
            return redirect(url_for("items"))
        try:
            db.execute(
                """
                INSERT INTO items (sku, name, category_id, quantity, safety_stock, unit_cost, unit, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (gen_sku(), name, int(category_id), quantity, safety_stock, unit_cost, unit, now()),
            )
            db.commit()
            flash("库存品创建成功")
        except sqlite3.IntegrityError:
            flash("库存品创建失败，请重试")
        return redirect(url_for("items"))

    categories_data = db.execute(
        f"SELECT id, name FROM categories WHERE name IN ({','.join('?' for _ in FIXED_CATEGORIES)}) ORDER BY name",
        FIXED_CATEGORIES,
    ).fetchall()
    rows = db.execute(
        """
        SELECT i.*, c.name AS category_name
        FROM items i
        JOIN categories c ON c.id = i.category_id
        ORDER BY i.id DESC
        """
    ).fetchall()
    return render_template("items.html", items=rows, categories=categories_data)


@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
def edit_item(item_id: int):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        flash("库存品不存在")
        return redirect(url_for("items"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category_id = request.form.get("category_id", "").strip()
        quantity = int(request.form.get("quantity", "0") or 0)
        safety_stock = int(request.form.get("safety_stock", "0") or 0)
        unit_cost = float(request.form.get("unit_cost", "0") or 0)
        unit = request.form.get("unit", "件").strip() or "件"

        if not name or not category_id:
            flash("名称、品类为必填")
            return redirect(url_for("edit_item", item_id=item_id))

        db.execute(
            """
            UPDATE items
            SET name = ?, category_id = ?, quantity = ?, safety_stock = ?, unit_cost = ?, unit = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, int(category_id), quantity, safety_stock, unit_cost, unit, now(), item_id),
        )
        db.commit()
        flash("库存品已更新")
        return redirect(url_for("items"))

    categories_data = db.execute(
        f"SELECT id, name FROM categories WHERE name IN ({','.join('?' for _ in FIXED_CATEGORIES)}) ORDER BY name",
        FIXED_CATEGORIES,
    ).fetchall()
    return render_template("edit_item.html", item=item, categories=categories_data)


@app.route("/items/<int:item_id>/delete", methods=["POST"])
def delete_item(item_id: int):
    db = get_db()
    movement_count = db.execute(
        "SELECT COUNT(*) AS c FROM stock_movements WHERE item_id = ?", (item_id,)
    ).fetchone()["c"]
    stocktake_count = db.execute(
        "SELECT COUNT(*) AS c FROM stocktakes WHERE item_id = ?", (item_id,)
    ).fetchone()["c"]
    restock_count = db.execute(
        "SELECT COUNT(*) AS c FROM restock_requests WHERE item_id = ?", (item_id,)
    ).fetchone()["c"]
    outbound_count = db.execute(
        "SELECT COUNT(*) AS c FROM outbound_requests WHERE item_id = ?", (item_id,)
    ).fetchone()["c"]

    if movement_count + stocktake_count + restock_count + outbound_count > 0:
        flash("该库存品已有业务记录，暂不允许删除")
        return redirect(url_for("items"))

    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    flash("库存品已删除")
    return redirect(url_for("items"))


@app.route("/stock-in", methods=["GET", "POST"])
def stock_in():
    flash("入库页已下线，请在补货记录页完成提交与入库")
    return redirect(url_for("restock"))


@app.after_request
def add_cache_headers(resp):
    if request.path.startswith("/stocktake") or request.path == "/":
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.route("/stocktake", methods=["GET"])
def stocktake():
    db = get_db()
    batches = db.execute(
        """
        SELECT b.id, b.created_at, b.note, b.rolled_back, COUNT(s.id) AS item_count
        FROM stocktake_batches b
        LEFT JOIN stocktakes s ON s.batch_id = b.id
        GROUP BY b.id
        ORDER BY b.id DESC
        LIMIT 20
        """
    ).fetchall()
    return render_template("stocktake.html", batches=batches)


@app.route("/stocktake/start", methods=["POST"])
def stocktake_start():
    return redirect(url_for("stocktake_session"))


@app.route("/stocktake/session", methods=["GET"])
def stocktake_session():
    db = get_db()
    items_data = db.execute(
        """
        SELECT i.id, i.name, i.quantity, i.unit, i.safety_stock, c.name AS category_name
        FROM items i
        JOIN categories c ON c.id = i.category_id
        ORDER BY c.name, i.name
        """
    ).fetchall()
    return render_template("stocktake_session.html", items=items_data)


@app.route("/stocktake/submit", methods=["POST"])
def stocktake_submit():
    db = get_db()
    note = request.form.get("note", "").strip()
    items_data = db.execute("SELECT id, quantity FROM items").fetchall()

    changed_rows: list[tuple[int, int, int, int]] = []
    for item in items_data:
        field = f"actual_{item['id']}"
        raw = request.form.get(field, "").strip()
        if raw == "":
            continue
        actual_quantity = int(raw)
        previous_quantity = int(item["quantity"])
        diff = actual_quantity - previous_quantity
        changed_rows.append((int(item["id"]), previous_quantity, actual_quantity, diff))

    if not changed_rows:
        flash("请至少填写一个盘点数量")
        return redirect(url_for("stocktake_session"))

    cur = db.execute(
        "INSERT INTO stocktake_batches (created_at, note, status, rolled_back) VALUES (?, ?, 'pending', 0)",
        (now(), note),
    )
    batch_id = cur.lastrowid

    for item_id, previous_quantity, actual_quantity, diff in changed_rows:
        db.execute(
            """
            INSERT INTO stocktakes (item_id, previous_quantity, actual_quantity, diff, batch_id, created_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, previous_quantity, actual_quantity, diff, batch_id, now(), note),
        )
    db.commit()
    flash(f"盘点批次 #{batch_id} 已提交，等待审核")
    return redirect(url_for("stocktake"))


@app.route("/stocktake/batch/<int:batch_id>/rollback", methods=["POST"])
def rollback_stocktake_batch(batch_id: int):
    db = get_db()
    batch = db.execute(
        "SELECT id, status, rolled_back FROM stocktake_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        flash("盘点批次不存在")
        return redirect(url_for("stocktake"))
    if int(batch["rolled_back"]) == 1:
        flash("该批次已回滚，无需重复操作")
        return redirect(url_for("stocktake"))

    records = db.execute(
        "SELECT item_id, diff FROM stocktakes WHERE batch_id = ?", (batch_id,)
    ).fetchall()

    for record in records:
        diff = int(record["diff"])
        if diff == 0:
            continue
        item_id = int(record["item_id"])
        db.execute(
            "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
            (diff, now(), item_id),
        )
        db.execute(
            "INSERT INTO stock_movements (item_id, action, delta, note, created_at) VALUES (?, '盘点回滚', ?, ?, ?)",
            (item_id, -diff, f"回滚盘点批次#{batch_id}", now()),
        )

    db.execute("UPDATE stocktake_batches SET rolled_back = 1, status = 'rolled_back' WHERE id = ?", (batch_id,))
    db.commit()
    flash(f"盘点批次 #{batch_id} 已回滚")
    return redirect(url_for("stocktake"))


@app.route("/stocktake/batch/<int:batch_id>/edit", methods=["GET"])
def edit_stocktake_batch(batch_id: int):
    db = get_db()
    batch = db.execute(
        "SELECT id, note, rolled_back FROM stocktake_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        flash("盘点批次不存在")
        return redirect(url_for("stocktake"))

    records = db.execute(
        """
        SELECT s.id, s.item_id, s.previous_quantity, s.actual_quantity, s.diff,
               i.name AS item_name, i.quantity AS current_quantity, i.unit,
               c.name AS category_name
        FROM stocktakes s
        JOIN items i ON i.id = s.item_id
        JOIN categories c ON c.id = i.category_id
        WHERE s.batch_id = ?
        ORDER BY c.name, i.name
        """,
        (batch_id,),
    ).fetchall()
    return render_template("stocktake_edit.html", batch=batch, records=records)


@app.route("/stocktake/batch/<int:batch_id>/edit", methods=["POST"])
def submit_stocktake_edit(batch_id: int):
    db = get_db()
    batch = db.execute(
        "SELECT id, rolled_back FROM stocktake_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        flash("盘点批次不存在")
        return redirect(url_for("stocktake"))
    if int(batch["rolled_back"]) == 1:
        flash("已回滚的批次不能修改")
        return redirect(url_for("stocktake"))

    records = db.execute("SELECT id, item_id FROM stocktakes WHERE batch_id = ?", (batch_id,)).fetchall()
    changed = 0
    for rec in records:
        field = f"actual_{rec['item_id']}"
        raw = request.form.get(field, "").strip()
        if raw == "":
            continue
        try:
            new_actual = int(raw)
        except ValueError:
            continue
        current_qty = db.execute(
            "SELECT quantity FROM items WHERE id = ?", (int(rec["item_id"]),)
        ).fetchone()["quantity"]
        new_diff = new_actual - current_qty

        if new_diff == 0:
            continue

        db.execute(
            "UPDATE items SET quantity = ?, updated_at = ? WHERE id = ?",
            (new_actual, now(), int(rec["item_id"])),
        )
        db.execute(
            "UPDATE stocktakes SET actual_quantity = ?, diff = ? WHERE id = ?",
            (new_actual, new_diff, int(rec["id"])),
        )
        db.execute(
            """
            INSERT INTO stock_movements (item_id, action, delta, note, created_at)
            VALUES (?, '盘点修正', ?, ?, ?)
            """,
            (int(rec["item_id"]), new_diff, f"修正盘点批次#{batch_id}", now()),
        )
        changed += 1

    if changed == 0:
        flash("未检测到变更")
    else:
        db.commit()
        flash(f"盘点批次 #{batch_id} 已更新 {changed} 项")
    return redirect(url_for("stocktake"))


@app.route("/inventory")
def inventory():
    db = get_db()
    q = request.args.get("q", "").strip()
    if q:
        rows = db.execute(
            """
            SELECT i.*, c.name AS category_name
            FROM items i
            JOIN categories c ON c.id = i.category_id
            WHERE i.name LIKE ? OR c.name LIKE ?
            ORDER BY i.updated_at DESC
            """,
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT i.*, c.name AS category_name
            FROM items i
            JOIN categories c ON c.id = i.category_id
            ORDER BY i.updated_at DESC
            """
        ).fetchall()
    return render_template("inventory.html", items=rows, q=q)


@app.route("/restock", methods=["GET"])
def restock():
    db = get_db()
    requests_data = db.execute(
        """
        SELECT r.*, i.name AS item_name, i.unit
        FROM restock_requests r
        JOIN items i ON i.id = r.item_id
        ORDER BY r.id DESC
        LIMIT 100
        """
    ).fetchall()
    return render_template("restock.html", requests=requests_data)


@app.route("/restock/start", methods=["POST"])
def restock_start():
    return redirect(url_for("restock_session"))


@app.route("/restock/session", methods=["GET"])
def restock_session():
    db = get_db()
    items_data = db.execute(
        """
        SELECT i.id, i.name, i.quantity, i.unit, i.safety_stock, c.name AS category_name
        FROM items i
        JOIN categories c ON c.id = i.category_id
        ORDER BY c.name, i.name
        """
    ).fetchall()
    return render_template("restock_session.html", items=items_data)


@app.route("/restock/submit", methods=["POST"])
def restock_submit():
    db = get_db()
    reason = request.form.get("reason", "").strip()
    items_data = db.execute("SELECT id FROM items").fetchall()
    rows: list[tuple[int, int]] = []
    for item in items_data:
        raw = request.form.get(f"restock_{item['id']}", "").strip()
        if raw == "":
            continue
        qty = int(raw)
        if qty > 0:
            rows.append((int(item["id"]), qty))
    if not rows:
        flash("请至少填写一个补货数量")
        return redirect(url_for("restock_session"))
    for item_id, qty in rows:
        cur = db.execute(
            """
            INSERT INTO restock_requests (item_id, requested_quantity, reason, status, created_at)
            VALUES (?, ?, ?, '入库', ?)
            """,
            (item_id, qty, reason, now()),
        )
        req_id = int(cur.lastrowid)
        db.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (qty, now(), item_id),
        )
        db.execute(
            """
            INSERT INTO stock_movements (item_id, action, delta, note, created_at)
            VALUES (?, '补货入库', ?, ?, ?)
            """,
            (item_id, qty, f"补货记录#{req_id}入库", now()),
        )
    db.commit()
    flash("入库已执行")
    return redirect(url_for("restock"))


@app.route("/restock/<int:req_id>/status", methods=["POST"])
def update_restock_status(req_id: int):
    status = request.form.get("status", "").strip()
    if status not in {"提交", "入库"}:
        flash("状态非法")
        return redirect(url_for("restock"))

    db = get_db()
    req = db.execute(
        "SELECT item_id, requested_quantity, status FROM restock_requests WHERE id = ?",
        (req_id,),
    ).fetchone()
    if req is None:
        flash("补货记录不存在")
        return redirect(url_for("restock"))

    old_status = req["status"]
    if old_status != "入库" and status == "入库":
        db.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (int(req["requested_quantity"]), now(), int(req["item_id"])),
        )
        db.execute(
            """
            INSERT INTO stock_movements (item_id, action, delta, note, created_at)
            VALUES (?, '补货入库', ?, ?, ?)
            """,
            (
                int(req["item_id"]),
                int(req["requested_quantity"]),
                f"补货记录#{req_id}入库",
                now(),
            ),
        )
    db.execute("UPDATE restock_requests SET status = ? WHERE id = ?", (status, req_id))
    db.commit()
    flash("补货记录状态已更新")
    return redirect(url_for("restock"))


@app.route("/restock/<int:req_id>/delete", methods=["POST"])
def delete_restock(req_id: int):
    db = get_db()
    req = db.execute(
        "SELECT item_id, requested_quantity, status FROM restock_requests WHERE id = ?",
        (req_id,),
    ).fetchone()
    if req is None:
        flash("补货记录不存在")
        return redirect(url_for("restock"))

    if req["status"] == "入库":
        item = db.execute(
            "SELECT quantity FROM items WHERE id = ?",
            (int(req["item_id"]),),
        ).fetchone()
        if item is None:
            flash("库存品不存在，无法删除该记录")
            return redirect(url_for("restock"))
        if int(item["quantity"]) < int(req["requested_quantity"]):
            flash("当前库存不足，无法通过删除回滚该入库记录")
            return redirect(url_for("restock"))

        db.execute(
            "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
            (int(req["requested_quantity"]), now(), int(req["item_id"])),
        )
        db.execute(
            """
            INSERT INTO stock_movements (item_id, action, delta, note, created_at)
            VALUES (?, '补货删除回滚', ?, ?, ?)
            """,
            (
                int(req["item_id"]),
                -int(req["requested_quantity"]),
                f"删除补货记录#{req_id}回滚",
                now(),
            ),
        )

    db.execute("DELETE FROM restock_requests WHERE id = ?", (req_id,))
    db.commit()
    flash("补货记录已删除")
    return redirect(url_for("restock"))


@app.route("/outbound", methods=["GET"])
def outbound():
    db = get_db()
    requests_data = db.execute(
        """
        SELECT o.*, i.name AS item_name, i.unit
        FROM outbound_requests o
        JOIN items i ON i.id = o.item_id
        ORDER BY o.id DESC
        LIMIT 100
        """
    ).fetchall()
    return render_template("outbound.html", requests=requests_data)


@app.route("/outbound/start", methods=["POST"])
def outbound_start():
    return redirect(url_for("outbound_session"))


@app.route("/outbound/session", methods=["GET"])
def outbound_session():
    db = get_db()
    items_data = db.execute(
        """
        SELECT i.id, i.name, i.quantity, i.unit, i.safety_stock, c.name AS category_name
        FROM items i
        JOIN categories c ON c.id = i.category_id
        ORDER BY c.name, i.name
        """
    ).fetchall()
    return render_template("outbound_session.html", items=items_data)


@app.route("/outbound/submit", methods=["POST"])
def outbound_submit():
    db = get_db()
    reason = request.form.get("reason", "").strip()
    items_data = db.execute("SELECT id, quantity FROM items").fetchall()
    rows: list[tuple[int, int]] = []
    for item in items_data:
        raw = request.form.get(f"outbound_{item['id']}", "").strip()
        if raw == "":
            continue
        qty = int(raw)
        if qty <= 0:
            continue
        if qty > int(item["quantity"]):
            flash("存在出库数量大于当前库存的品项，请检查后重试")
            return redirect(url_for("outbound_session"))
        rows.append((int(item["id"]), qty))
    if not rows:
        flash("请至少填写一个出库数量")
        return redirect(url_for("outbound_session"))
    for item_id, qty in rows:
        cur = db.execute(
            """
            INSERT INTO outbound_requests (item_id, requested_quantity, reason, status, rolled_back, created_at)
            VALUES (?, ?, ?, '出库', 0, ?)
            """,
            (item_id, qty, reason, now()),
        )
        req_id = int(cur.lastrowid)
        db.execute(
            "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
            (qty, now(), item_id),
        )
        db.execute(
            """
            INSERT INTO stock_movements (item_id, action, delta, note, created_at)
            VALUES (?, '出库', ?, ?, ?)
            """,
            (item_id, -qty, f"出库记录#{req_id}出库", now()),
        )
    db.commit()
    flash("出库已执行")
    return redirect(url_for("outbound"))


@app.route("/outbound/<int:req_id>/rollback", methods=["POST"])
def rollback_outbound(req_id: int):
    db = get_db()
    req = db.execute(
        """
        SELECT item_id, requested_quantity, rolled_back
        FROM outbound_requests
        WHERE id = ? AND status = '出库'
        """,
        (req_id,),
    ).fetchone()
    if req is None:
        flash("出库记录不存在")
        return redirect(url_for("outbound"))
    if int(req["rolled_back"]) == 1:
        flash("该记录已回退，无需重复操作")
        return redirect(url_for("outbound"))
    db.execute(
        "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
        (int(req["requested_quantity"]), now(), int(req["item_id"])),
    )
    db.execute(
        """
        INSERT INTO stock_movements (item_id, action, delta, note, created_at)
        VALUES (?, '出库回退', ?, ?, ?)
        """,
        (
            int(req["item_id"]),
            int(req["requested_quantity"]),
            f"回退出库记录#{req_id}",
            now(),
        ),
    )
    db.execute("UPDATE outbound_requests SET rolled_back = 1 WHERE id = ?", (req_id,))
    db.commit()
    flash("出库记录已回退")
    return redirect(url_for("outbound"))


@app.route("/outbound/<int:req_id>/delete", methods=["POST"])
def delete_outbound(req_id: int):
    db = get_db()
    db.execute("DELETE FROM outbound_requests WHERE id = ?", (req_id,))
    db.commit()
    flash("出库记录已删除")
    return redirect(url_for("outbound"))


@app.route("/adjustment", methods=["GET"])
def adjustment():
    db = get_db()
    requests_data = db.execute(
        """
        SELECT a.*, i.name AS item_name, i.unit
        FROM adjustment_requests a
        JOIN items i ON i.id = a.item_id
        ORDER BY a.id DESC
        LIMIT 100
        """
    ).fetchall()
    return render_template("adjustment.html", requests=requests_data)


@app.route("/adjustment/session", methods=["GET"])
def adjustment_session():
    db = get_db()
    items_data = db.execute(
        """
        SELECT i.id, i.name, i.quantity, i.unit, i.safety_stock, c.name AS category_name
        FROM items i
        JOIN categories c ON c.id = i.category_id
        ORDER BY c.name, i.name
        """
    ).fetchall()
    return render_template("adjustment_session.html", items=items_data)


@app.route("/adjustment/submit", methods=["POST"])
def adjustment_submit():
    db = get_db()
    reason = request.form.get("reason", "").strip()
    items_data = db.execute("SELECT id, quantity FROM items").fetchall()

    rows: list[tuple[int, int]] = []
    for item in items_data:
        raw = request.form.get(f"adjustment_{item['id']}", "").strip()
        if raw == "":
            continue
        qty = int(raw)
        if qty <= 0:
            continue
        if qty > int(item["quantity"]):
            flash("存在调整数量大于当前库存的品项，请检查后重试")
            return redirect(url_for("adjustment_session"))
        rows.append((int(item["id"]), qty))

    if not rows:
        flash("请至少填写一个调整数量")
        return redirect(url_for("adjustment_session"))

    for item_id, qty in rows:
        cur = db.execute(
            """
            INSERT INTO adjustment_requests (item_id, adjusted_quantity, reason, rolled_back, created_at)
            VALUES (?, ?, ?, 0, ?)
            """,
            (item_id, qty, reason, now()),
        )
        req_id = int(cur.lastrowid)
        db.execute(
            "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
            (qty, now(), item_id),
        )
        db.execute(
            """
            INSERT INTO stock_movements (item_id, action, delta, note, created_at)
            VALUES (?, '调整出库', ?, ?, ?)
            """,
            (item_id, -qty, f"调整单#{req_id}", now()),
        )
    db.commit()
    flash("调整单已执行")
    return redirect(url_for("adjustment"))


@app.route("/adjustment/<int:req_id>/rollback", methods=["POST"])
def rollback_adjustment(req_id: int):
    db = get_db()
    req = db.execute(
        "SELECT item_id, adjusted_quantity, rolled_back FROM adjustment_requests WHERE id = ?",
        (req_id,),
    ).fetchone()
    if req is None:
        flash("调整记录不存在")
        return redirect(url_for("adjustment"))
    if int(req["rolled_back"]) == 1:
        flash("该记录已回退，无需重复操作")
        return redirect(url_for("adjustment"))

    db.execute(
        "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
        (int(req["adjusted_quantity"]), now(), int(req["item_id"])),
    )
    db.execute(
        """
        INSERT INTO stock_movements (item_id, action, delta, note, created_at)
        VALUES (?, '调整出库回滚', ?, ?, ?)
        """,
        (int(req["item_id"]), int(req["adjusted_quantity"]), f"回滚调整单#{req_id}", now()),
    )
    db.execute("UPDATE adjustment_requests SET rolled_back = 1 WHERE id = ?", (req_id,))
    db.commit()
    flash("调整记录已回退")
    return redirect(url_for("adjustment"))


@app.route("/adjustment/<int:req_id>/delete", methods=["POST"])
def delete_adjustment(req_id: int):
    db = get_db()
    db.execute("DELETE FROM adjustment_requests WHERE id = ?", (req_id,))
    db.commit()
    flash("调整记录已删除")
    return redirect(url_for("adjustment"))


@app.route("/report/outbound")
def report_outbound():
    db = get_db()
    scope = request.args.get("scope", "today")
    today = datetime.now().strftime("%Y-%m-%d")
    if scope == "all":
        # All 出库 movements grouped by date
        raw = db.execute(
            """
            SELECT i.name AS item_name, i.unit, ABS(m.delta) AS qty, m.created_at
            FROM stock_movements m
            JOIN items i ON i.id = m.item_id
            WHERE m.action = '出库'
            ORDER BY m.created_at ASC
            """
        ).fetchall()

        daily: dict[str, dict[str, int]] = {}
        for r in raw:
            d = r["created_at"][:10]
            key = (r["item_name"], d)
            if key not in daily:
                daily[key] = 0
            daily[key] += r["qty"]

        # All items (not just those with outbound movements)
        all_items_rows = db.execute("SELECT name, unit FROM items ORDER BY id").fetchall()
        item_names_order = [r["name"] for r in all_items_rows]
        all_items = {r["name"]: r["unit"] for r in all_items_rows}

        all_dates = sorted({r["created_at"][:10] for r in raw})
        records = []
        for item in item_names_order:
            row: dict[str, str | int] = {"item_name": item, "unit": all_items[item]}
            for d in all_dates:
                row[d] = daily.get((item, d), 0)
            records.append(row)

        return render_template(
            "report_outbound.html", records=records, date=today, scope=scope,
            dates=all_dates,
        )
    else:
        records = db.execute(
            """
            SELECT m.item_id, i.name AS item_name, i.unit, ABS(SUM(m.delta)) AS total_qty, COUNT(*) AS times,
                   MAX(m.created_at) AS last_time
            FROM stock_movements m
            JOIN items i ON i.id = m.item_id
            WHERE m.action = '出库' AND m.created_at LIKE ? || '%'
            GROUP BY m.item_id
            ORDER BY last_time DESC
            """,
            (today,),
        ).fetchall()
    return render_template("report_outbound.html", records=records, date=today, scope=scope)


@app.route("/report/inbound")
def report_inbound():
    db = get_db()
    scope = request.args.get("scope", "today")
    today = datetime.now().strftime("%Y-%m-%d")
    if scope == "all":
        raw = db.execute(
            """
            SELECT i.name AS item_name, i.unit, m.delta AS qty, m.created_at
            FROM stock_movements m
            JOIN items i ON i.id = m.item_id
            WHERE m.action = '补货入库'
            ORDER BY m.created_at ASC
            """
        ).fetchall()

        daily: dict[str, dict[str, int]] = {}
        for r in raw:
            d = r["created_at"][:10]
            key = (r["item_name"], d)
            if key not in daily:
                daily[key] = 0
            daily[key] += r["qty"]

        # All items (not just those with inbound movements)
        all_items_rows = db.execute("SELECT name, unit FROM items ORDER BY id").fetchall()
        item_names_order = [r["name"] for r in all_items_rows]
        all_items = {r["name"]: r["unit"] for r in all_items_rows}

        # Initial stock no longer tracked in DB; default to 0
        init_stock = {}

        all_dates = sorted({r["created_at"][:10] for r in raw})
        records = []
        for item in item_names_order:
            row: dict[str, str | int] = {"item_name": item, "unit": all_items[item]}
            row["初始库存"] = init_stock.get(item, 0)
            for d in all_dates:
                row[d] = daily.get((item, d), 0)
            records.append(row)

        return render_template(
            "report_inbound.html", records=records, date=today, scope=scope,
            dates=all_dates,
        )
    else:
        records = db.execute(
            """
            SELECT m.item_id, i.name AS item_name, i.unit, SUM(m.delta) AS total_qty, COUNT(*) AS times,
               MAX(m.created_at) AS last_time
            FROM stock_movements m
            JOIN items i ON i.id = m.item_id
            WHERE m.action = '补货入库' AND m.created_at LIKE ? || '%'
            GROUP BY m.item_id
            ORDER BY last_time DESC
            """,
            (today,),
        ).fetchall()
    return render_template("report_inbound.html", records=records, date=today, scope=scope)


@app.route("/sw.js")
def service_worker():
    return send_from_directory(BASE_DIR / "static", "sw.js")


@app.route("/manifest.webmanifest")
def webmanifest():
    return send_from_directory(BASE_DIR / "static", "manifest.webmanifest")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")), debug=os.getenv("FLASK_DEBUG", "") == "1")


@app.route("/api/revenue", methods=["POST"])
def api_upload_revenue():
    import os
    token = os.getenv("REVENUE_TOKEN", "")
    expected = request.form.get("token", "")
    if token and expected != token:
        return "Unauthorized", 401

    date_str = request.form.get("date", "").strip()
    amount_str = request.form.get("amount", "0").strip()

    if not date_str:
        return "Missing date", 400

    try:
        amount = float(amount_str)
    except ValueError:
        return "Invalid amount", 400

    db = get_db()
    cur = db.execute(
        """
        INSERT INTO daily_revenue (date, amount, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET amount = excluded.amount, created_at = excluded.created_at
        """,
        (date_str, amount, now()),
    )
    db.commit()
    return f"OK {date_str}={amount}"


if os.environ.get("GUNICORN_MASTER") != "1" and os.environ.get("RUNAPP") == "1":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")), debug=os.getenv("FLASK_DEBUG", "") == "1")


@app.route("/stocktake/batch/<int:batch_id>/approve", methods=["POST"])
def approve_stocktake_batch(batch_id: int):
    db = get_db()
    batch = db.execute(
        "SELECT id, status, rolled_back FROM stocktake_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        flash("盘点批次不存在")
        return redirect(url_for("stocktake"))
    if batch["status"] == "approved":
        flash("该批次已审核通过，无需重复操作")
        return redirect(url_for("stocktake"))
    if int(batch["rolled_back"]) == 1:
        flash("该批次已回滚，无法审核")
        return redirect(url_for("stocktake"))

    records = db.execute(
        "SELECT item_id, diff FROM stocktakes WHERE batch_id = ?", (batch_id,)
    ).fetchall()

    loss_items = []
    gain_items = []
    for record in records:
        diff = int(record["diff"])
        if diff == 0:
            continue
        item_id = int(record["item_id"])
        if diff < 0:
            loss_items.append((item_id, diff))
        else:
            gain_items.append((item_id, diff))

    for item_id, diff in loss_items + gain_items:
        db.execute(
            "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (diff, now(), item_id),
        )

    for item_id, diff in loss_items:
        db.execute(
            "INSERT INTO stock_movements (item_id, action, delta, note, created_at) VALUES (?, '出库', ?, ?, ?)",
            (item_id, diff, f"盘点审核#{batch_id}盘亏", now()),
        )

    for item_id, diff in gain_items:
        db.execute(
            "INSERT INTO stock_movements (item_id, action, delta, note, created_at) VALUES (?, '库存调整', ?, ?, ?)",
            (item_id, diff, f"盘点审核#{batch_id}盘盈", now()),
        )

    db.execute(
        "UPDATE stocktake_batches SET status = 'approved' WHERE id = ?", (batch_id,)
    )
    db.commit()

    msg = f"盘点批次 #{batch_id} 已审核"
    if loss_items:
        msg += f"，{len(loss_items)}项盘亏已计入出库"
    if gain_items:
        msg += f"，{len(gain_items)}项盘盈已调整为库存"
    flash(msg)
    return redirect(url_for("stocktake"))



