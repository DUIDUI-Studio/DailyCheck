"""Pure functions and policy constants for the canonical-items feature.

Spec: docs/2026-10-03-canonical-item-design.md
     §1.3 (canonical_synced_json scope)
     §1.5 (Q6 price ownership branching)
     §1.6 (Q1=deny + whitelist + three-way-out)
     §1.7 (9 规格品项拆分)
     §1.8 (unit family — aux_unit/aux_rate/gram_per_unit derivation)
     §2.4 (field permission matrix → CANONICAL_FIELD_POLICY)
     §2.5 (canonical_status state machine — Q3=deactivate_only)
     §3.1 (strong signal detection)
     §3.2 (claim / unclaim)
     §3.5 (fanout conflict judgment — Q4=freeze)
     §6.1.1 (two-phase canonical_sku write)
     §7.7  (Q7 inventory protection — seven guards)

The pure layer is the single source of truth for canonical-item policy.
Route handlers (blueprints/canonical.py) and the existing items blueprint
must call into this file instead of re-implementing constants or checks.

Conventions (carried over from blueprints/*_pure.py pattern):
- All functions take a sqlite3.Connection as the first argument, never
  touch flask.g, and never open connections themselves (except the
  private _open_warehouse helper for cross-warehouse iteration).
- No flask imports. tests/test_canonical_pure.py must run without an app.
- Business-field inventories live in three constants below:
      NEVER_TOUCH_COLUMNS, CANONICAL_FIELD_POLICY, STOREFRONT_OWNED_FIELDS
  No blueprint is allowed to keep a parallel whitelist — that is how Q7
  "no UPDATE on inventory columns" gets bypassed in code review.

Public surface (this file):

  Constants:
    NEVER_TOUCH_COLUMNS, CANONICAL_FIELD_POLICY, STOREFRONT_OWNED_FIELDS
    ALLOWED_TABLES, ALLOWED_WHERE_COLUMNS, CATEGORY_CODE_MAP

  T1 — policy switches:
    is_syncable_field(field) -> bool
    check_new_item_policy(name, canonical_id=None, category_code=None,
                          similar_canonicals=None) -> dict
    set_canonical_status(canonical_id, status) -> str
    resolve_conflict_policy(local_value, last_synced_value,
                            canonical_value, force=False) -> str

  T4 — Canonical items CRUD:
    next_canonical_sku(master_conn) -> str
    create_canonical_item(master_conn, *, name, unit, category_code=None,
                          gram_per_unit=0, aux_unit=None, aux_rate=0,
                          barcode=None, status='active',
                          created_from='rd_manual', created_by=None) -> dict
    update_canonical_item(master_conn, canonical_id, fields) -> dict
    list_canonical_items(master_conn, *, status=None, category_code=None,
                         limit=200, offset=0) -> list[dict]
    get_canonical_item_detail(master_conn, canonical_id) -> dict

  T5 — Categories:
    seed_canonical_categories(master_conn) -> list[dict]
    backfill_category_mappings(wh_conn) -> dict
    list_missing_category_mappings(master_conn, wh_code=None) -> list[dict]
    resolve_category_id(master_conn, wh_conn, canonical_category_code,
                        wh_category_name) -> int

  T6 — Strong signal detection:
    parse_local_sku(sku) -> dict
    normalize_name(name) -> str
    name_similarity(a, b) -> float
    detect_similar_items(rows_per_wh) -> list[dict]
    collect_all_items(master_conn) -> dict[str, list[dict]]

  T7 — Claim / Unclaim:
    claim_item(master_conn, wh_conn, *, warehouse_code, local_item_id,
               canonical_id, is_alias=False, submitted_by=None,
               local_keep_name=None, reason=None) -> dict
    unclaim_item(master_conn, wh_conn, *, warehouse_code, local_item_id,
                 reviewed_by=None, reason=None) -> dict
    submit_claim_request(master_conn, *, warehouse_code, local_item_id=None,
                         local_sku=None, local_name=None, canonical_id=None,
                         proposed_category_code=None, proposed_unit=None,
                         request_type='claim', reason=None,
                         submitted_by=None) -> int
    review_claim_request(master_conn, *, request_id, decision,
                         reviewed_by, review_note=None) -> dict

  T8 — Fanout:
    _build_category_id_map(wh_conn, canon_by_id, code_to_name) -> dict[int, int|None]
    apply_canonical_to_warehouse(wh_conn, canonical, last_snapshot,
                                 action='overwrite', force=False,
                                 category_id_map=None) -> dict
    fanout_canonical_items(master_conn, wh_db_map, *, canonical_ids,
                           warehouse_codes, action='overwrite',
                           force=False, dry_run=False, started_by=None,
                           summary=None) -> dict
    resolve_conflict(master_conn, *, conflict_id, decision,
                     reviewed_by, note=None) -> dict

  T9 — Cross-warehouse reads:
    collect_bindings(master_conn) -> dict[str, list[dict]]
    diff_summary(master_conn) -> dict
    list_unbound_storefront_items(master_conn) -> list[dict]
    find_orphan_bindings(master_conn) -> list[dict]
    list_store_exclusive_items(master_conn) -> list[dict]

  T22 — Bulk create:
    seed_default_canonical_items(master_conn) -> list[dict]
    bulk_create_canonical_items(master_conn, csv_path) -> dict

  T23 — Q7 inventory guards:
    build_update_sql(conn, table, updates, where_col, where_val,
                     set_allowlist) -> tuple[str, list]
    select_item_columns(conn) -> list[str]
    assert_no_never_touch(updates_dict) -> None
    backup_warehouse_db(db_path, tag) -> Path
    guard_real_db(path) -> None
    snapshot_inventory(conn) -> dict[int, Any]
    assert_inventory_unchanged(before, after) -> None
    assert_ids_stable(before_ids, after_ids) -> None
    assert_row_count_conserved(conn, table, before) -> None

  T25 — Canonical align CLI (issue #11):
    ALIGN_SCOPE_WAREHOUSES
    dry_run_report(master_conn, target_wh_codes=None) -> str
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
import uuid
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# Import policy switches and paths from config so a single file controls
# everything (no other file owns its own whitelist — see module docstring).
from config import (
    BACKUP_WAREHOUSE_DIR,
    BASE_DIR,
    CANONICAL_POLICY,
    DRYRUN_COPY_DIR,
    FIXED_CATEGORIES,  # noqa: F401  (re-exported below)
)

# ─────────────────────────────────────────────────────────────────────
#  Field permission matrix (Spec §2.4)
#
#  The triple below is the ONE source of truth. Any change to the field
#  permission matrix must happen here, and here only. Blueprints import
#  the constants and call is_syncable_field() — they do not have their
#  own whitelists.
#
#  Q6 note (Spec §1.5): when q6_price_ownership == "canonical_managed",
#  the two commented lines for "selling_price" and "unit_cost" are
#  inserted into CANONICAL_FIELD_POLICY, and the same two names are
#  removed from NEVER_TOUCH_COLUMNS / STOREFRONT_OWNED_FIELDS.
#  is_syncable_field() is the only place that reads CANONICAL_FIELD_POLICY,
#  so the rest of the codebase follows automatically.
# ─────────────────────────────────────────────────────────────────────

# 铁律：任何 action、任何分支（含 force）都不得出现在任何 UPDATE 的 SET 列表里。
# ⚠️ selling_price / unit_cost 是否在此列表内取决于 Q6（§1.5）。
NEVER_TOUCH_COLUMNS: tuple[str, ...] = (
    "id",
    "sku",
    "quantity",
    "safety_stock",
    "initial_quantity",
    "selling_price",
    "unit_cost",
    "category_id",
    # 本仓运营开关：DC 可订 / 单仓启停 —— 都不随主数据下发（P0-10）。
    "is_orderable",
    "is_active",
)

# 可下发字段：主数据字段名 -> (策略, 目标列名)
#   策略取值:
#     "overwritable"  → 可下发，写入目标列（且入 canonical_synced_json）
#     "mapping_only"  → 只改 categories.canonical_code，不下发到 items
#     "not_synced"    → 暂不下发（P0 明确不做）
CANONICAL_FIELD_POLICY: dict[str, tuple[str, str | None]] = {
    "name":           ("overwritable", "name"),
    "unit":           ("overwritable", "unit"),
    "gram_per_unit":  ("overwritable", "gram_per_unit"),
    "aux_unit":       ("overwritable", "aux_unit"),
    "aux_rate":       ("overwritable", "aux_rate"),
    "status":         ("overwritable", "__canonical_status__"),  # 写 items.canonical_status
    "category_code":  ("mapping_only", None),                     # 只改 categories.canonical_code
    "barcode":        ("not_synced",   None),                     # P0 不下发（§2.2）
    # ★ Q6=canonical_managed 时追加下面两行；Q6=storefront_autonomous 时不存在。
    # 见 §1.5 —— 这是 Q6 的唯一实现落点，别处不许再写 if。
    # "selling_price": ("overwritable", "selling_price"),
    # "unit_cost":     ("overwritable", "unit_cost"),
}

# 门店自治字段：Q2=open 拍板后即为最终集合。
# Q6=canonical_managed 时降为 4 项（售价/采购价升为「可下发」）。
STOREFRONT_OWNED_FIELDS: tuple[str, ...] = (
    "local_alias",        # = items.name (当 is_alias=1 时)
    "category_id",
    "selling_price",      # 门店售价；Q6=canonical_managed 时移出本集合
    "unit_cost",          # 门店采购价；Q6=canonical_managed 时移出本集合
    "safety_stock",
    "item_note",          # 门店备注
)


# Q7 §7.7.2 措施③ 第1层：SQL 生成器只允许 update 这几张表的 items 业务字段，
# 主键表与日志表不开放（防止工程师顺手打开 stock_movements 之类）。
ALLOWED_TABLES: tuple[str, ...] = ("items", "categories")
# WHERE 子句只允许用主键/业务键：写错/漏写都直接抛 TypeError，
# 这是 R5（漏 WHERE 全仓清空）的唯一防线（§7.7.2 措施③ 第1层）。
ALLOWED_WHERE_COLUMNS: tuple[str, ...] = ("id", "sku", "canonical_id")


# ─────────────────────────────────────────────────────────────────────
#  Domain exceptions (callers can import these to catch specific failures)
# ─────────────────────────────────────────────────────────────────────

class CanonicalPolicyError(ValueError):
    """Base class for policy-level refusals (Q1/Q3/Q4/Q7 violations)."""


class NewItemDenied(CanonicalPolicyError):
    """Q1=deny: 门店尝试新建一个主数据里没有、品类也不在白名单的品项。"""


class DeactivateOnlyViolation(CanonicalPolicyError):
    """Q3=deactivate_only: 试图物理删除（status='deleted'）主数据项。"""


class NeverTouchViolation(CanonicalPolicyError):
    """Q7 §7.7.2 措施③: 写入触及 NEVER_TOUCH_COLUMNS（库存/主键/门店自治字段）。"""


class UpdateSqlViolation(CanonicalPolicyError):
    """build_update_sql 在 SQL 生成阶段检测到的不合法调用。"""


class InventoryMutated(CanonicalPolicyError):
    """Q7 §7.7.2 措施③ 第3层: 写仓前后 (id → quantity) 快照不一致。"""


class RowCountChanged(CanonicalPolicyError):
    """Q7 §7.7.2 措施③ 第2层: 写仓后表行数变化。"""


class IdSetChanged(CanonicalPolicyError):
    """Q7 §7.7.2 措施⑤: items.id 集合在写仓前后不一致（会出现引用孤儿）。"""


class RealDatabaseForbidden(CanonicalPolicyError):
    """Q7 §7.7.2 措施⑦: 试图对真实仓库 db/warehouses/* 直接写入。"""


class BackupFailed(CanonicalPolicyError):
    """Q7 §7.7.2 措施①: shutil.copy2 备份失败 —— 整个操作必须中止。"""


# ─────────────────────────────────────────────────────────────────────
#  Helpers (private)
# ─────────────────────────────────────────────────────────────────────

def _now_str() -> str:
    """Section 6.2 时间格式：'YYYY-MM-DD HHMM:SS'，与 publish_recipe_pure.py:33 一致。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _backup_stamp() -> str:
    """Section 7.7.2 措施①：备份文件名时间戳格式 YYYYMMDD-HHMMSS。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ─────────────────────────────────────────────────────────────────────
#  T1 — Q1-Q4 / Q7 policy switches (Spec §1.5, §1.6, §2.4, §2.5, §3.5)
# ─────────────────────────────────────────────────────────────────────

def is_syncable_field(field: str) -> bool:
    """某字段是否可被主数据下发（Spec §1.3 / §2.4 铁律）。

    这是「字段是否进 canonical_synced_json」的唯一真相源：
      - CANONICAL_FIELD_POLICY 中存在且策略为 "overwritable" → True
      - 其余情况（mapping_only / not_synced / 不在表中） → False
    """
    entry = CANONICAL_FIELD_POLICY.get(field)
    return entry is not None and entry[0] == "overwritable"


def check_new_item_policy(
    name: str,
    canonical_id: int | None = None,
    category_code: str | None = None,
    similar_canonicals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Q1=deny 拦截 + 三条出路的判定（Spec §1.6.5 判定表）。

    参数:
        name: 门店拟新建的品项名。仅用于日志/回显，不参与判定。
        canonical_id:
            - None   → 门店**新建**品项
            - 非 None → 门店**编辑**已绑定的品项（Q2=open 下仍允许）
        category_code:
            - 本仓 categories.canonical_code（与 canonical_categories.code 同源）
            - 若 None：保守地按「不在白名单」处理
        similar_canonicals:
            - 由 T6 强信号检测给出的候选主数据项列表（T6 实现后接入）
            - 每项形如 {"canonical_id": int, "name": str, "score": float}
            - 若 None：按「无相似项」处理

    返回:
        {
          "allowed":                bool,    # 是否允许本次操作
          "requires_store_exclusive": bool,   # 若允许，是否标记 is_store_exclusive=1
          "suggestions":             list,    # 候选主数据项（同入参列表）
          "reason":                  str,     # 给门店看的人话解释（含三条出路文案）
          "next_action":             str,     # "edit"/"create"/"choose"/"request_new"
        }

    next_action 取值:
        "edit"          → 已有 canonical_id，仅做编辑校验（Q2=open 允许）
        "create"        → 允许新建（白名单品类 / 编辑已纳管行）
        "choose"        → 拒绝，让门店从候选主数据项里选用已有
        "request_new"   → 拒绝，让门店走「提交新增申请」通道
    """
    suggestions = list(similar_canonicals or [])

    # 编辑已纳管品项（canonical_id 非空）— Q2=open 下永远允许
    if canonical_id is not None:
        return {
            "allowed": True,
            "requires_store_exclusive": False,
            "suggestions": [],
            "reason": (
                f"编辑已纳管主数据项 id={canonical_id}，"
                f"自治字段（售价/安全库存/分类/本地别名）可自由编辑。"
            ),
            "next_action": "edit",
        }

    # Q1=deny 默认：不允许新建。除非品类在白名单内。
    whitelist: tuple[str, ...] = CANONICAL_POLICY.get(
        "storefront_new_item_whitelist", ()
    )
    in_whitelist = category_code is not None and category_code in whitelist

    # 路径 ①: 白名单品类 + 无相似主数据 → 允许新建，标记 is_store_exclusive=1
    if in_whitelist and not suggestions:
        return {
            "allowed": True,
            "requires_store_exclusive": True,
            "suggestions": [],
            "reason": (
                f"品类 {category_code!r} 在门店自建白名单内，允许新建，"
                f"但该品项会被标记为门店专属（is_store_exclusive=1），"
                f"总部可在「门店专属品项巡检」中决定收编或豁免。"
            ),
            "next_action": "create",
        }

    # 路径 ②: 白名单品类 + 有相似主数据 → 允许新建但先建议选用
    if in_whitelist and suggestions:
        sugg_str = "、".join(s.get("name", "?") for s in suggestions[:3])
        return {
            "allowed": True,
            "requires_store_exclusive": True,
            "suggestions": suggestions,
            "reason": (
                f"品类 {category_code!r} 在白名单内，但主数据里已有相似项：{sugg_str}。"
                f"如非规格差异，建议优先选用已有项（避免重复主数据）。"
            ),
            "next_action": "create",   # 仍允许，仅提示
        }

    # 路径 ③: 非白名单 + 有相似主数据 → 拒绝，引导选用已有
    if suggestions:
        sugg_str = "、".join(s.get("name", "?") for s in suggestions[:3])
        return {
            "allowed": False,
            "requires_store_exclusive": False,
            "suggestions": suggestions,
            "reason": (
                f"主数据中已有相近品项（{sugg_str}），请选用已有品项。"
                f"如确认是新规格，请走「提交新增申请」由总部审批。"
            ),
            "next_action": "choose",
        }

    # 路径 ④: 非白名单 + 无相似主数据 → 拒绝，走申请单
    return {
        "allowed": False,
        "requires_store_exclusive": False,
        "suggestions": [],
        "reason": (
            f"Q1=deny 且品类 {category_code!r} 不在门店自建白名单内。"
            f"如确认需要新建，请提交新增申请，由总部在主数据页审批后即可选用。"
        ),
        "next_action": "request_new",
    }


def set_canonical_status(canonical_id: int, status: str) -> str:
    """Q3=deactivate_only 守卫（Spec §2.5）。

    只接受 'active' / 'disabled'，**拒绝 'deleted'**。
    返回规范化后的 status；调用方负责执行 UPDATE。

    为什么不暴露 `delete_canonical_item()`：
        Spec §2.5 末段写明——把"禁止删除"做成「签名上不存在」而不是
        「函数内部 raise」，是最强的静态保证。本文件不提供删除函数。
    """
    if canonical_id is None:
        raise DeactivateOnlyViolation("canonical_id 不能为空")

    allowed = ("active", "disabled")
    if status not in allowed:
        if status == "deleted":
            raise DeactivateOnlyViolation(
                "Q3=deactivate_only: 不允许物理删除主数据项。"
                "请改用 'disabled' 停用。若必须删除，请走 §7.10 流程"
                "在 master 侧手工 SQL 删除（需 Eric 单独授权）。"
            )
        raise DeactivateOnlyViolation(
            f"非法 status: {status!r}，仅接受 {allowed}"
        )
    return status


def resolve_conflict_policy(
    local_value: Any,
    last_synced_value: Any,
    canonical_value: Any,
    force: bool = False,
) -> str:
    """Q4=freeze 判定（Spec §3.5 判定顺序）。

    返回:
        "allowed"   → 安全可下发（无冲突）
        "frozen"    → 检测到冲突，按 Q4=freeze 冻结字段（需人工处理）
        "force_only"→ 检测到冲突，仅在 force=True 时才允许下发

    判定矩阵（与 §3.5 一一对应）:
        local == canonical                                   → 'allowed'  (1)
        local == last_synced                                 → 'allowed'  (2)
        local ≠ last_synced AND local ≠ canonical            → 'frozen'   (3)
            with force=True                                  → 'force_only' (3+)
    """
    # 短路：local 与 canonical 完全一致（包括都未变化/都已变化到同一值）
    if local_value == canonical_value:
        return "allowed"
    # 门店自上次下发以来未变动 → 安全覆盖
    if local_value == last_synced_value:
        return "allowed"
    # 门店已偏离 → 冲突
    if force:
        return "force_only"
    return "frozen"


# ─────────────────────────────────────────────────────────────────────
#  T23 — Q7 库存保护守卫层（Spec §7.7.2 七条措施）
# ─────────────────────────────────────────────────────────────────────

def assert_no_never_touch(updates_dict: dict[str, Any]) -> None:
    """Q7 §7.7.2 措施③ 第1层：UPDATE 的 SET 列不得触及 NEVER_TOUCH_COLUMNS。

    双向断言：
        正向 — updates_dict 的键不能与 NEVER_TOUCH_COLUMNS 有交集
        反向 — updates_dict 的键必须都在 CANONICAL_FIELD_POLICY 中
                （防止「漏白名单」绕过 — 例如不小心把 selling_price 加进
                CANONICAL_FIELD_POLICY 但忘了从 NEVER_TOUCH 移除）

    Raises:
        NeverTouchViolation: 触及门禁列
        AssertionError:      updates_dict 的键既不在可下发集也不在门禁集
                            （签名漂移 — 应作为 code review 强制拦下）
    """
    bad = set(updates_dict.keys()) & set(NEVER_TOUCH_COLUMNS)
    if bad:
        raise NeverTouchViolation(
            f"试图写入门禁字段 {sorted(bad)}; 这些字段在 NEVER_TOUCH_COLUMNS 中，"
            f"Q7 守卫禁止任何路径（认领/扇出/force/导入）写入。"
        )
    # 反向：必须可下发
    for f in updates_dict:
        assert is_syncable_field(f), (
            f"{f!r} 不在 CANONICAL_FIELD_POLICY 中却被写入 SET；"
            f"若要新增可下发字段，必须同步修改 CANONICAL_FIELD_POLICY "
            f"并确保 is_syncable_field() 返回 True。"
        )


def build_update_sql(
    conn: sqlite3.Connection,
    table: str,
    updates: dict[str, Any],
    where_col: str,
    where_val: Any,
    set_allowlist: tuple[str, ...],
) -> tuple[str, list[Any]]:
    """统一 UPDATE SQL 出口（Q7 §7.7.2 措施③ 第1层 + R5 防线）。

    参数:
        conn:  目标连接（用于 assert_no_never_touch，无其他副作用）
        table: 仅接受 ALLOWED_TABLES 内的表名
        updates: 待写入列的 dict
        where_col: 必填位置参数 — 仅接受 ALLOWED_WHERE_COLUMNS。
                   **不可能漏 WHERE**：缺值直接 TypeError（位置参数缺省）
        where_val: WHERE 列的对应值
        set_allowlist: 调用方声明允许写的列；与 NEVER_TOUCH_COLUMNS 求交集必须为空，
                       且 updates_dict 的键必须 ⊆ set_allowlist

    返回:
        (sql, params): 参数化 SQL 与绑定值；调用方自行 conn.execute()

    Raises:
        UpdateSqlViolation / NeverTouchViolation / TypeError
    """
    del conn  # 接口保留 conn 是为了将来加 conn 相关的校验（schema 一致性等）

    # 1. updates 非空
    if not updates:
        raise UpdateSqlViolation("updates 必须为非空 dict")

    # 2. 表名白名单
    if table not in ALLOWED_TABLES:
        raise UpdateSqlViolation(
            f"table {table!r} 不在 ALLOWED_TABLES {ALLOWED_TABLES} 中；"
            f"若需新表，必须修改 ALLOWED_TABLES 并在 code review 显式说明。"
        )

    # 3. WHERE 列白名单（这是 R5 「漏 WHERE 全仓清空」的唯一防线）
    if where_col not in ALLOWED_WHERE_COLUMNS:
        raise UpdateSqlViolation(
            f"where_col {where_col!r} 不在 ALLOWED_WHERE_COLUMNS 中；"
            f"仅接受 {ALLOWED_WHERE_COLUMNS}。漏 WHERE / 错列都会直接抛错。"
        )

    # 4. set_allowlist 必须与 NEVER_TOUCH_COLUMNS 不相交
    overlap = set(set_allowlist) & set(NEVER_TOUCH_COLUMNS)
    if overlap:
        raise UpdateSqlViolation(
            f"set_allowlist 与 NEVER_TOUCH_COLUMNS 求交集非空: {sorted(overlap)};"
            f"set_allowlist 不应包含门禁字段。"
        )

    # 5. updates_dict 的键 ⊆ set_allowlist
    out_of_allowlist = set(updates.keys()) - set(set_allowlist)
    if out_of_allowlist:
        raise UpdateSqlViolation(
            f"updates 含未在 set_allowlist 的列: {sorted(out_of_allowlist)};"
            f"set_allowlist={set_allowlist}。"
        )

    # 6. Q7 守卫：禁止写门禁字段（即便 set_allowlist 误传也会再拦一次）
    assert_no_never_touch(updates)

    # 7. 生成 SQL（参数化，禁止 f-string 拼值）
    cols = list(updates.keys())
    set_clause = ", ".join(f"{c}=?" for c in cols)
    sql = f"UPDATE {table} SET {set_clause} WHERE {where_col}=?"
    params: list[Any] = [updates[c] for c in cols] + [where_val]
    return sql, params


def select_item_columns(conn: sqlite3.Connection) -> list[str]:
    """跨仓动态 SELECT 列名（Q7 §7.7.2 措施④ 阻断 R4）。

    `initial_quantity` 仅 wh_001 存在，其他仓硬 SELECT 会抛 `no such column`。
    按 `PRAGMA table_info(items)` 动态读取实际列名，让跨仓遍历通用化。

    返回:
        列名列表（保留 PRAGMA 顺序）；调用方自行 join 成 SELECT 列表。
    """
    return [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]


def snapshot_inventory(conn: sqlite3.Connection) -> dict[int, Any]:
    """操作前 (id → quantity) 快照（Q7 §7.7.2 措施③ 第3层）。

    ⚠️ 关键设计点：只 SELECT id, quantity 两列，**绝不 SELECT initial_quantity**
    （R4：wh_002 没有这列会直接抛异常）。

    ⚠️ 不对 quantity 做任何 round() —— 这是 R3b 的核心防线：
       `assert_inventory_unchanged` 必须用原始值逐行全等比较，
       任何 round() 都会把浮点累加误差「正常化」而放过漂移。
    """
    return {
        r["id"]: r["quantity"]
        for r in conn.execute("SELECT id, quantity FROM items")
    }


def assert_inventory_unchanged(
    before: dict[int, Any],
    after: dict[int, Any],
) -> None:
    """逐行 `==` 校验 quantity（Q7 §7.7.2 措施③ 第3层）。

    字典比较天然兼容 INTEGER/REAL：
        Python `49.5 == 49.5` 为 True；`1 == 1.0` 为 True；
        但 `92.98999999999995 == 92.99` 为 False（这正是我们要抓的 R3b）。
    """
    if before == after:
        return

    diffs: list[str] = []
    for item_id, qty in before.items():
        if item_id not in after:
            diffs.append(f"id={item_id} 消失（原 quantity={qty!r}）")
        elif after[item_id] != qty:
            diffs.append(
                f"id={item_id}: quantity {qty!r} -> {after[item_id]!r}"
            )
    for item_id in after:
        if item_id not in before:
            diffs.append(f"id={item_id} 新增（quantity={after[item_id]!r}）")

    raise InventoryMutated("；".join(diffs[:20]))


def assert_ids_stable(before_ids: set[int], after_ids: set[int]) -> None:
    """主键不变式（Q7 §7.7.2 措施⑤）。

    `items.id` 是 9 张引用表的连接键，id 变了 = 数千条历史单据变孤儿。
    认领/扇出天然不会改 id（§3.2 铁律），本断言防的是「将来有人觉得
    只 UPDATE 太麻烦改成删了重建」的危险重构。
    """
    if before_ids == after_ids:
        return
    missing = sorted(before_ids - after_ids)
    added = sorted(after_ids - before_ids)
    parts: list[str] = []
    if missing:
        parts.append(f"消失 {len(missing)} 个 id（前 5: {missing[:5]}）")
    if added:
        parts.append(f"新增 {len(added)} 个 id（前 5: {added[:5]}）")
    raise IdSetChanged("；".join(parts))


def assert_row_count_conserved(
    conn: sqlite3.Connection,
    table: str,
    before: int,
) -> None:
    """行数守恒校验（Q7 §7.7.2 措施③ 第2层）。

    在写仓事务的 **COMMIT 之前** 调一次：异常触发时 ROLLBACK 自动撤销。
    """
    if table not in ALLOWED_TABLES:
        raise UpdateSqlViolation(
            f"assert_row_count_conserved: table {table!r} 不在 ALLOWED_TABLES 中"
        )
    after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if after != before:
        raise RowCountChanged(
            f"{table} 行数从 {before} 变成 {after}，已触发 ROLLBACK。"
            f"任何写仓事务都不要在这条断言后继续写。"
        )


def backup_warehouse_db(db_path: str | Path, tag: str) -> Path:
    """复制仓库到备份目录（Q7 §7.7.2 措施①）。

    命名规则: {code}-{YYYYMMDD-HHMMSS}-{tag}.db
    存放位置: {BASE_DIR}/backups/warehouses/

    四条硬要求（§7.7.2 措施① 末段）：
        1. **失败即中止** — shutil.copy2 抛异常直接传播，绝不 try/except 吞掉
        2. 使用 copy2（保留 mtime，便于按时间排序清理）
        3. 复制而非 VACUUM INTO（前者对正在被打开的库安全）
        4. **不备份 master.db**（主数据可重建，量小且有 event 表可回放）
           但扇出事件必须记录备份路径到 canonical_publish_events（见 §2.1）

    参数:
        db_path: 真实仓库 db 路径；由调用方先经 guard_real_db 放行
        tag:     描述备份目的的 tag（如 "pre-fanout"、"pre-claim"）

    Returns:
        备份文件路径

    Raises:
        BackupFailed: 复制失败时抛（包装 shutil 的异常）
    """
    src = Path(db_path)
    if not src.exists():
        raise BackupFailed(f"源库不存在: {src}")
    BACKUP_WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_WAREHOUSE_DIR / f"{src.stem}-{_backup_stamp()}-{tag}.db"
    try:
        shutil.copy2(src, dest)  # 失败即抛，符合「绝不吞异常」
    except OSError as exc:
        raise BackupFailed(
            f"备份失败（已中止整个操作）: src={src} dest={dest} err={exc}"
        ) from exc
    return dest


def guard_real_db(path: str | Path) -> None:
    """真实库保护（Q7 §7.7.2 措施⑦）。

    默认拒绝指向 db/warehouses/ 的写入路径；副本统一放 /tmp/dc_dryrun/{ts}/。
    把这条做成代码而不是文档，因为文档约束在赶工期时必然被绕过。

    仅当 CANONICAL_POLICY["allow_real_db_write"] == True 时放行
    （默认 False；临时翻成 True 仅用于人工预演，**不允许在生产路径**）。

    参数:
        path: 拟操作的 db 文件路径
    """
    if CANONICAL_POLICY.get("allow_real_db_write", False):
        return

    p = Path(path).resolve()
    real = (BASE_DIR / "db" / "warehouses").resolve()

    # 注意：用 parent 比较而不是完整路径，确保不论传入相对/绝对都能拦截
    if p.parent == real:
        raise RealDatabaseForbidden(
            f"禁止对真实仓库 {p} 写入。预演请先复制副本到 "
            f"{DRYRUN_COPY_DIR}/{{ts}}/（每个 dry-run 用独立 ts 子目录隔离）。"
        )


# ─────────────────────────────────────────────────────────────────────
#  CATEGORY_CODE_MAP — re-exported here for canonical pure layer access.
#  Stored in config (single source of truth, Spec §6.1).
# ─────────────────────────────────────────────────────────────────────
from config import CATEGORY_CODE_MAP  # noqa: E402,F401  (deliberate re-export)

# ─────────────────────────────────────────────────────────────────────
#  T4 — Canonical items CRUD + next_canonical_sku (Spec §6.1.1 two-phase)
# ─────────────────────────────────────────────────────────────────────

# 9 个规格品项（Spec §1.7）的桶装 / 散装对 —— 用于 §1.7.1 单位族规则
# 与 §1.7.4 WP0215 桶装不创建的特例处理。
SPECIAL_BULK_SKUS: frozenset[str] = frozenset({
    "WP0129", "WP0212", "WP0213", "WP0215", "WP0216",
    "WP0217", "WP0218", "WP0219", "WP0223",
})
SPECIAL_BULK_NO_BARREL_SKUS: frozenset[str] = frozenset({"WP0215"})


def next_canonical_sku(master_conn: sqlite3.Connection) -> str:
    """生成下一个 canonical_sku —— 格式 IC-%06d（Spec §6.1.1）。

    两阶段写入:先占位插入 IC-PENDING-<uuid4hex8>,再回填正式 SKU。
    这样并发 INSERT 不会撞 UNIQUE 约束;失败整体回滚（§6.1.1）。

    这里是"第一步"专用;create_canonical_item() 自己处理两步。
    本函数仅在外部需要"先拿一个唯一占位"时调用（实际场景很少）。
    """
    return "IC-PENDING-" + uuid.uuid4().hex[:8]


def create_canonical_item(
    master_conn: sqlite3.Connection,
    *,
    name: str,
    unit: str,
    category_code: str | None = None,
    gram_per_unit: float = 0,
    aux_unit: str | None = None,
    aux_rate: float = 0,
    barcode: str | None = None,
    status: str = "active",
    created_from: str = "rd_manual",
    created_by: int | None = None,
) -> dict:
    """T4 —— 在 master 侧创建 canonical_items 行。

    实现 Spec §6.1.1 的两阶段写入:
      1) 占位 INSERT (canonical_sku='IC-PENDING-<uuid8>') —— 拿 id
      2) UPDATE 回填 canonical_sku = 'IC-%06d' % id
      失败整体 ROLLBACK。

    派生关系:gram_per_unit = aux_rate if aux_unit=='克' else 0 (§1.8.2)。
    """
    master_conn.row_factory = sqlite3.Row

    # §1.8.2 派生关系:若 aux_unit!='克'则强制 gram_per_unit=0
    if aux_unit != "克":
        gram_per_unit = 0.0

    status = set_canonical_status(0, status)  # 仅做合法性校验

    placeholder = next_canonical_sku(master_conn)
    ts = _now_str()
    try:
        cur = master_conn.execute(
            """INSERT INTO canonical_items
               (canonical_sku, name, category_code, unit, gram_per_unit,
                aux_unit, aux_rate, barcode, status, created_from,
                created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (placeholder, name, category_code, unit, gram_per_unit,
             aux_unit, aux_rate, barcode, status, created_from,
             created_by, ts, ts),
        )
        new_id = int(cur.lastrowid)
        final_sku = f"IC-{new_id:06d}"
        master_conn.execute(
            "UPDATE canonical_items SET canonical_sku=? WHERE id=?",
            (final_sku, new_id),
        )
        master_conn.commit()
    except Exception:
        master_conn.rollback()
        raise

    return {
        "id": new_id,
        "canonical_sku": final_sku,
        "name": name,
        "category_code": category_code,
        "unit": unit,
        "gram_per_unit": gram_per_unit,
        "aux_unit": aux_unit,
        "aux_rate": aux_rate,
        "barcode": barcode,
        "status": status,
        "created_from": created_from,
        "created_by": created_by,
        "created_at": ts,
        "updated_at": ts,
    }


def update_canonical_item(
    master_conn: sqlite3.Connection,
    canonical_id: int,
    fields: dict[str, Any],
) -> dict:
    """T4 —— 更新 canonical_items。

    允许的字段:name / unit / gram_per_unit / aux_unit / aux_rate /
              category_code / barcode / status。
    **绝不能** 改 id / canonical_sku / created_from / created_by / created_at。

    Raises:
        ValueError: 字段不允许
        DeactivateOnlyViolation: status='deleted'
    """
    master_conn.row_factory = sqlite3.Row
    allowed = {
        "name", "unit", "gram_per_unit", "aux_unit", "aux_rate",
        "category_code", "barcode", "status",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"字段 {sorted(bad)} 不可更新")

    # status 合法性（Q3=deactivate_only）
    if "status" in fields:
        fields["status"] = set_canonical_status(canonical_id, fields["status"])

    # 派生关系（§1.8.2）
    if fields.get("aux_unit") is not None and fields.get("aux_unit") != "克":
        fields["gram_per_unit"] = 0.0
    elif (
        "aux_rate" in fields and fields.get("aux_unit") != "克"
    ):
        fields["gram_per_unit"] = 0.0

    if not fields:
        return get_canonical_item_detail(master_conn, canonical_id)

    fields["updated_at"] = _now_str()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    cur = master_conn.execute(
        f"UPDATE canonical_items SET {set_clause} WHERE id=?",
        (*fields.values(), canonical_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"canonical_id={canonical_id} 不存在")
    master_conn.commit()
    return get_canonical_item_detail(master_conn, canonical_id)


# ─────────────────────────────────────────────────────────────────────
#  P0-11 —— 主数据批量统一修改
#
#  方案：docs/2026-10-09-item-master-unify-plan.md §3 P0-11
#  原则：批量与单条走同一个 update_canonical_item()，绝不另开写路径；
#        字段白名单、aux_unit/gram_per_unit 派生关系都在那一处强制。
#  ⚠️ 价格（unit_cost / selling_price）不在批量范围内：价格收归主数据
#     （P0-9）尚未实施，价格暂由各仓维护。
# ─────────────────────────────────────────────────────────────────────

BATCH_EDITABLE_FIELDS: tuple[str, ...] = (
    "category_code",   # 品类
    "unit",            # 单位
    "gram_per_unit",   # 克重
    "aux_unit",        # 辅单位
    "aux_rate",        # 辅单位换算率
)

BATCH_FIELD_LABELS: dict[str, str] = {
    "category_code": "品类",
    "unit": "单位",
    "gram_per_unit": "克重",
    "aux_unit": "辅单位",
    "aux_rate": "辅单位换算率",
}


def coerce_batch_value(field: str, raw: Any) -> Any:
    """P0-11 —— 表单原始值 → 批量修改值（空串转 None，数字字段转 float）。"""
    if field not in BATCH_EDITABLE_FIELDS:
        raise ValueError(f"字段 {field!r} 不支持批量修改")
    text = ("" if raw is None else str(raw)).strip()
    if field in ("category_code", "aux_unit"):
        return text or None
    if field == "unit":
        if not text:
            raise ValueError("单位不能为空")
        return text
    try:
        num = float(text or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{BATCH_FIELD_LABELS[field]}必须是数字") from None
    if num < 0:
        raise ValueError(f"{BATCH_FIELD_LABELS[field]}不能为负数")
    return num


def batch_update_canonical_items(
    master_conn: sqlite3.Connection,
    canonical_ids: list[int],
    field: str,
    value: Any,
) -> dict:
    """P0-11 —— 把选中主数据项的某个字段统一设为同一值。

    Returns:
        {"field": str, "label": str, "value": Any,
         "updated": int, "error_count": int, "errors": [str, ...]}
    """
    if field not in BATCH_EDITABLE_FIELDS:
        raise ValueError(f"字段 {field!r} 不支持批量修改")
    master_conn.row_factory = sqlite3.Row
    updated = 0
    errors: list[str] = []
    for cid in canonical_ids:
        row = master_conn.execute(
            "SELECT aux_unit, name FROM canonical_items WHERE id=?", (int(cid),)
        ).fetchone()
        if row is None:
            errors.append(f"#{cid}: 主数据项不存在")
            continue
        fields: dict[str, Any] = {field: value}
        # aux_rate / gram_per_unit 的派生逻辑要看 aux_unit：把现值带上，
        # 避免「只改换算率却把克重清零」的副作用（§1.8.2）。
        if field in ("aux_rate", "gram_per_unit"):
            fields["aux_unit"] = row["aux_unit"]
        try:
            update_canonical_item(master_conn, int(cid), fields)
            updated += 1
        except ValueError as e:  # noqa: PERF203
            errors.append(f"#{cid} {row['name']}: {e}")
    return {
        "field": field,
        "label": BATCH_FIELD_LABELS[field],
        "value": value,
        "updated": updated,
        "error_count": len(errors),
        "errors": errors,
    }


def list_canonical_items(
    master_conn: sqlite3.Connection,
    *,
    status: str | None = None,
    category_code: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """T4 —— 列出 canonical_items。"""
    master_conn.row_factory = sqlite3.Row
    where_parts: list[str] = []
    params: list[Any] = []
    if status is not None:
        where_parts.append("status = ?")
        params.append(status)
    if category_code is not None:
        where_parts.append("category_code = ?")
        params.append(category_code)
    where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.extend([limit, offset])
    rows = master_conn.execute(
        f"""SELECT * FROM canonical_items{where}
            ORDER BY id LIMIT ? OFFSET ?""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_canonical_item_detail(
    master_conn: sqlite3.Connection,
    canonical_id: int,
) -> dict:
    """T4 —— 查单条 canonical_items + 别名 + 跨仓 binding 概览。"""
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        "SELECT * FROM canonical_items WHERE id=?", (canonical_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"canonical_id={canonical_id} 不存在")
    detail = dict(row)
    # 别名（PRD P1-6 全局别名可见）
    detail["aliases"] = [
        dict(r) for r in master_conn.execute(
            "SELECT * FROM canonical_item_aliases WHERE canonical_id=?",
            (canonical_id,),
        ).fetchall()
    ]
    return detail


# ─────────────────────────────────────────────────────────────────────
#  T5 — Categories (Spec §2.3 + §6.1 CATEGORY_CODE_MAP)
# ─────────────────────────────────────────────────────────────────────

def seed_canonical_categories(master_conn: sqlite3.Connection) -> list[dict]:
    """T5 —— 用 config.FIXED_CATEGORIES + CATEGORY_CODE_MAP 初始化
    canonical_categories。已存在的跳过（幂等）。
    """
    master_conn.row_factory = sqlite3.Row
    ts = _now_str()
    inserted: list[dict] = []
    for name in FIXED_CATEGORIES:
        code = CATEGORY_CODE_MAP.get(name)
        if code is None:
            # 不在映射表里的本地名 → 跳过（不应发生,因为映射表覆盖全部 9 个）
            continue
        existing = master_conn.execute(
            "SELECT id FROM canonical_categories WHERE code=?", (code,)
        ).fetchone()
        if existing is not None:
            continue
        cur = master_conn.execute(
            """INSERT INTO canonical_categories
               (code, name, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (code, name, "FIXED_CATEGORIES 种子", ts, ts),
        )
        inserted.append({"id": int(cur.lastrowid), "code": code, "name": name})
    master_conn.commit()
    return inserted


def backfill_category_mappings(wh_conn: sqlite3.Connection) -> dict:
    """T5 —— 对单仓:把 categories.canonical_code 按 name-exact 填入
    CATEGORY_CODE_MAP 映射表。**仅当 name 完全一致**(绝不猜)。

    返回:{"filled": N, "missing": [name1, name2, ...]}
    """
    wh_conn.row_factory = sqlite3.Row
    filled = 0
    missing: list[str] = []
    rows = wh_conn.execute(
        "SELECT id, name, canonical_code FROM categories"
    ).fetchall()
    for r in rows:
        if r["canonical_code"]:
            continue  # 已映射
        code = CATEGORY_CODE_MAP.get(r["name"])
        if code is None:
            missing.append(r["name"])
            continue
        wh_conn.execute(
            "UPDATE categories SET canonical_code=? WHERE id=?", (code, r["id"])
        )
        filled += 1
    wh_conn.commit()
    return {"filled": filled, "missing": missing}


def list_missing_category_mappings(
    master_conn: sqlite3.Connection,
    wh_code: str | None = None,
) -> list[dict]:
    """T5 —— 列出全仓(或指定仓)中未映射到 canonical_code 的本地分类。

    wh_code=None → 遍历所有仓库返回合并视图。
    """
    from config import BASE_DIR  # local import to avoid circular at module load

    master_conn.row_factory = sqlite3.Row
    missing: list[dict] = []
    wh_rows = master_conn.execute(
        "SELECT code, db_path FROM warehouses"
    ).fetchall()
    for wh in wh_rows:
        if wh_code is not None and wh["code"] != wh_code:
            continue
        db_path = Path(wh["db_path"])
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        if not db_path.exists():
            continue
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, name FROM categories
                   WHERE canonical_code IS NULL OR canonical_code = ''"""
            ).fetchall()
            for r in rows:
                missing.append({
                    "warehouse_code": wh["code"],
                    "category_id": r["id"],
                    "category_name": r["name"],
                })
    return missing


def resolve_category_id(
    master_conn: sqlite3.Connection,
    wh_conn: sqlite3.Connection,
    canonical_category_code: str | None,
    wh_category_name: str | None,
) -> int:
    """T5 —— 给定一个 canonical code + 仓本地名,返回该仓 categories.id。

    优先按 canonical_code 查;若无则按 name 查;两者都没有则按 name 创建。
    """
    wh_conn.row_factory = sqlite3.Row
    if canonical_category_code:
        row = wh_conn.execute(
            "SELECT id FROM categories WHERE canonical_code=?",
            (canonical_category_code,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    if wh_category_name:
        row = wh_conn.execute(
            "SELECT id FROM categories WHERE name=?",
            (wh_category_name,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        # 兜底创建
        cur = wh_conn.execute(
            """INSERT INTO categories (name, description, created_at)
               VALUES (?, 'canonical auto-created', ?)""",
            (wh_category_name, _now_str()),
        )
        wh_conn.commit()
        return int(cur.lastrowid)
    raise ValueError("必须提供 canonical_category_code 或 wh_category_name")


# ─────────────────────────────────────────────────────────────────────
#  T6 — Strong signal detection (Spec §3.1)
# ─────────────────────────────────────────────────────────────────────

# Spec §3.1: ^(?:pre)[A-Za-z]{2}\d{3}-(stem)-(rand)\d{3,4}$
_SKU_RE = re.compile(
    r"^(?P<pre>[A-Za-z]{2,3}\d{3,4})-(?P<stem>.+)-(?P<rand>\d{3,6})$"
)


def parse_local_sku(sku: str) -> dict[str, Any]:
    """T6 —— 解析门店 SKU 为 {pre, stem, rand}。解析失败时只返回原始 sku。

    WH001-冰块-9935 → {pre:'WH001', stem:'冰块', rand:'9935'}
    """
    m = _SKU_RE.match(sku or "")
    if not m:
        return {"raw": sku, "pre": None, "stem": None, "rand": None}
    return {"raw": sku, "pre": m.group("pre"), "stem": m.group("stem"),
            "rand": m.group("rand")}


def normalize_name(name: str) -> str:
    """T6 —— 归一化中文名:去空格 / 去常见前后缀。不做语义改写。"""
    if not name:
        return ""
    s = name.strip()
    s = re.sub(r"\s+", "", s)
    return s


def name_similarity(a: str, b: str) -> float:
    """T6 —— 名称相似度,使用 difflib.SequenceMatcher。"""
    return SequenceMatcher(None, a or "", b or "").ratio()


def _confidence_signals(
    rows: list[dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    """Spec §3.1 校准:多信号加权。

    主判据(SKU 全等)—— team-lead 修正:WP 编号 5 仓已完全一致,
    直接以 SKU 简名全等为主判据(权重 0.6)。

    名称相似度降为辅助(权重 0.3)。
    单位一致(权重 0.1)作为门槛。
    """
    units = {r.get("unit") for r in rows if r.get("unit")}
    if len(units) > 1:
        return 0.0, {"reason": "unit_mismatch"}

    # 1. SKU 简名全等
    stems = set()
    for r in rows:
        sku_info = parse_local_sku(r.get("sku", ""))
        if sku_info["stem"]:
            stems.add(sku_info["stem"])
    if len(stems) == 1 and len(rows) >= 2:
        return 0.6, {"primary": "sku_full_match", "stem": next(iter(stems))}

    # 2. 名称相似度 (主指标低于 0.6 时降级)
    names = [normalize_name(r.get("name", "")) for r in rows]
    if len(names) >= 2 and all(names):
        ratios = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                ratios.append(name_similarity(names[i], names[j]))
        if ratios:
            avg = sum(ratios) / len(ratios)
            return 0.3 + 0.3 * avg, {"primary": "name_similarity", "avg_ratio": avg}
    return 0.0, {"primary": "no_signal"}


def detect_similar_items(rows_per_wh: dict[str, list[dict[str, Any]]]) -> list[dict]:
    """T6 —— 跨仓相似检测（Spec §3.1 校准）。

    输入:rows_per_wh = {warehouse_code: [item_row, ...]}
        item_row 至少含 sku / name / unit 字段。
    输出:候选分组列表,每组形如:
        {"members": [...], "confidence": float, "signals": {...}}

    实现:按 unit 分组 → 每组内两两比较 → 用 _confidence_signals 计算。
    不写 SQL,纯 Python。
    """
    groups: dict[tuple[str, ...], dict[str, list[dict]]] = {}
    for wh_code, rows in rows_per_wh.items():
        for r in rows:
            unit = r.get("unit", "")
            key = (unit,)
            groups.setdefault(key, {}).setdefault(wh_code, []).append(r)

    out: list[dict] = []
    for (unit,), wh_map in groups.items():
        if len(wh_map) < 2:
            continue
        flat = []
        for wh_code, rs in wh_map.items():
            for r in rs:
                flat.append({**r, "_warehouse_code": wh_code})
        # 简化:整组做一次 confidence（不展开到 N×N 配对,避免噪声）
        confidence, signals = _confidence_signals(flat)
        if confidence < 0.5:
            continue
        out.append({
            "unit": unit,
            "members": flat,
            "confidence": round(confidence, 3),
            "signals": signals,
        })
    return out


def collect_all_items(master_conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """T6 —— 跨仓收集 items 用于强信号检测。只 SELECT 必要列。"""
    from config import BASE_DIR  # 避免循环 import

    master_conn.row_factory = sqlite3.Row
    out: dict[str, list[dict]] = {}
    wh_rows = master_conn.execute(
        """SELECT code, db_path, warehouse_type
           FROM warehouses
           WHERE warehouse_type='storefront'"""
    ).fetchall()
    for wh in wh_rows:
        db_path = Path(wh["db_path"])
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        if not db_path.exists():
            continue
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            # select_item_columns 动态列(避免 initial_quantity 不存在时抛异常)
            cols = select_item_columns(conn)
            want = [c for c in ("sku", "name", "unit", "category_id")
                    if c in cols]
            select_clause = ", ".join(want)
            rows = conn.execute(
                f"SELECT id, {select_clause} FROM items"
            ).fetchall()
            out[wh["code"]] = [dict(r) for r in rows]
    return out


# ─────────────────────────────────────────────────────────────────────
#  T7 — Claim / Unclaim / Submit / Review (Spec §3.2 + §1.6.4 + §7.7.4)
# ─────────────────────────────────────────────────────────────────────

# Spec §3.2 铁律:认领只允许写这 3(+1) 列。
ALLOWED_CLAIM_UPDATES: frozenset[str] = frozenset(
    {"canonical_id", "is_alias", "canonical_status", "updated_at"}
)


def claim_item(
    master_conn: sqlite3.Connection,
    wh_conn: sqlite3.Connection,
    *,
    warehouse_code: str,
    local_item_id: int,
    canonical_id: int,
    is_alias: bool = False,
    is_store_exclusive: bool = False,
    submitted_by: int | None = None,
    local_keep_name: str | None = None,
    reason: str | None = None,
) -> dict:
    """T7 —— 认领一个 items 行到 canonical_items.id。

    实现 Spec §3.2 铁律:
      - 写 items 行: 仅 canonical_id / is_alias / canonical_status / is_store_exclusive / updated_at
      - 不动 name / unit / category_id / quantity / safety_stock / selling_price / unit_cost
      - 乐观锁: WHERE canonical_id IS NULL,rowcount==0 → 409
      - 备份 + 三快照校验(Q7 §7.7.2)

    Returns: 写入后的 items 行 + master 侧 claim_requests.id
    Raises:
        NewItemDenied / ValueError / 等
    """
    master_conn.row_factory = sqlite3.Row
    wh_conn.row_factory = sqlite3.Row

    # 1. 校验 canonical_id 存在
    canon = master_conn.execute(
        "SELECT * FROM canonical_items WHERE id=?", (canonical_id,)
    ).fetchone()
    if canon is None:
        raise ValueError(f"canonical_id={canonical_id} 不存在")

    # 2. 校验 items 行存在 + 拿当前行做断言
    row = wh_conn.execute(
        "SELECT * FROM items WHERE id=?", (local_item_id,)
    ).fetchone()
    if row is None:
        raise ValueError(
            f"warehouse={warehouse_code} local_item_id={local_item_id} 不存在"
        )
    if row["canonical_id"] is not None:
        raise ValueError(
            f"该行已被认领到 canonical_id={row['canonical_id']}；"
            f"若要改绑定，请先 unclaim"
        )

    # 3. Q7: 备份 + 拍快照
    wh_path = Path(wh_conn.execute("PRAGMA database_list").fetchone()["file"])
    backup_path = backup_warehouse_db(wh_path, tag="pre-claim")
    before_ids = {r["id"] for r in wh_conn.execute("SELECT id FROM items")}
    before_inv = snapshot_inventory(wh_conn)
    before_cnt = wh_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    # 4. 单位族不匹配检查（§1.8.5 + §7.10）—— 不阻断认领,记 canonical_conflicts
    has_unit_conflict = (row["unit"] != canon["unit"])
    if has_unit_conflict:
        master_conn.execute(
            """INSERT INTO canonical_conflicts
               (canonical_id, warehouse_code, local_item_id, field,
                conflict_type, canonical_value, local_value, last_synced_value,
                status, created_at)
               VALUES (?, ?, ?, 'unit', 'unit_conversion', ?, ?, NULL,
                       'open', ?)""",
            (canonical_id, warehouse_code, local_item_id,
             str(canon["unit"]), str(row["unit"]), _now_str()),
        )

    # 5. 单事务写仓
    try:
        wh_conn.execute("BEGIN IMMEDIATE")
        cur = wh_conn.execute(
            """UPDATE items
               SET canonical_id=?, is_alias=?, canonical_status=?,
                   is_store_exclusive=?, updated_at=?
               WHERE id=? AND canonical_id IS NULL""",
            (canonical_id, 1 if is_alias else 0, "active",
             1 if is_store_exclusive else 0,
             _now_str(), local_item_id),
        )
        if cur.rowcount == 0:
            raise ValueError("乐观锁失败：行已被并发认领")
        # 断言:严格只允许写这 4 列(避免未来代码漂移)
        # 我们已通过 SQL 字面量保证;此处再做行级断言
        new_row = wh_conn.execute(
            "SELECT * FROM items WHERE id=?", (local_item_id,)
        ).fetchone()
        # 业务字段完全未变(除了 updated_at)
        for col in ("name", "unit", "category_id", "quantity",
                    "safety_stock", "selling_price", "unit_cost"):
            assert new_row[col] == row[col], (
                f"认领后业务字段 {col} 变化: {row[col]!r} -> {new_row[col]!r}"
            )
        assert_inventory_unchanged(before_inv, snapshot_inventory(wh_conn))
        assert_ids_stable(before_ids,
                          {r["id"] for r in wh_conn.execute("SELECT id FROM items")})
        assert_row_count_conserved(wh_conn, "items", before_cnt)
        wh_conn.commit()
    except Exception:
        wh_conn.rollback()
        raise

    # 6. master 侧写认领记录 + 别名
    req_cur = master_conn.execute(
        """INSERT INTO canonical_claim_requests
           (warehouse_code, local_item_id, local_sku, local_name,
            canonical_id, local_keep_name, request_type, reason, status,
            submitted_by, reviewed_by, reviewed_at, review_note,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'claim', ?, 'approved',
                   ?, ?, ?, ?, ?)""",
        (warehouse_code, local_item_id, row["sku"], row["name"],
         canonical_id, local_keep_name, reason,
         submitted_by, submitted_by, _now_str(), "auto-approved on claim",
         _now_str()),
    )
    request_id = int(req_cur.lastrowid)
    if local_keep_name:
        master_conn.execute(
            """INSERT INTO canonical_item_aliases
               (canonical_id, alias, normalized_alias, warehouse_code,
                source, created_at)
               VALUES (?, ?, ?, ?, 'storefront_claim', ?)""",
            (canonical_id, local_keep_name, normalize_name(local_keep_name),
             warehouse_code, _now_str()),
        )
    master_conn.commit()

    return {
        "warehouse_code": warehouse_code,
        "local_item_id": local_item_id,
        "canonical_id": canonical_id,
        "is_alias": 1 if is_alias else 0,
        "is_store_exclusive": 1 if is_store_exclusive else 0,
        "canonical_status": "active",
        "request_id": request_id,
        "backup_path": str(backup_path),
        "unit_conflict": has_unit_conflict,
    }


def unclaim_item(
    master_conn: sqlite3.Connection,
    wh_conn: sqlite3.Connection,
    *,
    warehouse_code: str,
    local_item_id: int,
    reviewed_by: int | None = None,
    reason: str | None = None,
) -> dict:
    """T7 —— 解绑(对称于 claim_item,§7.8.3)。

    严格 ALLOWED_CLAIM_UPDATES 写 3 列 NULL/0。
    业务字段一行不动(对称性是硬要求)。
    """
    master_conn.row_factory = sqlite3.Row
    wh_conn.row_factory = sqlite3.Row

    row = wh_conn.execute(
        "SELECT * FROM items WHERE id=?", (local_item_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"local_item_id={local_item_id} 不存在")
    if row["canonical_id"] is None:
        raise ValueError("该行未纳管,无需解绑")
    canonical_id = int(row["canonical_id"])

    # 整组校验:同 canonical_id 在本仓绑了 N 行,要求一次全解(否则 2 行悬空)
    siblings = wh_conn.execute(
        "SELECT id FROM items WHERE canonical_id=? AND id!=?",
        (canonical_id, local_item_id),
    ).fetchall()
    if siblings:
        raise ValueError(
            f"本仓还有 {len(siblings)} 行绑同一 canonical_id={canonical_id}，"
            f"需一次解绑整组(IDs={[s['id'] for s in siblings]})"
        )

    # Q7: 备份 + 三快照
    wh_path = Path(wh_conn.execute("PRAGMA database_list").fetchone()["file"])
    backup_warehouse_db(wh_path, tag="pre-unclaim")
    before_ids = {r["id"] for r in wh_conn.execute("SELECT id FROM items")}
    before_inv = snapshot_inventory(wh_conn)
    before_cnt = wh_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    try:
        wh_conn.execute("BEGIN IMMEDIATE")
        cur = wh_conn.execute(
            """UPDATE items
               SET canonical_id=NULL, is_alias=0, canonical_status=NULL,
                   updated_at=?
               WHERE id=? AND canonical_id=?""",
            (_now_str(), local_item_id, canonical_id),
        )
        if cur.rowcount == 0:
            raise ValueError("乐观锁失败:行已被并发修改")
        new_row = wh_conn.execute(
            "SELECT * FROM items WHERE id=?", (local_item_id,)
        ).fetchone()
        for col in ("name", "unit", "category_id", "quantity",
                    "safety_stock", "selling_price", "unit_cost"):
            assert new_row[col] == row[col], (
                f"解绑后业务字段 {col} 变化"
            )
        assert_inventory_unchanged(before_inv, snapshot_inventory(wh_conn))
        assert_ids_stable(before_ids,
                          {r["id"] for r in wh_conn.execute("SELECT id FROM items")})
        assert_row_count_conserved(wh_conn, "items", before_cnt)
        wh_conn.commit()
    except Exception:
        wh_conn.rollback()
        raise

    # master: 把对应 claim_requests 标 cancelled
    master_conn.execute(
        """UPDATE canonical_claim_requests
           SET status='cancelled', reviewed_by=?, reviewed_at=?, review_note=?
           WHERE warehouse_code=? AND local_item_id=? AND status='approved'
           ORDER BY id DESC LIMIT 1""",
        (reviewed_by, _now_str(), reason or "unclaimed",
         warehouse_code, local_item_id),
    )
    master_conn.commit()
    return {
        "warehouse_code": warehouse_code,
        "local_item_id": local_item_id,
        "canonical_id": canonical_id,
        "action": "unclaimed",
    }


def submit_claim_request(
    master_conn: sqlite3.Connection,
    *,
    warehouse_code: str,
    local_item_id: int | None = None,
    local_sku: str | None = None,
    local_name: str,
    canonical_id: int | None = None,
    proposed_category_code: str | None = None,
    proposed_unit: str | None = None,
    request_type: str = "claim",
    reason: str | None = None,
    submitted_by: int | None = None,
) -> int:
    """T24 —— 写一条 canonical_claim_requests。返回 request_id。"""
    if request_type not in ("claim", "new_item", "exempt"):
        raise ValueError(f"未知 request_type: {request_type!r}")
    if request_type == "new_item" and not proposed_category_code:
        raise ValueError("new_item 类申请必须提供 proposed_category_code")
    if request_type == "claim" and canonical_id is None:
        raise ValueError("claim 类申请必须提供 canonical_id")
    master_conn.row_factory = sqlite3.Row
    cur = master_conn.execute(
        """INSERT INTO canonical_claim_requests
           (warehouse_code, local_item_id, local_sku, local_name,
            canonical_id, proposed_category_code, proposed_unit,
            request_type, reason, status, submitted_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (warehouse_code, local_item_id, local_sku, local_name,
         canonical_id, proposed_category_code, proposed_unit,
         request_type, reason, submitted_by, _now_str()),
    )
    master_conn.commit()
    return int(cur.lastrowid)


def review_claim_request(
    master_conn: sqlite3.Connection,
    *,
    request_id: int,
    decision: str,  # 'approved' | 'rejected'
    reviewed_by: int,
    review_note: str | None = None,
) -> dict:
    """T24 —— 批准/驳回 canonical_claim_requests。"""
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision 必须是 approved/rejected，得到 {decision!r}")
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        "SELECT * FROM canonical_claim_requests WHERE id=?", (request_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"request_id={request_id} 不存在")
    if row["status"] != "pending":
        raise ValueError(f"request_id={request_id} 已是 {row['status']}，不能再审")

    master_conn.execute(
        """UPDATE canonical_claim_requests
           SET status=?, reviewed_by=?, reviewed_at=?, review_note=?
           WHERE id=?""",
        (decision, reviewed_by, _now_str(), review_note, request_id),
    )
    master_conn.commit()
    return {"request_id": request_id, "status": decision}


# ─────────────────────────────────────────────────────────────────────
#  T8 — Fanout (Spec §3.3, §3.5) —— M2 关键路径
# ─────────────────────────────────────────────────────────────────────

# Spec §3.3 写回主数据后,canonical_synced_json 仅含写入的字段
def _syncable_snapshot(canonical_row: dict) -> dict[str, Any]:
    """构造 canonical_synced_json(只含 is_syncable_field 的字段)。"""
    snap: dict[str, Any] = {}
    for field in ("name", "unit", "gram_per_unit", "aux_unit", "aux_rate"):
        if is_syncable_field(field):
            snap[field] = canonical_row.get(field)
    return snap


def _build_category_id_map(
    wh_conn: sqlite3.Connection,
    canon_by_id: dict[int, dict],
    code_to_name: dict[str, str],
) -> dict[int, int | None]:
    """T8 —— 预解析本批次每个 canonical_items.id → target categories.id。

    解析策略(顺序):
      1. 按 categories.canonical_code = canonical_items.category_code
      2. 兜底按 categories.name = canonical_categories.name(对应该 code)
      3. 都没有 → 返回 None(供 apply_canonical_to_warehouse 跳过,不自动创建)

    不调用现有 resolve_category_id(它会自动建行,在扇出路径上不合适:
    我们这里对源头数据缺失的语义是「跳过」而不是「兜底建」)。
    """
    wh_conn.row_factory = sqlite3.Row
    # 单次全表 categories 扫描,仓内品类数量固定 9,不用 IN 子句优化。
    cat_rows = wh_conn.execute(
        "SELECT id, name, canonical_code FROM categories"
    ).fetchall()
    by_code: dict[str, int] = {}
    by_name: dict[str, int] = {}
    for r in cat_rows:
        if r["canonical_code"]:
            # 同一 canonical_code 出现多次(历史脏数据)时,取最小 id,
            # 与 resolve_category_id 的隐含语义一致。
            cid = int(r["id"])
            if r["canonical_code"] not in by_code or cid < by_code[r["canonical_code"]]:
                by_code[r["canonical_code"]] = cid
        if r["name"]:
            nid = int(r["id"])
            if r["name"] not in by_name or nid < by_name[r["name"]]:
                by_name[r["name"]] = nid

    out: dict[int, int | None] = {}
    for cid, canon in canon_by_id.items():
        code = canon.get("category_code")
        if not code:
            out[cid] = None
            continue
        local_id = by_code.get(code)
        if local_id is None:
            name = code_to_name.get(code)
            if name:
                local_id = by_name.get(name)
        out[cid] = local_id
    return out


def apply_canonical_to_warehouse(
    wh_conn: sqlite3.Connection,
    canonical: dict[str, Any],
    last_snapshot: dict[str, Any] | None,
    action: str = "overwrite",
    force: bool = False,
    category_id_map: dict[int, int | None] | None = None,
) -> dict:
    """Spec §3.5 判定顺序:
      1) 先算冻结(local != last_synced)
      2) action 只决定"对未冻结字段怎么处理"
      3) force 跳过第 1 步

    Args:
      category_id_map: 仅 INSERT 分支使用。
        - None: 调用方没传 → 在 INSERT 分支抛 RuntimeError（防御:直调本函数
          又想 INSERT 的代码路径应该先建映射,不应静默写死 0）。
        - dict: {canonical_items.id: target_wh.categories.id | None}。
          值是 None 表示目标仓无对应品类 → 跳过该 canonical_id,返回
          skipped_reason="no matching category",不写 items,不进 conflict 队列
          (源头数据缺失,不是门店冲突)。

    Returns: {
      "written": {field: value, ...},
      "frozen": {field: {"canonical": ..., "local": ..., "last": ...}, ...},
      "inserted": bool,
      "skipped_reason": str|None,
    }
    """
    wh_conn.row_factory = sqlite3.Row

    # 命中:先按 canonical_id,再按 sku
    canon_id = canonical["id"]
    existing = wh_conn.execute(
        "SELECT * FROM items WHERE canonical_id=?", (canon_id,)
    ).fetchone()
    if existing is None:
        # 兜底:按 sku 匹配(历史上某些行没 canonical_id 但 sku 与 canonical_sku 同)
        # 注:§6.1.1 严格决策里 sku 永远不一致(主数据 sku 与门店 sku 不同),此分支基本不可达
        # 保留是为了兜底历史数据
        existing = wh_conn.execute(
            "SELECT * FROM items WHERE sku=?", (canonical["canonical_sku"],)
        ).fetchone()

    if existing is None:
        # 未命中 —— INSERT 新行(quantity=0, safety_stock=0)
        # 防御:直调本函数又没传 category_id_map 的旧代码路径,不再静默写死
        # category_id=0 (FK 会拦下来),直接抛异常以防漏改。
        if category_id_map is None:
            raise RuntimeError(
                "apply_canonical_to_warehouse: INSERT 分支要求 category_id_map "
                "(由 fanout_canonical_items 预解析后传入)"
            )
        local_category_id = category_id_map.get(canon_id)
        if local_category_id is None:
            # 源头数据缺失:目标仓无对应品类。Spec 决策是跳过(不强行创建,
            # 不写 items,不进 conflict 队列)。fanout_canonical_items 会把
            # 这条事件记成 status='skipped' + error_message。
            return {
                "written": {},
                "frozen": {},
                "inserted": False,
                "skipped_reason": "no matching category",
            }
        new_sku = f"AUTO-{canonical['canonical_sku']}"  # 唯一不撞门店 SKU
        wh_conn.execute(
            """INSERT INTO items
               (sku, name, category_id, quantity, safety_stock,
                unit, gram_per_unit, aux_unit, aux_rate,
                updated_at, canonical_id, is_alias, canonical_status,
                canonical_synced_json)
               VALUES (?, ?, ?, 0, 0,
                       ?, ?, ?, ?,
                       ?, ?, 0, 'active', ?)""",
            (
                new_sku, canonical["name"], local_category_id,
                canonical["unit"], canonical["gram_per_unit"],
                canonical["aux_unit"], canonical["aux_rate"],
                _now_str(), canon_id,
                json.dumps(_syncable_snapshot(canonical), ensure_ascii=False),
            ),
        )
        return {
            "written": _syncable_snapshot(canonical),
            "frozen": {},
            "inserted": True,
            "skipped_reason": None,
        }

    # 命中 —— 逐字段判定
    written: dict[str, Any] = {}
    frozen: dict[str, Any] = {}

    for field in ("name", "unit", "gram_per_unit", "aux_unit", "aux_rate"):
        if not is_syncable_field(field):
            continue
        target_col = CANONICAL_FIELD_POLICY[field][1]
        if target_col == "__canonical_status__":
            target_col = "canonical_status"

        local = existing[target_col]
        last = (last_snapshot or {}).get(field) if last_snapshot else None
        cval = canonical[field]

        # Spec §3.5: last_synced_value 为 None(从未下发过,例如刚认领的门店行)
        # → 视为"门店没动过" → 正常下发,不算冲突
        if last is None:
            written[field] = cval
            continue

        # 判定
        verdict = resolve_conflict_policy(local, last, cval, force=force)
        if verdict == "frozen":
            frozen[field] = {
                "canonical": cval, "local": local, "last": last,
            }
            continue
        if verdict == "force_only" and not force:
            frozen[field] = {
                "canonical": cval, "local": local, "last": last,
            }
            continue
        # allowed 或 force_only-with-force
        written[field] = cval

    # §1.8.2 派生关系
    if "aux_unit" in written or "aux_rate" in written:
        new_aux_unit = written.get("aux_unit", existing["aux_unit"])
        new_aux_rate = written.get("aux_rate", existing["aux_rate"])
        written["gram_per_unit"] = (
            new_aux_rate if new_aux_unit == "克" else 0.0
        )

    # 单元族整组冻结:任一字段冲突 → 整组不写(§1.8)
    unit_fields = {"unit", "gram_per_unit", "aux_unit", "aux_rate"}
    if any(f in frozen for f in unit_fields) and not force:
        for f in unit_fields:
            if f in written:
                del written[f]

    # action 语义:keep 不覆盖已有,merge 只补空
    if action == "keep" and not frozen:
        # keep 模式下不写已有字段(新建除外)
        written = {}

    # status 特殊处理:写 canonical_status
    if "status" in CANONICAL_FIELD_POLICY:
        cstatus = canonical.get("status", "active")
        if cstatus in ("active", "disabled", "inactive"):
            if existing["canonical_status"] != cstatus:
                written["__canonical_status__"] = cstatus

    return {
        "written": written,
        "frozen": frozen,
        "inserted": False,
        "skipped_reason": None,
    }


def fanout_canonical_items(
    master_conn: sqlite3.Connection,
    wh_db_map: dict[str, sqlite3.Connection],
    *,
    canonical_ids: list[int],
    warehouse_codes: list[str],
    action: str = "overwrite",
    force: bool = False,
    dry_run: bool = False,
    started_by: int | None = None,
    summary: str | None = None,
    backup_paths: list[str] | None = None,
) -> dict:
    """T8 —— 主扇出函数。Spec §3.3 时序图完整实现。

    Args:
        wh_db_map: {warehouse_code: sqlite3.Connection}
        canonical_ids: 要下发的 canonical_items.id 列表
        warehouse_codes: 目标仓列表
        action: keep / merge / overwrite / force(force 通过 force=True 走)
        dry_run: True 时只算 plan,不写库
        backup_paths: 写前备份文件路径列表(Q7 措施①);写入事件 backup_paths_json。
    Returns:
        {
          "event_id": int,
          "dry_run": bool,
          "per_canonical": [{canonical_id, per_warehouse: [{wh_code, written, frozen, ...}]}],
          "total_written": int,
          "total_frozen": int,
          "status": "complete" | "partial" | "failed",
        }
    """
    master_conn.row_factory = sqlite3.Row
    target_codes_json = json.dumps(sorted(warehouse_codes), ensure_ascii=False)
    event_id: int | None = None
    if not dry_run:
        cur = master_conn.execute(
            """INSERT INTO canonical_publish_events
               (summary, status, started_by, started_at,
                target_warehouse_codes_json, item_count)
               VALUES (?, 'pending', ?, ?, ?, ?)""",
            (summary or "fanout", started_by, _now_str(),
             target_codes_json, len(canonical_ids)),
        )
        event_id = int(cur.lastrowid)
        master_conn.commit()

    # ─────────────────────────────────────────────────────────────────
    # 预加载:本批次所有 canonical_items(避免循环里反复 SELECT)。
    # 还要拿 canonical_categories.name,这样目标仓 categories.canonical_code
    # 为空(比如 rd_001)时,可以用 name 兜底映射。
    # ─────────────────────────────────────────────────────────────────
    canon_by_id: dict[int, dict] = {}
    for cid in canonical_ids:
        row = master_conn.execute(
            "SELECT * FROM canonical_items WHERE id=?", (cid,)
        ).fetchone()
        if row is not None:
            canon_by_id[cid] = dict(row)

    code_to_name: dict[str, str] = {
        r["code"]: r["name"]
        for r in master_conn.execute(
            "SELECT code, name FROM canonical_categories"
        ).fetchall()
    }

    # 每个目标仓预解析一份 category_id_map:
    #   {canonical_items.id: target_wh.categories.id | None}
    # None 表示该仓无对应品类(写库时会被 apply_canonical_to_warehouse 跳过)。
    # 这里查询失败时退化为空 dict,后续 INSERT 分支会全部走"跳过"分支,
    # 不会出现部分写入部分崩溃的中间态。
    per_wh_category_maps: dict[str, dict[int, int | None]] = {}
    for wh_code in warehouse_codes:
        conn = wh_db_map.get(wh_code)
        if conn is None:
            continue
        try:
            per_wh_category_maps[wh_code] = _build_category_id_map(
                conn, canon_by_id, code_to_name,
            )
        except Exception:  # noqa: BLE001
            # 预解析失败:留空 dict,所有 INSERT 都会走 skipped 分支
            per_wh_category_maps[wh_code] = {}

    per_canonical: list[dict] = []
    total_written = 0
    total_frozen = 0
    any_partial = False
    backup_paths = backup_paths or []

    for cid in canonical_ids:
        canon = canon_by_id.get(cid)
        if canon is None:
            continue
        per_wh: list[dict] = []
        for wh_code in warehouse_codes:
            conn = wh_db_map.get(wh_code)
            if conn is None:
                continue
            category_id_map = per_wh_category_maps.get(wh_code, {})
            try:
                conn.row_factory = sqlite3.Row
                # 读上一次同步快照
                row = conn.execute(
                    """SELECT canonical_synced_json FROM items
                       WHERE canonical_id=?""", (cid,)
                ).fetchone()
                last_snapshot = None
                if row and row["canonical_synced_json"]:
                    try:
                        last_snapshot = json.loads(row["canonical_synced_json"])
                    except json.JSONDecodeError:
                        last_snapshot = None

                # ─────────────────────────────────────────────────────
                # 把 INSERT/UPDATE 失败(主要是 FK 兜底)隔离到 inner try:
                # 外层 except 之前会漏掉 event_items 记录(参见 issue:
                # rd_001 第一次扇出 0 行 event_items 的根因)。
                # 隔离后,异常会被转成 status='failed' + error_message,
                # 调用方在 /canonical/fanout_event 看到完整 per-item 失败表。
                # ─────────────────────────────────────────────────────
                try:
                    result = apply_canonical_to_warehouse(
                        conn, canon, last_snapshot,
                        action=action, force=force,
                        category_id_map=category_id_map,
                    )
                except Exception as apply_exc:  # noqa: BLE001
                    err_msg = str(apply_exc)[:200]
                    if not dry_run and event_id is not None:
                        master_conn.execute(
                            """INSERT INTO canonical_publish_event_items
                               (publish_event_id, canonical_id, target_warehouse_code,
                                local_item_id, status, applied_fields_json,
                                skipped_fields_json, error_message)
                               VALUES (?, ?, ?, NULL, 'failed',
                                       '[]', '[]', ?)""",
                            (event_id, cid, wh_code, err_msg),
                        )
                    per_wh.append({
                        "warehouse_code": wh_code,
                        "error": err_msg,
                        "inserted": False,
                        "skipped_reason": None,
                    })
                    any_partial = True
                    continue

                # 写入主数据字段
                if not dry_run and result["written"]:
                    written = result["written"]
                    # 分组:业务字段 vs canonical_status 字段
                    set_parts: list[str] = []
                    params: list[Any] = []
                    for field, val in written.items():
                        if field == "__canonical_status__":
                            set_parts.append("canonical_status=?")
                            params.append(val)
                        else:
                            col = CANONICAL_FIELD_POLICY[field][1] or field
                            set_parts.append(f"{col}=?")
                            params.append(val)
                    # 同步 JSON
                    new_snap = dict(last_snapshot or {})
                    new_snap.update({
                        k: v for k, v in written.items()
                        if k != "__canonical_status__"
                    })
                    set_parts.append("canonical_synced_json=?")
                    params.append(json.dumps(new_snap, ensure_ascii=False))
                    set_parts.append("updated_at=?")
                    params.append(_now_str())
                    params.append(cid)
                    conn.execute(
                        f"""UPDATE items SET {', '.join(set_parts)}
                            WHERE canonical_id=?""",
                        params,
                    )
                    conn.commit()

                # 写冲突行
                if not dry_run and result["frozen"]:
                    for field, vals in result["frozen"].items():
                        # 查 local_item_id
                        local_id_row = conn.execute(
                            "SELECT id FROM items WHERE canonical_id=?",
                            (cid,),
                        ).fetchone()
                        local_id = int(local_id_row["id"]) if local_id_row else 0
                        master_conn.execute(
                            """INSERT INTO canonical_conflicts
                               (canonical_id, warehouse_code, local_item_id,
                                publish_event_id, field, conflict_type,
                                canonical_value, local_value, last_synced_value,
                                status, created_at)
                               VALUES (?, ?, ?, ?, ?, 'value',
                                       ?, ?, ?, 'open', ?)""",
                            (
                                cid, wh_code, local_id, event_id, field,
                                str(vals["canonical"]),
                                str(vals["local"]),
                                str(vals["last"]) if vals["last"] is not None else None,
                                _now_str(),
                            ),
                        )

                # 写 per-item 事件
                # 状态优先级: skipped (源头数据缺失) > conflict (门店改了字段) >
                #           success (正常)。任何字段写到 master_conn 都在
                #           同一个事务里;失败的 case 已在 inner try 拦截并写入
                #           自己的 event_items 行。
                if not dry_run and event_id is not None:
                    if result.get("skipped_reason"):
                        item_status = "skipped"
                        event_err = result["skipped_reason"]
                    elif result["frozen"]:
                        item_status = "conflict"
                        event_err = None
                    else:
                        item_status = "success"
                        event_err = None
                    local_row = conn.execute(
                        "SELECT id FROM items WHERE canonical_id=?", (cid,)
                    ).fetchone()
                    master_conn.execute(
                        """INSERT INTO canonical_publish_event_items
                           (publish_event_id, canonical_id, target_warehouse_code,
                            local_item_id, status, applied_fields_json,
                            skipped_fields_json, error_message)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event_id, cid, wh_code,
                            int(local_row["id"]) if local_row else None,
                            item_status,
                            json.dumps(
                                {k: v for k, v in result["written"].items()
                                 if k != "__canonical_status__"},
                                ensure_ascii=False,
                            ),
                            json.dumps(list(result["frozen"].keys()),
                                       ensure_ascii=False),
                            event_err,
                        ),
                    )

                if result["written"]:
                    total_written += len(result["written"])
                if result["frozen"]:
                    total_frozen += len(result["frozen"])
                    any_partial = True
                per_wh.append({
                    "warehouse_code": wh_code,
                    "written": result["written"],
                    "frozen": result["frozen"],
                    "inserted": result["inserted"],
                    "skipped_reason": result.get("skipped_reason"),
                })
            except Exception as exc:  # noqa: BLE001
                per_wh.append({
                    "warehouse_code": wh_code,
                    "error": str(exc),
                })
                any_partial = True

        per_canonical.append({
            "canonical_id": cid,
            "canonical_sku": canon["canonical_sku"],
            "per_warehouse": per_wh,
        })

    # 更新事件状态
    if not dry_run and event_id is not None:
        final_status = (
            "failed" if total_written == 0 and total_frozen == 0
            else "partial" if any_partial else "complete"
        )
        master_conn.execute(
            """UPDATE canonical_publish_events
               SET status=?, completed_at=?, backup_paths_json=?
               WHERE id=?""",
            (final_status, _now_str(),
             json.dumps(backup_paths, ensure_ascii=False) or None,
             event_id),
        )
        master_conn.commit()

    return {
        "event_id": event_id,
        "dry_run": dry_run,
        "per_canonical": per_canonical,
        "total_written": total_written,
        "total_frozen": total_frozen,
        "status": (
            "dry_run" if dry_run else
            ("failed" if total_written == 0 and total_frozen == 0
             else "partial" if any_partial else "complete")
        ),
    }


def resolve_conflict(
    master_conn: sqlite3.Connection,
    *,
    conflict_id: int,
    decision: str,  # 'keep_local' | 'accept_canonical' | 'waive'
    reviewed_by: int,
    note: str | None = None,
) -> dict:
    """T8 —— 冲突裁决。"""
    if decision not in ("keep_local", "accept_canonical", "waive"):
        raise ValueError(f"decision 必须 keep_local/accept_canonical/waive, 得到 {decision!r}")
    master_conn.row_factory = sqlite3.Row
    row = master_conn.execute(
        "SELECT * FROM canonical_conflicts WHERE id=?", (conflict_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"conflict_id={conflict_id} 不存在")
    if row["status"] != "open":
        raise ValueError(f"conflict_id={conflict_id} 状态为 {row['status']}，不可再裁决")

    master_conn.execute(
        """UPDATE canonical_conflicts
           SET status=?, resolution_note=?, resolved_by=?, resolved_at=?
           WHERE id=?""",
        (decision, note, reviewed_by, _now_str(), conflict_id),
    )
    master_conn.commit()
    return {"conflict_id": conflict_id, "status": decision}


# ─────────────────────────────────────────────────────────────────────
#  T9 — Cross-warehouse reads + inspection (Spec §3.1 + §7.7.4 兜底)
# ─────────────────────────────────────────────────────────────────────

def collect_bindings(master_conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """T9 —— 跨仓收集 (warehouse_code, items.id, canonical_id, sku, name, unit)。

    v2（P0-10）：带上 is_active，供主数据详情页按仓停用/启用。
    """
    from config import BASE_DIR
    from db import migrate_warehouse_db_columns

    master_conn.row_factory = sqlite3.Row
    out: dict[str, list[dict]] = {}
    wh_rows = master_conn.execute(
        """SELECT code, db_path FROM warehouses
           WHERE warehouse_type='storefront'"""
    ).fetchall()
    for wh in wh_rows:
        db_path = Path(wh["db_path"])
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        if not db_path.exists():
            continue
        # 直连仓库必须先补幂等列迁移，否则旧库会撞 no such column: is_active
        migrate_warehouse_db_columns(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, sku, name, unit, canonical_id,
                          is_alias, canonical_status, is_store_exclusive,
                          is_active
                   FROM items
                   WHERE canonical_id IS NOT NULL"""
            ).fetchall()
            out[wh["code"]] = [dict(r) for r in rows]
    return out


def set_binding_active(
    wh_conn: sqlite3.Connection,
    warehouse_code: str,
    canonical_id: int,
    active: bool,
) -> dict:
    """P0-10 —— 按仓启停某个主数据项的绑定行（is_active）。

    只写 items.is_active / updated_at：库存、历史流水、主数据字段一律不动
    （Q7 零丢失精神）。返回 {"item_id", "warehouse_code", "canonical_id",
    "active", "quantity"}，quantity 供调用方在停用时做「仍有库存」软提示。

    Raises:
        ValueError: 该仓没有绑定这一 canonical_id 的行
    """
    wh_conn.row_factory = sqlite3.Row
    row = wh_conn.execute(
        "SELECT id, quantity FROM items WHERE canonical_id = ?", (int(canonical_id),)
    ).fetchone()
    if row is None:
        raise ValueError(
            f"仓库 {warehouse_code} 没有绑定 canonical_id={canonical_id} 的品项"
        )
    wh_conn.execute(
        "UPDATE items SET is_active = ?, updated_at = ? WHERE id = ?",
        (1 if active else 0, _now_str(), int(row["id"])),
    )
    wh_conn.commit()
    return {
        "item_id": int(row["id"]),
        "warehouse_code": warehouse_code,
        "canonical_id": int(canonical_id),
        "active": bool(active),
        "quantity": float(row["quantity"] or 0),
    }


def diff_summary(master_conn: sqlite3.Connection) -> dict:
    """T9 —— 跨仓差异看板 4 类统计。"""
    from config import BASE_DIR

    master_conn.row_factory = sqlite3.Row
    out = {
        "by_canonical": {},      # canonical_id → {wh_count, members}
        "unbound_count": 0,
        "store_exclusive_count": 0,
        "conflicts_open": 0,
    }
    bindings = collect_bindings(master_conn)
    for wh_code, rows in bindings.items():
        for r in rows:
            cid = r["canonical_id"]
            slot = out["by_canonical"].setdefault(
                cid, {"warehouses": [], "members": []}
            )
            slot["warehouses"].append(wh_code)
            slot["members"].append({"warehouse": wh_code, **r})

    # unbound
    wh_rows = master_conn.execute(
        """SELECT code, db_path FROM warehouses
           WHERE warehouse_type='storefront'"""
    ).fetchall()
    unbound = 0
    store_exclusive = 0
    for wh in wh_rows:
        db_path = Path(wh["db_path"])
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        if not db_path.exists():
            continue
        with sqlite3.connect(db_path) as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM items WHERE canonical_id IS NULL"
            ).fetchone()[0]
            unbound += cnt
            se = conn.execute(
                """SELECT COUNT(*) FROM items
                   WHERE is_store_exclusive=1"""
            ).fetchone()[0]
            store_exclusive += se
    out["unbound_count"] = unbound
    out["store_exclusive_count"] = store_exclusive

    # conflicts open
    row = master_conn.execute(
        "SELECT COUNT(*) FROM canonical_conflicts WHERE status='open'"
    ).fetchone()
    out["conflicts_open"] = int(row[0])

    return out


def list_unbound_storefront_items(master_conn: sqlite3.Connection) -> list[dict]:
    """T9 —— 列出所有仓中 canonical_id IS NULL 的品项。"""
    from config import BASE_DIR

    master_conn.row_factory = sqlite3.Row
    out: list[dict] = []
    wh_rows = master_conn.execute(
        """SELECT code, db_path FROM warehouses
           WHERE warehouse_type='storefront'"""
    ).fetchall()
    for wh in wh_rows:
        db_path = Path(wh["db_path"])
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        if not db_path.exists():
            continue
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, sku, name, unit, category_id, quantity,
                          safety_stock, selling_price, unit_cost
                   FROM items
                   WHERE canonical_id IS NULL"""
            ).fetchall()
            for r in rows:
                out.append({"warehouse_code": wh["code"], **dict(r)})
    return out


def find_orphan_bindings(master_conn: sqlite3.Connection) -> list[dict]:
    """T9 —— 找孤儿引用(canonical_id 指向不存在的 master 主数据)。

    Q3=deactivate_only 后,Q3 保证 canonical_items.status='inactive' 但不删行,
    所以"孤儿"**不会**由 Q3 产生(原 canonical_items 行还在)。
    本函数仍提供,用于发现手工改库造成的问题,不误报为缺主数据。
    """
    master_conn.row_factory = sqlite3.Row
    out: list[dict] = []
    wh_rows = master_conn.execute(
        """SELECT code, db_path FROM warehouses
           WHERE warehouse_type='storefront'"""
    ).fetchall()
    for wh in wh_rows:
        db_path = Path(wh["db_path"])
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        if not db_path.exists():
            continue
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, sku, name, canonical_id FROM items
                   WHERE canonical_id IS NOT NULL"""
            ).fetchall()
            for r in rows:
                # master 侧必须有这一行
                exists = master_conn.execute(
                    "SELECT 1 FROM canonical_items WHERE id=?",
                    (r["canonical_id"],),
                ).fetchone()
                if exists is None:
                    out.append({
                        "warehouse_code": wh["code"],
                        "local_item_id": r["id"],
                        "sku": r["sku"],
                        "name": r["name"],
                        "canonical_id": r["canonical_id"],
                    })
    return out


def list_store_exclusive_items(master_conn: sqlite3.Connection) -> list[dict]:
    """T9 —— 列出所有 is_store_exclusive=1 的行(总部巡检输入)。"""
    from config import BASE_DIR

    master_conn.row_factory = sqlite3.Row
    out: list[dict] = []
    wh_rows = master_conn.execute(
        """SELECT code, db_path FROM warehouses
           WHERE warehouse_type='storefront'"""
    ).fetchall()
    for wh in wh_rows:
        db_path = Path(wh["db_path"])
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        if not db_path.exists():
            continue
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, sku, name, unit, category_id, quantity,
                          canonical_id
                   FROM items WHERE is_store_exclusive=1"""
            ).fetchall()
            for r in rows:
                out.append({"warehouse_code": wh["code"], **dict(r)})
    return out


# ─────────────────────────────────────────────────────────────────────
#  T22 — Bulk create canonical items (CSV ingest)
# ─────────────────────────────────────────────────────────────────────

# 8 条种子数据(Spec §0.3 + §1.5)。8 = 9 条规格品项的桶装数 9 - 1 (WP0215 不创建) - 1 (WP0105 走 aux_unit)
# 简化为:8 条最常见的散装规格品项(规格品项是 M2 重点,M1 走 fixtures 验证)
SEED_DEFAULT_CANONICAL_ITEMS: list[dict[str, Any]] = [
    # name, category_code, unit, aux_unit, aux_rate
    {"name": "冰激凌成品-通用",  "category_code": "ICE_CREAM_PRODUCT", "unit": "份"},
    {"name": "包材-通用",       "category_code": "PACKAGING",         "unit": "件"},
    {"name": "辅料-通用",       "category_code": "CONSUMABLE",        "unit": "kg"},
    {"name": "调味酱-通用",     "category_code": "SAUCE",             "unit": "桶"},
    {"name": "调味酱-分装-通用","category_code": "SAUCE_FRACTION",   "unit": "罐"},
    {"name": "风味奶浆-通用",   "category_code": "CREAM_SYRUP",       "unit": "桶"},
    {"name": "乳制品-通用",     "category_code": "DAIRY",             "unit": "盒"},
    {"name": "生产消耗品-通用", "category_code": "PRODUCE_CONSUMABLE", "unit": "件"},
]


def seed_default_canonical_items(master_conn: sqlite3.Connection) -> list[dict]:
    """T22 —— 写入 8 条种子 canonical_items。

    已存在(按 name + category_code + unit 唯一组合)则跳过;幂等。
    """
    master_conn.row_factory = sqlite3.Row
    inserted: list[dict] = []
    for seed in SEED_DEFAULT_CANONICAL_ITEMS:
        existing = master_conn.execute(
            """SELECT id FROM canonical_items
               WHERE name=? AND category_code=? AND unit=?""",
            (seed["name"], seed["category_code"], seed["unit"]),
        ).fetchone()
        if existing is not None:
            continue
        created = create_canonical_item(
            master_conn,
            name=seed["name"],
            unit=seed["unit"],
            category_code=seed["category_code"],
            aux_unit=seed.get("aux_unit"),
            aux_rate=seed.get("aux_rate", 0),
            created_from="rd_manual",
        )
        inserted.append(created)
    return inserted


def claim_items_batch(
    master_conn: sqlite3.Connection,
    warehouse_code: str,
    wh_conn: sqlite3.Connection,
    *,
    rows: list[dict[str, Any]],
    submitted_by: int | None = None,
    default_is_store_exclusive: bool = True,
) -> dict:
    """批量认领 —— 主路径(T22 + T7 联合使用)。

    每行 dict 至少含 local_item_id + canonical_id;
    可选 is_alias / is_store_exclusive / local_keep_name / reason。
    返回 {success: int, skipped: int, errors: [str, ...]}。
    """
    master_conn.row_factory = sqlite3.Row
    wh_conn.row_factory = sqlite3.Row
    success = 0
    skipped = 0
    errors: list[str] = []
    for row in rows:
        try:
            claim_item(
                master_conn,
                wh_conn,
                warehouse_code=warehouse_code,
                local_item_id=int(row["local_item_id"]),
                canonical_id=int(row["canonical_id"]),
                is_alias=bool(row.get("is_alias", False)),
                is_store_exclusive=bool(row.get(
                    "is_store_exclusive", default_is_store_exclusive
                )),
                submitted_by=submitted_by,
                local_keep_name=row.get("local_keep_name"),
                reason=row.get("reason"),
            )
            success += 1
        except ValueError as e:
            msg = str(e)
            if "已被认领" in msg or "未纳管" in msg or "乐观锁失败" in msg:
                skipped += 1
            else:
                errors.append(f"local_item_id={row.get('local_item_id')}: {msg}")
        except Exception as e:
            errors.append(f"local_item_id={row.get('local_item_id')}: {e!r}")
    master_conn.commit()
    wh_conn.commit()
    return {"success": success, "skipped": skipped, "errors": errors}


def bulk_create_canonical_items(
    master_conn: sqlite3.Connection,
    csv_path: str | Path,
) -> dict:
    """T22 —— 从 CSV 灌入 canonical_items。

    CSV 列:
        warehouse_code,current_sku,item_id,current_name,current_unit,
        current_quantity,bind_to_canonical,is_store_exclusive,will_change

    实现:
      - 按 (canonical_sku='IC-PENDING-<uuid>', name, unit) 不可达(CSV 没 canonical_sku)
      - 改用 name + unit 唯一组合判定:存在 → UPDATE,不存在 → INSERT
      - 同名同单位的多个 CSV 行(同一物不同仓) → 共用同一 canonical_items 行
      - 9 条规格品项(SKU 在 SPECIAL_BULK_SKUS)按 §1.7 拆 A/B 两个 canonical
        简化版:用 (current_sku, current_name) 区分,A=桶装 B=散装
    """
    master_conn.row_factory = sqlite3.Row
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)
    inserted = 0
    updated = 0
    skipped = 0
    with csv_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = row.get("current_sku", "").strip()
            name = row.get("current_name", "").strip()
            unit = row.get("current_unit", "").strip()
            if not name or not unit:
                skipped += 1
                continue

            # 9 条规格品项的特殊处理(§1.7.3):
            # 用 (sku, name) 作为分组键 —— 同 SKU 的不同 name 会得到不同 canonical
            is_special = sku in SPECIAL_BULK_SKUS

            # 查询现有
            if is_special:
                existing = master_conn.execute(
                    """SELECT id FROM canonical_items
                       WHERE name=? AND unit=?""",
                    (name, unit),
                ).fetchone()
            else:
                existing = master_conn.execute(
                    """SELECT id FROM canonical_items
                       WHERE name=? AND unit=?""",
                    (name, unit),
                ).fetchone()

            if existing is not None:
                updated += 1
            else:
                # 推一个 category_code
                cat_code = _infer_category_code(name)
                create_canonical_item(
                    master_conn,
                    name=name,
                    unit=unit,
                    category_code=cat_code,
                    created_from="rd_manual",
                )
                inserted += 1
    master_conn.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def _infer_category_code(name: str) -> str | None:
    """极简分类推断。生产中 T22 接收的 CSV 通常已包含品类列,
    这里仅在 CSV 缺品类时做关键字兜底,**不参与主路径**。
    """
    name_l = name or ""
    if "冰激凌" in name_l or "冰淇凌" in name_l:
        return "ICE_CREAM_PRODUCT"
    if "酱" in name_l:
        return "SAUCE"
    if "奶" in name_l:
        return "DAIRY"
    if "糖" in name_l or "粉" in name_l:
        return "CONSUMABLE"
    if "盒" in name_l or "碗" in name_l or "托" in name_l or "盖" in name_l:
        return "PACKAGING"
    return None


# ─────────────────────────────────────────────────────────────────────
#  T25 — Canonical align CLI (issue #11, 2026-10-05)
#
#  对齐全流程的 CLI 支撑层：
#    align-seed   → seed_canonical_categories + seed_default_canonical_items
#    align-detect → dry_run_report()（纯只读）
#    align-apply  → fanout_canonical_items(dry_run=False)（危险,双确认）
#  命令本体在 cli.py；范围常量与报告格式化放本文件（单一真相源）。
# ─────────────────────────────────────────────────────────────────────

# v4 冻结的对齐范围（docs/2026-10-03-canonical-item-design.md §6）。
# wh_001(已关) / wh_010(空) 不进对齐范围。
# v5（2026-10-09，方案 P0-7）纳入 rd_001：研发品项是主数据的镜像副本，
# 必须跟随主数据更新；依据 docs/2026-10-09-item-master-unify-plan.md。
ALIGN_SCOPE_WAREHOUSES: tuple[str, ...] = (
    "wh_000", "wh_002", "wh_003", "wh_004", "wh_006", "rd_001",
)


# ─────────────────────────────────────────────────────────────────────
#  T26 — bulk-publish-canonical (issue #14, 2026-10-09)
#
#  ops 救场工具：DC 仓已有真实 inventory 但全部 canonical_id IS NULL，
#  storefront catalog 因此被 WHERE canonical_id IS NOT NULL 过滤掉。
#  本函数一次扫整个 wh_XXX.items，按 (name, unit) 复用 master.canonical_items
#  已有行 / 推 category_code + 两阶段创，回写 items.canonical_id 绑定。
#  替代 ops 写 ad-hoc /tmp/publish_wh000_to_canonical.py 救场。
# ─────────────────────────────────────────────────────────────────────


def bulk_publish_canonical_items(
    master_conn: sqlite3.Connection,
    wh_conn: sqlite3.Connection,
    *,
    dc_warehouse_code: str,
    include_bound: bool = False,
    dry_run: bool = False,
) -> dict:
    """T26 —— 把 wh_XXX.items 批量 publish 到 master.canonical_items（issue #14）。

    默认行为：只处理 ``canonical_id IS NULL`` 的行（--only-no-canonical）。
    ``include_bound=True`` 时扫全部行，但已绑的行会被 WHERE 子句过滤掉
    （保持幂等：不会重复创建 canonical_items）。

    分类映射策略（按 issue #14 期望）：
      - 拿 items.category_id → wh.categories.name
      - 按 name 查 master.canonical_categories.name → 拿 code
      - 拿不到（罕见，name 漂移）→ 用 _infer_category_code(name) 兜底
      - 兜底还拿不到 → category_code=None（canonical_items 允许 NULL）

    重复判定：(name, unit) 命中 master.canonical_items 现有行 → 复用；
    未命中 → create_canonical_item()（两阶段 SKU 写入）。

    事务：调用方负责持 master_conn + wh_conn 的事务；失败时调用方
    ROLLBACK 两个连接。本函数只 INSERT/UPDATE 不 COMMIT。

    Returns:
        {
          "scanned":  int,    # 扫到 wh.items 行数
          "created":  int,    # 新建 canonical_items 行数
          "reused":   int,    # 复用现有 canonical_items 行数
          "linked":   int,    # 回写 items.canonical_id 的行数
          "skipped":  int,    # 因已绑/异常跳过的行数
          "dry_run":  bool,
          "no_category_code": int,  # 拿不到 category_code 的行数(供审查)
        }
    """
    master_conn.row_factory = sqlite3.Row
    wh_conn.row_factory = sqlite3.Row

    # 1) 扫目标 wh 仓 items
    where_clause = "" if include_bound else "WHERE canonical_id IS NULL"
    rows = wh_conn.execute(
        f"""SELECT id, sku, name, unit, category_id, canonical_id
            FROM items
            {where_clause}
            ORDER BY id""",
    ).fetchall()

    # 2) 拿 wh 本地 category_id → name 映射(单次查询)
    cat_rows = wh_conn.execute("SELECT id, name FROM categories").fetchall()
    wh_cat_name_by_id = {int(r["id"]): str(r["name"]) for r in cat_rows}

    # 3) 拿 master canonical_categories (name → code)
    master_cats = master_conn.execute(
        "SELECT code, name FROM canonical_categories"
    ).fetchall()
    cat_code_by_name = {str(r["name"]): str(r["code"]) for r in master_cats}

    scanned = len(rows)
    created = 0
    reused = 0
    linked = 0
    skipped = 0
    no_category_code = 0

    for r in rows:
        item_id = int(r["id"])
        if r["canonical_id"] is not None and not include_bound:
            # 已被绑定的行(理论不会进 WHERE canonical_id IS NULL,但 include_bound=False 防御一下)
            skipped += 1
            continue
        name = str(r["name"])
        unit = str(r["unit"]) if r["unit"] else "件"
        if not name.strip():
            skipped += 1
            continue

        # category_code 推算
        wh_cat_name = wh_cat_name_by_id.get(int(r["category_id"]) if r["category_id"] is not None else 0)
        cat_code: str | None = None
        if wh_cat_name and wh_cat_name in cat_code_by_name:
            cat_code = cat_code_by_name[wh_cat_name]
        else:
            cat_code = _infer_category_code(name)
        if cat_code is None:
            no_category_code += 1

        # 查 (name, unit) 复用
        existing = master_conn.execute(
            "SELECT id FROM canonical_items WHERE name=? AND unit=?",
            (name, unit),
        ).fetchone()
        if existing is not None:
            canonical_id = int(existing["id"])
            reused += 1
        else:
            if dry_run:
                # dry-run 不创
                created += 1
                continue
            new_row = create_canonical_item(
                master_conn,
                name=name,
                unit=unit,
                category_code=cat_code,
                created_from="rd_publish",  # 标记为 publish 路径产物
            )
            canonical_id = int(new_row["id"])
            created += 1

        if dry_run:
            linked += 1
            continue

        # 回写 wh.items.canonical_id
        cur = wh_conn.execute(
            "UPDATE items SET canonical_id=? WHERE id=? AND canonical_id IS NULL",
            (canonical_id, item_id),
        )
        if cur.rowcount > 0:
            linked += 1
        else:
            # 已被并发绑定 → 算 reuse
            skipped += 1

    return {
        "scanned": scanned,
        "created": created,
        "reused": reused,
        "linked": linked,
        "skipped": skipped,
        "dry_run": dry_run,
        "no_category_code": no_category_code,
        "dc_warehouse_code": dc_warehouse_code,
    }


def dry_run_report(
    master_conn: sqlite3.Connection,
    target_wh_codes: tuple[str, ...] | None = None,
) -> str:
    """T25 —— 跨仓同物候选 dry-run 报告（人类可读,纯只读,不写任何表）。

    输出三段:
      1. 各仓现状: items 总数 / canonical_id 覆盖数与覆盖率
      2. 同物候选分组: 成员(仓+sku+name+unit) + confidence + signals
      3. 推荐操作汇总

    范围: 默认 ALIGN_SCOPE_WAREHOUSES,且只取 warehouse_type='storefront'
    （rd_001 自动排除,与 collect_all_items 同语义）。

    返回 str——不直接 print,让 CLI 命令决定 stdout / --out <file>。
    """
    from config import BASE_DIR  # call-time import：测试 monkeypatch 生效

    scope = tuple(target_wh_codes) if target_wh_codes else ALIGN_SCOPE_WAREHOUSES
    master_conn.row_factory = sqlite3.Row

    lines: list[str] = []
    ts = _now_str()
    lines.append(f"Canonical 对齐 dry-run 报告  ({ts})")
    lines.append(f"范围(冻结): {', '.join(scope)}")
    lines.append("=" * 62)

    # ── 1. 各仓现状 ──────────────────────────────────────────────────
    wh_rows = master_conn.execute(
        """SELECT code, db_path FROM warehouses
           WHERE warehouse_type='storefront' ORDER BY code"""
    ).fetchall()
    rows_per_wh: dict[str, list[dict[str, Any]]] = {}
    coverage: dict[str, tuple[int, int]] = {}  # code -> (total, bound)

    for wh in wh_rows:
        code = wh["code"]
        if code not in scope:
            continue
        db_path = Path(wh["db_path"])
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        if not db_path.exists():
            lines.append(f"[{code}] db 缺失({db_path}),跳过")
            continue
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cols = select_item_columns(conn)
            want = [c for c in ("sku", "name", "unit") if c in cols]
            select_clause = ", ".join(want)
            rows = conn.execute(
                f"SELECT id, {select_clause} FROM items"
            ).fetchall()
            item_rows = [dict(r) for r in rows]
            # canonical_id 列可能缺失(旧仓尚未迁移):纯只读模式不做迁移、不写表,
            # 缺失列按「已纳管 0」处理,维持 align-detect 的只读契约。
            bound = 0
            if "canonical_id" in cols:
                bound = conn.execute(
                    "SELECT COUNT(*) FROM items WHERE canonical_id IS NOT NULL"
                ).fetchone()[0]
        rows_per_wh[code] = item_rows
        total = len(item_rows)
        coverage[code] = (total, int(bound))

    lines.append("")
    lines.append("【1】各仓现状")
    if not coverage:
        lines.append("  范围内没有可用的 storefront 仓库。")
    for code in scope:
        if code not in coverage:
            continue
        total, bound = coverage[code]
        pct = (bound / total * 100) if total else 0.0
        lines.append(
            f"  {code}: items={total}, 已纳管={bound} (覆盖率 {pct:.1f}%)"
        )

    # ── 2. 同物候选分组 ──────────────────────────────────────────────
    groups = detect_similar_items(rows_per_wh)
    lines.append("")
    lines.append(f"【2】同物候选分组 (共 {len(groups)} 组)")
    if not groups:
        lines.append("  未检测到跨仓相似候选（阈值 confidence>=0.5）。")
    for i, g in enumerate(groups, 1):
        lines.append(
            f"  组{i} unit={g['unit']!r} confidence={g['confidence']} "
            f"signals={g['signals']}"
        )
        for m in g["members"]:
            lines.append(
                f"    - {m['_warehouse_code']}/{m.get('sku', '?')}: "
                f"{m.get('name', '?')} ({m.get('unit', '?')})"
            )

    # ── 3. 推荐操作 ──────────────────────────────────────────────────
    lines.append("")
    lines.append("【3】推荐操作")
    lines.append("  - 新主数据: 对确认同物的组,先在 /canonical 页面创建")
    lines.append("    canonical_items,再把各仓成员认领过去。")
    lines.append("  - 已有主数据未认领: 用 /canonical/claim 或批量认领页绑定。")
    lines.append("  - 确认无误后执行:")
    lines.append("    flask --app app align-apply --canonical-ids <ids> "
                "--warehouses <codes> [--action overwrite|merge|keep] --yes")
    lines.append("  本报告为只读 dry-run,未写任何表。")
    lines.append("")
    return "\n".join(lines)
