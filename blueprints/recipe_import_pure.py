"""门店存量配方导入研发中心 — 纯函数层（F2, 2026-10-16）。

背景：门店日常用生产模块（products / product_bom）维护自己的冰激凌配方，
研发中心（rd 仓）的 ic_recipes 为空。本模块把门店 products/product_bom
迁移为研发中心的 ic_recipes/ic_recipe_items，实现配方对齐。

数据语义（与 production.py::submit 一致）：
- product_bom.qty_per_unit = 克/件（品项启用克重时）或 库存单位/件
- ic_recipe_items.qty_per_unit = 克（或库存单位，同一 qty_to_stock 语义）
→ 数值可直接搬，无需换算。

身份匹配：源仓 item → RD 仓 item，canonical_id 优先、sku 兜底
（与 publish_recipe_pure._resolve_target_item_id 同一套规则）。

设计约束：
- 纯函数：所有 conn 由调用方传入，本模块不打开任何 DB 文件、不依赖 Flask g。
- 只读源仓；只写目标（RD）仓 + 返回值供路由层写 audit。
"""
from __future__ import annotations

import sqlite3


def _row_factory(conn) -> None:
    conn.row_factory = sqlite3.Row


def list_source_warehouses(master_conn) -> list[dict]:
    """master.db 中所有 storefront 仓（导入源候选）。"""
    _row_factory(master_conn)
    rows = master_conn.execute(
        "SELECT code, name, db_path FROM warehouses "
        "WHERE warehouse_type = 'storefront' ORDER BY code"
    ).fetchall()
    return [dict(r) for r in rows]


def scan_source_products(source_conn, target_conn) -> list[dict]:
    """盘点源门店的全部 products + BOM，并预匹配目标（RD）仓品项。

    返回 list[dict]，每个 product：
      product_id / name / note / bom_count / total_grams /
      conflict（RD 已有同名 ic_recipe）/
      lines: [{item_id(源), item_name, sku, canonical_id, qty_per_unit,
               gram_per_unit, matched_item_id(RD)|None, matched_item_name}]

    行匹配规则：canonical_id 优先 → sku 兜底。matched_item_id=None 的行
    表示 RD 仓没有对应品项（需先在品项主数据扇出，导入时该产品被拒绝）。
    """
    _row_factory(source_conn)
    _row_factory(target_conn)

    # RD 侧索引：canonical_id → item / sku → item
    rd_by_canonical: dict[int, dict] = {}
    rd_by_sku: dict[str, dict] = {}
    for r in target_conn.execute(
        "SELECT id, name, sku, canonical_id FROM items"
    ).fetchall():
        if r["canonical_id"] is not None:
            rd_by_canonical[int(r["canonical_id"])] = dict(r)
        if r["sku"]:
            rd_by_sku.setdefault(r["sku"], dict(r))

    rd_names = {
        r["name"]
        for r in target_conn.execute("SELECT name FROM ic_recipes").fetchall()
    }

    products = source_conn.execute(
        "SELECT id, name, unit, note FROM products ORDER BY id"
    ).fetchall()

    out: list[dict] = []
    for p in products:
        lines = []
        total_grams = 0.0
        for b in source_conn.execute(
            """SELECT b.qty_per_unit, i.id AS item_id, i.name AS item_name,
                      i.sku, i.canonical_id, i.gram_per_unit
               FROM product_bom b JOIN items i ON i.id = b.item_id
               WHERE b.product_id = ? ORDER BY b.id""",
            (int(p["id"]),),
        ).fetchall():
            matched = None
            if b["canonical_id"] is not None:
                matched = rd_by_canonical.get(int(b["canonical_id"]))
            if matched is None and b["sku"]:
                matched = rd_by_sku.get(b["sku"])
            qty = float(b["qty_per_unit"] or 0)
            total_grams += qty
            lines.append({
                "item_id": int(b["item_id"]),
                "item_name": b["item_name"],
                "sku": b["sku"],
                "canonical_id": (
                    int(b["canonical_id"]) if b["canonical_id"] is not None else None
                ),
                "qty_per_unit": qty,
                "gram_per_unit": float(b["gram_per_unit"] or 0),
                "matched_item_id": int(matched["id"]) if matched else None,
                "matched_item_name": matched["name"] if matched else None,
            })
        out.append({
            "product_id": int(p["id"]),
            "name": p["name"],
            "note": p["note"] or "",
            "bom_count": len(lines),
            "total_grams": round(total_grams, 2),
            "conflict": p["name"] in rd_names,
            "lines": lines,
            "importable": bool(lines) and all(
                ln["matched_item_id"] is not None for ln in lines
            ),
        })
    return out


