"""MCP 入库工具测试：restock_create 写入后 restock_list 立即读回。"""
from mcp_server.protocol.tools import inbound as inbound_tools
from mcp_server.service import inbound as inbound_service
from mcp_server.service.auth import AuthContext


def _patch_warehouse(monkeypatch, db_path):
    """把服务层的仓库解析指向临时库（工具层经 service 层取数）。"""
    monkeypatch.setattr(
        inbound_service,
        "resolve_warehouse",
        lambda code: {"code": code, "db_path": db_path},
    )


def test_restock_create_then_restock_list_reads_back(warehouse_db, monkeypatch):
    """MCP 工具链：restock_create 落库 → restock_list 立刻返回同一条入库单."""
    _patch_warehouse(monkeypatch, warehouse_db)
    ctx = AuthContext(
        token_id=1,
        allowed_read_paths=["/api/v1/restock"],
        allowed_write_paths=["/api/v1/restock"],
        allowed_warehouses=["WH001"],
    )
    monkeypatch.setattr(inbound_tools, "_get_ctx", lambda: ctx)

    created = inbound_tools.restock_create_impl(
        {
            "item_id": 1,
            "quantity": 5,
            "warehouse_code": "WH001",
            "reason": "demo",
        }
    )
    rows = inbound_tools.restock_list_impl({"warehouse_code": "WH001"})

    assert created["item_id"] == 1
    assert created["quantity"] == 5
    assert len(rows) == 1
    assert set(rows[0]) == {
        "id", "item_id", "item_name", "quantity", "status", "reason", "created_at"
    }
    assert rows[0]["item_id"] == 1
    assert rows[0]["item_name"] == "Item A"
    assert rows[0]["quantity"] == 5
    assert rows[0]["status"] == "入库"
    assert rows[0]["reason"] == "demo"
    assert rows[0]["created_at"]
