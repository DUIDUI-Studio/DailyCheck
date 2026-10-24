"""/feedback 蓝图：用户问题提单 + 管理员处理闭环。

两条使用路径：
1. 普通用户（任意登录用户）：提交问题、看自己提交过的单子。
2. 平台超管：收件箱 → 详情 → 判定。
   - 不重要 → 忽略（终态）
   - 有意义 → 写评论 + 发送 → 建 Gitea Issue（终态，进开发闭环）

纯逻辑在 ``feedback_pure``；外部通道在 ``feedback_notify``（企微）、
``feedback_dispatch``（Gitea）。本模块只做「鉴权 + 组装 + 重定向」。

Spec: docs/ARCHITECTURE.md §3.10（反馈闭环）。
"""
from __future__ import annotations

import logging

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import config
from db import get_master_db
from permissions import require_login, require_platform_admin

from . import feedback_dispatch as dispatch
from . import feedback_introspect as introspect
from . import feedback_notify as notify
from .feedback_pure import (
    STATUS_DISPATCHED,
    STATUS_IGNORED,
    STATUS_OPEN,
    TicketNoExhaustedError,
    add_comment,
    attach_issue,
    build_admin_user_ids,
    count_open,
    create_ticket,
    get_ticket,
    list_comments,
    list_for_admin,
    list_for_user,
    mark_comment_dispatched,
    mark_ignored,
    normalize_category,
    summarize,
    validate_comment,
)
from .notifications_pure import SUMMARY_MAX_LEN, emit_event

logger = logging.getLogger(__name__)

bp = Blueprint("feedback", __name__)

EVENT_FEEDBACK_SUBMITTED = "feedback_submitted"

# 提交页允许用户带的「当前页面」回填参数白名单化处理在 feedback_pure 里做，
# 这里只管取。


def _base_url() -> str:
    return str(getattr(config, "PUBLIC_BASE_URL", "") or "")


def _is_platform_admin() -> bool:
    user = g.get("user")
    if user is None:
        return False
    try:
        return bool(user["is_admin"])
    except (KeyError, IndexError, TypeError):
        return False


@bp.before_request
def _api_guard():
    """``POST /feedback/api`` 的前置鉴权：已登录但未选仓时回 409 JSON。

    ⚠️ 未登录这一档**拦在这里已经太晚**：``blueprints/auth.py`` 的
    ``before_app_request`` 在蓝图钩子之前就跑，未登录会先 302 到登录页，
    再被全局 ``register_ajax_handlers`` 改写成 **200 + {ok:true, location}**。
    所以前端**不能拿 ``ok`` 当成功判据**，必须见到 ``ticket_no``
    （见 static/feedback-widget.js）。本钩子只管「登录了但没选仓」这档 ——
    auth 的选仓重定向只对 GET 生效，POST 会走到这里。
    """
    if request.endpoint != "feedback.submit_feedback_api":
        return None
    if g.get("user") is None:
        # 正常不会走到（auth 钩子已先重定向）；留作纵深防御。
        return jsonify({
            "ok": False,
            "reason": "unauthenticated",
            "message": "登录已过期，请重新登录后再提交反馈。",
        }), 401
    if g.get("warehouse_db_path") is None:
        return jsonify({
            "ok": False,
            "reason": "no_warehouse",
            "message": "请先在右上角选择仓库，再提交反馈。",
        }), 409
    return None


def _create_ticket(form):
    """表单 → 落库 + 站内扇出超管。校验失败抛 ``ValueError``。

    表单端点（PRG）与悬浮窗端点（JSON）共用：校验与落库只此一份，
    避免两条路径对「空标题 / 超长正文」给出不同判据。
    """
    warehouse = g.get("warehouse")
    with get_master_db() as db:
        ticket = create_ticket(
            db,
            user_id=g.user["id"],
            title=form.get("title", ""),
            body=form.get("body", ""),
            category=normalize_category(form.get("category")),
            warehouse_code=warehouse["code"] if warehouse is not None else None,
            page_path=form.get("page_path", ""),
        )
        ticket["submitter"] = g.user["username"]
        _fanout_admins(db, ticket)
    return ticket


