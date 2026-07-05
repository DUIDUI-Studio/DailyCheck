"""上传 + 预览路由测试。"""
import io

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
        "warehouse_code": "wh_test",
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
        "warehouse_code": "wh_test",
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


def test_non_admin_cannot_access(logged_client):
    """非 admin 访问 /admin/import-items 应被拒。"""
    client, _wh_path = logged_client
    # 用全新 client,没有 session → 应重定向登录或 403
    client2 = client.application.test_client()
    resp = client2.get("/admin/import-items")
    assert resp.status_code in (302, 403)
