"""Central configuration. Read by app factory and blueprints.

Multi-warehouse model: one master.db (users/warehouses/permissions) +
one SQLite file per warehouse under db/warehouses/.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# DB 根目录覆盖（Issue #28 蓝绿 DB 隔离）：
# 绿进程的 systemd unit 设 Environment=DAILYCHECK_DB_DIR=<绿库目录> 后，
# 绿读写的是 up.sh cp -aL 出来的独立副本，蓝绿不再共享同一物理库。
# 必须在 import 期读取（db/__init__.py 与 mcp_server/config.py 都是
# `from config import MASTER_DB` 的 import 期值绑定）。
# 未设置时保持原行为（repo 内 db/）——蓝进程与所有现有部署不受影响。
_DB_DIR_ENV = os.environ.get("DAILYCHECK_DB_DIR")
DB_DIR = Path(_DB_DIR_ENV) if _DB_DIR_ENV else BASE_DIR / "db"
MASTER_DB = DB_DIR / "master.db"
WAREHOUSE_DB_DIR = DB_DIR / "warehouses"

IS_PRODUCTION = os.environ.get("DAILYCHECK_ENV") == "production"
SECRET_KEY = os.environ.get("DAILYCHECK_SECRET_KEY")
# Known-insecure values that must never be accepted in production.
_WEAK_SECRET_KEYS = frozenset({"dev-key-change-me", "dev-secret-key-not-for-prod"})
if IS_PRODUCTION and (not SECRET_KEY or SECRET_KEY in _WEAK_SECRET_KEYS):
    raise RuntimeError(
        "DAILYCHECK_SECRET_KEY must be set to a strong, non-default value "
        "when DAILYCHECK_ENV=production"
    )
if not SECRET_KEY:
    # Dev/test fallback only — production is guarded above.
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

# CATEGORY_CODE_MAP — Spec §6.1: 9 个 FIXED_CATEGORIES 手写映射到稳定的
# canonical_categories.code。**不做通用 slugify**（理由见 §6.1）。
# 命名约定: <大类>_<小类>（全大写 + 下划线）。门店可能持有的本地分类若
# 不在本表里,进 list_missing_category_mappings() 走人工映射,不自动猜测。
CATEGORY_CODE_MAP = {
    "包材":       "PACKAGING",
    "辅料":       "CONSUMABLE",
    "调味酱":     "SAUCE",
    "调味酱 分":  "SAUCE_FRACTION",
    "风味奶浆":   "CREAM_SYRUP",
    "乳制品":     "DAIRY",
    "生产消耗品": "PRODUCE_CONSUMABLE",
    "生产工具":   "PRODUCE_TOOL",
    "冰激凌成品": "ICE_CREAM_PRODUCT",
}

# Role rank for require_role().
ROLE_RANK = {"staff": 1, "manager": 2, "admin": 3}

# agent_tokens.token_prefix length — the plaintext head kept for an indexed
# lookup, so authentication is one row + one pbkdf2 check instead of a
# full-table scan. MUST match mcp_server.service.auth.TOKEN_PREFIX_LEN.
AGENT_TOKEN_PREFIX_LEN = 8

# Warehouse types used across the platform.
WAREHOUSE_TYPE_STOREFRONT: str = "storefront"
WAREHOUSE_TYPE_RD: str = "rd"
WAREHOUSE_TYPE_DC: str = "distribution_center"

# ─────────────────────────────────────────────────────────────────────
# Canonical item policy switches (Q1-Q4 + Q7).
#
# Spec: docs/2026-10-03-canonical-item-design.md §1.5, §1.6, §1.8, §7.7.
# M1 scope: the switches below are Eric's v4 decisions.
# Q6 (selling_price ownership) 于 2026-10-09 拍板为 "canonical_managed"：
# 售价/采购价收归主数据（/canonical 维护 + 扇出下发），门店/rd 只读。
# 见 docs/2026-10-09-item-master-unify-plan.md §3 P0-9 / §5-A。
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

    # Q6 — 售价/采购价归属（2026-10-09 拍板：收归主数据统一维护并扇出下发）
    "q6_price_ownership": "canonical_managed",

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

# 付款凭证（Issue #135）：门店提交订单时上传，财务审批时查看。
# 文件按 <dir>/<order_id>/<uuid>.<ext> 存放；蓝绿两实例共用 BASE_DIR，
# 因此凭证天然共享。目录不进 git（见 .gitignore）。
PAYMENT_PROOF_DIR = BASE_DIR / "uploads" / "payment_proofs"


# ─────────────────────────────────────────────────────────────────────
# 安全库存自动计算（2026-10-09 Eric 拍板，方案 P0-12）
#
#   safety_stock = Σ 近 WINDOW_DAYS 天消耗量 × FACTOR
#
# 消耗口径与 /inventory 页一致（outbound_requests 排除生产领料行 +
# production_run_items），详见 blueprints/items_pure.py。
# 品项的消耗历史覆盖不满 WINDOW_DAYS 窗口时写 0（数据不足以代表一周）。
# 该字段对全角色只读，扇出（canonical fanout）永不覆盖。
# ─────────────────────────────────────────────────────────────────────
SAFETY_STOCK_WINDOW_DAYS = 7
SAFETY_STOCK_FACTOR = 1.2
SAFETY_STOCK_ROUND_DIGITS = 2


# ─────────────────────────────────────────────────────────────────────
# 外部通知通道（企业微信群机器人，2026-10-16 起）
#
# 门店提交订货单时推一条 markdown 到工作群，见
# blueprints/wecom_notify_pure.py 与 docs/ARCHITECTURE.md §3.9。
#
# - 两个值都由**服务器 env 注入**（生产写 /etc/dailycheck/app.env），
#   **不进 git**：webhook 的 key= 等同密钥，泄露即可被任意人往群里发消息。
# - WECOM_STORE_ORDER_WEBHOOK 为空 = 功能关闭（代码静默跳过，不报错）。
# - PUBLIC_BASE_URL 用于拼消息里的「查看单据」链接（群里点得开 = 可外网
#   访问的域名，不是 127.0.0.1）。
# ─────────────────────────────────────────────────────────────────────
WECOM_STORE_ORDER_WEBHOOK = os.environ.get("WECOM_STORE_ORDER_WEBHOOK", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://kjtang.cn").rstrip("/")

# ─────────────────────────────────────────────────────────────────────
# 用户反馈闭合通道（2026-10-17）
#
# 用户在页面提交「使用问题」→ 企微群机器人推一条 → 管理员判定：
#   忽略（终态） / 加评论并发起 GitHub Issue（终态，进开发闭环）。
# 见 blueprints/feedback_pure.py 与 docs/ARCHITECTURE.md §3.10。
#
# - WECOM_FEEDBACK_WEBHOOK：反馈专用 webhook，**为空 = 不推企微**
#   （站内通知仍然照发，不提消息通道就丢数据）。
#   独立于订货单 webhook，方便「反馈消息太吵」时单独静音。
# - FEEDBACK_GITHUB_TOKEN：建 Issue 用的 PAT（repo scope）。
#   **为空 = 不建 Issue**：路由降级为只存评论 + 提示管理员去手动建单，
#   绝不伪装成功。
# - FEEDBACK_GITHUB_REPO：owner/name。默认主仓。
# ─────────────────────────────────────────────────────────────────────
WECOM_FEEDBACK_WEBHOOK = os.environ.get("WECOM_FEEDBACK_WEBHOOK", "").strip()
FEEDBACK_GITHUB_TOKEN = os.environ.get("FEEDBACK_GITHUB_TOKEN", "").strip()
FEEDBACK_GITHUB_REPO = os.environ.get(
    "FEEDBACK_GITHUB_REPO", "ericmr1981/DailyCheck"
).strip()

# ─────────────────────────────────────────────────────────────────────
# Owner 拍板队列（2026-10-21）
#
# Prod Agent 命中红线后，不再只开一张 GitHub issue 等人发现，而是登记到
# owner_approvals 表 → 推企微反馈通知群 → owner 在 /ops/approvals 一键批。
# 见 blueprints/owner_approvals_pure.py 与 docs/ARCHITECTURE.md §3.12。
#
# - OPS_AGENT_TOKEN：Prod Agent 调登记/消费接口用的共享令牌。
#   **为空 = 两个 API 全部 403**（默认关闭，宁可页面上没数据，
#   也不能让未鉴权的请求往队列里塞东西）。站内页面不受影响。
#   放在 /etc/dailycheck/ops-agent.env，与用户 token、MCP token 互不复用。
# ─────────────────────────────────────────────────────────────────────
OPS_AGENT_TOKEN = os.environ.get("OPS_AGENT_TOKEN", "").strip()


# ─────────────────────────────────────────────────────────────────────
# 周度采购建议邮件（企业微信邮箱 SMTP，2026-10-21 起）
#
# 每周二把「配送中心本周该采什么、采多少、各门店还有多少、近 7 天用了
# 多少」发到指定邮箱。见 blueprints/procurement_digest.py 与
# docs/ARCHITECTURE.md §3.13。
#
# - 全部凭证由服务器 env 注入（生产写 /etc/dailycheck/app.env），**不进
#   git**：SMTP 授权码等同账号密码。
# - DIGEST_MAIL_TO / USER / PASSWORD 任一为空 = 功能关闭：调度线程照常跑
#   但直接跳过并留一条 WARNING，绝不半途发一封缺附件缺数据的残邮件。
# - 企业微信邮箱（腾讯 exmail）默认 smtp.exmail.qq.com:465（SSL）。
#   465 走 SMTP_SSL，其它端口走明文 + STARTTLS。
# - DIGEST_WEEKDAY 用 Python 口径：0=周一 … 6=周日，默认 1=周二。
# - DIGEST_HOUR 为服务器本地小时（生产 CST）。整点内每 60s 轮询一次，
#   由每日 sentinel 文件保证当天只发一封。
# ─────────────────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    """env 取整数：非法值回退默认，**绝不在 import 期抛**。

    config 是 import 期求值，一个手滑的 `DIGEST_MAIL_PORT=465 ` 或空串
    就能让整个 app 起不来 —— 邮件是锦上添花，不值得这个代价。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DIGEST_MAIL_HOST = (os.environ.get("DIGEST_MAIL_HOST") or "smtp.exmail.qq.com").strip()
DIGEST_MAIL_PORT = _env_int("DIGEST_MAIL_PORT", 465)
DIGEST_MAIL_USER = (os.environ.get("DIGEST_MAIL_USER") or "").strip()
DIGEST_MAIL_PASSWORD = os.environ.get("DIGEST_MAIL_PASSWORD") or ""
DIGEST_MAIL_FROM = (os.environ.get("DIGEST_MAIL_FROM") or "").strip() or DIGEST_MAIL_USER
DIGEST_MAIL_TO = (os.environ.get("DIGEST_MAIL_TO") or "").strip()
DIGEST_WEEKDAY = _env_int("DIGEST_WEEKDAY", 1)
DIGEST_HOUR = _env_int("DIGEST_HOUR", 8)
