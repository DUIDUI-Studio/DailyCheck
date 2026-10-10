"""Web 端品项批量导入:上传 → 预览 → 确认写入。"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import IO

from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)

from db import get_master_db
from permissions import require_login, require_role
from werkzeug.datastructures import FileStorage


def parse_xlsx(file_stream: IO[bytes]) -> dict:
    """解析 xlsx 文件为分组预览数据。

    期望格式:Sheet1,跳过前 2 行(标题 + 表头),第 3 行起为数据。
    分类列首行有值,后续空 → 沿用上一行(向下合并)。
    列序:分类(1)|物料名称(2)|SKU(3)|规格(4)|单价/元(5)|
          现有库存(6)|单位(7)|隐藏栏/盘点单位单价(8)|库存金额(9)

    Returns:
        {
            "groups_order": ["常温物料", ...],
            "groups_rows": {"常温物料": [{"row": 3, "name": "...", "spec": "...",
                                          "unit_cost": float, "unit": "str"}, ...]},
            "problems": ["行 N: 缺少分类", ...]
        }

    注意:本函数不写库,不读 session,纯函数。
    """
    import openpyxl

    wb = openpyxl.load_workbook(file_stream, data_only=True)
    ws = wb["Sheet1"]
    groups_order: list[str] = []
    groups_rows: dict[str, list[dict]] = {}
    problems: list[str] = []
    prev_cat: str | None = None

    for r in range(3, ws.max_row + 1):
        name = ws.cell(r, 2).value
        if not name:
            # 合计行 / 空行 → 跳过
            continue
        cat = ws.cell(r, 1).value or prev_cat
        if cat is None:
            problems.append(f"行 {r}: 缺少分类")
            continue
        prev_cat = cat
        if cat not in groups_rows:
            groups_order.append(cat)
            groups_rows[cat] = []
        unit_cost_raw = ws.cell(r, 8).value
        try:
            unit_cost = float(unit_cost_raw) if unit_cost_raw is not None else 0.0
        except (TypeError, ValueError):
            unit_cost = 0.0
            problems.append(f"行 {r}: 单价无法解析为数字 → 记为 0")
        unit_raw = ws.cell(r, 7).value
        unit = str(unit_raw).strip() if unit_raw is not None else ""
        if not unit:
            unit = "件"
        spec_raw = ws.cell(r, 4).value
        groups_rows[cat].append({
            "row": r,
            "name": str(name).strip(),
            "spec": str(spec_raw) if spec_raw is not None else None,
            "unit_cost": unit_cost,
            "unit": unit,
        })

    return {
        "groups_order": groups_order,
        "groups_rows": groups_rows,
        "problems": problems,
    }


bp = Blueprint("import_items", __name__, url_prefix="/admin/import-items")


@bp.route("", methods=["GET"])
@require_login
@require_role("admin")
def upload_form():
    """渲染上传表单。目标仓库 = 当前登录用户的 g.warehouse。"""
    return render_template(
        "admin/import_items.html",
        target_wh_code=g.warehouse["code"],
        target_wh_name=g.warehouse["name"],
    )


@bp.route("", methods=["POST"])
@require_login
@require_role("admin")
def upload_parse():
    """解析上传的 xlsx → 缓存到 session → 重定向预览。目标仓库 = g.warehouse。"""
    file = request.files.get("file")

    if not isinstance(file, FileStorage) or not file.filename:
        flash("请选择文件")
        return redirect(url_for("import_items.upload_form"))

    if not file.filename.lower().endswith(".xlsx"):
        flash("仅支持 .xlsx 文件")
        return redirect(url_for("import_items.upload_form"))

    try:
        preview = parse_xlsx(file.stream)
    except Exception:
        current_app.logger.exception("Excel 解析失败")
        flash("Excel 解析失败,请检查文件格式")
        return redirect(url_for("import_items.upload_form"))

    if not preview["groups_order"]:
        flash("Excel 中无数据行")
        return redirect(url_for("import_items.upload_form"))

    session["import_preview"] = {
        "warehouse_code": g.warehouse["code"],
        "filename": file.filename,
        **preview,
    }
    return redirect(url_for("import_items.preview"))


@bp.route("/preview", methods=["GET"])
@require_login
@require_role("admin")
def preview():
    """渲染预览页。逐行标注是否已匹配到本仓现有品项（未匹配不会写入）。"""
    from config import BASE_DIR

    pv = session.get("import_preview")
    if not pv:
        flash("预览已过期,请重新上传")
        return redirect(url_for("import_items.upload_form"))

    master = get_master_db()
    wh = master.execute(
        "SELECT name, db_path FROM warehouses WHERE code = ?", (pv["warehouse_code"],)
    ).fetchone()
    target_wh_name = wh["name"] if wh else pv["warehouse_code"]

    matched_keys: set[tuple[str, str]] = set()
    if wh is not None and wh["db_path"]:
        wh_path = Path(BASE_DIR) / wh["db_path"]
        if wh_path.exists():
            with closing(sqlite3.connect(wh_path)) as conn:
                matched_keys = {
                    (r[0], r[1]) for r in conn.execute(
                        """SELECT c.name, i.name
                           FROM items i JOIN categories c ON c.id = i.category_id"""
                    ).fetchall()
                }

    total_rows = sum(len(v) for v in pv["groups_rows"].values())
    matched_rows = sum(
        1
        for cat_name, rows in pv["groups_rows"].items()
        for item in rows
        if (cat_name, item["name"]) in matched_keys
    )
    return render_template(
        "admin/import_items_preview.html",
        target_wh_name=target_wh_name,
        total_rows=total_rows,
        matched_keys=matched_keys,
        matched_rows=matched_rows,
        **pv,
    )


@bp.route("/commit", methods=["POST"])
@require_login
@require_role("admin")
def commit():
    """非破坏性批量更新：只更新「本仓已存在品项」的进货单价。

    P0-3（docs/2026-10-09-item-master-unify-plan.md §3）：
      - 废除旧的 DELETE + INSERT 整表替换——旧实现会连库存流水 / 出入库记录
        一起删掉，且绕过全部主数据策略检查；
      - 本入口不再新建品项、不再新建品类：新品项必须先在「品项主数据」建档
        并扇出，未匹配的行一律跳过并回报；
      - 安全库存由系统按消耗计算（P0-12），本入口不写。
    P0-9（2026-10-09）：价格收归主数据 —— 已纳管行（canonical_id 非空）的进货价
      改写 canonical_items.unit_cost（随后需扇出下发），不再直写门店行；
      未纳管的门店自建行仍写本仓 items.unit_cost。
    匹配键：(品类名, 品项名) —— 与 xlsx 的两列一一对应。
    """
    from blueprints._helpers import now
    from config import BASE_DIR

    pv = session.pop("import_preview", None)
    if not pv:
        flash("预览已过期,请重新上传")
        return redirect(url_for("import_items.upload_form"))

    # 目标仓库始终是当前 g.warehouse(防 session 写入其他 code 的退化场景)
    warehouse_code = g.warehouse["code"]
    groups_order = pv["groups_order"]

    master = get_master_db()
    wh_row = master.execute(
        "SELECT db_path, name FROM warehouses WHERE code = ?", (warehouse_code,)
    ).fetchone()
    if wh_row is None:
        flash(f"仓库 {warehouse_code} 不存在")
        return redirect(url_for("import_items.upload_form"))

    db_path = Path(BASE_DIR) / wh_row["db_path"]

    from blueprints import canonical_pure as cp
    price_managed = cp.is_syncable_field("unit_cost")

    updated = 0            # 未纳管行：写本仓 items.unit_cost
    updated_canonical = 0  # 已纳管行：写主数据 canonical_items.unit_cost
    skipped: list[str] = []
    canon_updates: dict[int, float] = {}
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            ts = now()
            index = {
                (r["category_name"], r["name"]): (int(r["id"]), r["canonical_id"])
                for r in conn.execute(
                    """SELECT i.id, i.name, i.canonical_id, c.name AS category_name
                       FROM items i JOIN categories c ON c.id = i.category_id"""
                ).fetchall()
            }
            for cat_name in groups_order:
                for item in pv["groups_rows"][cat_name]:
                    hit = index.get((cat_name, item["name"]))
                    if hit is None:
                        skipped.append(f"{cat_name}/{item['name']}")
                        continue
                    item_id, canonical_id = hit
                    # P0-9：已纳管行价格收归主数据 → 写 canonical_items（稍后扇出）。
                    if price_managed and canonical_id is not None:
                        canon_updates[int(canonical_id)] = float(item["unit_cost"])
                        updated_canonical += 1
                        continue
                    conn.execute(
                        "UPDATE items SET unit_cost=?, updated_at=? WHERE id=?",
                        (float(item["unit_cost"]), ts, item_id),
                    )
                    updated += 1
            conn.commit()
    except sqlite3.Error:
        current_app.logger.exception("批量导入写库失败")
        flash("导入失败,请重试")
        return redirect(url_for("import_items.upload_form"))

    # 已纳管行的价写入主数据（master.db），随后由管理员扇出下发到门店。
    if canon_updates:
        ts2 = now()
        for cid, cost in canon_updates.items():
            master.execute(
                "UPDATE canonical_items SET unit_cost=?, updated_at=? WHERE id=?",
                (cost, ts2, cid),
            )
        master.commit()

    # 4. audit
    from blueprints.auth import audit
    audit("import_items.import", "warehouse", warehouse_code, {
        "updated": updated,
        "updated_canonical": updated_canonical,
        "skipped": len(skipped),
        "filename": pv.get("filename"),
    })
    if updated:
        flash(f"已更新 {updated} 条本仓自建品项的进货单价")
    if updated_canonical:
        flash(
            f"{updated_canonical} 条已纳管品项的进货价已写入「品项主数据」；"
            f"请到主数据页扇出下发到门店"
        )
    if skipped:
        preview_txt = "；".join(skipped[:5]) + ("…" if len(skipped) > 5 else "")
        flash(
            f"{len(skipped)} 条未匹配本仓现有品项，已跳过（新品项请先在"
            f"「品项主数据」建档后扇出）：{preview_txt}"
        )
    return redirect(url_for("items.items_list"))