# ---------------------------------------------------------------------------
# 用户侧
# ---------------------------------------------------------------------------


@bp.route("/feedback", methods=["GET"])
@require_login
def my_feedback():
    """我的反馈列表。"""
    with get_master_db() as db:
        tickets = list_for_user(db, g.user["id"])
    return render_template("feedback_list.html", tickets=tickets)


@bp.route("/feedback/new", methods=["GET"])
@require_login
def new_feedback():
    """提交表单。``page`` 参数用于回填「出问题的页面」。"""
    return render_template(
        "feedback_new.html",
        page_path=(request.args.get("page") or "").strip(),
    )


@bp.route("/feedback", methods=["POST"])
@require_login
def submit_feedback():
    """落库 → 站内通知扇出 → 企微推送，然后回到「我的反馈」。"""
    try:
        ticket = _create_ticket(request.form)
    except TicketNoExhaustedError:
        # 服务端瞬时故障，不是用户输入问题。表单（非 AJAX）路径靠 flash 文案区分，
        # 仍走 302 PRG——不能返 503：浏览器不跟随 503 的 Location，用户会看到错误页。
        logger.exception("feedback ticket_no exhausted")
        flash("系统繁忙，提交没成功，请稍后重试（你写的内容不会被清空）。")
        return redirect(url_for("feedback.new_feedback"))
    except ValueError as exc:
        flash(f"提交失败：{exc}")
        return redirect(url_for("feedback.new_feedback"))

    # 企微推送在**事务外**、后台线程里跑：外通道故障绝不影响已落库的单子。
    notify.notify_feedback_submitted(ticket)
    flash(f"已提交（{ticket.get('ticket_no', '')}），管理员会尽快处理。")
    return redirect(url_for("feedback.my_feedback"))


@bp.route("/feedback/api", methods=["POST"])
def submit_feedback_api():
    """悬浮窗的 JSON 提交端点（Issue #108）。

    与 ``POST /feedback`` 只差**响应形态**：表单端点走 PRG（302 + flash），
    本端点返回 JSON，让悬浮窗在当前页就地提交 —— 用户填了一半的表单、
    滚动位置、已展开的弹层都不丢。落库 / 扇出 / 企微推送复用同一份
    ``_create_ticket``，两条路径不存第二套校验。

    ⚠️ 这里**不用 ``@require_login``**：它未登录 / 未选仓时返回 302，
    而全局 ``register_ajax_handlers`` 会把带 ``X-Requested-With: fetch``
    的 3xx 改写成 JSON 信封（2026-10-18 起按 Location 语义分流：
    →登录页 401、→选仓页 409，见 ``_helpers._redirect_kind``）。即便如此，
    本端点仍在 ``_api_guard()`` 里自己返回 401 / 409 JSON，语义显式、不依赖
    hook 的隐式分类。
    """
    try:
        ticket = _create_ticket(request.form)
    except TicketNoExhaustedError:
        # 服务端瞬时故障（并发撞单号超预算）→ 503 可重试，不是用户输入问题。
        logger.exception("feedback ticket_no exhausted")
        return jsonify({
            "ok": False,
            "reason": "server_busy",
            "message": "系统繁忙，请稍后重试（你写的内容已保留）。",
        }), 503
    except ValueError as exc:
        return jsonify({"ok": False, "message": f"提交失败：{exc}"}), 400

    notify.notify_feedback_submitted(ticket)
    ticket_no = ticket.get("ticket_no", "")
    message = f"已提交（{ticket_no}），管理员会尽快处理。"
    return jsonify({"ok": True, "ticket_no": ticket_no, "message": message})


