"""Pure logic for the user-feedback (问题反馈) ticket board.

Spec: docs/ARCHITECTURE.md §3.10（反馈闭环）.

设计约定：
- 纯函数：无 Flask、无网络。所有 DB 操作接收已打开的 ``sqlite3.Connection``。
- 数据落 master.db —— 反馈是「系统级」问题，不属于任何单个仓库。
- 校验失败一律 raise ``ValueError``（沿用 notifications_pure 的范式），
  且**不写库**，调用方可先校验再落库。
- 状态机极简（三态）：open → ignored | dispatched，均为终态，不可回退。
  不引入「已解决」态：真解决了是 GitHub Issue closed，以那为准，别造两套真相。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

# 反馈分类：给管理员分流用，不参与权限判定。
CATEGORIES: dict[str, str] = {
    "bug": "功能异常",
    "usability": "用起来别扭",
    "data": "数据不对",
    "other": "其他",
}
DEFAULT_CATEGORY = "other"

STATUS_OPEN = "open"
STATUS_IGNORED = "ignored"
STATUS_DISPATCHED = "dispatched"
ALL_STATUSES = frozenset({STATUS_OPEN, STATUS_IGNORED, STATUS_DISPATCHED})
STATUS_LABELS: dict[str, str] = {
    STATUS_OPEN: "待处理",
    STATUS_IGNORED: "已忽略",
    STATUS_DISPATCHED: "已派发开发",
}

TITLE_MAX_LEN = 120
BODY_MAX_LEN = 4000
COMMENT_MAX_LEN = 2000
PAGE_PATH_MAX_LEN = 200

_LIST_LIMIT = 200
_TICKET_NO_ATTEMPTS = 5


def _now() -> str:
    """本地时间 ``YYYY-MM-DD HH:MM:SS``（与全站落库口径一致）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def normalize_category(value: Any) -> str:
    """非法/缺失分类回落到 ``other``（分类不参与鉴权，宽进更友好）。"""
    text = str(value or "").strip().lower()
    return text if text in CATEGORIES else DEFAULT_CATEGORY


def normalize_page_path(value: Any) -> str:
    """只接受站内相对路径（``/items`` 形态）；其余一律丢弃。

    这是安全边界而非洁癖：该字段会进企微 markdown 与 GitHub Issue 正文，
    若允许 ``http://evil`` 或 ``javascript:`` 进来，就是一条注入通道。
    """
    text = str(value or "").strip()
    if not text or len(text) > PAGE_PATH_MAX_LEN:
        return ""
    if not text.startswith("/") or text.startswith("//"):
        return ""
    if "://" in text or "\\" in text:
        return ""
    # 去掉控制字符（含换行）——issue/markdown 正文里换行有语义
    cleaned = "".join(ch for ch in text if ch.isprintable())
    return cleaned if cleaned.startswith("/") else ""


def validate_ticket(title: Any, body: Any) -> tuple[str, str]:
    """校验标题与正文，返回 去空白后的 ``(title, body)``；非法 raise ValueError。"""
    t = str(title or "").strip()
    b = str(body or "").strip()
    if not t:
        raise ValueError("title 不能为空")
    if len(t) > TITLE_MAX_LEN:
        raise ValueError(f"title 超过 {TITLE_MAX_LEN} 字")
    if not b:
        raise ValueError("body 不能为空")
    if len(b) > BODY_MAX_LEN:
        raise ValueError(f"body 超过 {BODY_MAX_LEN} 字")
    return t, b


def validate_comment(body: Any) -> str:
    """校验管理员评论，返回去空白后的正文；非法 raise ValueError。"""
    text = str(body or "").strip()
    if not text:
        raise ValueError("评论不能为空")
    if len(text) > COMMENT_MAX_LEN:
        raise ValueError(f"评论超过 {COMMENT_MAX_LEN} 字")
    return text


# ---------------------------------------------------------------------------
# 单据号
# ---------------------------------------------------------------------------

def build_ticket_no(day: str, seq: int) -> str:
    """``FB-20261017-0001``。``day`` 形如 ``YYYYMMDD``（8 位）。"""
    return f"FB-{day}-{int(seq):04d}"


