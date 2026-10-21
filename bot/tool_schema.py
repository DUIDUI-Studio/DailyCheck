"""给 LLM 的工具定义（JSON Schema）。

description 里刻意堆了同义说法 —— 这是「意图映射宽」的主要手段。员工不会
按字典说话：「还有多少」「剩几箱」「够不够」「快没了吧」都得落到同一个工具。
靠正则兜这些说法既脆又难维护，交给模型判断，但得把线索喂足。
"""

from __future__ import annotations

from typing import Any

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_items",
            "description": (
                "按名称或 SKU 模糊搜索品项，返回候选清单。"
                "当员工说的名字不完整、可能有多个近似项、或者你不确定具体是哪一个品项时，"
                "先调这个。关键词只用核心名词（如「面粉」「芒果」「PP017」），"
                "不要带「库存」「多少」这类问句词。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，品名片段或 SKU，例如「芒果」「PP017」",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回几条，默认 20",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": ["keyword"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_item_detail",
            "description": (
                "查询某个品项在各仓库的库存数量、安全库存、单位、售价和成本。"
                "凡是问「还有多少」「库存多少」「剩几箱」「够不够」「在哪个仓」"
                "「多少钱」「价格多少」都用这个。"
                "默认查全部仓库并给出总量；只有员工明确提到某个仓时才传 warehouse_code。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sku_or_name": {
                        "type": "string",
                        "description": "品项名称或 SKU，可以是完整名称，也可以是唯一片段",
                    },
                    "warehouse_code": {
                        "type": "string",
                        "description": "仓库编码，如 wh_000。仅在员工明确指定仓库时传，否则留空查全部",
                    },
                },
                "required": ["sku_or_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_warehouse_items",
            "description": (
                "列出**单个**仓库有哪些品项，返回分类概览和库存最多的若干条。"
                "用于「新天地店有什么」「某某仓都备了什么货」这类问单个仓的问题。"
                "问「各门店库存状况」「各仓对比」**不要**用这个（逐个调会漏），"
                "改用 list_store_overview。"
                "必须传仓库编码；员工说的是仓名时，先调 list_warehouses 对出编码。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "warehouse_code": {
                        "type": "string",
                        "description": "仓库编码，如 wh_003",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多列几条品项，默认 30",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": ["warehouse_code"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_low_stock",
            "description": (
                "列出低于安全库存的品项，按缺口从大到小排序。"
                "用于「哪些快断货了」「有什么需要补货的」「缺货清单」「库存预警」这类问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "warehouse_code": {
                        "type": "string",
                        "description": "仓库编码，留空则查全部仓库",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回几条，默认 15",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_warehouses",
            "description": (
                "列出所有仓库的编码和名称。"
                "当员工提到的仓库名你无法对应到编码时，先调这个确认，不要猜。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_store_overview",
            "description": (
                "一次给出所有仓库/门店的库存概览：品项数、有货品项数、零库存数、"
                "低于安全库存数。"
                "凡是问「各门店库存状况」「哪个店货多」「整体库存怎么样」"
                "「各家门店对比」都用这个——**一次拿全，不要逐个仓去调 "
                "list_warehouse_items**。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_item_consumption",
            "description": (
                "查某个品项最近一段时间的消耗量，按仓给出小计。"
                "用于「最近消耗了多少」「这个月用了多少」「出库量多少」"
                "「哪个店用得最快」这类问题。"
                "只统计出库类流水，不含补货入库与出库回退。"
                "默认近 30 天；员工说「这周」「最近三个月」时按对应天数传。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sku_or_name": {
                        "type": "string",
                        "description": "品项名称或 SKU",
                    },
                    "days": {
                        "type": "integer",
                        "description": "统计最近多少天，默认 30。「这周」传 7，「三个月」传 90",
                        "minimum": 1,
                        "maximum": 365,
                    },
                    "warehouse_code": {
                        "type": "string",
                        "description": "仓库编码，仅在员工明确指定某仓时传，否则留空查全部",
                    },
                },
                "required": ["sku_or_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_orders",
            "description": (
                "查门店订货单，返回订单号、门店、状态、时间以及**可直接点开的链接**。"
                "用于「哪些订单还没处理」「最近有什么订单」「昨天订了什么」"
                "「有没有待发货的」这类问题。"
                "「还没处理」的准确口径是 status=approved（已审批待发货），"
                "不要自己猜成别的状态；问「最近的订单」就不要传 status。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "订单状态筛选。approved=已审批待发货（即「没处理」），shipped=已发货，delivered=已送达，cancelled=已取消，pending=待审批。留空查全部",
                        "enum": ["pending", "approved", "shipped", "delivered", "cancelled"],
                    },
                    "days": {
                        "type": "integer",
                        "description": "查最近多少天，默认 7",
                        "minimum": 1,
                        "maximum": 365,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回几条，默认 20",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_help",
            "description": (
                "返回系统的能力介绍与常用问法。"
                "用于「你能干什么」「怎么用」「有哪些功能」「能查什么」"
                "以及员工第一次打招呼时。返回的是固定文案，**原样转发**，不要改写。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]

TOOL_NAMES = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
