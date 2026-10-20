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
                "列出某个仓库有哪些品项，返回分类概览和库存最多的若干条。"
                "用于「新天地店有什么」「某某仓都备了什么货」「这个仓有哪些东西」这类问题。"
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
]

TOOL_NAMES = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
