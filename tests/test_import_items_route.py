"""上传 + 预览 + commit 路由测试。"""
import io
import sqlite3
from datetime import datetime

from openpyxl import Workbook


def _login_admin(client):
    """直接设 session 为 admin(role='admin' on warehouse)。"""
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["warehouse_id"] = 1


def _make_xlsx_bytes(rows: list[tuple]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.cell(1, 1, "title")
    ws.cell(2, 1, "分类"); ws.cell(2, 2, "物料名称")
    ws.cell(2, 7, "单位"); ws.cell(2, 8, "隐藏栏/盘点单位单价")
    for r_idx, row in enumerate(rows, start=3):
        for c_idx, val in enumerate(row, start=1):
            ws.cell(r_idx, c_idx, val)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_upload_form_renders_for_admin(logged_client):
    client, _ = logged_client
    _login_admin(client)
    resp = client.get("/admin/import-items")
    assert resp.status_code == 200
    # 检查页面中至少包含一个核心标识词
    assert b"\xe6\x9c\x8d\xe5\x8a\xa1\xe5\x99\xa8\xe7\x9b\xae\xe5\x89\x8d" in resp.data or \
           b"\xe5\x93\x81\xe9\xa1\xb9\xe6\x89\xb9\xe9\x87\x8f\xe5\xaf\xbc\xe5\x85\xa5" in resp.data


def test_upload_parse_redirects_to_preview(logged_client):
    client, _ = logged_client
    _login_admin(client)
    xlsx_bytes = _make_xlsx_bytes([
        ("X", "A", None, "s", 100, 5, "箱", 10, 50),
    ])
    data = {
        "file": (io.BytesIO(xlsx_bytes), "test.xlsx"),
    }
    resp = client.post("/admin/import-items", data=data,
                       content_type="multipart/form-data", follow_redirects=False)
    assert resp.status_code == 302
    assert "/admin/import-items/preview" in resp.headers["Location"]


def test_upload_rejects_non_xlsx(logged_client):
    client, _ = logged_client
    _login_admin(client)
    data = {
        "file": (io.BytesIO(b"not an xlsx"), "test.txt"),
    }
    resp = client.post("/admin/import-items", data=data,
                       content_type="multipart/form-data", follow_redirects=False)
    # 拒绝: 重定向回 form
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin/import-items"


def test_preview_without_session_redirects(logged_client):
    client, _ = logged_client
    _login_admin(client)
    resp = client.get("/admin/import-items/preview", follow_redirects=False)
    # 没有 session 缓存 → 重定向回 form
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin/import-items"


def test_unauthenticated_cannot_access(logged_client):
    """未登录访问 /admin/import-items 应被拒。"""
    client, _wh_path = logged_client
    # 用全新 client,没有 session → 应重定向登录或 403
    client2 = client.application.test_client()
    resp = client2.get("/admin/import-items")
    assert resp.status_code in (302, 403)


# ---------------------------------------------------------------------------
# commit 路由测试 —— P0-3 收口后的「非破坏性」语义
#
# 旧语义（DELETE 分组下 items + 子表流水 + INSERT 新行）已按
# docs/2026-10-09-item-master-unify-plan.md §3 P0-3 废除。
# 新契约：
#   1. 只更新「本仓已存在品项」的进货单价，按 (品类名, 品项名) 匹配；
#   2. 不新建品项、不新建品类、不删除任何行（含历史流水）；
#   3. 未匹配行跳过并回报；
#   4. 安全库存不写（P0-12 由系统计算）。
# ---------------------------------------------------------------------------


def _first_category_name(wh_path) -> str:
    conn = sqlite3.connect(wh_path)
    name = conn.execute("SELECT name FROM categories ORDER BY id LIMIT 1").fetchone()[0]
    conn.close()
    return name


def _seed_wh_item(wh_path, cat_name, item_name, unit="件", unit_cost=1.0):
    """在测试仓内插一个已有品项（可指定分类名），返回 item_id。"""
    conn = sqlite3.connect(wh_path)
    conn.row_factory = sqlite3.Row
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute("SELECT id FROM categories WHERE name=?", (cat_name,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO categories (name, description, created_at) VALUES (?, '', ?)",
            (cat_name, ts))
        cat_id = cur.lastrowid
    else:
        cat_id = int(row["id"])
    cur = conn.execute(
        "INSERT INTO items (sku, name, category_id, quantity, safety_stock, "
        "unit_cost, unit, gram_per_unit, updated_at) "
        "VALUES (?, ?, ?, 0, 0, ?, ?, 0, ?)",
        (f"SKU-{item_name}", item_name, cat_id, unit_cost, unit, ts))
    item_id = cur.lastrowid
    conn.commit()
    conn.close()
    return item_id


def _upload(client, rows):
    xlsx_bytes = _make_xlsx_bytes(rows)
    return client.post(
        "/admin/import-items",
        data={"file": (io.BytesIO(xlsx_bytes), "test.xlsx")},
        content_type="multipart/form-data", follow_redirects=False,
    )


def test_commit_requires_session(logged_client):
    """无 session 缓存 → commit 拒绝,items 表不应有新增。"""
    client, wh_path = logged_client
    _login_admin(client)
    resp = client.post("/admin/import-items/commit", follow_redirects=True)
    assert resp.status_code in (200, 400)
    wh = sqlite3.connect(wh_path)
    wh.row_factory = sqlite3.Row
    n = wh.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    assert n == 0
    wh.close()


def test_commit_never_creates_item_or_category(logged_client):
    """P0-3：xlsx 里的新品项/新品类一律不落库（须先在主数据建档）。"""
    client, wh_path = logged_client
    _login_admin(client)
    _upload(client, [("不存在的分类", "A", None, "s", 100, 5, "箱", 10, 50)])
    resp = client.post("/admin/import-items/commit", follow_redirects=False)
    assert resp.status_code == 302
    wh = sqlite3.connect(wh_path)
    wh.row_factory = sqlite3.Row
    assert wh.execute(
        "SELECT COUNT(*) AS c FROM categories WHERE name='不存在的分类'"
    ).fetchone()["c"] == 0
    assert wh.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0
    wh.close()


def test_commit_updates_unit_cost_only_for_matched_rows(logged_client):
    """匹配行只改进货单价；未匹配行跳过；行数与其它字段不变。"""
    client, wh_path = logged_client
    _login_admin(client)
    cat = _first_category_name(wh_path)
    _seed_wh_item(wh_path, cat, "AAA", unit="箱", unit_cost=1.0)
    _upload(client, [
        (cat, "AAA", None, "spec", 100, 5, "袋", 10, 50),   # 匹配 → 单价 1→10
        (cat, "BBB", None, "spec", 100, 5, "袋", 99, 50),   # 未匹配 → 跳过
    ])
    resp = client.post("/admin/import-items/commit", follow_redirects=False)
    assert resp.status_code == 302
    wh = sqlite3.connect(wh_path)
    wh.row_factory = sqlite3.Row
    rows = wh.execute("SELECT name, unit, unit_cost FROM items ORDER BY id").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "AAA"
    assert rows[0]["unit_cost"] == 10.0       # 单价被更新
    assert rows[0]["unit"] == "箱"            # 单位是主数据字段，不被导入改动
    assert wh.execute("SELECT COUNT(*) AS c FROM items WHERE name='BBB'").fetchone()["c"] == 0
    wh.close()


def test_commit_is_idempotent(logged_client):
    """重复 commit → 行数不变、单价收敛到同一值。"""
    client, wh_path = logged_client
    _login_admin(client)
    cat = _first_category_name(wh_path)
    _seed_wh_item(wh_path, cat, "AAA", unit="箱", unit_cost=1.0)
    for _ in range(2):
        _upload(client, [(cat, "AAA", None, "s", 100, 5, "箱", 10, 50)])
        client.post("/admin/import-items/commit", follow_redirects=False)
    wh = sqlite3.connect(wh_path)
    wh.row_factory = sqlite3.Row
    rows = wh.execute("SELECT name, unit_cost FROM items").fetchall()
    assert len(rows) == 1
    assert rows[0]["unit_cost"] == 10.0
    wh.close()


def test_commit_preserves_items_and_history(logged_client):
    """P0-3 核心回归：不再 DELETE items / 子表流水。"""
    client, wh_path = logged_client
    _login_admin(client)
    cat = _first_category_name(wh_path)
    item_id = _seed_wh_item(wh_path, cat, "OldItem", unit_cost=1.0)
    wh = sqlite3.connect(wh_path)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wh.execute(
        "INSERT INTO stock_movements (item_id, action, delta, created_at) "
        "VALUES (?, 'restock', 10, ?)", (item_id, ts))
    wh.commit()
    wh.close()

    _upload(client, [(cat, "NewItem", None, "s", 100, 5, "箱", 10, 50)])
    resp = client.post("/admin/import-items/commit", follow_redirects=False)
    assert resp.status_code == 302

    wh = sqlite3.connect(wh_path)
    wh.row_factory = sqlite3.Row
    names = [r["name"] for r in wh.execute("SELECT name FROM items").fetchall()]
    assert names == ["OldItem"]                       # 旧行没被删、新行没被建
    assert wh.execute("SELECT COUNT(*) AS c FROM stock_movements").fetchone()["c"] == 1
    wh.close()


def test_preview_marks_matched_and_unmatched(logged_client):
    """预览页逐行标注匹配状态，未匹配行明确提示会跳过。"""
    client, wh_path = logged_client
    _login_admin(client)
    cat = _first_category_name(wh_path)
    _seed_wh_item(wh_path, cat, "AAA", unit="箱", unit_cost=1.0)
    _upload(client, [
        (cat, "AAA", None, "s", 100, 5, "箱", 10, 50),
        (cat, "BBB", None, "s", 100, 5, "箱", 20, 50),
    ])
    resp = client.get("/admin/import-items/preview")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "已匹配" in body
    assert "未匹配" in body
    assert "不会新增或删除任何品项" in body

