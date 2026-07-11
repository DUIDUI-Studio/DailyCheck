#!/usr/bin/env python3
"""
sync_revenue.py
===============
从 ~/Downloads/SQB/ 读取收钱吧交易日报 Excel，
提取每日营业额汇总数据，upsert 到 VPS DailyCheck 数据库的 daily_revenue 表。

用法
----
  python3 sync_revenue.py [--dry-run] [--date YYYY-MM-DD]
  --dry-run: 只打印，不写入 VPS
  --date:    指定日期（默认处理所有文件）
"""

import argparse
import re
import sys
from datetime import date
from pathlib import Path

SQB_DIR = Path.home() / "Downloads" / "SQB"
REPORT_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日的交易日报-交易\.xlsx$")


from typing import Optional, Dict, Any

def parse_report(path: Path) -> Optional[Dict[str, Any]]:
    """返回 {'date': 'YYYY-MM-DD', 'amount': float, 'count': int} 或 None"""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception:
        return None
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    # 找汇总行
    for row in rows:
        if row and str(row[0]).strip() == "汇总":
            # col2 = 交易金额, col1 = 交易笔数
            try:
                count = int(float(row[1])) if row[1] else 0
                amount = float(str(row[2]).replace(",", "")) if row[2] else 0.0
                return {"amount": amount, "count": count}
            except (ValueError, TypeError):
                pass
    return None


def upsert_vps(date_str: str, amount: float) -> dict:
    """SSH 到 VPS 执行 SQL upsert，返回结果"""
    import subprocess, json
    sql = (
        "INSERT INTO daily_revenue (date, amount, created_at) "
        "VALUES ('{d}', {a}, datetime('now')) "
        "ON CONFLICT(date) DO UPDATE SET amount=excluded.amount, created_at=excluded.created_at;".format(
            d=date_str, a=amount
        )
    )
    result = subprocess.run(
        [
            "ssh", "-p", "33756", "root@112.124.18.246",
            f"sqlite3 /root/DailyCheck/inventory.db \"{sql}\""
        ],
        capture_output=True, text=True,
    )
    return {"date": date_str, "amount": amount, "returncode": result.returncode, "stderr": result.stderr.strip()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date", help="YYYY-MM-DD，仅处理指定日期")
    args = parser.parse_args()

    files = sorted(SQB_DIR.glob("*交易日报-交易.xlsx"))
    results = []

    for f in files:
        m = REPORT_RE.match(f.name)
        if not m:
            continue
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        file_date = date(y, mo, d)
        date_str = file_date.isoformat()

        if args.date and date_str != args.date:
            continue

        data = parse_report(f)
        if not data:
            print(f"  [SKIP] {f.name} 无法解析")
            continue

        print(f"  {date_str}: 交易金额={data['amount']:.2f} 交易笔数={data['count']}")

        if args.dry_run:
            results.append({**data, "date": date_str, "action": "DRY RUN"})
        else:
            resp = upsert_vps(date_str, data["amount"])
            results.append({**data, "date": date_str, "action": "UPSERTED" if resp["returncode"] == 0 else f"ERROR: {resp['stderr']}"})

    # 汇总
    total = sum(r["amount"] for r in results)
    print(f"\n=== 共处理 {len(results)} 天，合计营业额: ¥{total:.2f} ===")
    for r in results:
        print(f"  {r['date']}: ¥{r['amount']:.2f}  ({r.get('count',0)}笔) [{r['action']}]")

    if not args.dry_run and results:
        # 打印 VPS 最新状态
        import subprocess
        out = subprocess.run(
            ["ssh", "-p", "33756", "root@112.124.18.246",
             "sqlite3 /root/DailyCheck/inventory.db 'SELECT date, amount FROM daily_revenue ORDER BY date;'"],
            capture_output=True, text=True,
        )
        print("\n=== VPS daily_revenue 当前数据 ===")
        print(out.stdout or "(无数据)")


if __name__ == "__main__":
    main()
