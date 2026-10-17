"""反馈 Issue 正文渲染：把反馈单 + 管理员判定 + 自动探测的代码线索拼成任务单。

为什么单独一个模块：正文是 Issue 的「验收契约」，规则多（四要素、信息缺口
判定、引用格式、验收项），与「怎么调 GitHub API」是两件事。混在一个文件里
两边都读不清，且会让 dispatch 突破单文件行数上限。

本模块是**纯函数**：不读库、不发网络、不依赖 Flask。给什么渲染什么，所以可以
直接用字典断言正文内容（见 ``tests/test_feedback_introspect.py``）。

Spec: docs/ARCHITECTURE.md §3.10（反馈闭环 / Issue 精确化）。
"""
from __future__ import annotations

from typing import Any

from .feedback_pure import summarize

# 「信息缺口」的判定阈值：低于这些长度的描述基本无法据以定位问题。
# 只用于正文提示，不阻断派发。
MIN_BODY_LEN = 20
MIN_ADMIN_NOTE_LEN = 15


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
        ("页面", ticket.get("page_path")),
        ("分类", ticket.get("category_label") or ticket.get("category")),
        ("提交时间", ticket.get("created_at")),
    ]
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


def admin_notes(comments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """只取管理员写的、非空的评论，按时间正序（调用方已排好，这里只过滤）。"""
    return [
        c for c in (comments or [])
        if isinstance(c, dict)
        and c.get("is_admin")
        and str(c.get("body") or "").strip()
    ]


def _quote(text: str) -> str:
    """把用户原文转成引用块。逐行加 ``> ``，空行保留成 ``>``。"""
    if not text:
        return "> （未填写描述）"
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())


def _render_clue_lines(clues: dict[str, Any]) -> list[str]:
    """代码线索段。探测失败时给一句实话 + 下一步，而不是留白。"""
    if not clues or not clues.get("matched"):
        path = str((clues or {}).get("page_path") or "").strip()
        reason = (
            f"`{path}` 没有匹配到任何 GET 路由（可能是前端路由、已下线页面，或参数不合法）"
            if path else "提交时没有记录出问题的页面"
        )
        return [
            f"- 未能自动定位：{reason}。",
            "- 请从用户原文和描述里推断入口，必要时按关键词在 `blueprints/` 全文检索。",
        ]

    lines: list[str] = []
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
    """验收标准：每条都必须能被第三方复核，不写「体验变好」这类无法判定的词。"""
    page = ""
    if clues and clues.get("matched"):
        page = str(clues.get("rule") or clues.get("page_path") or "").strip()
    first = (
        f"- [ ] 站内 `{page}` 页面按「现象」描述逐项复现，修复后不再出现"
        if page
        else "- [ ] 按「现象」描述能稳定复现，修复后不再出现"
    )
    return [
        first,
        "- [ ] 修复不改变其他页面和接口的现有行为",
        "- [ ] 全量 `pytest` 的失败集 ⊆ `tests/known_failures.txt`（零新增失败）",
        "- [ ] 在本 issue 评论说明改动点、影响范围与验证方式",
    ]


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


