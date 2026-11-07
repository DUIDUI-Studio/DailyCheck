"""Pure logic for the user-feedback (问题反馈) ticket board.

Spec: docs/ARCHITECTURE.md §3.10（反馈闭环）.

设计约定：
- 纯函数：无 Flask、无网络。所有 DB 操作接收已打开的 ``sqlite3.Connection``。
- 数据落 master.db —— 反馈是「系统级」问题，不属于任何单个仓库。
- 校验失败一律 raise ``ValueError``（沿用 notifications_pure 的范式），
  且**不写库**，调用方可先校验再落库。
- 例外：**单号生成重试耗尽** raise ``TicketNoExhaustedError``（RuntimeError 子类，
  非 ValueError）—— 那是服务端瞬时故障，不是用户输入问题，端点须回 5xx 可重试，
  不能被 ``except ValueError → 400`` 误吞（2026-10-18 复盘 F6）。
- 状态机（2026-10-25 起改为二审制）：

      open ──┬── 忽略 ────────────────────→ ignored      （终态）
             └── 一审通过 ──→ analyzing ──┬── 分析完成 → analyzed ──┬── 二审派发 → dispatched（终态）
                                          └── 分析失败 → open ──────┴── 忽略 → ignored（终态）

  一审（`open → analyzing`）由管理员点「通过，开始分析」触发；`analyzing →
  analyzed` 由后台 LLM 线程落库，失败则退回 `open` 并留 `analysis_error`
  供重试 —— **失败不会假装成功，也不会把单子卡死在一个不可操作的状态里**。
  二审（`analyzed → dispatched`）用管理员编辑过的正文建 Issue。

  仍然**不设「已解决」态**：真解决了是 Gitea Issue closed，以那为准。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

# 反馈分类：给管理员分流用，不参与权限判定。
CATEGORIES: dict[str, str] = {
    "bug": "功能异常",
    "usability": "用起来别扭",
    "data": "数据不对",
    "other": "其他",
}
DEFAULT_CATEGORY = "other"

STATUS_OPEN = "open"
STATUS_ANALYZING = "analyzing"
STATUS_ANALYZED = "analyzed"
STATUS_IGNORED = "ignored"
STATUS_DISPATCHED = "dispatched"
ALL_STATUSES = frozenset({
    STATUS_OPEN, STATUS_ANALYZING, STATUS_ANALYZED,
    STATUS_IGNORED, STATUS_DISPATCHED,
})
# 「还没走完」的状态：可以忽略、可以重新分析、计入管理员待办角标。
PENDING_STATUSES = (STATUS_OPEN, STATUS_ANALYZING, STATUS_ANALYZED)
STATUS_LABELS: dict[str, str] = {
    STATUS_OPEN: "待处理",
    STATUS_ANALYZING: "分析中",
    STATUS_ANALYZED: "待二次审批",
    STATUS_IGNORED: "已忽略",
    STATUS_DISPATCHED: "已派发开发",
}
# 收件箱排序优先级：等二审的排在等一审的前面 —— 二审是最后一道闸，
# 卡在那里等于整条闭环停了。
_STATUS_ORDER = {STATUS_ANALYZED: 0, STATUS_ANALYZING: 1, STATUS_OPEN: 2}

TITLE_MAX_LEN = 120
BODY_MAX_LEN = 4000
COMMENT_MAX_LEN = 2000
PAGE_PATH_MAX_LEN = 200
# 二审正文可以被管理员改，但要挡住「清空后派发」（空正文的任务单没人能执行）。
DRAFT_BODY_MAX_LEN = 12000
DRAFT_BODY_MIN_LEN = 40

_LIST_LIMIT = 200
_TICKET_NO_ATTEMPTS = 5


class TicketNoExhaustedError(RuntimeError):
    """单号生成重试耗尽（并发撞车超过预算）。

    这是**服务端瞬时故障**，不是用户输入问题：标题/正文都合法，只是这一瞬间
    单号序列被并发请求占满。必须与校验失败的 ``ValueError`` 分开——
    前者应回 5xx（可重试 / 该告警），后者回 400（用户改输入）。

    2026-10-18 复盘 F6：旧版两者共用 ValueError，端点统一 catch 成 400，
    于是「服务端没能建单」被报成「你提交的东西有问题」，既误导用户又埋没告警。
    继承 RuntimeError 而非 ValueError，保证任何 ``except ValueError`` 都不会
    误吞它。
    """


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

    这是安全边界而非洁癖：该字段会进企微 markdown 与 Issue 正文，
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


def normalize_draft(title: Any, body: Any) -> tuple[str, str]:
    """二审派发前校验 Issue 标题与正文；非法 raise ValueError。

    正文下限刻意低（40 字）：真正的把关是「管理员看过并点了确认」，
    这里只挡「清空后误点派发」这类手滑 —— 一张空正文的 issue 会让开发侧
    完全无从下手，而拒绝派发只会让管理员补几个字。
    """
    t = str(title or "").strip()
    b = str(body or "").strip()
    if not t:
        raise ValueError("Issue 标题不能为空")
    if len(t) > TITLE_MAX_LEN:
        raise ValueError(f"Issue 标题超过 {TITLE_MAX_LEN} 字")
    if not b:
        raise ValueError("Issue 正文不能为空")
    if len(b) < DRAFT_BODY_MIN_LEN:
        raise ValueError(f"Issue 正文少于 {DRAFT_BODY_MIN_LEN} 字，请补充或在下方重新分析")
    if len(b) > DRAFT_BODY_MAX_LEN:
        raise ValueError(f"Issue 正文超过 {DRAFT_BODY_MAX_LEN} 字")
    return t, b


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
    raise TicketNoExhaustedError(
        f"生成反馈单号失败（重试 {_TICKET_NO_ATTEMPTS} 次）"
    ) from last_exc


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
    """标记忽略（终态）。只要还没进终态就能忽略，已终态的返回 False。

    允许从 ``analyzing`` / ``analyzed`` 忽略是必要的：管理员可能在看到分析结果
    后才判断「这条不值得做」，此时只让 open 可忽略等于逼他先派发再关闭。
    """
    now = _now()
    placeholders = ", ".join("?" for _ in PENDING_STATUSES)
    cur = db.execute(
        f"""UPDATE feedback_tickets
            SET status=?, handled_by=?, handled_at=?, updated_at=?
            WHERE id=? AND status IN ({placeholders})""",
        (STATUS_IGNORED, int(admin_id), now, now, int(ticket_id), *PENDING_STATUSES),
    )
    db.commit()
    return cur.rowcount > 0


def mark_analyzing(
    db: sqlite3.Connection,
    ticket_id: int,
    page_path: str | None = None,
) -> bool:
    """一审通过：``open`` / ``analyzed`` → ``analyzing``（可重跑）。

    幂等性靠状态而不是锁：只有这两个状态能进 ``analyzing``，所以
    「连点两次通过」第二次必然返回 False，不会起两个后台分析线程去抢同一张单。
    重跑时清掉上一次的错误信息 —— 留着旧报错会让人以为这次也失败了。

    ``page_path`` 是管理员在一审页纠正过的「实际页面」（``None`` = 没改，
    沿用提交时页面）。**单独存一列而不是覆盖 ``page_path``**：原始值要保留
    （正文里两种都要写，见 ``build_issue_body``），且分析结果与所用页面的
    对应关系必须可追溯 —— 拿错页面分析出来的结论，事后得能查出来。
    """
    now = _now()
    cur = db.execute(
        """UPDATE feedback_tickets
           SET status=?, analysis_error=NULL, updated_at=?,
               analysis_page_path=COALESCE(?, analysis_page_path)
           WHERE id=? AND status IN (?, ?)""",
        (STATUS_ANALYZING, now, normalize_page_path(page_path) or None,
         int(ticket_id), STATUS_OPEN, STATUS_ANALYZED),
    )
    db.commit()
    return cur.rowcount > 0


def save_analysis(
    db: sqlite3.Connection,
    ticket_id: int,
    analysis_text: str,
    model: str,
    draft_title: str = "",
    draft_body: str = "",
) -> bool:
    """分析完成：``analyzing`` → ``analyzed``，落原始分析 + 预填草稿。

    ``draft_title`` / ``draft_body`` 是二审编辑器的初值。落库而不是每次渲染
    现算：管理员可能在二审页改了几行再走开，下次打开要能看到自己改过的版本，
    而不是又变回模型原稿。
    """
    now = _now()
    cur = db.execute(
        """UPDATE feedback_tickets
           SET status=?, analysis_text=?, analysis_error=NULL,
               analysis_model=?, analysis_at=?,
               draft_title=?, draft_body=?, updated_at=?
           WHERE id=? AND status=?""",
        (STATUS_ANALYZED, str(analysis_text or ""), str(model or ""),
         now, str(draft_title or "")[:TITLE_MAX_LEN], str(draft_body or ""),
         now, int(ticket_id), STATUS_ANALYZING),
    )
    db.commit()
    return cur.rowcount > 0


def fail_analysis(db: sqlite3.Connection, ticket_id: int, error: str) -> bool:
    """分析失败：``analyzing`` → 退回 ``open``，留错误原因供重试。

    为什么退回 ``open`` 而不是停在 ``analyzing``：停住的话这张单既不能派发
    也没人能推动，只能人工改库。退回待处理至少在收件箱里还是红点，点进去
    能看到失败原因和「重试 / 直接派发（模板兜底）」两个出口。
    """
    now = _now()
    cur = db.execute(
        """UPDATE feedback_tickets
           SET status=?, analysis_error=?, updated_at=?
           WHERE id=? AND status=?""",
        (STATUS_OPEN, str(error or "")[:500], now, int(ticket_id), STATUS_ANALYZING),
    )
    db.commit()
    return cur.rowcount > 0


def save_draft(db: sqlite3.Connection, ticket_id: int, title: str, body: str) -> bool:
    """落二审草稿（派发前先本地保存，再调外部 API）。

    与「先落评论再建 Issue」同一个理由：Gitea 挂了最多是没建单，
    管理员在二审页改的内容一个字都不会丢。
    """
    now = _now()
    cur = db.execute(
        """UPDATE feedback_tickets
           SET draft_title=?, draft_body=?, updated_at=?
           WHERE id=? AND issue_number IS NULL""",
        (str(title or "")[:TITLE_MAX_LEN], str(body or ""), now, int(ticket_id)),
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
    """把反馈单与 Gitea Issue 绑定并置为 ``dispatched``（终态）。

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


