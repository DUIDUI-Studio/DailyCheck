"""Read committed DC shipment batches and export the store-order workbook."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal, DecimalException
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HEADERS = (
    "序号", "来源单号", "订货单号", "序号", "门店编码", "品牌", "门店名称", "收货人",
    "收件人电话", "收货地址", "品项名称", "品项编码", "规格", "订货单位", "订货数量",
    "期望到货日期", "是否加急", "备注",
)
COLUMN_WIDTHS = (
    6.5, 20, 20, 7.832, 16.5, 16.5, 22.664, 7.832, 12.5, 52.5,
    20, 10.164, 10.164, 10.332, 10.5, 16.332, 11.332, 11.664,
)

_EXPORTABLE_BATCHES = """
    SELECT d.id, d.delivery_no, d.shipped_at, so.order_no, so.store_warehouse_code,
           so.expected_delivery_date, so.note, store.name AS store_name
      FROM store_order_deliveries d
      JOIN store_orders so ON so.id = d.order_id
      JOIN warehouses store ON store.code = so.store_warehouse_code
      JOIN warehouses dc ON dc.code = d.warehouse_code
     WHERE so.id = :order_id AND d.warehouse_code = :dc_code
       AND so.flow_type = 'store_order' AND so.approved_at IS NOT NULL
       AND dc.warehouse_type = 'distribution_center'
       AND d.id IN (
           SELECT di.delivery_id FROM store_order_delivery_items di
             JOIN store_order_items soi ON soi.id = di.order_item_id
            WHERE soi.order_id = :order_id AND di.warehouse_code = :dc_code
       )
"""


def exportable_batch_ids(conn: sqlite3.Connection, order_id: int, dc_code: str) -> set[int]:
    rows = conn.execute(_EXPORTABLE_BATCHES, {'order_id': order_id, 'dc_code': dc_code})
    return {row['id'] for row in rows}


def get_batch(
    conn: sqlite3.Connection, order_id: int, dc_code: str, batch_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        _EXPORTABLE_BATCHES + " AND d.id = :batch_id",
        {'order_id': order_id, 'dc_code': dc_code, 'batch_id': batch_id},
    ).fetchone()


def get_batch_lines(
    conn: sqlite3.Connection, batch_id: int, order_id: int, dc_code: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT di.quantity, soi.unit, soi.product_snapshot,
                  ci.name, ci.canonical_sku
             FROM store_order_delivery_items di
             JOIN store_order_items soi ON soi.id = di.order_item_id
             JOIN canonical_items ci ON ci.id = soi.canonical_id
            WHERE di.delivery_id = ? AND soi.order_id = ? AND di.warehouse_code = ?
            ORDER BY soi.id""",
        (batch_id, order_id, dc_code),
    ).fetchall()


def shipment_profile(path: str | None, dc_code: str) -> dict:
    if not path:
        return {}
    profiles = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(profiles, dict):
        raise ValueError('Invalid shipment export profiles')
    profile = profiles.get(dc_code, {})
    if not isinstance(profile, dict):
        raise ValueError('Invalid DC shipment export profile')
    for key in ('stores', 'items'):
        entries = profile.get(key, {})
        if not isinstance(entries, dict) or any(not isinstance(v, dict) for v in entries.values()):
            raise ValueError(f'Invalid shipment export {key}')
        for entry in entries.values():
            if any(value is not None and not isinstance(value, str) for value in entry.values()):
                raise ValueError(f'Shipment export {key} fields must be text')
    return profile


def _export_quantity(line: sqlite3.Row, item: dict) -> tuple[str, Decimal]:
    unit = line['unit']
    quantity = Decimal(str(line['quantity']))
    export_unit = item.get('unit') or unit
    if export_unit == unit:
        return unit, quantity
    raw_factor = item.get('base_units_per_order_unit')
    if item.get('base_unit') != unit or raw_factor is None:
        raise ValueError('Shipment export unit conversion is missing or has a different base unit')
    try:
        factor = Decimal(raw_factor)
        if not factor.is_finite() or factor <= 0:
            raise ValueError('Shipment export unit conversion must be positive and finite')
        converted = quantity / factor
    except DecimalException as exc:
        raise ValueError('Invalid shipment export unit conversion') from exc
    if not Decimal('2.2250738585072e-308') <= converted <= Decimal('9.99999999999999e307'):
        raise ValueError('Shipment export quantity is outside the Excel numeric range')
    return export_unit, converted


def make_workbook(batch: sqlite3.Row, lines: list[sqlite3.Row], profile: dict) -> tuple[BytesIO, bool]:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '门店订货单模板'
    sheet.append(HEADERS)
    sheet.row_dimensions[1].height = 16
    thin = Side(style='thin', color='000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    centered = Alignment(horizontal='center', vertical='center', wrap_text=True)
    wrapped = Alignment(vertical='center', wrap_text=True)
    header_font = Font(name='宋体', size=12, bold=True)
    body_font = Font(name='宋体', size=11)
    larger_body_font = Font(name='宋体', size=12)
    item_font = Font(name='Tahoma', size=12)
    unit_font = Font(name='微软雅黑', size=12)
    for idx, width in enumerate(COLUMN_WIDTHS, 1):
        sheet.column_dimensions[get_column_letter(idx)].width = width
        cell = sheet.cell(1, idx)
        cell.font = header_font
        cell.alignment = centered
        cell.border = border
        cell.fill = PatternFill('solid', fgColor='FFFFFF00' if idx == 6 else 'FFC0C0C0')

    store = profile.get('stores', {}).get(batch['store_warehouse_code'], {})
    items = profile.get('items', {})
    expected = batch['expected_delivery_date']
    complete = all(store.get(field) for field in ('code', 'brand', 'recipient', 'phone', 'address'))
    for number, line in enumerate(lines, 1):
        product = json.loads(line['product_snapshot']) if line['product_snapshot'] else {}
        sku = product.get('canonical_sku') or line['canonical_sku']
        item = items.get(sku, {})
        complete = complete and bool(item.get('code') and item.get('spec'))
        unit, quantity = _export_quantity(line, item)
        values = (
            number, batch['delivery_no'], batch['order_no'], number,
            store.get('code') or batch['store_warehouse_code'], store.get('brand'),
            store.get('name') or batch['store_name'], store.get('recipient'), store.get('phone'),
            store.get('address'), item.get('name') or product.get('name') or line['name'],
            item.get('code') or sku, item.get('spec'), unit, quantity,
            date.fromisoformat(expected) if expected else None, None, batch['note'],
        )
        row_number = number + 1
        sheet.row_dimensions[row_number].height = 32
        for col, value in enumerate(values, 1):
            if isinstance(value, str):
                value = ILLEGAL_CHARACTERS_RE.sub('', value)
            cell = sheet.cell(row_number, col, value)
            if isinstance(value, str):
                cell.data_type = 's'
            cell.font = larger_body_font if col in (5, 6, 15) else body_font
            cell.alignment = wrapped
            if 11 <= col <= 14:
                cell.font = unit_font if col == 14 else item_font
                cell.border = border
                cell.alignment = centered
            if col == 16 and expected:
                cell.number_format = 'yyyy-mm-dd'

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output, complete
