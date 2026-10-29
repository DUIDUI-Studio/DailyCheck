"""研发配方编辑页：原料行删除改独立路由 + 保存撞唯一约束整批回滚。

回归 Issue #47（承接 PR #42 明确排除的范围）。同型缺陷两条：

  1. **原料行删除索引错位（静默数据损坏，比 500 更危险）**
     旧实现用 `bom_delete[]` checkbox 与 `bom_row_id[]` 平行数组按索引配对。
     **checkbox 未勾选就不提交** → 数组长度必然短于行数 → 索引整体错位：
     勾第 3 行实际删掉第 1 行，BOM 与用量静默对不上且不报错。

  2. **未捕获 IntegrityError → 500 + 半保存**
     同一表单两行指向同一原料 → `UNIQUE(ic_recipe_id, item_id)` 冲突 →
     事务未提交 → 用户只看到「保存失败」。

修复后的契约（两个研发配方页都适用）：
  - 原料删除是行级独立路由，自带 bom_id，不依赖任何数组顺序
  - 路由内校验 bom_id 属于该配方，跨配方删行被拒绝
  - 「保存配方」表单不再提交 bom_delete[]，只负责新增与改用量
  - 保存撞唯一约束 → 整批 rollback() + 明确指出第几行重复 + flash(..., "error")
    （沿用 PR #42 的 business_rule 409 语义，见 blueprints/_helpers.py）
"""
import re
import sqlite3

TS = "2026-10-27 10:00:00"


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert m, "页面应渲染 csrf_token hidden"
    return m.group(1)


def _seed_items(wh_path, count=3):
    """造 count 个可选用原料，返回 id 列表。"""
    ids = []
    with sqlite3.connect(wh_path) as db:
        db.execute("INSERT OR IGNORE INTO categories (id, name) VALUES (1, '测试')")
        for i in range(count):
            cur = db.execute(
                "INSERT INTO items (sku, name, category_id, unit, quantity, "
                "safety_stock, unit_cost, gram_per_unit, aux_rate, updated_at) "
                "VALUES (?, ?, 1, 'g', 0, 0, 0, 0, 0, ?)",
                (f"RD-{i}", f"原料{i}", TS),
            )
            ids.append(cur.lastrowid)
        db.commit()
    return ids


def _seed_ic_recipe(wh_path, name, item_ids, qtys=None):
    """建一个冰激凌配方 + BOM 行。返回 (ic_recipe_id, bom_ids)。"""
    qtys = qtys or [500.0] * len(item_ids)
    with sqlite3.connect(wh_path) as db:
        cur = db.execute(
            "INSERT INTO ic_recipes (name, output_unit, output_qty, sale_price, "
            "created_at, updated_at) VALUES (?, 'g', 0, 0, ?, ?)",
            (name, TS, TS),
        )
        rid = cur.lastrowid
        bom_ids = []
        for item_id, qty in zip(item_ids, qtys, strict=False):
            b = db.execute(
                "INSERT INTO ic_recipe_items (ic_recipe_id, item_id, qty_per_unit) "
                "VALUES (?, ?, ?)",
                (rid, item_id, qty),
            )
            bom_ids.append(b.lastrowid)
        db.commit()
    return rid, bom_ids


def _seed_recipe(wh_path, name, rows):
    """建一个出品配方 + BOM 行。

    rows: [(source_type, target_id, qty), ...] —— source_type ∈ item / ic_recipe。
    返回 (recipe_id, bom_ids)。
    """
    with sqlite3.connect(wh_path) as db:
        cur = db.execute(
            "INSERT INTO recipes (name, output_unit, output_qty, sale_price, "
            "created_at, updated_at) VALUES (?, 'g', 0, 0, ?, ?)",
            (name, TS, TS),
        )
        rid = cur.lastrowid
        bom_ids = []
        for st, target_id, qty in rows:
            item_id = target_id if st == "item" else None
            ic_id = target_id if st == "ic_recipe" else None
            b = db.execute(
                "INSERT INTO recipe_items (recipe_id, source_type, item_id, "
                "ic_recipe_id, qty_per_unit) VALUES (?, ?, ?, ?, ?)",
                (rid, st, item_id, ic_id, qty),
            )
            bom_ids.append(b.lastrowid)
        db.commit()
    return rid, bom_ids


def _ic_rows(wh_path, rid):
    with sqlite3.connect(wh_path) as db:
        return db.execute(
            "SELECT id, item_id, qty_per_unit FROM ic_recipe_items "
            "WHERE ic_recipe_id = ? ORDER BY id",
            (rid,),
        ).fetchall()


