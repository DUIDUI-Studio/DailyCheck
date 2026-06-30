"""Generate 30 days of realistic demo data into the default master.db +
wh_001.db so the new Phase 1+2+3 features have content to render.

Usage:
  /Users/ericmr/Documents/GitHub/DailyCheck/.venv/bin/python scripts/seed_demo_data.py
"""
from __future__ import annotations

import os
import random
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

# Allow running from project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import MASTER_DB, WAREHOUSE_DB_DIR  # noqa: E402

random.seed(42)

WAREHOUSE_CODES = ["wh_001", "wh_002", "wh_003"]  # created by create-warehouse
DAYS_BACK = 30
NOW = datetime.now()


def _now_str() -> str:
    return NOW.strftime("%Y-%m-%d %H:%M:%S")


def _days_ago(n: int) -> str:
    return (NOW - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Item catalog (per warehouse)
# ---------------------------------------------------------------------------
# 8 items, deterministic qty + cost.
ITEMS = [
    ("茶叶-红茶",  "辅料",      50.0,  30.0, "g",   120.0),
    ("茶叶-绿茶",  "辅料",      40.0,  28.0, "g",   100.0),
    ("木樨子油",   "调味酱",     0.5, 180.0, "瓶",    1.0),
    ("柠檬浓缩液", "调味酱 分", 12.0,  45.0, "瓶",   10.0),
    ("白砂糖",     "辅料",     100.0,   6.0, "kg",   50.0),
    ("冰块",       "生产消耗品",  0.0,   0.0, "kg",    0.0),  # qty=0, cold-start candidate
    ("珍珠",       "辅料",      30.0,  12.0, "g",    50.0),
    ("奶精粉",     "辅料",      25.0,  35.0, "g",    40.0),
]

# 2 products, each with a BOM
PRODUCTS = [
    ("经典柠檬茶", "经典柠檬茶 v3 配方"),
    ("珍珠奶茶",   "珍珠奶茶 v2 配方"),
]
PRODUCT_BOM = {
    "经典柠檬茶": [
        ("茶叶-红茶",  5.0),
        ("木樨子油",   0.02),
        ("柠檬浓缩液", 0.1),
        ("白砂糖",     30.0),
    ],
    "珍珠奶茶": [
        ("茶叶-绿茶",  5.0),
        ("珍珠",       30.0),
        ("奶精粉",     20.0),
        ("白砂糖",     20.0),
    ],
}


def _seed_master_users() -> None:
    """Ensure admin + manager + staff users exist, each bound to all 3 warehouses."""
    with closing(sqlite3.connect(MASTER_DB)) as m:
        m.execute("PRAGMA foreign_keys = ON")
        ts = _now_str()
        # admin: id=1 (already exists from create-user), skip if present
        # manager: id=2
        # staff:  id=3
        for uid, name, is_admin in [(2, "manager1", 0), (3, "staff1", 0)]:
            try:
                m.execute(
                    "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
                    "VALUES (?, ?, 'x', ?, ?)",
                    (uid, name, is_admin, ts),
                )
            except sqlite3.IntegrityError:
                pass
        # ensure admin has pbkdf2 hash for login (bypass hashlib.scrypt bug)
        import hashlib
        hashlib.scrypt = lambda *a, **kw: (_ for _ in ()).throw(NotImplementedError())
        from werkzeug.security import generate_password_hash
        admin_hash = generate_password_hash("admin123", method="pbkdf2:sha256")
        m.execute("UPDATE users SET password_hash=? WHERE id=1", (admin_hash,))
        m.execute("UPDATE users SET password_hash=? WHERE id=2", (admin_hash,))
        m.execute("UPDATE users SET password_hash=? WHERE id=3", (admin_hash,))
        # bind each user to each warehouse with their role
        for uid, role in [(1, "admin"), (2, "manager"), (3, "staff")]:
            for code in WAREHOUSE_CODES:
                m.execute(
                    "INSERT OR IGNORE INTO warehouse_users (user_id, warehouse_id, role) "
                    "SELECT ?, w.id, ? FROM warehouses w WHERE w.code=?",
                    (uid, role, code),
                )
        m.commit()
    print(f"  master: ensured 3 users + bindings for {len(WAREHOUSE_CODES)} warehouses")


def _ensure_warehouses() -> None:
    """wh_002, wh_003 — wh_001 assumed to exist."""
    with closing(sqlite3.connect(MASTER_DB)) as m:
        for code, name in [("wh_002", "二号仓"), ("wh_003", "三号仓")]:
            try:
                m.execute(
                    "INSERT INTO warehouses (code, name, db_path, created_at) VALUES (?, ?, ?, ?)",
                    (code, name, str(WAREHOUSE_DB_DIR / f"{code}.db"), _now_str()),
                )
            except sqlite3.IntegrityError:
                pass
        m.commit()
    print("  master: ensured wh_002 + wh_003 exist")


def _ensure_warehouse_db(wh_path: Path) -> None:
    """Run init_warehouse_db if the file is missing. Idempotent."""
    if wh_path.exists() and wh_path.stat().st_size > 0:
        return
    from db import init_warehouse_db
    init_warehouse_db(wh_path)
    print(f"  initialized {wh_path.name}")


def _seed_warehouse_items(wh_path: Path, prefix: str) -> None:
    """Seed the ITEMS catalog with random current qty (not all warm — some
    cold-start so the forecast page has visible '数据积累中' state)."""
    with closing(sqlite3.connect(wh_path)) as w:
        w.execute("PRAGMA foreign_keys = OFF")
        w.row_factory = sqlite3.Row
        # Get first category id (any of them; we just need one)
        cat_row = w.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()
        if cat_row is None:
            print(f"  WARN: {wh_path.name} has no categories; skipping item seed")
            return
        cat_id = cat_row["id"]
        ts = _now_str()
        # Wipe existing items + dependent tables in FK-safe order.
        for tbl in ("product_bom", "production_run_items", "production_runs",
                    "outbound_requests", "stock_movements", "items"):
            w.execute(f"DELETE FROM {tbl}")
        for name, _cat, qty, cost, unit, safety in ITEMS:
            sku = f"{prefix}-{name[:4]}-{random.randint(1000,9999)}"
            # 2 of 8 items are 'cold start' (qty near 0, no recent outbounds)
            if name in ("冰块", "木樨子油") and random.random() < 0.5:
                w.execute(
                    """INSERT INTO items
                       (sku, name, category_id, quantity, safety_stock, unit,
                        unit_cost, gram_per_unit, updated_at)
                       VALUES (?, ?, ?, 0, ?, ?, ?, 0, ?)""",
                    (sku, name, cat_id, safety, unit, cost, ts),
                )
            else:
                w.execute(
                    """INSERT INTO items
                       (sku, name, category_id, quantity, safety_stock, unit,
                        unit_cost, gram_per_unit, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    (sku, name, cat_id, qty, safety, unit, cost, ts),
                )
        w.commit()
    print(f"  {wh_path.name}: seeded {len(ITEMS)} items")


def _seed_movements(wh_path: Path, prefix: str) -> None:
    """Generate 30 days of outbound_requests + production_run activity.

    Outbound qty for each item has a weekly pattern (weekday heavier,
    weekend lighter) plus a 5% chance of an unusual spike.
    """
    with closing(sqlite3.connect(wh_path)) as w:
        w.execute("PRAGMA foreign_keys = ON")
        w.row_factory = sqlite3.Row
        items = w.execute("SELECT id, name, quantity FROM items").fetchall()
        if not items:
            print(f"  WARN: {wh_path.name} has no items; skipping movements")
            return
        # Wipe historical movements + outbounds for clean re-seed.
        w.execute("DELETE FROM stock_movements")
        w.execute("DELETE FROM outbound_requests")
        w.execute("DELETE FROM production_run_items")
        w.execute("DELETE FROM production_runs")
        w.execute("DELETE FROM products")
        # Re-create products + BOMs (1 production per product per day avg)
        for pname, _note in PRODUCTS:
            cur = w.execute(
                "INSERT INTO products (name, unit, note, created_at) VALUES (?, '杯', ?, ?)",
                (pname, f"auto-seed {prefix}", _now_str()),
            )
            pid = cur.lastrowid
            for iname, qpu in PRODUCT_BOM[pname]:
                irow = w.execute("SELECT id FROM items WHERE name=?", (iname,)).fetchone()
                if irow is None:
                    continue
                w.execute(
                    "INSERT INTO product_bom (product_id, item_id, qty_per_unit) VALUES (?, ?, ?)",
                    (pid, irow["id"], qpu),
                )
        w.commit()
        # Refresh items after delete/insert
        items = w.execute("SELECT id, name FROM items").fetchall()
        item_names = [r["name"] for r in items]
        for d in range(DAYS_BACK, -1, -1):
            ts = _days_ago(d)
            weekday = (NOW - timedelta(days=d)).weekday()  # 0=Mon
            base_outbound = 3 if weekday < 5 else 1
            for it in items:
                if it["name"] in ("冰块",) and d < 25:
                    # Some items have very few outbounds → cold start
                    continue
                n_outbound = max(0, int(random.gauss(base_outbound, 1.5)))
                for _ in range(n_outbound):
                    qty = round(max(0.5, random.gauss(3, 1.2)), 2)
                    w.execute(
                        """INSERT INTO outbound_requests
                           (item_id, requested_quantity, reason, status,
                            rolled_back, created_at)
                           VALUES (?, ?, '', '出库', 0, ?)""",
                        (it["id"], qty, ts),
                    )
            # Production runs (1-2 per day)
            n_runs = random.choice([0, 1, 1, 2])
            for _ in range(n_runs):
                pname, _ = random.choice(PRODUCTS)
                prow = w.execute("SELECT id FROM products WHERE name=?", (pname,)).fetchone()
                if prow is None:
                    continue
                pid = prow["id"]
                out_qty = random.randint(20, 80)
                cur = w.execute(
                    """INSERT INTO production_runs
                       (product_id, output_qty, note, rolled_back, created_at)
                       VALUES (?, ?, '', 0, ?)""",
                    (pid, out_qty, ts),
                )
                run_id = cur.lastrowid
                for iname, qpu in PRODUCT_BOM[pname]:
                    irow = w.execute("SELECT id FROM items WHERE name=?", (iname,)).fetchone()
                    if irow is None:
                        continue
                    actual = round(qpu * out_qty, 2)
                    w.execute(
                        """INSERT INTO production_run_items
                           (run_id, item_id, planned_qty, actual_qty)
                           VALUES (?, ?, ?, ?)""",
                        (run_id, irow["id"], actual, actual),
                    )
        w.commit()
        n_out = w.execute("SELECT COUNT(*) AS c FROM outbound_requests").fetchone()["c"]
        n_pr = w.execute("SELECT COUNT(*) AS c FROM production_runs").fetchone()["c"]
    print(f"  {wh_path.name}: seeded {n_out} outbound_requests + {n_pr} production_runs over 30 days")


def _emit_initial_forecast_run(wh_code: str) -> None:
    """Trigger a fresh forecast run row so /health has a non-null last-success."""
    from blueprints.forecast import _run_daily_forecast
    rid = _run_daily_forecast()
    print(f"  {wh_code}: forecast_runs row id={rid} created")


def main() -> None:
    if not MASTER_DB.exists():
        print(f"ERROR: master.db not found at {MASTER_DB}.")
        print("Run: flask --app app create-user admin admin123 --admin")
        print("     flask --app app create-warehouse wh_001 中央仓")
        sys.exit(1)

    WAREHOUSE_DB_DIR.mkdir(parents=True, exist_ok=True)
    print("Seeding master.db...")
    _ensure_warehouses()
    _seed_master_users()

    # Wipe procurement_cache from any previous seed (the cache holds
    # item_id references; if items get re-numbered, the cache would show
    # ghost items).
    with closing(sqlite3.connect(MASTER_DB)) as m:
        m.execute("DELETE FROM procurement_cache")
        m.commit()
    print("  master: cleared procurement_cache (item IDs may have changed)")

    # For each warehouse, init its db + seed items + movements
    for code in WAREHOUSE_CODES:
        wh_path = WAREHOUSE_DB_DIR / f"{code}.db"
        _ensure_warehouse_db(wh_path)
        if not wh_path.exists():
            print(f"  WARN: {wh_path.name} not found after init; skipping")
            continue
        prefix = code.replace("wh_", "WH")
        _seed_warehouse_items(wh_path, prefix)
        _seed_movements(wh_path, prefix)
        _emit_initial_forecast_run(code)

    # Emit a few notifications so /notifications has content to render
    from blueprints.notifications_pure import emit_event
    with closing(sqlite3.connect(MASTER_DB)) as m:
        m.row_factory = sqlite3.Row
        user_ids = [r["id"] for r in m.execute("SELECT id FROM users").fetchall()]
    summaries = [
        ("recipe_published", "经典柠檬茶 v3 已发布", "/products/1"),
        ("recipe_published", "珍珠奶茶 v2 已发布", "/products/2"),
        ("recipe_published", "夏季限定杨枝甘露已发布", "/products/3"),
    ]
    with closing(sqlite3.connect(MASTER_DB)) as m:
        for ev_type, summary, url in summaries:
            try:
                emit_event(m, ev_type, summary, url, user_ids)
            except ValueError as e:
                print(f"  emit skip: {e}")
    print(f"  master: emitted {len(summaries)} notifications (3 events)")

    print("\nDone. Login as admin / admin123.")


if __name__ == "__main__":
    main()
