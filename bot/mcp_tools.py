"""Explicit bot MCP adapter: approved handlers + explicit call-owned AuthContext."""
from __future__ import annotations

from bot.access import SenderAccess
from bot.agent_config import BotAgentConfig
from bot.tool_contract import GLOBAL_READS, MCP_NAMES, READ_PATHS, WRITE_PATHS
from mcp_server.infra.errors import ForbiddenError
from mcp_server.service.auth import AuthContext, check_path, check_warehouse


def handlers() -> dict:
    from mcp_server.protocol.tools import (
        canonical,
        consumption,
        forecast,
        inbound,
        inventory,
        outbound,
        procurement,
        store_order,
    )

    modules = (canonical, consumption, forecast, inbound, inventory, outbound, procurement, store_order)
    return {
        name: getattr(module, name + "_impl")
        for module in modules for name in MCP_NAMES if hasattr(module, name + "_impl")
    }


def execute(name: str, args: dict, config: BotAgentConfig, sender: SenderAccess):
    if name not in MCP_NAMES or name not in config.enabled_tools:
        raise ForbiddenError("tool_disabled: 工具未启用")
    read_ctx = sender.read_context()
    path = WRITE_PATHS.get(name) or READ_PATHS[name]
    warehouse = args.get("warehouse_code")
    if name in WRITE_PATHS:
        if not sender.identity_verified:
            raise ForbiddenError("unverified_userid: owner 真实 userid 尚未核实，拒绝写入")
        if not sender.userid or sender.userid not in config.write_allowed_userids:
            raise ForbiddenError("forbidden_userid: 你没有改数据的权限（发送者不在白名单）")
        # Check trusted deployment policy BEFORE constructing a write context.
        from mcp_server.agent_mpc_pure import path_matches

        if not any(path_matches(pattern, path) for pattern in sender.write_paths):
            raise ForbiddenError("forbidden_path: 没有此写路径权限")
        if not isinstance(warehouse, str) or not warehouse or not check_warehouse(read_ctx, warehouse):
            raise ForbiddenError("forbidden_warehouse: 没有此仓库权限")
        ctx = AuthContext(0, [], [path], [warehouse])
    else:
        if not (check_path(read_ctx, "GET", path) or (
            name.startswith("store_order_") and check_path(read_ctx, "GET", path + "/")
        )):
            raise ForbiddenError("forbidden_path: 没有此查询路径权限")
        if warehouse is not None and not check_warehouse(read_ctx, warehouse):
            raise ForbiddenError("forbidden_warehouse: 没有此仓库权限")
        if name in GLOBAL_READS and (not sender.global_read or sender.warehouses is not None):
            raise ForbiddenError("forbidden_global_read: 此工具含全局主数据/跨仓信息，需要显式授权")
        ctx = read_ctx
    result = handlers()[name](args, ctx=ctx)
    if name == "restock_create" and isinstance(result.get("id"), dict):
        result = {**result, **result["id"]}
    return result