def import_products(
    target_conn,
    products: list[dict],
    conflict_mode: str = "skip",
    note_prefix: str = "门店配方导入",
    source_warehouse_code: str = "",
    now_fn=None,
) -> dict:
    """把已盘点的 products 写入目标（RD）仓 ic_recipes。

    `products` 必须是 scan_source_products 的产出（含 lines 的
    matched_item_id）。仅处理 importable=True 的产品。本函数不读源仓
    （所需数据已全部在 products 参数里）。

    conflict_mode:
      - 'skip'      同名 ic_recipe 存在 → 跳过
      - 'overwrite' 同名 ic_recipe 存在 → 删除（BOM 级联）后重建

    返回 {"imported": [(product_id, ic_recipe_id, name)], "skipped": [...],
          "rejected": [(product_id, name, reason)]}

    事务：整个导入一个事务；任何一步失败全部回滚（保证不出现半导入）。
    调用方（路由层）负责 commit 前写 audit —— 本函数内部 commit。
    """
    if conflict_mode not in ("skip", "overwrite"):
        raise ValueError(f"unknown conflict_mode: {conflict_mode!r}")
    _row_factory(target_conn)
    ts = now_fn() if now_fn else _default_now()

    imported: list[tuple] = []
    skipped: list[tuple] = []
    rejected: list[tuple] = []

    try:
        for p in products:
            pid = int(p["product_id"])
            name = p["name"]
            if not p.get("importable"):
                missing = [
                    ln["item_name"] for ln in p["lines"]
                    if ln["matched_item_id"] is None
                ]
                if not p["lines"]:
                    reason = "源产品没有 BOM 行"
                else:
                    reason = (
                        f"{len(missing)} 个原料未匹配 RD 品项："
                        + "、".join(missing[:5])
                        + ("…" if len(missing) > 5 else "")
                    )
                rejected.append((pid, name, reason))
                continue

            existing = target_conn.execute(
                "SELECT id FROM ic_recipes WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None:
                if conflict_mode == "skip":
                    skipped.append((pid, name, "RD 已存在同名配方"))
                    continue
                # overwrite 守卫：被出品配方引用的 ic_recipe 不可删
                # （recipe_items.ic_recipe_id 无外键级联，删了会留悬空引用）。
                used = target_conn.execute(
                    "SELECT COUNT(*) AS c FROM recipe_items "
                    "WHERE source_type='ic_recipe' AND ic_recipe_id=?",
                    (int(existing["id"]),),
                ).fetchone()["c"]
                if used > 0:
                    rejected.append((
                        pid, name,
                        f"RD 同名配方被 {used} 个出品配方引用，不能覆盖导入",
                    ))
                    continue
                # overwrite：删旧（ic_recipe_items 级联删除）
                target_conn.execute(
                    "DELETE FROM ic_recipe_items WHERE ic_recipe_id = ?",
                    (int(existing["id"]),),
                )
                target_conn.execute(
                    "DELETE FROM ic_recipes WHERE id = ?", (int(existing["id"]),)
                )

            note = f"{note_prefix}（源：{source_warehouse_code} product#{pid}）"
            if p.get("note"):
                note = f"{p['note']}｜{note}"
            cur = target_conn.execute(
                """INSERT INTO ic_recipes
                   (name, note, output_unit, output_qty, sale_price,
                    created_at, updated_at)
                   VALUES (?, ?, 'g', ?, 0, ?, ?)""",
                (name, note, round(float(p["total_grams"]), 2), ts, ts),
            )
            new_id = int(cur.lastrowid)
            for ln in p["lines"]:
                target_conn.execute(
                    """INSERT INTO ic_recipe_items
                       (ic_recipe_id, item_id, qty_per_unit)
                       VALUES (?, ?, ?)""",
                    (new_id, int(ln["matched_item_id"]),
                     float(ln["qty_per_unit"])),
                )
            imported.append((pid, new_id, name))
        target_conn.commit()
    except Exception:
        target_conn.rollback()
        raise

    return {"imported": imported, "skipped": skipped, "rejected": rejected}


def _default_now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
