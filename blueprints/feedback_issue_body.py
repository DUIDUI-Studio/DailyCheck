"""反馈 Issue 正文渲染：把反馈单 + 管理员判定 + 自动探测的代码线索拼成任务单。

为什么单独一个模块：正文是 Issue 的「验收契约」，规则多（四要素、信息缺口
判定、引用格式、验收项），与「怎么调 GitHub API」是两件事。混在一个文件里
两边都读不清，且会让 dispatch 突破单文件行数上限。

本模块是**纯函数**：不读库、不发网络、不依赖 Flask。给什么渲染什么，所以可以
直接用字典断言正文内容（见 ``tests/test_feedback_introspect.py``）。

Spec: docs/ARCHITECTURE.md §3.10（反馈闭环 / Issue 精确化）。
"""
from __future__ import annotations

import re
from typing import Any

from .feedback_pure import summarize

# 「信息缺口」的判定阈值：低于这些长度的描述基本无法据以定位问题。
# 只用于正文提示，不阻断派发。
MIN_BODY_LEN = 20
MIN_ADMIN_NOTE_LEN = 15

# 管理员详情页「插入结构模板」按钮会前置插入三行骨架
# （现象：/ 影响范围：/ 期望结果：）。用户点完按钮后光标落在文本框末尾，
# 很自然从最后一行往下写，于是三行空骨架被原样提交进 Issue 正文
# （Issue #93 实况）。空标签对开发侧零信息，必须在渲染前剥掉 ——
# 前端光标定位也修了，但不能指望所有入口都守规矩。
_EMPTY_STRUCT_LABEL_RE = re.compile(r"^(现象|影响范围|期望结果)\s*[:：]\s*$")


# ---------------------------------------------------------------------------
# 正文渲染
# ---------------------------------------------------------------------------

def build_issue_title(ticket: dict[str, Any] | None) -> str:
    """``[反馈] FB-20261017-0001 库存页导出按钮没反应``。"""
    ticket = ticket if isinstance(ticket, dict) else {}
    no = str(ticket.get("ticket_no") or "").strip() or "无单号"
    title = summarize(ticket.get("title") or "", 60) or "（无标题）"
    return f"[反馈] {no} {title}"


def build_issue_body(
    ticket: dict[str, Any] | None,
    comments: list[dict[str, Any]] | None = None,
    base_url: str = "",
    code_clues: dict[str, Any] | None = None,
) -> str:
    """Issue 正文：一句结论 + 原文 + 管理员判定 + 代码线索 + 验收标准 + 范围边界。

    正文不是「需求描述」，是**验收契约**。开发侧（人或 agent）拿到一单要能做
    四件事：定位到代码、判定做完了、知道哪些不许碰、确认这不是产品决策。
    这四项缺任何一项，负责自动推进的巡检 agent 就会停下来转人工。

    所以正文的精确度主要来自 ``code_clues``（由 ``feedback_introspect`` 从
    ``page_path`` 反射生成），而不是来自用户措辞的精细程度 —— 用户和管理员
    写得粗糙是常态，不该指望他们补。
    """
    ticket = ticket if isinstance(ticket, dict) else {}
    clues = code_clues if isinstance(code_clues, dict) else {}
    notes = admin_notes(comments)

    # 第一句必须是「已经确认要做」。巡检侧对「判断不清或有产品决策属性」的单子
    # 会跳过并转人工，这句是把它挡在跳过的门外。
    lines: list[str] = [
        "> 本单由管理员在站内判定后派发，**已确认需要修复**，不需要再回问是否要做。",
        "> 直接实施并在本 issue 说明改动点；只有牵涉产品行为语义变更时才需要确认。",
        "",
        "## 来源",
        "",
    ]

    meta = [
        ("单号", ticket.get("ticket_no")),
        ("提交人", ticket.get("submitter") or ticket.get("username")),
        ("仓库", ticket.get("warehouse_code")),
        ("提交时所在页面", ticket.get("page_path")),
        ("分类", ticket.get("category_label") or ticket.get("category")),
        ("提交时间", ticket.get("created_at")),
    ]
    # 管理员手填了「实际页面」= 有意纠正自动定位，两条都要留，谁改的能追溯。
    override = str(ticket.get("page_path_override") or "").strip()
    if override:
        meta.insert(4, ("实际页面（管理员指定）", override))
    for label, value in meta:
        text = str(value or "").strip()
        if text:
            lines.append(f"- **{label}**：{text}")

    site = str(base_url or "").strip().rstrip("/")
    if site and ticket.get("id") is not None:
        lines.append(f"- **站内处理页**：{site}/admin/feedback/{int(ticket['id'])}")

    lines += [
        "",
        "## 现象（用户原文）",
        "",
        _quote(str(ticket.get("body") or "").strip()),
        "",
        "## 管理员判定",
        "",
    ]
    if notes:
        for note in notes:
            who = str(note.get("username") or "管理员").strip()
            when = str(note.get("created_at") or "").strip()
            lines.append(f"- **{who}**（{when}）：{str(note.get('body') or '').strip()}")
    else:
        lines.append("- （未填写处理意见）")

    lines += ["", "## 代码线索（自动生成）", ""]
    lines += _render_clue_lines(clues)

    gaps = detect_gaps(ticket, notes, clues)
    lines += ["", "## 信息缺口", ""]
    if gaps:
        lines.append("以下信息提交时没有提供。**请开发侧自行从代码、日志和页面现状推断，")
        lines.append("不要因为这个停下来等回复**；确实推断不出来时才在本 issue 留言提问。")
        lines.append("")
        lines += [f"- {gap}" for gap in gaps]
    else:
        lines.append("无 —— 页面、描述和处理意见都足够定位。")

    lines += [
        "",
        "## 验收标准（逐项核对）",
        "",
    ]
    lines += _render_acceptance(clues)

    lines += [
        "",
        "## 范围边界",
        "",
        "- 本单只处理上面描述的现象，不做顺带重构，不改其他页面的现有行为。",
        "- 若必须改变现有产品行为语义才能修好，**先在本 issue 评论提出**，"
        "不要自行扩大范围 —— 这类改动需要 owner 拍板。",
        "- 缺陷修复不需要新增产品决策；本单默认属于「确定要修」的范畴。",
        "",
        "## 闭环要求",
        "",
        "开发 → PR → CI 全绿 → merge → 部署。",
        "修复后请在本 issue 说明改动点和影响范围，便于回到站内标记闭环。",
    ]
    return "\n".join(lines)


