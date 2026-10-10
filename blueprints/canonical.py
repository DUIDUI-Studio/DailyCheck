"""Canonical items blueprint — M1 蓝图层。

Spec: docs/2026-10-03-canonical-item-design.md §3.1/§3.2/§3.5/§3.3 + §1.6

This blueprint wraps the pure functions in canonical_pure.py with:
  - Role gates (manager+)
  - Flash messages
  - GET / POST handling
  - Template rendering

Pages:
  /canonical/list               — master canonical_items 列表
  /canonical/detail/<id>        — 单条 + 别名 + 跨仓 bindings
  /canonical/edit[/<id>]        — 新建 / 编辑表单
  /canonical/diff               — 跨仓差异看板
  /canonical/conflicts          — open conflicts 列表
  /canonical/unbound            — 未纳管品项
  /canonical/claim-requests     — 申请单(claim/new_item/exempt)
  /canonical/claim              — POST 认领
  /canonical/unclaim            — POST 解绑
  /canonical/bulk-import        — POST CSV 灌入(T22)
  /canonical/fanout[/<id>]      — 扇出页 + POST 扇出
  /canonical/conflict/<id>      — 冲突裁决页 + POST resolve

T10a/T10b 共用此蓝图。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from flask import (
    Blueprint, abort, flash, g, redirect, render_template, request, url_for,
)
from werkzeug.exceptions import BadRequest

from permissions import require_login, require_platform_admin, require_role
from db import get_master_db
from config import BASE_DIR

import blueprints.canonical_pure as cp


bp = Blueprint("canonical", __name__, url_prefix="/canonical")


# ─────────────────────────────────────────────────────────────────────
#  Helper — open a per-warehouse connection (Spec §6.4)
# ─────────────────────────────────────────────────────────────────────
def _open_warehouse(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    if not p.is_absolute():
        p = BASE_DIR / p
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _all_warehouse_conns(master_conn) -> dict[str, sqlite3.Connection]:
    """返回 {warehouse_code: sqlite3.Connection}。调用方负责关闭。"""
    master_conn.row_factory = sqlite3.Row
    out: dict[str, sqlite3.Connection] = {}
    rows = master_conn.execute(
        "SELECT code, db_path, warehouse_type FROM warehouses"
    ).fetchall()
    for r in rows:
        if not r["db_path"]:
            continue
        p = Path(r["db_path"])
        if not p.is_absolute():
            p = BASE_DIR / p
        if not p.exists():
            continue
        try:
            out[r["code"]] = _open_warehouse(p)
        except Exception:  # noqa: BLE001 — skip if file is busy
            continue
    return out


# ─────────────────────────────────────────────────────────────────────
#  List / Detail / Edit
# ─────────────────────────────────────────────────────────────────────

@bp.route("/list")
@require_login
def canonical_list():
    m = get_master_db()
    items = cp.list_canonical_items(m, limit=500)
    return render_template(
        "canonical/list.html",
        items=items,
        price_managed=cp.is_syncable_field("selling_price"),
    )


@bp.route("/detail/<int:canonical_id>")
@require_login
def canonical_detail(canonical_id: int):
    m = get_master_db()
    detail = cp.get_canonical_item_detail(m, canonical_id)
    # 跨仓 bindings
    bindings = cp.collect_bindings(m)
    matching = []
    for wh_code, rows in bindings.items():
        for r in rows:
            if r["canonical_id"] == canonical_id:
                matching.append({"warehouse_code": wh_code, **r})
    return render_template(
        "canonical/detail.html",
        canonical=detail, bindings=matching,
    )


@bp.route("/edit", methods=["GET", "POST"])
@bp.route("/edit/<int:canonical_id>", methods=["GET", "POST"])
@require_platform_admin
@require_role("manager")
def canonical_edit(canonical_id: int | None = None):
    m = get_master_db()
    if request.method == "POST":
        def _opt_price(key: str) -> float | None:
            """空串/None → None（主数据未定价）；否则 float（非负校验在 pure 层）。"""
            raw = (request.form.get(key) or "").strip()
            return float(raw) if raw else None

        try:
            fields = {
                "name": request.form["name"].strip(),
                "unit": request.form["unit"].strip(),
                "category_code": request.form.get("category_code") or None,
                "gram_per_unit": float(request.form.get("gram_per_unit", 0) or 0),
                "aux_unit": request.form.get("aux_unit") or None,
                "aux_rate": float(request.form.get("aux_rate", 0) or 0),
                "barcode": request.form.get("barcode") or None,
                "status": request.form.get("status", "active"),
            }
            # Q6=canonical_managed：售价/进货价收归主数据（P0-9）。
            # is_syncable_field 是开关的唯一真相源 —— 不在这里重复判断 q6。
            if cp.is_syncable_field("selling_price"):
                fields["selling_price"] = _opt_price("selling_price")
                fields["unit_cost"] = _opt_price("unit_cost")
        except (KeyError, ValueError) as e:
            flash(f"表单数据错误: {e}")
            return redirect(url_for("canonical.canonical_edit", canonical_id=canonical_id))

        if canonical_id is None:
            # 新建时不带价格的字段先剔除（create 只接受已知 kwarg）
            create_fields = {
                k: v for k, v in fields.items()
                if k in ("name", "unit", "category_code", "gram_per_unit",
                         "aux_unit", "aux_rate", "barcode", "status",
                         "selling_price", "unit_cost")
            }
            created = cp.create_canonical_item(
                m,
                name=create_fields["name"], unit=create_fields["unit"],
                category_code=create_fields["category_code"],
                gram_per_unit=create_fields["gram_per_unit"],
                aux_unit=create_fields["aux_unit"], aux_rate=create_fields["aux_rate"],
                barcode=create_fields["barcode"],
                status=create_fields["status"],
                selling_price=create_fields.get("selling_price"),
                unit_cost=create_fields.get("unit_cost"),
                created_from="rd_manual",
                created_by=g.user["id"] if g.get("user") else None,
            )
            flash(f"已创建主数据项 {created['canonical_sku']}")
            return redirect(url_for("canonical.canonical_detail", canonical_id=created["id"]))
        else:
            cp.update_canonical_item(m, canonical_id, fields)
            flash("已更新")
            return redirect(url_for("canonical.canonical_detail", canonical_id=canonical_id))

    canonical = None
    if canonical_id is not None:
        canonical = cp.get_canonical_item_detail(m, canonical_id)
    cats = m.execute("SELECT code, name FROM canonical_categories ORDER BY code").fetchall()
    return render_template(
        "canonical/edit.html",
        canonical=canonical, categories=cats,
    )


@bp.route("/binding-active", methods=["POST"])
@require_platform_admin
def canonical_binding_active():
    """P0-10 —— 主数据详情页按仓停用/启用某个门店的绑定行。

    只改目标仓 items.is_active（本仓字段，扇出 NEVER_TOUCH），
    库存与历史记录不动；库存 >0 时回页面软提示。
    """
    from db import migrate_warehouse_db_columns

    m = get_master_db()
    try:
        warehouse_code = request.form["warehouse_code"].strip()
        canonical_id = int(request.form["canonical_id"])
        active = request.form.get("active", "0") == "1"
    except (KeyError, ValueError, BadRequest):
        flash("表单数据错误")
        return redirect(url_for("canonical.canonical_list"))

    back = redirect(url_for("canonical.canonical_detail", canonical_id=canonical_id))
    wh = m.execute(
        "SELECT code, name, db_path FROM warehouses WHERE code = ?", (warehouse_code,)
    ).fetchone()
    if wh is None or not wh["db_path"]:
        flash(f"仓库 {warehouse_code} 不存在或未配置文件")
        return back
    path = Path(wh["db_path"])
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.exists():
        flash(f"仓库 {warehouse_code} 的数据文件不存在")
        return back

    # 直连仓库必须先补幂等列迁移（旧库可能还没有 is_active 列）。
    migrate_warehouse_db_columns(path)
    wh_conn = _open_warehouse(path)
    try:
        result = cp.set_binding_active(wh_conn, warehouse_code, canonical_id, active)
    except ValueError as e:
        flash(str(e))
        return back
    finally:
        wh_conn.close()

    from blueprints.auth import audit
    audit("canonical.binding_active", "canonical_item", canonical_id, {
        "warehouse_code": warehouse_code,
        "active": result["active"],
        "quantity": result["quantity"],
    })
    if active:
        flash(f"{wh['name']}（{warehouse_code}）已启用该品项")
    elif result["quantity"] > 0:
        flash(
            f"{wh['name']}（{warehouse_code}）已停用该品项；"
            f"注意该仓仍有库存 {result['quantity']}，建议先出清或盘点"
        )
    else:
        flash(f"{wh['name']}（{warehouse_code}）已停用该品项")
    return back


@bp.route("/batch-edit", methods=["POST"])
@require_platform_admin
def canonical_batch_edit():
    """P0-11 —— 把勾选的主数据项某字段统一设为同一值（保存后可一键扇出）。

    Q6=canonical_managed 后价格（成本/售价）也在此批量范围内。
    """
    m = get_master_db()
    ids = [int(x) for x in request.form.getlist("canonical_ids") if str(x).strip()]
    if not ids:
        flash("请至少勾选一个主数据项")
        return redirect(url_for("canonical.canonical_list"))
    field = request.form.get("field", "").strip()
    try:
        value = cp.coerce_batch_value(field, request.form.get("value"))
        result = cp.batch_update_canonical_items(m, ids, field, value)
    except ValueError as e:
        flash(f"批量修改失败: {e}")
        return redirect(url_for("canonical.canonical_list"))

    shown = "" if value is None else f" → {value}"
    msg = f"批量修改完成：{result['updated']}/{len(ids)} 项已把「{result['label']}」统一设为{shown or '空'}"
    if result["error_count"]:
        msg += f"；{result['error_count']} 项失败（{'; '.join(result['errors'][:3])}）"
    flash(msg)
    # 改完直接把人送到扇出页，并预选刚改的这些主数据项。
    return redirect(url_for(
        "canonical.canonical_fanout", ids=",".join(str(i) for i in ids)
    ))


@bp.route("/backfill-prices", methods=["POST"])
@require_platform_admin
def canonical_backfill_prices():
    """P0-9 —— 把各仓现行售价/采购价回填进主数据（全仓最高价）。

    切 Q6=canonical_managed 前的一次性动作：主数据价为 NULL 时扇出会清空
    门店价，所以先把存量价灌进主数据。默认只填空值，不覆盖已录入的价。
    """
    m = get_master_db()
    wh_db_map = _all_warehouse_conns(m)
    try:
        result = cp.backfill_prices_from_warehouses(m, wh_db_map, overwrite=False)
    except Exception as e:  # noqa: BLE001
        flash(f"价格回填失败: {e}")
        return redirect(url_for("canonical.canonical_list"))
    finally:
        for c in wh_db_map.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    if result["filled_count"]:
        flash(
            f"价格回填完成：{len(result['filled'])} 个主数据项、"
            f"{result['filled_count']} 个价格字段已写入（全仓最高价）。"
            f"另有 {result['skipped_existing']} 个字段因主数据已有值被跳过、"
            f"{result['no_value']} 个品项各仓无有效价。"
        )
    else:
        flash(
            f"无可回填的价：{result['skipped_existing']} 个字段主数据已有值，"
            f"{result['no_value']} 个品项各仓无有效价。"
        )
    from blueprints.auth import audit
    audit("canonical.backfill_prices", "canonical_item", None, {
        "filled_count": result["filled_count"],
        "skipped_existing": result["skipped_existing"],
    })
    return redirect(url_for("canonical.canonical_list"))


# ─────────────────────────────────────────────────────────────────────
#  Cross-warehouse: diff / conflicts / unbound / store_exclusive
# ─────────────────────────────────────────────────────────────────────

@bp.route("/diff")
@require_login
def canonical_diff():
    m = get_master_db()
    summary = cp.diff_summary(m)
    bindings = cp.collect_bindings(m)
    by_canonical_render = []
    for cid, slot in summary["by_canonical"].items():
        canon_row = m.execute(
            "SELECT * FROM canonical_items WHERE id=?", (cid,)
        ).fetchone()
        if canon_row is None:
            continue
        by_canonical_render.append({
            "canonical_id": cid,
            "canonical_sku": canon_row["canonical_sku"],
            "name": canon_row["name"],
            "unit": canon_row["unit"],
            "warehouse_count": len(slot["warehouses"]),
            "members": slot["members"],
        })
    by_canonical_render.sort(key=lambda x: x["warehouse_count"], reverse=True)
    return render_template(
        "canonical/diff.html",
        summary=summary, by_canonical=by_canonical_render,
    )


@bp.route("/conflicts")
@require_login
def canonical_conflicts():
    m = get_master_db()
    conflicts = [
        dict(r) for r in m.execute(
            """SELECT c.*, ci.canonical_sku, ci.name AS canonical_name
               FROM canonical_conflicts c
               LEFT JOIN canonical_items ci ON ci.id = c.canonical_id
               WHERE c.status='open'
               ORDER BY c.created_at DESC"""
        ).fetchall()
    ]
    return render_template("canonical/conflicts.html", conflicts=conflicts)


@bp.route("/conflict/<int:conflict_id>", methods=["GET", "POST"])
@require_role("manager")
def canonical_resolve_conflict(conflict_id: int):
    m = get_master_db()
    if request.method == "POST":
        decision = request.form.get("decision")
        note = request.form.get("note")
        try:
            cp.resolve_conflict(
                m, conflict_id=conflict_id, decision=decision,
                reviewed_by=g.user["id"], note=note,
            )
            flash("已裁决")
        except ValueError as e:
            flash(f"裁决失败: {e}")
        return redirect(url_for("canonical.canonical_conflicts"))

    row = m.execute(
        """SELECT c.*, ci.canonical_sku, ci.name AS canonical_name
           FROM canonical_conflicts c
           LEFT JOIN canonical_items ci ON ci.id = c.canonical_id
           WHERE c.id=?""", (conflict_id,)
    ).fetchone()
    if row is None:
        abort(404)
    return render_template("canonical/conflict_detail.html", conflict=dict(row))


@bp.route("/unbound")
@require_login
def canonical_unbound():
    m = get_master_db()
    unbound = cp.list_unbound_storefront_items(m)
    store_excl = cp.list_store_exclusive_items(m)
    orphans = cp.find_orphan_bindings(m)
    return render_template(
        "canonical/unbound.html",
        unbound=unbound, store_exclusive=store_excl,
        orphans=orphans,
    )


# ─────────────────────────────────────────────────────────────────────
#  Claim / Unclaim (POST)
# ─────────────────────────────────────────────────────────────────────

@bp.route("/claim", methods=["POST"])
@require_role("manager")
def canonical_claim():
    m = get_master_db()
    warehouse_code = g.warehouse["code"]
    local_item_id = int(request.form["local_item_id"])
    canonical_id = int(request.form["canonical_id"])
    is_alias = bool(request.form.get("is_alias"))
    local_keep_name = request.form.get("local_keep_name") or None
    reason = request.form.get("reason") or None

    wh = _open_warehouse(g.warehouse_db_path)
    try:
        cp.claim_item(
            m, wh,
            warehouse_code=warehouse_code,
            local_item_id=local_item_id,
            canonical_id=canonical_id,
            is_alias=is_alias,
            is_store_exclusive=False,
            submitted_by=g.user["id"],
            local_keep_name=local_keep_name,
            reason=reason,
        )
        flash("认领成功")
    except (ValueError, cp.NeverTouchViolation, cp.InventoryMutated,
            cp.RowCountChanged, cp.IdSetChanged) as e:
        flash(f"认领失败: {e}")
    finally:
        wh.close()
    return redirect(request.referrer or url_for("canonical.canonical_list"))


@bp.route("/unclaim", methods=["POST"])
@require_role("manager")
def canonical_unclaim():
    m = get_master_db()
    warehouse_code = g.warehouse["code"]
    local_item_id = int(request.form["local_item_id"])
    reason = request.form.get("reason") or None

    wh = _open_warehouse(g.warehouse_db_path)
    try:
        cp.unclaim_item(
            m, wh,
            warehouse_code=warehouse_code,
            local_item_id=local_item_id,
            reviewed_by=g.user["id"],
            reason=reason,
        )
        flash("解绑成功")
    except ValueError as e:
        flash(f"解绑失败: {e}")
    finally:
        wh.close()
    return redirect(request.referrer or url_for("canonical.canonical_list"))


# ─────────────────────────────────────────────────────────────────────
#  Claim requests (T24)
# ─────────────────────────────────────────────────────────────────────

@bp.route("/claim-requests")
@require_login
def canonical_claim_requests():
    m = get_master_db()
    requests_ = [
        dict(r) for r in m.execute(
            """SELECT cr.*, ci.canonical_sku, ci.name AS canonical_name
               FROM canonical_claim_requests cr
               LEFT JOIN canonical_items ci ON ci.id = cr.canonical_id
               ORDER BY cr.created_at DESC LIMIT 200"""
        ).fetchall()
    ]
    return render_template("canonical/claim_requests.html", requests=requests_)


@bp.route("/claim-requests/new", methods=["POST"])
@require_login
def canonical_submit_claim_request():
    m = get_master_db()
    request_type = request.form.get("request_type", "claim")
    try:
        rid = cp.submit_claim_request(
            m,
            warehouse_code=g.warehouse["code"],
            local_item_id=int(request.form["local_item_id"]) if request.form.get("local_item_id") else None,
            local_sku=request.form.get("local_sku"),
            local_name=request.form["local_name"],
            canonical_id=int(request.form["canonical_id"]) if request.form.get("canonical_id") else None,
            proposed_category_code=request.form.get("proposed_category_code") or None,
            proposed_unit=request.form.get("proposed_unit") or None,
            request_type=request_type,
            reason=request.form.get("reason"),
            submitted_by=g.user["id"],
        )
        flash(f"已提交申请 #{rid}")
    except ValueError as e:
        flash(f"提交失败: {e}")
    return redirect(url_for("canonical.canonical_claim_requests"))


@bp.route("/claim-requests/<int:request_id>/review", methods=["POST"])
@require_role("manager")
def canonical_review_claim_request(request_id: int):
    m = get_master_db()
    decision = request.form.get("decision")
    note = request.form.get("note")
    try:
        cp.review_claim_request(
            m, request_id=request_id, decision=decision,
            reviewed_by=g.user["id"], review_note=note,
        )
        flash("已审批")
    except ValueError as e:
        flash(f"审批失败: {e}")
    return redirect(url_for("canonical.canonical_claim_requests"))


# ─────────────────────────────────────────────────────────────────────
#  T22 — Bulk import (POST CSV)
# ─────────────────────────────────────────────────────────────────────

@bp.route("/bulk-import", methods=["GET", "POST"])
@require_role("admin")
def canonical_bulk_import():
    if request.method == "POST":
        from flask import current_app
        from werkzeug.utils import secure_filename
        f = request.files.get("csv")
        if f is None or f.filename == "":
            flash("请上传 CSV 文件")
            return redirect(url_for("canonical.canonical_bulk_import"))
        # 保存到临时目录(避免污染 BASE_DIR)
        import tempfile
        tmpdir = Path(tempfile.gettempdir())
        dest = tmpdir / secure_filename(f.filename)
        f.save(dest)
        m = get_master_db()
        try:
            result = cp.bulk_create_canonical_items(m, dest)
            flash(
                f"导入完成: 新增 {result['inserted']}, 更新 {result['updated']}, "
                f"跳过 {result['skipped']}"
            )
        except Exception as e:  # noqa: BLE001
            flash(f"导入失败: {e}")
        return redirect(url_for("canonical.canonical_bulk_import"))

    return render_template("canonical/bulk_import.html")


# ─────────────────────────────────────────────────────────────────────
#  T8 — Fanout (POST + page)
# ─────────────────────────────────────────────────────────────────────

@bp.route("/fanout", methods=["GET", "POST"])
@bp.route("/fanout/<int:canonical_id>", methods=["GET", "POST"])
@require_role("admin")
def canonical_fanout(canonical_id: int | None = None):
    m = get_master_db()
    if request.method == "POST":
        # 解析 canonical_ids 与 warehouse_codes
        try:
            canonical_ids = [int(x) for x in request.form.getlist("canonical_ids")]
            warehouse_codes = request.form.getlist("warehouse_codes")
            action = request.form.get("action", "overwrite")
            force = bool(request.form.get("force"))
            confirm_text = request.form.get("confirm_text", "")
        except (ValueError, BadRequest) as e:
            flash(f"表单解析失败: {e}")
            return redirect(url_for("canonical.canonical_fanout"))

        # force 二次确认
        if force and canonical_ids:
            target = m.execute(
                "SELECT name FROM canonical_items WHERE id=?",
                (canonical_ids[0],)
            ).fetchone()
            if target is None or target["name"] != confirm_text:
                flash(
                    f"force 二次确认失败: 需输入与品项标准名 '{target['name'] if target else '?'}' "
                    f"完全一致"
                )
                return redirect(url_for("canonical.canonical_fanout"))

        wh_db_map = _all_warehouse_conns(m)
        wh_db_map = {k: v for k, v in wh_db_map.items() if k in warehouse_codes}
        try:
            result = cp.fanout_canonical_items(
                m, wh_db_map,
                canonical_ids=canonical_ids,
                warehouse_codes=warehouse_codes,
                action=action,
                force=force,
                started_by=g.user["id"],
                summary=request.form.get("summary"),
            )
        except Exception as e:  # noqa: BLE001
            flash(f"扇出失败: {e}")
            return redirect(url_for("canonical.canonical_fanout"))
        finally:
            for c in wh_db_map.values():
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass

        flash(
            f"扇出: 写入 {result['total_written']} 字段, "
            f"冻结 {result['total_frozen']} 字段, status={result['status']}"
        )
        return redirect(url_for(
            "canonical.canonical_fanout_event", event_id=result["event_id"]
        ))

    items = cp.list_canonical_items(m, limit=500)
    selected = None
    if canonical_id is not None:
        selected = cp.get_canonical_item_detail(m, canonical_id)
    # P0-11：批量修改后跳过来时，用 ?ids=1,2,3 预选刚改的主数据项。
    preselect_ids: list[int] = []
    for raw in (request.args.get("ids") or "").split(","):
        raw = raw.strip()
        if raw.isdigit():
            preselect_ids.append(int(raw))
    wh_rows = m.execute(
        "SELECT code, name FROM warehouses "
        "WHERE warehouse_type IN ('storefront', 'rd') ORDER BY code"
    ).fetchall()
    return render_template(
        "canonical/fanout.html",
        items=items, selected=selected, warehouses=wh_rows,
        preselect_ids=preselect_ids,
    )


@bp.route("/fanout/event/<int:event_id>")
@require_login
def canonical_fanout_event(event_id: int):
    m = get_master_db()
    ev = m.execute(
        "SELECT * FROM canonical_publish_events WHERE id=?", (event_id,)
    ).fetchone()
    if ev is None:
        abort(404)
    warehouses = [
        dict(r) for r in m.execute(
            "SELECT * FROM canonical_publish_event_warehouses "
            "WHERE publish_event_id=? ORDER BY warehouse_code",
            (event_id,),
        ).fetchall()
    ]
    items = [
        dict(r) for r in m.execute(
            """SELECT ei.*, ci.canonical_sku, ci.name AS canonical_name
               FROM canonical_publish_event_items ei
               LEFT JOIN canonical_items ci ON ci.id = ei.canonical_id
               WHERE ei.publish_event_id=?
               ORDER BY ci.canonical_sku""",
            (event_id,),
        ).fetchall()
    ]
    return render_template(
        "canonical/fanout_event.html",
        event=dict(ev), warehouses=warehouses, items=items,
    )
