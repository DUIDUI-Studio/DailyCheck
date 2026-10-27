"""生产配方：归档（隐藏）+ 原料行删除 + 保存失败回滚。

回归三条真实故障（2026-10-25 韩国街店）：
  1. 有生产记录的产品点「删除」→ 后端拒绝，但 AJAX hook 一律包成
     ok:true，前端把卡片移除 → 用户以为删掉了，刷新又回来。
  2. 配方里用 checkbox 打勾删原料 → 未勾选的行不提交，bom_delete[]
     数组比 bom_row_id[] 短 → 按索引配对整体错位（删 A 行结果删掉 B 行）。
  3. 保存配方时 UPDATE 撞 UNIQUE(product_id,item_id) → 未捕获 500，
     且部分语句已执行、事务未 commit，状态不明。

修复后的契约：
  - 删除有生产记录的产品 → error flash + 产品保留（AJAX 返 ok:false）
  - 管理员可隐藏/恢复，隐藏不进列表与生产录入下拉，BOM/历史全留
  - 原料删除是行级独立路由，自带 bom_id，不依赖任何数组顺序
  - 保存撞唯一约束 → 整批回滚 + 明确提示，不再 500
"""
import sqlite3

TS = "2026-10-25 09:00:00"


def _csrf(html: str) -> str:
    import re

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
                (f"PRD-{i}", f"原料{i}", TS),
            )
            ids.append(cur.lastrowid)
        db.commit()
    return ids


def _seed_product(wh_path, name, item_ids, qtys=None, with_run=False):
    """建一个产品 + BOM 行（可选带一条生产记录）。返回 (product_id, bom_ids)。"""
    qtys = qtys or [500.0] * len(item_ids)
    with sqlite3.connect(wh_path) as db:
        cur = db.execute(
            "INSERT INTO products (name, unit, note, is_hidden, created_at) "
            "VALUES (?, '件', '', 0, ?)",
            (name, TS),
        )
        pid = cur.lastrowid
        bom_ids = []
        for item_id, qty in zip(item_ids, qtys, strict=False):
            b = db.execute(
                "INSERT INTO product_bom (product_id, item_id, qty_per_unit) "
                "VALUES (?, ?, ?)",
                (pid, item_id, qty),
            )
            bom_ids.append(b.lastrowid)
        if with_run:
            db.execute(
                "INSERT INTO production_runs "
                "(product_id, output_qty, note, rolled_back, created_by, created_at) "
                "VALUES (?, 1.0, '', 0, 'tester', ?)",
                (pid, TS),
            )
        db.commit()
    return pid, bom_ids


def _product_row(wh_path, pid):
    with sqlite3.connect(wh_path) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()