def next_ticket_no(db: sqlite3.Connection, now: str | None = None) -> str:
    """按当天已有最大序号 +1 生成单据号（调用方需处理 UNIQUE 冲突重试）。"""
    stamp = (now or _now())[:10].replace("-", "")
    prefix = f"FB-{stamp}-"
    row = db.execute(
        "SELECT ticket_no FROM feedback_tickets WHERE ticket_no LIKE ? "
        "ORDER BY ticket_no DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    seq = 1
    if row is not None:
        tail = str(row["ticket_no"]).rsplit("-", 1)[-1]
        if tail.isdigit():
            seq = int(tail) + 1
    return build_ticket_no(stamp, seq)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def create_ticket(
    db: sqlite3.Connection,
    user_id: int,
    title: str,
    body: str,
    category: Any = DEFAULT_CATEGORY,
    warehouse_code: str | None = None,
    page_path: Any = "",
) -> dict[str, Any]:
    """新建反馈单，返回新单据 dict。

    单据号靠「查当天最大 +1」，并发下可能撞 UNIQUE —— 捕获后重算重试
    （写多读少的场景，重试比引入序列表划算）。重试耗尽才向上抛。
    """
    title, body = validate_ticket(title, body)
    cat = normalize_category(category)
    now = _now()
    last_exc: Exception | None = None
    for _ in range(_TICKET_NO_ATTEMPTS):
        ticket_no = next_ticket_no(db, now)
        try:
            cur = db.execute(
                """INSERT INTO feedback_tickets
                   (ticket_no, user_id, warehouse_code, page_path, category,
                    title, body, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ticket_no, int(user_id), warehouse_code or None,
                 normalize_page_path(page_path), cat, title, body,
                 STATUS_OPEN, now, now),
            )
            db.commit()
            return get_ticket(db, int(cur.lastrowid)) or {}
        except sqlite3.IntegrityError as exc:
            # 只可能是 ticket_no 撞车；别的完整性错误重试也没用，直接抛。
            db.rollback()
            if "ticket_no" not in str(exc):
                raise
            last_exc = exc
    raise ValueError(f"生成反馈单号失败（重试 {_TICKET_NO_ATTEMPTS} 次）") from last_exc


def add_comment(
    db: sqlite3.Connection,
    ticket_id: int,
    user_id: int,
    body: str,
    is_admin: bool = False,
    dispatched: bool = False,
) -> int:
    """追加一条处理评论，返回评论 id。``dispatched=True`` 标记「这条触发了派发」。"""
    text = validate_comment(body)
    now = _now()
    cur = db.execute(
        """INSERT INTO feedback_comments
           (ticket_id, user_id, body, is_admin, dispatched_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (int(ticket_id), int(user_id), text, 1 if is_admin else 0,
         now if dispatched else None, now),
    )
    db.execute(
        "UPDATE feedback_tickets SET updated_at=? WHERE id=?",
        (now, int(ticket_id)),
    )
    db.commit()
    return int(cur.lastrowid)


def mark_comment_dispatched(db: sqlite3.Connection, comment_id: int) -> bool:
    """把某条评论标记为「触发了派发」。

    单独一个函数而不是塞进 ``add_comment``：评论必须先落库（管理员写的字
    一个字都不能丢），Issue 建成之后才能标这个时点。
    """
    now = _now()
    cur = db.execute(
        "UPDATE feedback_comments SET dispatched_at=? WHERE id=? AND dispatched_at IS NULL",
        (now, int(comment_id)),
    )
    db.commit()
    return cur.rowcount > 0


def mark_ignored(db: sqlite3.Connection, ticket_id: int, admin_id: int) -> bool:
    """标记忽略（终态）。只对 ``open`` 生效，已处理过的返回 False。"""
    now = _now()
    cur = db.execute(
        """UPDATE feedback_tickets
           SET status=?, handled_by=?, handled_at=?, updated_at=?
           WHERE id=? AND status=?""",
        (STATUS_IGNORED, int(admin_id), now, now, int(ticket_id), STATUS_OPEN),
    )
    db.commit()
    return cur.rowcount > 0


def attach_issue(
    db: sqlite3.Connection,
    ticket_id: int,
    issue_number: int,
    issue_url: str,
    admin_id: int | None = None,
) -> bool:
    """把反馈单与 GitHub Issue 绑定并置为 ``dispatched``（终态）。

    幂等：已有 ``issue_number`` 时不覆盖、不再派发（防止重复点「发送」造重复单）。
    """
    now = _now()
    cur = db.execute(
        """UPDATE feedback_tickets
           SET status=?, issue_number=?, issue_url=?,
               handled_by=COALESCE(?, handled_by),
               handled_at=?,
               updated_at=?
           WHERE id=? AND issue_number IS NULL""",
        (STATUS_DISPATCHED, int(issue_number), issue_url,
         int(admin_id) if admin_id is not None else None,
         now, now, int(ticket_id)),
    )
    db.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

_COLUMNS = (
    "id, ticket_no, user_id, warehouse_code, page_path, category, title, body, "
    "status, issue_number, issue_url, created_at, updated_at, handled_by, handled_at"
)


def get_ticket(db: sqlite3.Connection, ticket_id: int) -> dict[str, Any] | None:
    row = db.execute(
        f"SELECT {_COLUMNS} FROM feedback_tickets WHERE id=?",
        (int(ticket_id),),
    ).fetchone()
    return _ticket_dict(row) if row is not None else None


def get_ticket_by_no(db: sqlite3.Connection, ticket_no: str) -> dict[str, Any] | None:
    row = db.execute(
        f"SELECT {_COLUMNS} FROM feedback_tickets WHERE ticket_no=?",
        (str(ticket_no or "").strip(),),
    ).fetchone()
    return _ticket_dict(row) if row is not None else None


def list_for_user(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """当前用户自己提交的反馈（新的在前）。"""
    rows = db.execute(
        f"SELECT {_COLUMNS} FROM feedback_tickets WHERE user_id=? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (int(user_id), _LIST_LIMIT),
    ).fetchall()
    return [_ticket_dict(r) for r in rows]


def list_for_admin(
    db: sqlite3.Connection,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """管理员收件箱。``status`` 为空 = 全部；非法状态按「全部」处理。"""
    sql = (
        "SELECT t.id, t.ticket_no, t.user_id, t.warehouse_code, t.page_path, "
        "t.category, t.title, t.body, t.status, t.issue_number, t.issue_url, "
        "t.created_at, t.updated_at, t.handled_by, t.handled_at, "
        "u.username AS submitter "
        "FROM feedback_tickets t LEFT JOIN users u ON u.id = t.user_id"
    )
    params: list[Any] = []
    if status in ALL_STATUSES:
        sql += " WHERE t.status=?"
        params.append(status)
    sql += " ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END, t.created_at DESC, t.id DESC LIMIT ?"
    params.append(_LIST_LIMIT)
    rows = db.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        item = _ticket_dict(r)
        item["submitter"] = r["submitter"] if "submitter" in r.keys() else None
        out.append(item)
    return out


def count_open(db: sqlite3.Connection) -> int:
    """待处理数量（导航角标用）。"""
    row = db.execute(
        "SELECT COUNT(*) AS n FROM feedback_tickets WHERE status=?",
        (STATUS_OPEN,),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def list_comments(db: sqlite3.Connection, ticket_id: int) -> list[dict[str, Any]]:
    """某单据的处理评论（时间正序）。"""
    rows = db.execute(
        """SELECT c.id, c.ticket_id, c.user_id, c.body, c.is_admin,
                  c.dispatched_at, c.created_at, u.username
           FROM feedback_comments c LEFT JOIN users u ON u.id = c.user_id
           WHERE c.ticket_id=? ORDER BY c.id""",
        (int(ticket_id),),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "ticket_id": r["ticket_id"],
            "user_id": r["user_id"],
            "username": r["username"],
            "body": r["body"],
            "is_admin": bool(r["is_admin"]),
            "dispatched_at": r["dispatched_at"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _ticket_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "ticket_no": row["ticket_no"],
        "user_id": row["user_id"],
        "warehouse_code": row["warehouse_code"],
        "page_path": row["page_path"],
        "category": row["category"],
        "category_label": CATEGORIES.get(row["category"], CATEGORIES[DEFAULT_CATEGORY]),
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "status_label": STATUS_LABELS.get(row["status"], row["status"]),
        "issue_number": row["issue_number"] if "issue_number" in keys else None,
        "issue_url": row["issue_url"] if "issue_url" in keys else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "handled_by": row["handled_by"] if "handled_by" in keys else None,
        "handled_at": row["handled_at"] if "handled_at" in keys else None,
    }


# ---------------------------------------------------------------------------
# 渲染（企微 markdown / GitHub Issue 正文共用）
# ---------------------------------------------------------------------------

def summarize(title: str, limit: int = 60) -> str:
    """单行摘要：压掉换行，超长截断加省略号。"""
    text = " ".join(str(title or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_admin_user_ids(db: sqlite3.Connection) -> list[int]:
    """所有平台超管的 user_id（站内通知扇出用）。

    口径与 ``permissions.require_platform_admin`` 一致：看 ``users.is_admin = 1``。
    没有超管时返回空列表 —— 调用方据此跳过扇出，绝不凭空造一个收件人。
    """
    rows = db.execute(
        "SELECT id FROM users WHERE is_admin = 1 ORDER BY id"
    ).fetchall()
    return [int(r["id"]) for r in rows]
