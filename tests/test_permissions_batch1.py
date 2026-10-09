"""批 1 权限回归（FIX-1 删除权限倒挂 + FIX-4 revenue 越权）。

FIX-1: /items/<id>/delete 曾只有 @require_role("staff")，比查看（platform_admin）
       还宽松 → 绑定门店的 staff 用户可直接删品项。现对齐为 platform_admin。
FIX-4: /api/revenue 原无任何角色校验（仅可选 REVENUE_TOKEN）→ 任意登录用户可篡改
       营业额。现要求登录 + 至少 manager。
"""
from tests.conftest import _seed_item


def test_staff_cannot_delete_item(staff_client):
    """FIX-1：staff 删品项必须 403（改前会 302 成功删除）。"""
    client, wh_path = staff_client
    item_id, _ = _seed_item(wh_path, "smoke-del", 1, 1.0)
    resp = client.post(f"/items/{item_id}/delete")
    assert resp.status_code == 403, resp.status_code


def test_admin_can_delete_item(logged_client):
    """FIX-1：平台管理员删除路径不受影响。"""
    client, wh_path = logged_client
    item_id, _ = _seed_item(wh_path, "smoke-del-ok", 1, 1.0)
    resp = client.post(f"/items/{item_id}/delete")
    assert resp.status_code == 302, resp.status_code


def test_staff_cannot_upload_revenue(staff_client):
    """FIX-4：staff 写营业额必须 403（改前无校验，直接 200）。"""
    client, _ = staff_client
    resp = client.post("/api/revenue", data={"date": "2026-10-01", "amount": "12.5"})
    assert resp.status_code == 403, resp.status_code


def test_admin_can_upload_revenue(logged_client):
    """FIX-4：具备权限的调用方仍可正常 upsert。"""
    client, _ = logged_client
    resp = client.post("/api/revenue", data={"date": "2026-10-01", "amount": "12.5"})
    assert resp.status_code == 200, resp.status_code
    assert resp.get_data(as_text=True).startswith("OK ")