@bp.route("/feedback/<int:ticket_id>", methods=["GET"])
@require_login
def ticket_detail(ticket_id: int):
    """用户查看自己提交的单子；超管可看全部。"""
    with get_master_db() as db:
        ticket = get_ticket(db, ticket_id)
        if ticket is None:
            abort(404)
        if not _is_platform_admin() and ticket["user_id"] != g.user["id"]:
            abort(404)
        comments = list_comments(db, ticket_id)
    return render_template(
        "feedback_detail.html",
        ticket=ticket,
        comments=comments,
        is_admin_view=False,
    )


# ---------------------------------------------------------------------------
# 管理员侧
# ---------------------------------------------------------------------------


@bp.route("/admin/feedback", methods=["GET"])
@require_platform_admin
def admin_inbox():
    """收件箱。``status`` 筛选：open / ignored / dispatched / 空=全部。"""
    status = (request.args.get("status") or "").strip()
    with get_master_db() as db:
        tickets = list_for_admin(db, status or None)
        open_count = count_open(db)
    return render_template(
        "admin/feedback_inbox.html",
        tickets=tickets,
        active_status=status,
        open_count=open_count,
        dispatch_enabled=dispatch.dispatch_enabled(),
    )


@bp.route("/admin/feedback/<int:ticket_id>", methods=["GET"])
@require_platform_admin
def admin_feedback_detail(ticket_id: int):
    """管理员处理页：原文 + 历史评论 + 判定操作。"""
    with get_master_db() as db:
        ticket = get_ticket(db, ticket_id)
        if ticket is None:
            abort(404)
        comments = list_comments(db, ticket_id)
        for c in comments:
            c["is_trigger"] = c["dispatched_at"] is not None
        submitter = db.execute(
            "SELECT username FROM users WHERE id=?", (ticket["user_id"],)
        ).fetchone()
    ticket["submitter"] = submitter["username"] if submitter is not None else None
    # 判定区给管理员一个「派发后正文会带什么代码线索」的预览。只在待处理时算：
    # 终态单子不再派发，没必要为它跑一次 git。collect 全程 fail-soft。
    clues: dict = {}
    if ticket.get("status") == STATUS_OPEN:
        clues = introspect.collect(current_app._get_current_object(), ticket.get("page_path"))
    return render_template(
        "admin/feedback_detail.html",
        ticket=ticket,
        comments=comments,
        is_admin_view=True,
        dispatch_enabled=dispatch.dispatch_enabled(),
        code_clues=clues,
    )


@bp.route("/admin/feedback/<int:ticket_id>/ignore", methods=["POST"])
@require_platform_admin
def ignore_feedback(ticket_id: int):
    """判定「不重要」：标记已读忽略（终态，不可回退）。"""
    with get_master_db() as db:
        if get_ticket(db, ticket_id) is None:
            abort(404)
        changed = mark_ignored(db, ticket_id, g.user["id"])
    flash("已标记忽略。" if changed else "该反馈已处理过，未做改动。")
    return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))


@bp.route("/admin/feedback/<int:ticket_id>/comment", methods=["POST"])
@require_platform_admin
def save_comment(ticket_id: int):
    """只存评论、不派发（管理员想先记一笔再决定）。"""
    with get_master_db() as db:
        if get_ticket(db, ticket_id) is None:
            abort(404)
        try:
            add_comment(db, ticket_id, g.user["id"],
                        request.form.get("comment", ""), is_admin=True)
        except ValueError as exc:
            flash(f"评论未保存：{exc}")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))
    flash("评论已保存。")
    return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))


