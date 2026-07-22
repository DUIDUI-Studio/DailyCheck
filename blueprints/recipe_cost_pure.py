"""配方成本计算纯函数：单位换算、行成本、冰激凌/出品配方总成本。

不做 DB 写。所有数据从调用方传入（conn + ids），无副作用，便于单测。
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal


def _safe_decimal(value) -> Decimal:
    """None / 数字 / 字符串 → Decimal(2dp)。None 视作 0。"""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def qty_to_stock_units(qty, item: dict) -> Decimal:
    """配方用量 → 库存单位（Decimal, 2dp）。

    item 必须含 gram_per_unit。gram_per_unit > 0 → qty 视为克，除以它；
    否则 qty 即库存单位。
    与 blueprints._helpers.qty_to_stock_units 等价；本函数独立避免 blueprint
    之间的循环依赖（pure 模块不依赖任何 blueprint）。
    """
    qty_d = _safe_decimal(qty)
    gpu = float(item.get("gram_per_unit") or 0)
    if gpu > 0:
        return (qty_d / Decimal(str(gpu))).quantize(Decimal("0.01"))
    return qty_d


def line_cost(
    qty,
    item: dict,
    temp_selling_price: Decimal | None = None,
) -> dict:
    """单条原料行：含克重换算的采购/销售成本。

    Returns: {qty_stock, cost_purchase, cost_selling} (all Decimal)
    """
    qty_stock = qty_to_stock_units(qty, item)
    unit_cost = _safe_decimal(item.get("unit_cost"))
    selling = (
        temp_selling_price
        if temp_selling_price is not None
        else _safe_decimal(item.get("selling_price"))
    )
    return {
        "qty_stock": qty_stock,
        "cost_purchase": (qty_stock * unit_cost).quantize(Decimal("0.01")),
        "cost_selling": (qty_stock * selling).quantize(Decimal("0.01")),
    }


def _margin(sale_price, cost_purchase) -> float | None:
    """Gross margin based on PURCHASE cost (the conservative KPI).

    formula: (sale_price - cost_purchase) / sale_price
    """
    if sale_price is None or float(sale_price) <= 0:
        return None
    sp = float(sale_price)
    cp = float(cost_purchase)
    return round((sp - cp) / sp, 4)


def _margin_selling(cost_selling, cost_purchase) -> float | None:
    """Gross margin based on SELLING cost (the optimistic KPI).

    formula: (cost_selling - cost_purchase) / cost_selling
    Use this to compare to the customer's actual revenue potential — when
    customers buy at the full selling price vs at the negotiated/promo price.
    Returns None if cost_selling <= 0.
    """
    cs = float(cost_selling)
    cp = float(cost_purchase)
    if cs <= 0:
        return None
    return round((cs - cp) / cs, 4)


def _profit(cost_selling, cost_purchase) -> float:
    """Profit = cost_selling - cost_purchase. Always non-negative when not using
    temp prices (cost_selling ≥ cost_purchase since selling price ≥ cost)."""
    return float(cost_selling) - float(cost_purchase)


def _lines_for_recipe(conn, table: str, recipe_id: int, recipe_id_col: str):
    """提取配方的所有 BOM 行（含 items JOIN）。"""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        f"""
        SELECT t.id AS line_id, t.qty_per_unit,
               t.item_id AS item_id,
               i.unit_cost, i.selling_price, i.gram_per_unit,
               i.aux_rate, i.unit
        FROM {table} t
        JOIN items i ON i.id = t.item_id
        WHERE t.{recipe_id_col} = ?
        ORDER BY t.id
        """,
        (recipe_id,),
    ).fetchall()


def ic_recipe_cost(
    conn,
    ic_recipe_id: int,
    temp_prices: dict | None = None,
) -> dict:
    """冰激凌配方总成本（采购 + 销售价值）+ 毛利率 + 每行小计。

    temp_prices: {item_id: Decimal}，覆盖对应 item 的 selling_price。
    """
    temp_prices = temp_prices or {}
    conn.row_factory = sqlite3.Row
    recipe = conn.execute(
        "SELECT id, sale_price, output_qty FROM ic_recipes WHERE id = ?",
        (ic_recipe_id,),
    ).fetchone()
    if recipe is None:
        return None

    rows = _lines_for_recipe(conn, "ic_recipe_items", ic_recipe_id, "ic_recipe_id")
    cost_purchase = Decimal("0.00")
    cost_selling = Decimal("0.00")
    lines = []
    for r in rows:
        item = {
            "unit_cost": r["unit_cost"],
            "selling_price": r["selling_price"],
            "gram_per_unit": r["gram_per_unit"],
            "aux_rate": r["aux_rate"],
            "unit": r["unit"],
        }
        tmp = temp_prices.get(int(r["item_id"]))
        lc = line_cost(r["qty_per_unit"], item, temp_selling_price=tmp)
        cost_purchase += lc["cost_purchase"]
        cost_selling += lc["cost_selling"]
        lines.append({
            "item_id": int(r["item_id"]),
            "qty_per_unit": float(r["qty_per_unit"]),
            **lc,
        })

    sale_price = float(recipe["sale_price"] or 0)
    return {
        "cost_purchase": cost_purchase,
        "cost_selling": cost_selling,
        "sale_price": sale_price,
        "margin_purchase": _margin(sale_price, cost_purchase),
        "margin_selling": _margin_selling(cost_selling, cost_purchase),
        "profit": _profit(cost_selling, cost_purchase),
        "lines": lines,
    }


def recipe_cost(
    conn,
    recipe_id: int,
    temp_prices: dict | None = None,
) -> dict:
    """出品配方总成本 + 毛利率。多态原料：
    - source_type='item' → line_cost（克重换算）
    - source_type='ic_recipe' → 按 ic_recipe.cost_purchase_per_unit × qty 计入
      （ic_recipe 引用不展开到底层 items，避免双重计算）
    """
    temp_prices = temp_prices or {}
    conn.row_factory = sqlite3.Row
    recipe = conn.execute(
        "SELECT id, sale_price, output_qty FROM recipes WHERE id = ?",
        (recipe_id,),
    ).fetchone()
    if recipe is None:
        return None

    rows = conn.execute(
        """
        SELECT ri.id AS line_id, ri.source_type, ri.qty_per_unit,
               ri.item_id, ri.ic_recipe_id,
               i.unit_cost, i.selling_price, i.gram_per_unit,
               i.aux_rate, i.unit
        FROM recipe_items ri
        LEFT JOIN items i ON i.id = ri.item_id
        WHERE ri.recipe_id = ?
        ORDER BY ri.id
        """,
        (recipe_id,),
    ).fetchall()

    cost_purchase = Decimal("0.00")
    cost_selling = Decimal("0.00")
    lines = []
    for r in rows:
        if r["source_type"] == "item":
            item = {
                "unit_cost": r["unit_cost"],
                "selling_price": r["selling_price"],
                "gram_per_unit": r["gram_per_unit"],
                "aux_rate": r["aux_rate"],
                "unit": r["unit"],
            }
            tmp = temp_prices.get(int(r["item_id"]))
            lc = line_cost(r["qty_per_unit"], item, temp_selling_price=tmp)
            cost_purchase += lc["cost_purchase"]
            cost_selling += lc["cost_selling"]
            lines.append({
                "line_type": "item",
                "item_id": int(r["item_id"]),
                "qty_per_unit": float(r["qty_per_unit"]),
                **lc,
            })
        elif r["source_type"] == "ic_recipe":
            ic_id = int(r["ic_recipe_id"])
            ic = conn.execute(
                "SELECT id, sale_price, output_qty FROM ic_recipes WHERE id = ?",
                (ic_id,),
            ).fetchone()
            if ic is None:
                continue
            ic_cost = ic_recipe_cost(conn, ic_id, temp_prices=temp_prices)
            out_qty = float(ic["output_qty"] or 0)
            qty = _safe_decimal(r["qty_per_unit"])
            if out_qty > 0:
                per_unit_purchase = (
                    ic_cost["cost_purchase"] / Decimal(str(out_qty))
                ).quantize(Decimal("0.0001"))
                per_unit_selling = (
                    ic_cost["cost_selling"] / Decimal(str(out_qty))
                ).quantize(Decimal("0.0001"))
            else:
                per_unit_purchase = Decimal("0.0000")
                per_unit_selling = Decimal("0.0000")
            line_purchase = (qty * per_unit_purchase).quantize(Decimal("0.01"))
            line_selling = (qty * per_unit_selling).quantize(Decimal("0.01"))
            cost_purchase += line_purchase
            cost_selling += line_selling
            lines.append({
                "line_type": "ic_recipe",
                "ic_recipe_id": ic_id,
                "qty_per_unit": float(r["qty_per_unit"]),
                "cost_purchase": line_purchase,
                "cost_selling": line_selling,
            })

    sale_price = float(recipe["sale_price"] or 0)
    return {
        "cost_purchase": cost_purchase,
        "cost_selling": cost_selling,
        "sale_price": sale_price,
        "margin_purchase": _margin(sale_price, cost_purchase),
        "margin_selling": _margin_selling(cost_selling, cost_purchase),
        "profit": _profit(cost_selling, cost_purchase),
        "lines": lines,
    }