def strip_empty_structure(text: Any) -> str:
    """剥掉「现象：」「影响范围：」「期望结果：」这类**后面没有内容**的骨架行。

    只删完全空白的标签行：``现象：``（含全角冒号、尾随空格）会被删，
    ``现象：点导出没反应`` 原样保留。宁可少删不可多删 —— 误删正文内容比
    留三个空标签糟得多。
    """
    raw = str(text or "")
    if not raw:
        return ""
    kept = [ln for ln in raw.splitlines() if not _EMPTY_STRUCT_LABEL_RE.match(ln.strip())]
    return "\n".join(kept).strip()


def admin_notes(comments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """只取管理员写的、净化后非空的评论，按时间正序（调用方已排好，这里只过滤）。

    净化在**入口处**做：正文渲染和信息缺口判定都消费这个结果，一处清洗两边受益。
    """
    notes: list[dict[str, Any]] = []
    for comment in comments or []:
        if not isinstance(comment, dict) or not comment.get("is_admin"):
            continue
        cleaned = strip_empty_structure(comment.get("body"))
        if not cleaned:
            continue
        note = dict(comment)
        note["body"] = cleaned
        notes.append(note)
    return notes


def _quote(text: str) -> str:
    """把用户原文转成引用块。逐行加 ``> ``，空行保留成 ``>``。"""
    if not text:
        return "> （未填写描述）"
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())


