"""成本类字段脱敏（Phase 3，服务端）。

统一函数，供报表 / 导出在「调用者无 `report.cost.view`」时把成本金额列
**就地留空**——脱敏发生在服务端（不是前端隐藏），无权限用户拿到的响应里
根本不含成本数值。

依据：docs/2026-10-12-permission-architecture.md §3.7（A/B/C/D/E 组成本边界）。
判定入口由调用方提供（`allow` 布尔），本模块保持纯函数、零 Flask 依赖，便于
单测与复用：

    allow = g.user["is_admin"] or ("report.cost.view" in current_permissions())
"""
from __future__ import annotations

from typing import Any

# §3.7-A 存储单价 + §3.7-B/C 金额/派生金额键名（供 dict 形态脱敏）。
COST_KEYS: frozenset[str] = frozenset({
    # A 组：存储单价
    "unit_cost", "selling_price", "sale_price",
    # B 组：金额
    "amount", "shipping_fee", "consumed_amount",
    # C 组：派生金额列
    "consumed_value", "total_inbound_value", "total_consumed_value",
    "total_stock_value", "inbound_value", "stock_value", "avg_stock_value",
})


def mask_cost(mapping: dict[str, Any], keys: object, allow: bool) -> dict[str, Any]:
    """`allow=False` 时把 `keys` 命中的键值置 `None`。返回新 dict，不改入参。"""
    if allow:
        return dict(mapping)
    key_set = set(keys)
    return {k: (None if k in key_set else v) for k, v in mapping.items()}


def mask_cost_csv_row(values: list[Any], indices: object, allow: bool) -> list[Any]:
    """`allow=False` 时把指定列索引的值替换为空串（列保留、值留空）。

    保留列结构（不删列），避免破坏下游按列位解析的消费方。
    """
    if allow:
        return list(values)
    idx = set(indices)
    return [("" if i in idx else v) for i, v in enumerate(values)]