@bp.route("/admin/feedback/<int:ticket_id>/dispatch", methods=["POST"])
@require_platform_admin
def dispatch_feedback(ticket_id: int):
    """写评论 + 建 Gitea Issue，把这张反馈交给开发闭环。

    顺序是刻意的：**先落评论，再建 Issue**。外部 API 挂了也只是没建单，
    管理员写的处理意见一个字都不会丢（Issue 正文正是从这些评论生成的）。
    """
    with get_master_db() as db:
        ticket = get_ticket(db, ticket_id)
        if ticket is None:
            abort(404)
        if ticket.get("issue_number"):
            flash(f"这张反馈已派发为 Issue #{ticket['issue_number']}，未重复建单。")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))
        try:
            text = validate_comment(request.form.get("comment", ""))
        except ValueError as exc:
            flash(f"未派发：{exc}")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))

        comment_id = add_comment(db, ticket_id, g.user["id"], text, is_admin=True)
        comments = list_comments(db, ticket_id)
        submitter = db.execute(
            "SELECT username FROM users WHERE id=?", (ticket["user_id"],)
        ).fetchone()
        ticket["submitter"] = submitter["username"] if submitter is not None else None

        if not dispatch.dispatch_enabled():
            flash("评论已保存，但派发通道未配置（FEEDBACK_GITHUB_TOKEN 为空），请手动建 Issue。")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))

        # 代码定位在发请求前做：正文的精确度主要靠这一段，而不是靠用户措辞。
        # collect 内部全程 fail-soft，失败只是少一段线索，不会让派发失败。
        #
        # 定位来源优先级：管理员在「实际页面」里手填的 > 用户提交时所在页面。
        # 前者是有意纠正（Issue #93：用户停在仓库选择页提的反馈，自动路径完全
        # 无关），后者只是「点反馈那一刻在哪」。clean_path 会把外部 URL 洗成空，
        # 所以非法输入自然回落，不需要额外校验。
        override = introspect.clean_path(request.form.get("page_override", ""))
        if override and override != str(ticket.get("page_path") or ""):
            ticket["page_path_override"] = override
        locate_from = ticket.get("page_path_override") or ticket.get("page_path")
        clues = introspect.collect(current_app._get_current_object(), locate_from)
        issue = dispatch.create_issue(
            dispatch.build_issue_title(ticket),
            dispatch.build_issue_body(
                ticket, comments, base_url=_base_url(), code_clues=clues),
        )
        if issue is None:
            flash("评论已保存，但创建 GitHub Issue 失败（详见服务端日志），请手动建单。")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))

        attach_issue(db, ticket_id, issue["number"], issue["url"], g.user["id"])
        mark_comment_dispatched(db, comment_id)

    flash(f"已派发为 Issue #{issue['number']}，开发闭环已启动。")
    return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _fanout_admins(db, ticket: dict) -> None:
    """站内通知扇出给所有平台超管。

    失败只记日志：通知没发出去不等于反馈没提成，用户已经看到成功页了。
    """
    try:
        # DRY_RUN 下 lastrowid 是假 id，get_ticket 查不到 → ticket 为 {}，
        # 此时没有可扇出的对象，直接跳过（不许 KeyError 打穿提交路径）。
        ticket_id = ticket.get("id")
        if not ticket_id:
            return
        admin_ids = build_admin_user_ids(db)
        if not admin_ids:
            logger.warning("feedback_no_admin_recipients: ticket=%s",
                           ticket.get("ticket_no", ""))
            return
        summary = f"{ticket.get('ticket_no', '')} {summarize(ticket.get('title') or '')}"
        emit_event(
            db,
            EVENT_FEEDBACK_SUBMITTED,
            summary[:SUMMARY_MAX_LEN],
            url_for("feedback.admin_feedback_detail", ticket_id=ticket_id),
            admin_ids,
        )
    except Exception:  # 兜底：扇出失败不影响提交结果
        logger.warning("feedback_notify_fanout_failed: ticket=%s",
                       ticket.get("ticket_no", ""), exc_info=True)


# 供测试断言状态常量与页面口径一致（避免模板里写裸字符串）
__all__ = [
    "bp",
    "EVENT_FEEDBACK_SUBMITTED",
    "STATUS_OPEN",
    "STATUS_IGNORED",
    "STATUS_DISPATCHED",
]