def draft_version(title: str, body: str) -> str:
    return hashlib.sha256(json.dumps([title or "", body or ""], ensure_ascii=False).encode()).hexdigest()


def save_private_draft(db, ticket: dict, title: str, body: str, version: str) -> bool:
    """Compare-and-save protects newer drafts from concurrent and stale retries."""
    old_title, old_body = ticket.get("draft_title") or "", ticket.get("draft_body") or ""
    if version and version != draft_version(old_title, old_body):
        return False
    if not version and (title, body) != (old_title, old_body):
        return False
    cur = db.execute(
        """UPDATE feedback_tickets SET draft_title=?, draft_body=?, updated_at=?
           WHERE id=? AND issue_number IS NULL AND COALESCE(draft_title,'')=?
           AND COALESCE(draft_body,'')=? AND status=?""",
        (title, body, _now(), ticket["id"], old_title, old_body, STATUS_ANALYZED),
    )
    db.commit()
    return cur.rowcount == 1


@dataclass(frozen=True)
class PrivateDeliveryClaim:
    ticket_id: int
    request_id: str
    payload_json: str
    owner: str
    generation: int
    action: str


def claim_private_delivery(db, ticket_id: int, admin_id: int,
                           comment_id: int | None = None) -> PrivateDeliveryClaim | None:
    """A lease can recover an intent, never authorize a second creation POST."""
    owner, now = str(uuid.uuid4()), time.time()
    with db:
        db.execute("BEGIN IMMEDIATE")
        ticket = db.execute("SELECT * FROM feedback_tickets WHERE id=?", (ticket_id,)).fetchone()
        if not ticket or ticket["issue_number"] or ticket["status"] != STATUS_ANALYZED:
            return None
        version = draft_version(ticket["draft_title"], ticket["draft_body"])
        row = db.execute(
            "SELECT * FROM feedback_delivery_receipts WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        if row is None:
            request_id = str(uuid.uuid4())
            payload = json.dumps({
                "title": ticket["draft_title"],
                "body": ticket["draft_body"] + f"\n\n<!-- dailycheck-private-request:v1 {request_id} -->",
                "labels": ["user-feedback"],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            db.execute(
                """INSERT INTO feedback_delivery_receipts
                   (ticket_id,provider,host,repository_id,repository,kind,request_id,
                    draft_sha256,payload_json,payload_sha256,admin_id,comment_id,state,updated_at)
                   VALUES (?,'github','github.com',1449125557,'DUIDUI-Studio/DailyCheck-ops',
                           'issue',?,?,?,?,?,?,'ready',?)""",
                (ticket_id, request_id, version, payload, hashlib.sha256(payload.encode()).hexdigest(),
                 admin_id, comment_id, _now()),
            )
            row = db.execute(
                "SELECT * FROM feedback_delivery_receipts WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
        if row["draft_sha256"] != version:
            db.execute(
                "UPDATE feedback_delivery_receipts SET state='blocked',updated_at=? WHERE ticket_id=?",
                (_now(), ticket_id),
            )
            return None
        if row["state"] in {"linked", "blocked"} or row["lease_until"] > now:
            return None
        action = "create" if row["state"] == "ready" else "reconcile"
        generation = row["generation"] + 1
        db.execute(
            """UPDATE feedback_delivery_receipts SET owner=?,generation=?,lease_until=?,
               state=?,updated_at=? WHERE ticket_id=?""",
            (owner, generation, now + 120, "ready" if action == "create" else "uncertain",
             _now(), ticket_id),
        )
        return PrivateDeliveryClaim(ticket_id, row["request_id"], row["payload_json"],
                                    owner, generation, action)


def begin_private_creation(db, claim: PrivateDeliveryClaim) -> bool:
    with db:
        cur = db.execute(
            """UPDATE feedback_delivery_receipts SET state='sending',updated_at=?
               WHERE ticket_id=? AND owner=? AND generation=? AND lease_until>?
               AND state='ready'""",
            (_now(), claim.ticket_id, claim.owner, claim.generation, time.time()),
        )
    return cur.rowcount == 1


def release_private_delivery(db, claim: PrivateDeliveryClaim) -> None:
    with db:
        db.execute(
            """UPDATE feedback_delivery_receipts SET owner=NULL,lease_until=0,
               state=CASE WHEN state='sending' THEN 'uncertain' ELSE state END,updated_at=?
               WHERE ticket_id=? AND owner=? AND generation=?""",
            (_now(), claim.ticket_id, claim.owner, claim.generation),
        )


def finalize_private_delivery(db, claim: PrivateDeliveryClaim, issue: dict) -> bool:
    """Atomically bind the confirmed private identity and original review receipt."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT * FROM feedback_delivery_receipts WHERE ticket_id=? AND owner=?
               AND generation=? AND lease_until>? AND state IN ('sending','uncertain')""",
            (claim.ticket_id, claim.owner, claim.generation, time.time()),
        ).fetchone()
        if row is None:
            return False
        ticket = db.execute("SELECT * FROM feedback_tickets WHERE id=?", (claim.ticket_id,)).fetchone()
        unchanged = (
            ticket["issue_number"] is None and ticket["status"] == STATUS_ANALYZED
            and draft_version(ticket["draft_title"], ticket["draft_body"]) == row["draft_sha256"]
        )
        db.execute(
            """UPDATE feedback_delivery_receipts SET state=?,remote_id=?,remote_number=?,
               remote_url=?,owner=NULL,lease_until=0,updated_at=? WHERE ticket_id=?""",
            ("linked" if unchanged else "blocked", issue["id"], issue["number"], issue["url"],
             _now(), claim.ticket_id),
        )
        if not unchanged:
            return False
        db.execute(
            """UPDATE feedback_tickets SET status=?,issue_number=?,issue_url=?,handled_by=?,
               handled_at=?,updated_at=? WHERE id=? AND issue_number IS NULL""",
            (STATUS_DISPATCHED, issue["number"], issue["url"], row["admin_id"],
             _now(), _now(), claim.ticket_id),
        )
        if row["comment_id"] is not None:
            db.execute(
                """UPDATE feedback_comments SET dispatched_at=? WHERE id=? AND ticket_id=?
                   AND dispatched_at IS NULL""", (_now(), row["comment_id"], claim.ticket_id),
            )
    return True


class PublicSummaryReview(Protocol):
    """Trusted off-repo reviewer adapter, supplied by code, never by an app form.

    Must check reviewer role, durable receipt, destination, request and exact hash;
    raise on absent/stale/unapproved authority. No implementation is installed here.
    """

    def verify_projection(self, receipt: str, *, payload_sha256: str,
                          request_id: str, repository_id: int) -> None: ...


def public_engineering_projection(draft: dict, request_id: str, approval_receipt: str,
                                  *, review: PublicSummaryReview) -> dict:
    """Separately authored prose only; syntactic guards do not replace privacy review.

    ABI for existing dc_state.create_issue's future public path, NOT WIRED to app
    dispatch or any public Issue transport. Private/public mappings stay external.
    """
    fields = {"title", "problem", "synthetic_reproduction", "acceptance", "component", "label"}
    if not isinstance(draft, dict) or set(draft) != fields:
        raise ValueError("public_summary_fields")
    if any(not isinstance(value, str) or not value.strip() for value in draft.values()):
        raise ValueError("public_summary_text")
    if draft["component"] not in {"feedback", "deploy", "inventory", "MCP", "automation", "migration"}:
        raise ValueError("public_summary_component")
    if draft["label"] not in {"bug", "enhancement", "maintenance", "migration"}:
        raise ValueError("public_summary_label")
    if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
        raise ValueError("public_summary_request")
    if (not isinstance(approval_receipt, str) or not approval_receipt
        or not callable(getattr(review, "verify_projection", None))):
        raise ValueError("public_summary_review_required")
    for key, limit in {"title": 120, "problem": 4000,
                       "synthetic_reproduction": 8000, "acceptance": 4000}.items():
        value = draft[key]
        if len(value) > limit or re.search(
            r"(?i)([a-z][a-z0-9+.-]*://|mailto:|data:|www\.|!\[|\]\(|[<>]|#\d+|dailycheck-ops|FB-\d{8}-|part-of:"
            r"|(?:password|passwd|secret|api[_-]?key)\s*[:=]|bearer\s+|token\s+"
            r"|gh[pousr]_[A-Za-z0-9_]+|github_pat_)", value,
        ) or any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("public_summary_prohibited_text")
    payload = {
        "title": draft["title"], "labels": [draft["label"]],
        "body": f"<!-- dailycheck-public-request:v1 {request_id} -->\n"
                f"Component: {draft['component']}\n\n## Problem\n{draft['problem']}\n\n"
                f"## Synthetic reproduction\n{draft['synthetic_reproduction']}\n\n"
                f"## Acceptance\n{draft['acceptance']}",
    }
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    if review.verify_projection(approval_receipt, payload_sha256=digest,
                                request_id=request_id, repository_id=1448986421) is not None:
        raise ValueError("public_summary_review_contract")
    return payload


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

_COLUMNS = (
    "id, ticket_no, user_id, warehouse_code, page_path, category, title, body, "
    "status, issue_number, issue_url, created_at, updated_at, handled_by, handled_at, "
    "analysis_text, analysis_error, analysis_model, analysis_at, "
    "analysis_page_path, draft_title, draft_body"
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
        "t.analysis_text, t.analysis_error, t.analysis_model, t.analysis_at, "
        "t.draft_title, t.draft_body, "
        "u.username AS submitter "
        "FROM feedback_tickets t LEFT JOIN users u ON u.id = t.user_id"
    )
    params: list[Any] = []
    if status in ALL_STATUSES:
        sql += " WHERE t.status=?"
        params.append(status)
    sql += (
        " ORDER BY CASE t.status"
        " WHEN 'analyzed' THEN 0 WHEN 'analyzing' THEN 1 WHEN 'open' THEN 2"
        " ELSE 3 END, t.created_at DESC, t.id DESC LIMIT ?"
    )
    params.append(_LIST_LIMIT)
    rows = db.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        item = _ticket_dict(r)
        item["submitter"] = r["submitter"] if "submitter" in r.keys() else None
        out.append(item)
    return out


def count_open(db: sqlite3.Connection) -> int:
    """管理员待办数量（导航角标用）。

    口径是「**还没走完**」而不是「状态=open」：等二审的单子（``analyzed``）
    和正在分析的单子同样是待办 —— 只数 open 会让收件箱在二审阶段静默清零，
    而闭环恰恰卡在那儿。
    """
    placeholders = ", ".join("?" for _ in PENDING_STATUSES)
    row = db.execute(
        f"SELECT COUNT(*) AS n FROM feedback_tickets WHERE status IN ({placeholders})",
        PENDING_STATUSES,
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
        "analysis_text": row["analysis_text"] if "analysis_text" in keys else None,
        "analysis_error": row["analysis_error"] if "analysis_error" in keys else None,
        "analysis_model": row["analysis_model"] if "analysis_model" in keys else None,
        "analysis_at": row["analysis_at"] if "analysis_at" in keys else None,
        "analysis_page_path": (
            row["analysis_page_path"] if "analysis_page_path" in keys else None
        ),
        "draft_title": row["draft_title"] if "draft_title" in keys else None,
        "draft_body": row["draft_body"] if "draft_body" in keys else None,
    }


# ---------------------------------------------------------------------------
# 渲染（企微 markdown / Gitea Issue 正文共用）
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
