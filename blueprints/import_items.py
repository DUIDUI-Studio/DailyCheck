"""Web 端品项批量导入:上传 → 预览 → 确认写入。"""
from __future__ import annotations

import io
from typing import IO


def parse_xlsx(file_stream: IO[bytes]) -> dict:
    """解析 xlsx 文件为分组预览数据。

    期望格式:Sheet1,跳过前 2 行(标题 + 表头),第 3 行起为数据。
    分类列首行有值,后续空 → 沿用上一行(向下合并)。
    列序:分类(1)|物料名称(2)|SKU(3)|规格(4)|单价/元(5)|
          现有库存(6)|单位(7)|隐藏栏/盘点单位单价(8)|库存金额(9)

    Returns:
        {
            "groups_order": ["常温物料", ...],
            "groups_rows": {"常温物料": [{"row": 3, "name": "...", "spec": "...",
                                          "unit_cost": float, "unit": "str"}, ...]},
            "problems": ["行 N: 缺少分类", ...]
        }

    注意:本函数不写库,不读 session,纯函数。
    """
    import openpyxl

    wb = openpyxl.load_workbook(file_stream, data_only=True)
    ws = wb["Sheet1"]
    groups_order: list[str] = []
    groups_rows: dict[str, list[dict]] = {}
    problems: list[str] = []
    prev_cat: str | None = None

    for r in range(3, ws.max_row + 1):
        name = ws.cell(r, 2).value
        if not name:
            # 合计行 / 空行 → 跳过
            continue
        cat = ws.cell(r, 1).value or prev_cat
        if cat is None:
            problems.append(f"行 {r}: 缺少分类")
            continue
        prev_cat = cat
        if cat not in groups_rows:
            groups_order.append(cat)
            groups_rows[cat] = []
        unit_cost_raw = ws.cell(r, 8).value
        try:
            unit_cost = float(unit_cost_raw) if unit_cost_raw is not None else 0.0
        except (TypeError, ValueError):
            unit_cost = 0.0
            problems.append(f"行 {r}: 单价无法解析为数字 → 记为 0")
        unit_raw = ws.cell(r, 7).value
        unit = str(unit_raw).strip() if unit_raw is not None else ""
        if not unit:
            unit = "件"
        groups_rows[cat].append({
            "row": r,
            "name": str(name).strip(),
            "spec": ws.cell(r, 4).value,
            "unit_cost": unit_cost,
            "unit": unit,
        })

    return {
        "groups_order": groups_order,
        "groups_rows": groups_rows,
        "problems": problems,
    }