def _render_clue_lines(clues: dict[str, Any]) -> list[str]:
    """代码线索段。

    探测失败**不能只写一句「未定位」就完事** —— 三种失败的可信度完全不同，
    开发侧需要知道该不该相信上下文：

    - ``transient_page``：已知不可信（中转页），必须明说「别用这个路径」；
    - ``unmatched``：路径存在但没路由，可能是前端路由/已下线；
    - ``no_path``：根本没记路径。

    成功时也要声明「这是提交时所在页面」，因为用户可能在 A 页遇到问题、
    切到 B 页才提反馈（Issue #93 实况）。
    """
    if not clues or not clues.get("matched"):
        clues = clues or {}
        path = str(clues.get("page_path") or "").strip()
        reason = str(clues.get("reason") or "").strip()

        if reason == "transient_page":
            endpoint = str(clues.get("transient_endpoint") or "").strip()
            return [
                f"- ⚠️ **提交时所在页面 `{path}` 是纯中转页**"
                + (f"（endpoint `{endpoint}`）" if endpoint else "")
                + "，登录/仓库选择一类，页面上没有任何业务内容，"
                "**不能作为出问题位置的证据**。",
                "- 原因：用户可能是**路过该页**时点开「反馈」的。",
                "- 请以「现象」为准：按关键词在 `templates/` 与 `blueprints/` 检索，"
                "或从描述提到的功能模块入手定位。",
            ]

        if reason == "unmatched" and path:
            return [
                f"- 未能自动定位：`{path}` 没有匹配到任何 GET 路由"
                "（可能是前端路由、已下线页面，或参数不合法）。",
                "- 请从用户原文和描述里推断入口，必要时按关键词在 `blueprints/` 全文检索。",
            ]

        return [
            "- 未能自动定位：提交时没有记录出问题的页面。",
            "- 请从用户原文和描述里推断入口，必要时按关键词在 `blueprints/` 全文检索。",
        ]

    lines: list[str] = [
        "- 以下位置依据是「**提交反馈时所在的页面**」，通常就是出问题的页面，"
        "但若与「现象」描述不符，**以现象为准**。",
    ]
    endpoint = str(clues.get("endpoint") or "").strip()
    rule = str(clues.get("rule") or clues.get("page_path") or "").strip()
    if rule and endpoint:
        lines.append(f"- 页面路由：`{rule}` → endpoint `{endpoint}`")

    source = str(clues.get("source") or "").strip()
    line_no = clues.get("line") or 0
    func = str(clues.get("func") or "").strip()
    if source:
        where = f"`{source}:{int(line_no)}`" if line_no else f"`{source}`"
        suffix = f"（`{func}`）" if func else ""
        lines.append(f"- 视图实现：{where}{suffix}")

    template = str(clues.get("template") or "").strip()
    if template:
        lines.append(f"- 渲染模板：`templates/{template}`")

    commits = clues.get("commits") or []
    if commits:
        lines.append("- 相关文件近期改动（谁动过这里）：")
        for commit in commits[:5]:
            if not isinstance(commit, dict):
                continue
            sha = str(commit.get("sha") or "").strip()
            date = str(commit.get("date") or "").strip()
            subject = str(commit.get("subject") or "").strip()
            lines.append(f"  - `{sha}` {date} {subject}".rstrip())

    if not lines:
        lines.append("- 未取到可用的代码位置信息。")
    return lines


def _render_acceptance(clues: dict[str, Any]) -> list[str]:
    """验收标准：每条都必须能被第三方复核，不写「体验变好」这类无法判定的词。

    第一条**不绑定具体页面路径**：路径来自「提交时所在页面」，可能与真实现象
    无关（Issue #93）。把错误路径写进验收标准，等于让开发侧改错文件还自认合格。
    改成「核对线索页面是否就是现象所在页面」，把它降级成一个待确认项。
    """
    items = ["- [ ] 按「现象」描述能稳定复现，修复后不再出现"]
    page = ""
    if clues and clues.get("matched"):
        page = str(clues.get("rule") or clues.get("page_path") or "").strip()
    if page:
        items.append(
            f"- [ ] 已核对「代码线索」指向的 `{page}` 确实就是现象所在页面；"
            "**不一致时以现象为准并在本 issue 说明真实页面**"
        )
    items += [
        "- [ ] 修复不改变其他页面和接口的现有行为",
        "- [ ] 全量 `pytest` 的失败集 ⊆ `tests/known_failures.txt`（零新增失败）",
        "- [ ] 在本 issue 评论说明改动点、影响范围与验证方式",
    ]
    return items


def detect_gaps(
    ticket: dict[str, Any] | None,
    notes: list[dict[str, Any]] | None,
    clues: dict[str, Any] | None,
) -> list[str]:
    """列出影响可执行性的信息缺口。

    刻意**不完整即阻断**：缺口只是写进正文的待办，不拦派发。用户描述粗糙是
    常态，为这个退回让他重填等于把系统的问题转嫁给他。
    """
    ticket = ticket if isinstance(ticket, dict) else {}
    gaps: list[str] = []

    if not (clues and clues.get("matched")):
        gaps.append("没有可自动定位的页面路径，需要人工从描述推断入口")
    if len(str(ticket.get("body") or "").strip()) < MIN_BODY_LEN:
        gaps.append(f"问题描述少于 {MIN_BODY_LEN} 字，缺少可复现的细节")
    if not admin_notes(notes):
        gaps.append("没有管理员处理意见，判定依据未知")
    elif max(len(str(n.get("body") or "").strip()) for n in admin_notes(notes)) < (
        MIN_ADMIN_NOTE_LEN
    ):
        gaps.append(f"处理意见少于 {MIN_ADMIN_NOTE_LEN} 字，没有说明期望结果")
    gaps.append("未提供复现步骤和期望行为 —— 这两项只能由开发侧从代码推断")
    return gaps


