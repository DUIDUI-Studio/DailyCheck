"""Jinja filter 与 helper 单元测试。

聚焦于模板中跨页面复用的小型格式化函数，确保覆盖边界场景（None /
空串 / name 与 code 相同 / code 缺失）。"""
from __future__ import annotations

from blueprints._helpers import fmt_warehouse

# ---------------------------------------------------------------------------
# Issue #122（2026-10-19）—— fmt_warehouse 模板 filter
#
# 用于 store_ordering 系列页面：把 warehouses.name + warehouses.code
# 渲染成 "name (code)"，name 缺失或与 code 等同时降级为 code。
# ---------------------------------------------------------------------------


class TestFmtWarehouse:
    def test_name_and_code_distinct(self):
        assert fmt_warehouse("徐汇店", "wh_003") == "徐汇店 (wh_003)"

    def test_name_missing_falls_back_to_code(self):
        """典型场景：master 里 warehouse 行被人工删 → LEFT JOIN NULL。"""
        assert fmt_warehouse(None, "wh_003") == "wh_003"

    def test_name_empty_string_falls_back_to_code(self):
        assert fmt_warehouse("", "wh_003") == "wh_003"

    def test_name_equals_code_drops_redundant_paren(self):
        """防止 "门店 (门店)" 这种噪声。"""
        assert fmt_warehouse("wh_003", "wh_003") == "wh_003"

    def test_code_missing_returns_empty(self):
        """防御：code 是订单必备字段；万一取不到，宁可空也别渲染 'None'。"""
        assert fmt_warehouse("徐汇店", None) == ""
        assert fmt_warehouse("徐汇店", "") == ""
