"""Pure functions and policy constants for the canonical-items feature.

Spec: docs/2026-10-03-canonical-item-design.md
     §1.3 (canonical_synced_json scope)
     §1.5 (Q6 price ownership branching)
     §1.6 (Q1=deny + whitelist + three-way-out)
     §1.8 (unit family — aux_unit/aux_rate/gram_per_unit derivation)
     §2.4 (field permission matrix → CANONICAL_FIELD_POLICY)
     §2.5 (canonical_status state machine — Q3=deactivate_only)
     §3.5 (fanout conflict judgment — Q4=freeze)
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
    ALLOWED_TABLES, ALLOWED_WHERE_COLUMNS

  T1 — policy switches:
    is_syncable_field(field) -> bool
    check_new_item_policy(name, canonical_id=None, category_code=None,
                          similar_canonicals=None) -> dict
    set_canonical_status(canonical_id, status) -> str
    resolve_conflict_policy(local_value, last_synced_value,
                            canonical_value, force=False) -> str

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
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

# Import policy switches and paths from config so a single file controls
# everything (no other file owns its own whitelist — see module docstring).
from config import (
    BASE_DIR,
    BACKUP_WAREHOUSE_DIR,
    CANONICAL_POLICY,
    DRYRUN_COPY_DIR,
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