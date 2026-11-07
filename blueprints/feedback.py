"""/feedback 蓝图：用户问题提单 + 管理员处理闭环。

两条使用路径：
1. 普通用户（任意登录用户）：提交问题、看自己提交过的单子。
2. 平台超管：收件箱 → 详情 → **一审** → LLM 预分析 → **二审** → 派发。
   - 一审（`/analyze`）：判定「值得修」，触发后台分析（或跳过分析用模板草稿）
   - 二审（`/dispatch`）：审模型产出的 Issue 草稿，可改，确认后建 Gitea Issue
   - 任一环节都能「忽略」（终态）

为什么加二审（2026-10-25）：一审时管理员只有一个输入框，要在「还没定位到
代码」的情况下写出可执行的 Issue 正文，结果正文质量取决于他当天写得多细。
拆成两步后，模型先按 Issue 处理标准产出根因假设 / 可核对验收项，人只做
「这个判断对不对」的复核 —— 判断比撰写便宜得多。

纯逻辑在 ``feedback_pure``；分析在 ``feedback_analysis_pure``（纯）与
``feedback_llm``（网络 + 线程）；外部通道在 ``feedback_notify``（企微）、
``feedback_dispatch``（Gitea）。本模块只做「鉴权 + 组装 + 重定向」。

Spec: docs/ARCHITECTURE.md §3.10（反馈闭环 / 二审制）。
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
from . import feedback_llm as llm
from . import feedback_notify as notify
from .feedback_analysis_pure import (
    analysis_to_json,
    analysis_view,
    build_draft,
    fallback_analysis,
)
from .feedback_pure import (
    STATUS_ANALYZED,
    STATUS_ANALYZING,
    STATUS_DISPATCHED,
    STATUS_IGNORED,
    STATUS_OPEN,
    TicketNoExhaustedError,
    add_comment,
    attach_issue,
    build_admin_user_ids,
    count_open,
    create_ticket,
    fail_analysis,
    get_ticket,
    list_comments,
    list_for_admin,
    list_for_user,
    mark_analyzing,
    mark_comment_dispatched,
    mark_ignored,
    normalize_category,
    normalize_draft,
    save_analysis,
    save_draft,
    summarize,
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
        llm_enabled=llm.llm_enabled(),
    )


@bp.route("/admin/feedback/<int:ticket_id>", methods=["GET"])
@require_platform_admin
def admin_feedback_detail(ticket_id: int):
    """管理员处理页：原文 + 历史评论 + 一审 / 二审操作。"""
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

    status = str(ticket.get("status") or "")
    analysis = analysis_view(ticket)
    clues: dict = {}
    draft: dict = {}
    if status in (STATUS_OPEN, STATUS_ANALYZED):
        # 一审要「这段线索会进正文」的预览；二审要「模型看到的是哪段代码」——
        # 二审按分析时实际用的页面回显，否则页面纠正过的会被打回原路径。
        if status == STATUS_ANALYZED:
            locate_from = ticket.get("analysis_page_path") or ticket.get("page_path")
        else:
            locate_from = ticket.get("page_path")
        clues = introspect.collect(current_app._get_current_object(), locate_from)

    if status == STATUS_ANALYZED:
        # 二审编辑器初值。管理员上次改过的正文优先 —— 让二次打开看到自己写的
        # 版本，而不是又变回模型原稿。
        default_title, default_body = build_draft(
            ticket, comments, clues, analysis, base_url=_base_url())
        draft = {
            "title": ticket.get("draft_title") or default_title,
            "body": ticket.get("draft_body") or default_body,
        }

    return render_template(
        "admin/feedback_detail.html",
        ticket=ticket,
        comments=comments,
        is_admin_view=True,
        dispatch_enabled=dispatch.dispatch_enabled(),
        llm_enabled=llm.llm_enabled(),
        code_clues=clues,
        analysis=analysis,
        draft=draft,
    )


@bp.route("/admin/feedback/<int:ticket_id>/ignore", methods=["POST"])
@require_platform_admin
def ignore_feedback(ticket_id: int):
    """判定「不重要」：标记已读忽略（终态，不可回退）。

    一审、分析中、二审前都能点 —— 看完模型分析才决定不做是最正常的判断路径，
    只允许 open 忽略等于逼管理员先派发再关闭。
    """
    with get_master_db() as db:
        if get_ticket(db, ticket_id) is None:
            abort(404)
        changed = mark_ignored(db, ticket_id, g.user["id"])
    flash("已标记忽略。" if changed else "该反馈已处理过，未做改动。")
    return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))


@bp.route("/admin/feedback/<int:ticket_id>/analyze", methods=["POST"])
@require_platform_admin
def analyze_feedback(ticket_id: int):
    """一审：确认值得修 → 起后台分析（或 `mode=template` 直接用模板草稿）。

    ``mode=template`` 是**不调模型的显式降级**：模型没配 / 挂了 / 管理员急着
    派单时用。它照样生成一版草稿并进二审 —— 二审这道闸不能被绕过，
    差别只是草稿里没有模型结论，页面上也会明确标「未调用模型」。

    重复触发是安全的：``mark_analyzing`` 只在 open/analyzed 时生效，
    连点两次第二次拿不到状态变更，直接提示「已在分析中」。
    """
    mode = (request.form.get("mode") or "llm").strip().lower()
    with get_master_db() as db:
        ticket = get_ticket(db, ticket_id)
        if ticket is None:
            abort(404)
        if ticket.get("issue_number"):
            flash(f"这张反馈已派发为 Issue #{ticket['issue_number']}，无需再分析。")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))

        # 一审意见是可选的：写得细，分析更准；不写也能分析（缺口会进正文）。
        comment = (request.form.get("comment") or "").strip()
        if comment:
            try:
                add_comment(db, ticket_id, g.user["id"], comment, is_admin=True)
            except ValueError as exc:
                flash(f"一审意见未保存：{exc}")
                return redirect(url_for(
                    "feedback.admin_feedback_detail", ticket_id=ticket_id))

        override = introspect.clean_path(request.form.get("page_override", ""))
        if not mark_analyzing(db, ticket_id, override or None):
            flash("这张反馈已在分析中或已终结，未重复触发。")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))

        if mode == "template":
            # 降级也走同一条落库路径（save_analysis），所以二审页拿到的
            # 结构与正常分析完全一致，只是 source=template。
            refreshed = get_ticket(db, ticket_id) or ticket
            comments = list_comments(db, ticket_id)
            clues = introspect.collect(
                current_app._get_current_object(),
                refreshed.get("analysis_page_path") or refreshed.get("page_path"),
            )
            analysis = fallback_analysis(
                refreshed, comments, clues, reason="管理员选择跳过模型分析")
            title, body = build_draft(
                refreshed, comments, clues, analysis, base_url=_base_url())
            save_analysis(
                db, ticket_id, analysis_to_json(analysis), llm.MODEL_TEMPLATE_LABEL,
                title, body)
            flash("已用模板生成草稿，请在下方二审后派发。")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))

    started = llm.launch(current_app._get_current_object(), ticket_id)
    if not started:
        # 只有 DRY_RUN 会走到这里（正常路径线程已起）。状态已被 mark_analyzing
        # 置成 analyzing，必须退回，否则这张单会永远卡在「分析中」。
        with get_master_db() as db:
            fail_analysis(db, ticket_id, "DRY_RUN 实例不执行分析，请直接在正式实例处理")
        flash("当前是 DRY_RUN 实例，不执行分析。请到正式实例处理这张反馈。")
        return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))

    flash("已开始分析，页面会自动刷新；通常 10~30 秒出结果。")
    return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))


@bp.route("/admin/feedback/<int:ticket_id>/dispatch", methods=["POST"])
@require_platform_admin
def dispatch_feedback(ticket_id: int):
    """二审：确认（可编辑过的）Issue 草稿，建 Gitea Issue 并终结本单。

    顺序是刻意的：**先把草稿落库，再调外部 API**。Gitea 挂了最多是没建单，
    管理员在二审页改过的字一个都不会丢；重建时正文直接从库里取回。

    只接受 ``analyzed`` 状态的单子。理由：二审的意义就是「先有草稿、再看一眼」，
    允许从 ``open`` 直接派发等于给了一条绕过复核的暗道，而这条暗道一定会被用
    （谁都想快点结束）。真需要跳过模型时走 ``/analyze?mode=template``，
    一步之遥但状态机仍然完整。
    """
    with get_master_db() as db:
        ticket = get_ticket(db, ticket_id)
        if ticket is None:
            abort(404)
        if ticket.get("issue_number"):
            flash(f"这张反馈已派发为 Issue #{ticket['issue_number']}，未重复建单。")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))
        if str(ticket.get("status") or "") != STATUS_ANALYZED:
            flash("请先点「通过，开始分析」（或「跳过分析，用模板生成草稿」）进入二审。")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))

        try:
            title, body = normalize_draft(
                request.form.get("draft_title") or ticket.get("draft_title") or "",
                request.form.get("draft_body") or ticket.get("draft_body") or "",
            )
        except ValueError as exc:
            flash(f"未派发：{exc}")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))

        # 二审时补一句意见也要留痕（不写进正文 —— 正文已被管理员定稿）。
        comment = (request.form.get("comment") or "").strip()
        comment_id = None
        if comment:
            try:
                comment_id = add_comment(
                    db, ticket_id, g.user["id"], comment, is_admin=True)
            except ValueError as exc:
                flash(f"二审意见未保存：{exc}")
                return redirect(url_for(
                    "feedback.admin_feedback_detail", ticket_id=ticket_id))

        save_draft(db, ticket_id, title, body)

        if not dispatch.dispatch_enabled():
            flash("草稿已保存，但派发通道未配置，请手动建 Issue。")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))

        issue = dispatch.create_issue(title, body)
        if issue is None:
            flash("草稿已保存，但创建 Issue 失败（详见服务端日志），请手动建单。")
            return redirect(url_for("feedback.admin_feedback_detail", ticket_id=ticket_id))

        attach_issue(db, ticket_id, issue["number"], issue["url"], g.user["id"])
        if comment_id is not None:
            mark_comment_dispatched(db, comment_id)

    flash(f"已派发为 Issue #{issue['number']}，开发闭环已启动。")
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
    "STATUS_ANALYZING",
    "STATUS_ANALYZED",
    "STATUS_IGNORED",
    "STATUS_DISPATCHED",
]
