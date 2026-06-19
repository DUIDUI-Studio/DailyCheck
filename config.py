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
