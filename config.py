"""Central configuration. Read by app factory and blueprints.

Multi-warehouse model: one master.db (users/warehouses/permissions) +
one SQLite file per warehouse under db/warehouses/.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "db"
MASTER_DB = DB_DIR / "master.db"
WAREHOUSE_DB_DIR = DB_DIR / "warehouses"

SECRET_KEY = os.environ.get("DAILYCHECK_SECRET_KEY")
if not SECRET_KEY:
    # Dev fallback; production must set the env var.
    SECRET_KEY = "dev-key-change-me"

# Each warehouse ships with the same fixed categories.
# Updated 2026-06-19: replaced 4-category set with the 9-category set used
# by the 新世界店 (wh_002) item master. Existing wh_001 still has the legacy
# 4 categories in its database (init_warehouse_db only seeds missing ones)
# — its single item '22 工具' is therefore intact.
FIXED_CATEGORIES = (
    "包材",
    "辅料",
    "调味酱",
    "调味酱 分",
    "风味奶浆",
    "乳制品",
    "生产消耗品",
    "生产工具",
    "冰激凌成品",
)

# Role rank for require_role().
ROLE_RANK = {"staff": 1, "manager": 2, "admin": 3}

# ─────────────────────────────────────────────────────────────────────
# Canonical item policy switches (Q1-Q4 + Q7).
#
# Spec: docs/2026-10-03-canonical-item-design.md §1.5, §1.6, §1.8, §7.7.
# M1 scope: the switches below are Eric's v4 decisions.
# Q6 (selling_price ownership) remains deferred per §1.5 — defaults to
# "storefront_autonomous" until Eric retunes after the M1 observation
# window.
# ─────────────────────────────────────────────────────────────────────
CANONICAL_POLICY = {
    # Q1 — 门店自建新主数据项
    "q1_storefront_new_item": "deny",            # Eric：「门店不能自建全新品项」(§1.5)
    # Q1=deny 的强制配套：按品类放行的紧急通道（§1.6.1 / §1.6.2）。
    # 存 category_code（与 canonical_categories.code 同源），不是中文名。
    # 空元组 = 无白名单，门店在任何品类下都不能自建（最严模式）。
    "storefront_new_item_whitelist": (),

    # Q2 — 门店自治字段数量
    "q2_storefront_field_count": "open",         # Eric：「允许门店自定义字段」(§2.7)

    # Q3 — 主数据删除
    "q3_canonical_delete": "deactivate_only",    # 「只允许停用，禁止物理删除」(§1.5, §2.5)

    # Q4 — 扇出冲突策略
    "q4_fanout_conflict": "freeze",              # 「冻结，然后人工处理」(§1.5, §3.5)

    # Q6 — 售价/采购价归属（§1.5 仍未拍板，默认 storefront_autonomous）
    "q6_price_ownership": "storefront_autonomous",

    # Q7 — 库存数据零丢失（§7.7 — Eric 拍板的最高优先级项）
    "q7_inventory_protection": "strict",
    # §7.7 措施⑦：禁止对真实库写入；副本走 /tmp/dc_dryrun/{ts}/
    # 仅在人工预演时由操作员临时翻成 True，不允许在生产路径里设。
    "allow_real_db_write": False,
}


# Backups destination for Q7 措施① (shutil.copy2 before any write that
# touches items). Layout: backups/warehouses/{code}-{YYYYMMDD-HHMMSS}-{tag}.db
BACKUP_WAREHOUSE_DIR = BASE_DIR / "backups" / "warehouses"
# Where Q7 措施⑦ routes dry-run copies so they never reach db/warehouses/.
DRYRUN_COPY_DIR = Path("/tmp/dc_dryrun")
