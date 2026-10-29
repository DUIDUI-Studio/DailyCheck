"""`/admin/bot-agent` 的纯逻辑（Issue #79 PR1）。

行集合来自运行时 ``get_tools()``（29 项）+ bot 自有 5 项 = 34 主行；
四个旧名作为已接入的兼容子条目挂在对应 MCP 行下（D1=A）。
这里只提供**展示元数据**，不复制/替代注册清单 —— 新增注册项没有元数据也会
出现在页面上，标成「待确认」，且不会因此获得执行能力。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bot.agent_config import REASONING_EFFORTS, BotAgentConfig

# 显示层：name -> (中文用途, 是否写操作)。只覆盖当前 29 个注册项；
# 缺失条目走 fallback，页面仍然显示（证明行集合不是这张表）。
MCP_TOOL_META: dict[str, tuple[str, bool]] = {
    "warehouse_list": ("列出此令牌可见的仓库编码与名称。", False),
    "items_list": ("列出单个仓库的全部品项（MCP 口径）。", False),
    "items_detail": ("按 item_id + 仓库查单品详情（MCP 口径，不是旧名跨仓查询）。", False),
    "movements_list": ("列出近期库存变动流水（出库单 + 盘点调整）。", False),
    "restock_create": ("创建入库记录并增加库存。", True),
    "restock_list": ("列出入库流水。", False),
    "item_forecast": ("按单品输出消耗预测。", False),
    "procurement_store": ("单仓采购建议（旧采购口径）。", False),
    "procurement_hub": ("跨仓聚合采购建议。", False),
    "outbound_create": ("创建出库申请并扣减库存。", True),
    "outbound_list": ("列出门店出库申请。", False),
    "outbound_rollback": ("回滚出库申请并恢复库存。", True),
    "warehouse_consumption": ("单仓逐品项消耗与周转（MCP 口径）。", False),
    "item_consumption": ("单品固定窗口消耗，可选周转（MCP 口径）。", False),
    "canonical_list": ("列出主数据（全局跨仓）。", False),
    "canonical_detail": ("主数据详情 + 别名 + 跨仓绑定。", False),
    "canonical_categories": ("主数据品类清单。", False),
    "canonical_fanout_targets": ("可扇出的目标仓候选。", False),
    "canonical_conflicts": ("未裁决的主数据冲突。", False),
    "canonical_fanout_event": ("扇出事件及逐仓逐品项结果。", False),
    "canonical_create": ("新建主数据。", True),
    "canonical_update": ("更新主数据字段 / 状态 / 价格。", True),
    "canonical_claim": ("仓内品项认领到主数据。", True),
    "canonical_unclaim": ("解除品项与主数据绑定。", True),
    "canonical_fanout": ("主数据字段跨仓扇出（force 可绕冻结）。", True),
    "canonical_resolve_conflict": ("裁决主数据冲突。", True),
    "canonical_set_binding_active": ("启停仓内品项绑定行。", True),
    "store_order_list": ("列门店订单（只读；门店仓或总仓任一命中可见）。", False),
    "store_order_detail": ("单张门店订单五段详情（只读）。", False),
}

OWN_TOOLS: tuple[tuple[str, str], ...] = (
    ("search_items", "按名称 / SKU 模糊搜索品项（跨仓候选）。"),
    ("list_low_stock", "按安全库存缺口列出缺货品项。"),
    ("list_store_overview", "一次返回全部仓库 / 门店库存概览。"),
    ("list_orders", "查近 N 天门店订单，返回状态与可点链接（保留五自有口径）。"),
    ("get_project_help", "返回固定帮助文案。"),
)

# 旧名兼容子条目：name -> (父 MCP 名称, 中文用途)。均为已接入的 bot 实现。
LEGACY_TOOLS: dict[str, tuple[str, str]] = {
    "list_warehouses": (
        "warehouse_list",
        "旧名兼容：列出所有仓库编码 / 名称（bot 自有实现，没有 MCP 令牌仓范围）。",
    ),
    "list_warehouse_items": (
        "items_list",
        "旧名兼容：按仓名列品项（保留分类概览 / 截断 / 仓名解析）。",
    ),
    "get_item_detail": (
        "items_detail",
        "旧名兼容：按 SKU / 名称跨仓查库存、售价、成本（保留价格未维护语义）。",
    ),
    "get_item_consumption": (
        "item_consumption",
        "旧名兼容：按动作流水统计消耗（旧口径；不等于 MCP 未回滚单据口径）。",
    ),
}

GET_PRICE_DIAGNOSTIC = (
    "get_price 只在 bot dispatch 中存在，不是 LLM schema 工具；"
    "D6 决定价格继续由 get_item_detail 承接，保持隐藏。"
)

EFFORT_LABELS = {
    "low": "低",
    "medium": "中（默认）",
    "high": "高",
    "xhigh": "超高",
    "max": "最高",
}

assert set(EFFORT_LABELS) == set(REASONING_EFFORTS)


@dataclass(frozen=True)
class ToolRow:
    name: str
    purpose: str
    write: bool | None
    executable: bool
    connected: bool
    status: str
    status_code: str
    enabled: bool | None
    call_count: int
    maps_to: str | None = None
    children: tuple[ToolRow, ...] = field(default_factory=tuple)


def _legacy_row(
    name: str,
    parent: str,
    effective: BotAgentConfig,
    counts: dict[str, int],
) -> ToolRow:
    _parent, purpose = LEGACY_TOOLS[name]
    return ToolRow(
        name=name,
        purpose=purpose,
        write=False,
        executable=True,
        connected=True,
        status="旧名兼容（已接入）",
        status_code="legacy",
        enabled=name in effective.enabled_tools,
        call_count=counts.get(name, 0),
        maps_to=parent,
    )


def build_tool_rows(
    mcp_tools: list[Any],
    effective: BotAgentConfig,
    counts: dict[str, int],
) -> list[ToolRow]:
    """34 主行：实时 MCP 注册项 + bot 五自有；旧名只作为子条目。"""
    rows: list[ToolRow] = []

    for tool in mcp_tools:
        name = getattr(tool, "name", "")
        if not name:
            continue
        meta = MCP_TOOL_META.get(name)
        if meta is None:
            description = (getattr(tool, "description", "") or "").strip()
            purpose = f"未登记用途：{description[:120]}" if description else "未登记用途。"
            write: bool | None = None
            status = "待确认（新增注册项，默认不执行）"
            status_code = "pending"
        else:
            purpose, write = meta
            status = "未接入（PR1 仅展示，PR2 才可能执行）"
            status_code = "not_connected"

        children = tuple(
            _legacy_row(child, name, effective, counts)
            for child in LEGACY_TOOLS
            if LEGACY_TOOLS[child][0] == name
        )
        rows.append(
            ToolRow(
                name=name,
                purpose=purpose,
                write=write,
                executable=False,
                connected=False,
                status=status,
                status_code=status_code,
                enabled=None,
                call_count=counts.get(name, 0),
                children=children,
            )
        )

    for name, purpose in OWN_TOOLS:
        rows.append(
            ToolRow(
                name=name,
                purpose=purpose,
                write=False,
                executable=True,
                connected=True,
                status="已接入",
                status_code="connected",
                enabled=name in effective.enabled_tools,
                call_count=counts.get(name, 0),
            )
        )
    return rows


def switchable_tool_names() -> frozenset[str]:
    """当前可被页面开关真正控制的工具（现有 9 项）。

    配置只写这些键；MCP 名称即使被伪造 POST 也不会落库、更不会进入执行集合。
    """
    from bot.tool_schema import TOOL_NAMES

    return frozenset(TOOL_NAMES)