def _recipe_rows(wh_path, rid):
    with sqlite3.connect(wh_path) as db:
        return db.execute(
            "SELECT id, source_type, item_id, ic_recipe_id, qty_per_unit "
            "FROM recipe_items WHERE recipe_id = ? ORDER BY id",
            (rid,),
        ).fetchall()


# ---------------------------------------------------------------------------
# 1. 冰激凌配方页（ic_recipe_edit.html）
# ---------------------------------------------------------------------------


def test_ic_bom_row_delete_removes_only_target_row(logged_client):
    """错位回归：删中间那一行，前后两行必须原封不动。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_ic_recipe(
        wh_path, "三行配方", items[:3], qtys=[500.0, 35.0, 10.0]
    )

    r = client.get(f"/recipe-cost/ic-recipes/{rid}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/recipe-cost/ic-recipes/{rid}/bom/{bom_ids[1]}/delete",
        data={"csrf_token": token},
    )
    assert r2.status_code == 302

    left = _ic_rows(wh_path, rid)
    assert [row[0] for row in left] == [bom_ids[0], bom_ids[2]], "只应删掉中间那行"
    assert [row[1] for row in left] == [items[0], items[2]]
    assert [row[2] for row in left] == [500.0, 10.0]


def test_ic_bom_row_delete_rejects_foreign_row(logged_client):
    """不能借 A 配方的 URL 删掉 B 配方的配方行。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path, count=4)
    rid_a, _ = _seed_ic_recipe(wh_path, "配方A", items[:2])
    rid_b, bom_b = _seed_ic_recipe(wh_path, "配方B", items[2:4])

    r = client.get(f"/recipe-cost/ic-recipes/{rid_a}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/recipe-cost/ic-recipes/{rid_a}/bom/{bom_b[0]}/delete",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert r2.status_code == 200
    assert "不属于此冰激凌配方" in r2.get_data(as_text=True)
    assert [row[1] for row in _ic_rows(wh_path, rid_b)] == items[2:4], (
        "别的配方的行不能被删"
    )


def test_ic_bom_row_delete_roundtrip_page_reflects_deletion(logged_client):
    """删除往返：删完再打开编辑页，该行不再出现。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_ic_recipe(wh_path, "往返配方", items[:2])

    r = client.get(f"/recipe-cost/ic-recipes/{rid}/edit")
    token = _csrf(r.get_data(as_text=True))
    client.post(
        f"/recipe-cost/ic-recipes/{rid}/bom/{bom_ids[0]}/delete",
        data={"csrf_token": token},
    )

    html = client.get(f"/recipe-cost/ic-recipes/{rid}/edit").get_data(as_text=True)
    assert f'name="bom_row_id" value="{bom_ids[0]}"' not in html, (
        "被删的行不应再出现在编辑页"
    )
    assert f'name="bom_row_id" value="{bom_ids[1]}"' in html, "另一行必须还在"


def test_ic_bom_delete_ajax_rejection_returns_ok_false(logged_client):
    """业务拒绝必须 ok:false，否则前端会当成删除成功。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path, count=4)
    rid_a, _ = _seed_ic_recipe(wh_path, "A配方", items[:2])
    rid_b, bom_b = _seed_ic_recipe(wh_path, "B配方", items[2:4])

    r = client.get(f"/recipe-cost/ic-recipes/{rid_a}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/recipe-cost/ic-recipes/{rid_a}/bom/{bom_b[0]}/delete",
        data={"csrf_token": token},
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert r2.status_code == 409, f"业务拒绝应是 409，实际 {r2.status_code}"
    body = r2.get_json()
    assert body["ok"] is False
    assert body["reason"] == "business_rule"
    assert "不属于此冰激凌配方" in body["message"]


def test_ic_bom_delete_ajax_success_returns_ok_true(logged_client):
    """正常删除走成功语义（ok:true），不被回滚逻辑误伤。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_ic_recipe(wh_path, "正常删除", items[:2])

    r = client.get(f"/recipe-cost/ic-recipes/{rid}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/recipe-cost/ic-recipes/{rid}/bom/{bom_ids[0]}/delete",
        data={"csrf_token": token},
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert r2.status_code == 200
    assert r2.get_json()["ok"] is True
    assert len(_ic_rows(wh_path, rid)) == 1


def test_ic_bom_save_duplicate_item_rolls_back(logged_client):
    """两行指向同一原料 → 整批回滚 + 指明第几行，不再 500。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_ic_recipe(
        wh_path, "重复原料配方", items[:2], qtys=[500.0, 35.0]
    )

    r = client.get(f"/recipe-cost/ic-recipes/{rid}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/recipe-cost/ic-recipes/{rid}/edit",
        data={
            "csrf_token": token,
            "name": "重复原料配方",
            "note": "",
            "output_unit": "g",
            "output_qty": "0",
            "sale_price": "0",
            "bom_row_id": [str(bom_ids[0]), str(bom_ids[1])],
            # 第二行改成与第一行相同的原料 → 撞 UNIQUE(ic_recipe_id, item_id)
            "bom_item_id": [str(items[0]), str(items[0])],
            "bom_qty": ["500", "35"],
        },
        follow_redirects=True,
    )
    assert r2.status_code == 200, "重复原料不应再抛 500"
    body = r2.get_data(as_text=True)
    assert "第 2 行" in body, "必须指出第几行重复"
    assert "重复" in body

    # 整批回滚：两行原样保留，没有被半保存改坏
    assert _ic_rows(wh_path, rid) == [
        (bom_ids[0], items[0], 500.0),
        (bom_ids[1], items[1], 35.0),
    ]


def test_ic_bom_save_updates_qty_normally(logged_client):
    """正常保存路径不能被回滚逻辑误伤。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_ic_recipe(wh_path, "正常配方", items[:1], qtys=[500.0])

    r = client.get(f"/recipe-cost/ic-recipes/{rid}/edit")
    token = _csrf(r.get_data(as_text=True))
    client.post(
        f"/recipe-cost/ic-recipes/{rid}/edit",
        data={
            "csrf_token": token,
            "name": "正常配方",
            "note": "",
            "output_unit": "g",
            "output_qty": "0",
            "sale_price": "0",
            "bom_row_id": [str(bom_ids[0])],
            "bom_item_id": [str(items[0])],
            "bom_qty": ["640"],
        },
    )
    assert _ic_rows(wh_path, rid) == [(bom_ids[0], items[0], 640.0)]


def test_ic_save_form_does_not_submit_bom_delete(logged_client):
    """「保存配方」表单不再提交 bom_delete[]（删除已改独立路由）。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_ic_recipe(wh_path, "表单契约", items[:2])

    html = client.get(f"/recipe-cost/ic-recipes/{rid}/edit").get_data(as_text=True)
    assert 'name="bom_delete"' not in html, "保存表单里不得再有 bom_delete checkbox"
    # 行尾是独立删除按钮 → 指向自带 bom_id 的删除路由
    assert f"/recipe-cost/ic-recipes/{rid}/bom/{bom_ids[0]}/delete" in html


def test_ic_new_recipe_duplicate_item_leaves_no_half_saved_recipe(logged_client):
    """新建配方撞重复原料 → 整批回滚，不留半保存的空配方。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)

    r = client.get("/recipe-cost/ic-recipes/new")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        "/recipe-cost/ic-recipes/new",
        data={
            "csrf_token": token,
            "name": "半保存配方",
            "note": "",
            "output_unit": "g",
            "output_qty": "0",
            "sale_price": "0",
            "bom_row_id": ["", ""],
            "bom_item_id": [str(items[0]), str(items[0])],
            "bom_qty": ["100", "50"],
        },
        follow_redirects=True,
    )
    assert r2.status_code == 200
    assert "第 2 行" in r2.get_data(as_text=True)

    with sqlite3.connect(wh_path) as db:
        cnt = db.execute(
            "SELECT COUNT(*) AS c FROM ic_recipes WHERE name = ?", ("半保存配方",)
        ).fetchone()[0]
    assert cnt == 0, "配方行必须与 BOM 行一起回滚"


def test_ic_save_with_duplicate_new_rows_reports_row_number(logged_client):
    """新增两行同原料 → 报第 2 行（不是笼统「保存失败」）。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, _ = _seed_ic_recipe(wh_path, "已有配方", items[:1])

    r = client.get(f"/recipe-cost/ic-recipes/{rid}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/recipe-cost/ic-recipes/{rid}/edit",
        data={
            "csrf_token": token,
            "name": "已有配方",
            "note": "",
            "output_unit": "g",
            "output_qty": "0",
            "sale_price": "0",
            "bom_row_id": ["", ""],
            "bom_item_id": [str(items[1]), str(items[1])],
            "bom_qty": ["100", "50"],
        },
        follow_redirects=True,
    )
    assert r2.status_code == 200
    assert "第 2 行" in r2.get_data(as_text=True)
    assert len(_ic_rows(wh_path, rid)) == 1, "整批回滚后不应留下新增行"


# ---------------------------------------------------------------------------
# 2. 出品配方页（recipe_edit.html）
# ---------------------------------------------------------------------------


def test_recipe_bom_row_delete_removes_only_target_row(logged_client):
    """错位回归：删中间那一行，前后两行必须原封不动。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_recipe(
        wh_path,
        "三行配方",
        [("item", items[0], 500.0), ("item", items[1], 35.0), ("item", items[2], 10.0)],
    )

    r = client.get(f"/recipe-cost/recipes/{rid}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/recipe-cost/recipes/{rid}/bom/{bom_ids[1]}/delete",
        data={"csrf_token": token},
    )
    assert r2.status_code == 302

    left = _recipe_rows(wh_path, rid)
    assert [row[0] for row in left] == [bom_ids[0], bom_ids[2]], "只应删掉中间那行"
    assert [row[2] for row in left] == [items[0], items[2]]
    assert [row[4] for row in left] == [500.0, 10.0]


def test_recipe_bom_row_delete_rejects_foreign_row(logged_client):
    """不能借 A 配方的 URL 删掉 B 配方的配方行。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path, count=4)
    rid_a, _ = _seed_recipe(wh_path, "出品A", [("item", items[0], 100.0)])
    rid_b, bom_b = _seed_recipe(wh_path, "出品B", [("item", items[2], 100.0)])

    r = client.get(f"/recipe-cost/recipes/{rid_a}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/recipe-cost/recipes/{rid_a}/bom/{bom_b[0]}/delete",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert r2.status_code == 200
    assert "不属于此出品配方" in r2.get_data(as_text=True)
    assert [row[2] for row in _recipe_rows(wh_path, rid_b)] == [items[2]], (
        "别的配方的行不能被删"
    )


def test_recipe_bom_row_delete_roundtrip_page_reflects_deletion(logged_client):
    """删除往返：删完再打开编辑页，该行不再出现。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_recipe(
        wh_path, "往返配方", [("item", items[0], 100.0), ("item", items[1], 50.0)]
    )

    r = client.get(f"/recipe-cost/recipes/{rid}/edit")
    token = _csrf(r.get_data(as_text=True))
    client.post(
        f"/recipe-cost/recipes/{rid}/bom/{bom_ids[0]}/delete",
        data={"csrf_token": token},
    )

    html = client.get(f"/recipe-cost/recipes/{rid}/edit").get_data(as_text=True)
    assert f'name="bom_row_id" value="{bom_ids[0]}"' not in html, (
        "被删的行不应再出现在编辑页"
    )
    assert f'name="bom_row_id" value="{bom_ids[1]}"' in html, "另一行必须还在"


def test_recipe_bom_delete_ajax_rejection_returns_ok_false(logged_client):
    """业务拒绝必须 ok:false，否则前端会当成删除成功。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path, count=4)
    rid_a, _ = _seed_recipe(wh_path, "出品A", [("item", items[0], 100.0)])
    rid_b, bom_b = _seed_recipe(wh_path, "出品B", [("item", items[2], 100.0)])

    r = client.get(f"/recipe-cost/recipes/{rid_a}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/recipe-cost/recipes/{rid_a}/bom/{bom_b[0]}/delete",
        data={"csrf_token": token},
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert r2.status_code == 409, f"业务拒绝应是 409，实际 {r2.status_code}"
    body = r2.get_json()
    assert body["ok"] is False
    assert body["reason"] == "business_rule"
    assert "不属于此出品配方" in body["message"]


def test_recipe_bom_delete_ajax_success_returns_ok_true(logged_client):
    """正常删除走成功语义（ok:true）。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_recipe(
        wh_path, "正常删除", [("item", items[0], 100.0), ("item", items[1], 50.0)]
    )

    r = client.get(f"/recipe-cost/recipes/{rid}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/recipe-cost/recipes/{rid}/bom/{bom_ids[0]}/delete",
        data={"csrf_token": token},
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert r2.status_code == 200
    assert r2.get_json()["ok"] is True
    assert len(_recipe_rows(wh_path, rid)) == 1


def test_recipe_bom_save_duplicate_item_rolls_back(logged_client):
    """两行指向同一原料 → 整批回滚 + 指明第几行，不再静默记两行。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_recipe(
        wh_path, "重复原料配方", [("item", items[0], 500.0), ("item", items[1], 35.0)]
    )

    r = client.get(f"/recipe-cost/recipes/{rid}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/recipe-cost/recipes/{rid}/edit",
        data={
            "csrf_token": token,
            "name": "重复原料配方",
            "note": "",
            "output_unit": "g",
            "output_qty": "0",
            "sale_price": "0",
            "bom_row_id": [str(bom_ids[0]), str(bom_ids[1])],
            "bom_source_type": ["item", "item"],
            # 第二行改成与第一行相同的原料 → 同一原料被记两行
            "bom_item_id": [str(items[0]), str(items[0])],
            "bom_ic_recipe_id": ["", ""],
            "bom_qty": ["500", "35"],
        },
        follow_redirects=True,
    )
    assert r2.status_code == 200
    body = r2.get_data(as_text=True)
    assert "第 2 行" in body, "必须指出第几行重复"
    assert "重复" in body

    # 整批回滚：两行原样保留
    assert _recipe_rows(wh_path, rid) == [
        (bom_ids[0], "item", items[0], None, 500.0),
        (bom_ids[1], "item", items[1], None, 35.0),
    ]


def test_recipe_bom_save_updates_qty_normally(logged_client):
    """正常保存路径不能被回滚逻辑误伤。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_recipe(wh_path, "正常配方", [("item", items[0], 500.0)])

    r = client.get(f"/recipe-cost/recipes/{rid}/edit")
    token = _csrf(r.get_data(as_text=True))
    client.post(
        f"/recipe-cost/recipes/{rid}/edit",
        data={
            "csrf_token": token,
            "name": "正常配方",
            "note": "",
            "output_unit": "g",
            "output_qty": "0",
            "sale_price": "0",
            "bom_row_id": [str(bom_ids[0])],
            "bom_source_type": ["item"],
            "bom_item_id": [str(items[0])],
            "bom_ic_recipe_id": [""],
            "bom_qty": ["640"],
        },
    )
    assert _recipe_rows(wh_path, rid) == [(bom_ids[0], "item", items[0], None, 640.0)]


def test_recipe_save_form_does_not_submit_bom_delete(logged_client):
    """「保存配方」表单不再提交 bom_delete[]（删除已改独立路由）。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_recipe(wh_path, "表单契约", [("item", items[0], 100.0)])

    html = client.get(f"/recipe-cost/recipes/{rid}/edit").get_data(as_text=True)
    assert 'name="bom_delete"' not in html, "保存表单里不得再有 bom_delete checkbox"
    assert f"/recipe-cost/recipes/{rid}/bom/{bom_ids[0]}/delete" in html


def test_recipe_new_recipe_duplicate_item_leaves_no_half_saved_recipe(logged_client):
    """新建配方撞重复原料 → 整批回滚，不留半保存的空配方。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)

    r = client.get("/recipe-cost/recipes/new")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        "/recipe-cost/recipes/new",
        data={
            "csrf_token": token,
            "name": "半保存出品配方",
            "note": "",
            "output_unit": "g",
            "output_qty": "0",
            "sale_price": "0",
            "bom_row_id": ["", ""],
            "bom_source_type": ["item", "item"],
            "bom_item_id": [str(items[0]), str(items[0])],
            "bom_ic_recipe_id": ["", ""],
            "bom_qty": ["100", "50"],
        },
        follow_redirects=True,
    )
    assert r2.status_code == 200
    assert "第 2 行" in r2.get_data(as_text=True)

    with sqlite3.connect(wh_path) as db:
        cnt = db.execute(
            "SELECT COUNT(*) AS c FROM recipes WHERE name = ?", ("半保存出品配方",)
        ).fetchone()[0]
    assert cnt == 0, "配方行必须与 BOM 行一起回滚"


# ---------------------------------------------------------------------------
# 3. 跨页共性契约
# ---------------------------------------------------------------------------


def test_bom_delete_route_denied_for_non_platform_admin(logged_client):
    """删除原料行是平台级操作 —— 非平台管理员不得删行。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    rid, bom_ids = _seed_ic_recipe(wh_path, "权限配方", items[:1])

    # 换一个非平台管理员的用户（user_id=2 在 master 里不存在 → 平台管理员为 False）
    with client.session_transaction() as s:
        s["user_id"] = 2

    r = client.post(
        f"/recipe-cost/ic-recipes/{rid}/bom/{bom_ids[0]}/delete",
        data={},
    )
    assert r.status_code in (302, 403, 404)
    assert len(_ic_rows(wh_path, rid)) == 1, "非管理员不得删行"