def _bom_item_ids(wh_path, pid):
    with sqlite3.connect(wh_path) as db:
        return [
            r[0]
            for r in db.execute(
                "SELECT item_id FROM product_bom WHERE product_id = ? ORDER BY id",
                (pid,),
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# 1. 删除守卫：有生产记录就不删，且必须是「明确的失败」
# ---------------------------------------------------------------------------


def test_delete_with_runs_is_rejected_and_product_survives(logged_client):
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    pid, _ = _seed_product(wh_path, "有历史配方", items[:2], with_run=True)

    r = client.get("/production")
    token = _csrf(r.get_data(as_text=True))

    r2 = client.post(
        f"/production/products/{pid}/delete",
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert r2.status_code == 200
    assert "不能删除" in r2.get_data(as_text=True)
    assert _product_row(wh_path, pid) is not None, "有生产记录的产品必须保留"


def test_delete_without_runs_still_works(logged_client):
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    pid, _ = _seed_product(wh_path, "无历史配方", items[:1])

    r = client.get("/production")
    token = _csrf(r.get_data(as_text=True))
    client.post(
        f"/production/products/{pid}/delete",
        data={"csrf_token": token},
    )
    assert _product_row(wh_path, pid) is None


def test_ajax_delete_rejection_returns_ok_false(logged_client):
    """核心回归：业务拒绝必须 ok:false，否则前端会 remove 掉卡片造成假成功。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    pid, _ = _seed_product(wh_path, "有历史-ajax", items[:1], with_run=True)

    r = client.get("/production")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/production/products/{pid}/delete",
        data={"csrf_token": token},
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert r2.status_code == 409, f"业务拒绝应是 409，实际 {r2.status_code}"
    body = r2.get_json()
    assert body["ok"] is False
    assert body["reason"] == "business_rule"
    assert "不能删除" in body["message"]
    assert _product_row(wh_path, pid) is not None


# ---------------------------------------------------------------------------
# 2. 隐藏 / 恢复
# ---------------------------------------------------------------------------


def test_hidden_product_is_out_of_list_but_recoverable(logged_client):
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    pid, _ = _seed_product(wh_path, "待隐藏配方", items[:2], with_run=True)

    r = client.get("/production")
    token = _csrf(r.get_data(as_text=True))

    client.post(
        f"/production/products/{pid}/toggle-hidden",
        data={"csrf_token": token, "hidden": "1"},
    )
    assert _product_row(wh_path, pid)["is_hidden"] == 1

    # 先 GET 一次把 flash（含产品名）消耗掉，否则断言会把提示文案当列表内容
    client.get("/production")
    default_html = client.get("/production").get_data(as_text=True)
    assert "待隐藏配方" not in default_html, "隐藏后默认列表不应出现"

    shown_html = client.get("/production?show_hidden=1").get_data(as_text=True)
    assert "待隐藏配方" in shown_html, "show_hidden=1 应能看到并恢复"
    assert "已隐藏" in shown_html

    r = client.get("/production?show_hidden=1")
    token = _csrf(r.get_data(as_text=True))
    client.post(
        f"/production/products/{pid}/toggle-hidden",
        data={"csrf_token": token, "hidden": "0"},
    )
    assert _product_row(wh_path, pid)["is_hidden"] == 0
    client.get("/production")  # 消耗「已恢复」flash
    assert "待隐藏配方" in client.get("/production").get_data(as_text=True)


def test_hidden_product_excluded_from_session_dropdown(logged_client):
    """隐藏的配方不能再出现在生产录入的产品下拉里。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    pid, _ = _seed_product(wh_path, "隐藏后不进下拉", items[:1])
    with sqlite3.connect(wh_path) as db:
        db.execute("UPDATE products SET is_hidden = 1 WHERE id = ?", (pid,))
        db.commit()

    html = client.get("/production/session").get_data(as_text=True)
    assert "隐藏后不进下拉" not in html


# ---------------------------------------------------------------------------
# 3. 原料行删除：只删目标行
# ---------------------------------------------------------------------------


def test_bom_row_delete_removes_only_target_row(logged_client):
    """错位回归：删中间那一行，前后两行必须原封不动。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    pid, bom_ids = _seed_product(
        wh_path, "三行配方", items[:3], qtys=[500.0, 35.0, 10.0]
    )

    r = client.get(f"/production/products/{pid}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/production/products/{pid}/bom/{bom_ids[1]}/delete",
        data={"csrf_token": token},
    )
    assert r2.status_code == 302

    with sqlite3.connect(wh_path) as db:
        left = db.execute(
            "SELECT id, item_id, qty_per_unit FROM product_bom "
            "WHERE product_id = ? ORDER BY id",
            (pid,),
        ).fetchall()
    assert [row[0] for row in left] == [bom_ids[0], bom_ids[2]], "只应删掉中间那行"
    assert [row[1] for row in left] == [items[0], items[2]]
    assert [row[2] for row in left] == [500.0, 10.0]


def test_bom_row_delete_rejects_foreign_row(logged_client):
    """不能借 A 产品的 URL 删掉 B 产品的配方行。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path, count=4)
    pid_a, _ = _seed_product(wh_path, "产品A", items[:2])
    pid_b, bom_b = _seed_product(wh_path, "产品B", items[2:4])

    r = client.get(f"/production/products/{pid_a}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/production/products/{pid_a}/bom/{bom_b[0]}/delete",
        data={"csrf_token": token},
    )
    assert r2.status_code == 302
    assert _bom_item_ids(wh_path, pid_b) == items[2:4], "别的产品的行不能被删"


# ---------------------------------------------------------------------------
# 4. 保存配方：重复原料整批回滚，不 500
# ---------------------------------------------------------------------------


def test_bom_save_duplicate_item_rolls_back(logged_client):
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    pid, bom_ids = _seed_product(
        wh_path, "重复原料配方", items[:2], qtys=[500.0, 35.0]
    )

    r = client.get(f"/production/products/{pid}/edit")
    token = _csrf(r.get_data(as_text=True))
    r2 = client.post(
        f"/production/products/{pid}/edit",
        data={
            "csrf_token": token,
            "name": "重复原料配方",
            "unit": "件",
            "note": "",
            "bom_row_id": [str(bom_ids[0]), str(bom_ids[1])],
            # 第二行改成与第一行相同的原料 → 撞 UNIQUE(product_id,item_id)
            "bom_item_id": [str(items[0]), str(items[0])],
            "bom_qty": ["500", "35"],
        },
        follow_redirects=True,
    )
    assert r2.status_code == 200, "重复原料不应再抛 500"
    assert "重复" in r2.get_data(as_text=True)

    # 整批回滚：两行原样保留，没有被半保存改坏
    with sqlite3.connect(wh_path) as db:
        rows = db.execute(
            "SELECT item_id, qty_per_unit FROM product_bom "
            "WHERE product_id = ? ORDER BY id",
            (pid,),
        ).fetchall()
    assert rows == [(items[0], 500.0), (items[1], 35.0)]


def test_bom_save_updates_qty_normally(logged_client):
    """正常保存路径不能被回滚逻辑误伤。"""
    client, wh_path = logged_client
    items = _seed_items(wh_path)
    pid, bom_ids = _seed_product(wh_path, "正常配方", items[:1], qtys=[500.0])

    r = client.get(f"/production/products/{pid}/edit")
    token = _csrf(r.get_data(as_text=True))
    client.post(
        f"/production/products/{pid}/edit",
        data={
            "csrf_token": token,
            "name": "正常配方",
            "unit": "件",
            "note": "",
            "bom_row_id": [str(bom_ids[0])],
            "bom_item_id": [str(items[0])],
            "bom_qty": ["640"],
        },
    )
    with sqlite3.connect(wh_path) as db:
        qty = db.execute(
            "SELECT qty_per_unit FROM product_bom WHERE id = ?", (bom_ids[0],)
        ).fetchone()[0]
    assert qty == 640.0
