"""反馈的「按 Issue 处理标准」预分析：提示词构造 + 模型输出解析 + 模板兜底。

为什么单独一层：LLM 的输出是**外部不可信输入**，必须像解析用户表单一样
逐字段校验/裁剪；而「怎么问模型」与「怎么调模型」是两件事（后者在
``feedback_llm``）。本模块纯函数、不出网、不读库，所以能直接用字典断言
提示词内容与解析结果。

分析在链路里的位置：**一审通过 → 分析 → 二审派发**。它只产出建议，
派发与否由管理员二审决定 —— 模型判断「不需要修」也不自动关单。

数据契约（模型必须回这段 JSON，其他自由文本一律丢弃）::

    {
      "verdict": "confirmed | suspected | not_a_defect | needs_decision",
      "severity": "high | medium | low",
      "summary": "一句话结论",
      "root_cause": "根因假设（说清依据）",
      "affected": ["blueprints/x.py:120 — 这里没做空值判断"],
      "reproduce": "复现路径 / 触发条件",
      "acceptance": ["可复核的验收项"],
      "gaps": ["缺哪些信息导致不能确定"],
      "notes": "给管理员的一句话提醒（涉及产品决策时必须写在这里）"
    }

正文**不由模型写**：Issue 骨架仍由 ``feedback_issue_body.build_issue_body``
渲染（四要素 + 闭环要求都有既有测试守着），模型的分析作为其中一节插入。
让模型自由写整篇正文，结构会漂、幻觉路径会混进「代码线索」，比没有更坏。

Spec: docs/ARCHITECTURE.md §3.10（反馈闭环 / 二审制与 LLM 预分析）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .feedback_issue_body import build_issue_body, build_issue_title, render_acceptance
from .feedback_pure import CATEGORIES, DEFAULT_CATEGORY, summarize

VERDICTS: dict[str, str] = {
    "confirmed": "确认是缺陷，需要修",
    "suspected": "疑似问题，需先复现",
    "not_a_defect": "符合预期，可能不是缺陷",
    "needs_decision": "涉及产品决策，需 owner 拍板",
}
DEFAULT_VERDICT = "suspected"
SEVERITIES: dict[str, str] = {
    "high": "高",
    "medium": "中",
    "low": "低",
}
DEFAULT_SEVERITY = "medium"

# 分析来源标记：模板兜底的分析要显式标出来，不能和真模型结果长得一样。
SOURCE_LLM = "llm"
SOURCE_TEMPLATE = "template"
SOURCE_LABELS: dict[str, str] = {
    SOURCE_LLM: "模型分析",
    SOURCE_TEMPLATE: "模板兜底（未调用模型）",
}

# 字段长度上限：模型偶尔会把整段代码贴回来，正文会因此胀到不可读。
FIELD_MAX_LEN = 600
LIST_ITEM_MAX_LEN = 240
MAX_LIST_ITEMS = 8
MAX_SUMMARY_LEN = 200

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)

SYSTEM_PROMPT = """你是 DailyCheck 库存系统的缺陷分析员。管理员已在站内一审通过一条用户反馈，
你的任务是把这条粗糙反馈变成一张**可直接执行的任务单所需的判断依据**。

规则（必须全部遵守）：
1. 只做分析，不改代码、不做产品决策。涉及行为语义变更时 verdict 用 needs_decision。
2. 一切结论必须有依据：写「文件:行号」或引用给你的代码片段；看不出来就写进 gaps。
3. 不要臆造不存在的函数名、路由名、变量名。没有证据的地方明确说「需确认」。
4. 用户措辞含糊是常态，不要因为信息少就拒答；把缺口列进 gaps 即可。
5. 验收项必须可被第三方复核（能跑、能看、能对比），不许写「体验变好」这类无法判定的词。
6. 只输出 JSON，不要解释，不要 markdown 代码围栏之外的内容。

输出 JSON 结构：
{
  "verdict": "confirmed | suspected | not_a_defect | needs_decision",
  "severity": "high | medium | low",
  "summary": "一句话结论（不超过 60 字）",
  "root_cause": "根因假设，说明依据",
  "affected": ["blueprints/xxx.py:120 — 具体问题点"],
  "reproduce": "复现路径或触发条件",
  "acceptance": ["可复核的验收项"],
  "gaps": ["缺失的信息"],
  "notes": "给管理员的一句话提醒（涉及产品决策/风险时必填）"
}"""


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

def build_messages(
    ticket: dict[str, Any] | None,
    comments: list[dict[str, Any]] | None = None,
    clues: dict[str, Any] | None = None,
    snippets: str = "",
) -> list[dict[str, str]]:
    """组装 chat messages。只喂与判断相关的事实，不喂整张表。"""
    ticket = ticket if isinstance(ticket, dict) else {}
    lines: list[str] = [
        "## 反馈单",
        "",
        f"- 单号：{ticket.get('ticket_no') or '（未知）'}",
        f"- 分类：{ticket.get('category_label') or CATEGORIES[DEFAULT_CATEGORY]}",
        f"- 标题：{summarize(ticket.get('title') or '', 120) or '（无）'}",
        f"- 提交时所在页面：{ticket.get('page_path') or '（未记录）'}",
        f"- 实际页面（管理员指定）：{ticket.get('page_path_override') or '（无）'}",
        f"- 所在仓库：{ticket.get('warehouse_code') or '（未记录）'}",
        "",
        "### 用户原始描述",
        "",
        _block(str(ticket.get("body") or ""), 2000),
        "",
        "### 管理员一审意见",
        "",
    ]
    notes = [
        f"- {summarize(str(c.get('body') or ''), 400)}"
        for c in (comments or [])
        if isinstance(c, dict) and c.get("is_admin")
    ]
    lines += notes or ["（管理员未填写意见）"]

    lines += ["", "### 机器定位到的代码位置", ""]
    lines += _clue_lines(clues)
    if str(snippets or "").strip():
        lines += ["", "### 相关代码片段", "", _block(snippets, 7000)]
    lines += [
        "",
        "请按要求输出 JSON。重点判断：这是否是真实缺陷、最可能的根因在哪、",
        "开发侧要改什么、怎么验收、还缺什么信息。",
    ]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _clue_lines(clues: dict[str, Any] | None) -> list[str]:
    clues = clues if isinstance(clues, dict) else {}
    if not clues.get("matched"):
        reason = str(clues.get("reason") or "").strip()
        hint = {
            "transient_page": "提交时停在登录/选仓这类中转页，该路径不能作为出问题位置的证据。",
            "unmatched": "记录的路径没有匹配到任何路由，可能是前端路由或已下线页面。",
            "no_path": "没有记录出问题的页面。",
        }.get(reason, "没有可用的代码线索。")
        return [f"- {hint}", "- 请仅依据用户描述与代码片段推断，并在 gaps 里说明。"]
    out = []
    rule = str(clues.get("rule") or "").strip()
    endpoint = str(clues.get("endpoint") or "").strip()
    if rule and endpoint:
        out.append(f"- 页面路由：{rule} → endpoint {endpoint}")
    source = str(clues.get("source") or "").strip()
    if source:
        line = clues.get("line") or 0
        out.append(f"- 视图实现：{source}:{line}" if line else f"- 视图实现：{source}")
    template = str(clues.get("template") or "").strip()
    if template:
        out.append(f"- 渲染模板：templates/{template}")
    return out or ["- 未取到可用位置。"]


def _block(text: str, limit: int) -> str:
    """包成围栏块并截断 —— 用户描述里的 markdown 不该改写提示词结构。"""
    body = str(text or "").strip()
    if len(body) > limit:
        body = body[:limit] + "\n…（已截断）"
    return "```\n" + body + "\n```" if body else "（空）"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def extract_json(text: Any) -> dict[str, Any]:
    r"""从模型回复里抠出 JSON 对象；抠不出 raise ValueError。

    三种现实情况都要吃下：纯 JSON、``\`\`\`json`` 围栏、JSON 前后带解释文字
    （模型偶尔还是会加一句「分析如下」）。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("模型返回为空")
    fence = _JSON_FENCE_RE.search(raw)
    candidates = [fence.group(1)] if fence else []
    candidates.append(raw)
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("模型返回里没有可用 JSON")


def parse_analysis(text: Any) -> dict[str, Any]:
    """模型回复 → 归一化分析字典。缺字段按默认值补，非法值回落默认。

    宽容而非严格：模型漏个 ``severity`` 不该让整次分析作废 —— 那会把一次
    几十秒的推理变成白跑一趟。但 ``summary`` 与 ``acceptance`` 都空的话
    确实等于没分析，此时 raise，交给上层降级。
    """
    data = extract_json(text)
    summary = _clip(data.get("summary"), MAX_SUMMARY_LEN)
    acceptance = _as_list(data.get("acceptance"))
    if not summary and not acceptance:
        raise ValueError("模型返回缺少 summary 与 acceptance")

    verdict = _pick(data.get("verdict"), VERDICTS, DEFAULT_VERDICT)
    severity = _pick(data.get("severity"), SEVERITIES, DEFAULT_SEVERITY)
    return {
        "verdict": verdict,
        "verdict_label": VERDICTS[verdict],
        "severity": severity,
        "severity_label": SEVERITIES[severity],
        "summary": summary,
        "root_cause": _clip(data.get("root_cause"), FIELD_MAX_LEN),
        "affected": _as_list(data.get("affected")),
        "reproduce": _clip(data.get("reproduce"), FIELD_MAX_LEN),
        "acceptance": acceptance,
        "gaps": _as_list(data.get("gaps")),
        "notes": _clip(data.get("notes"), FIELD_MAX_LEN),
        "source": SOURCE_LLM,
        "source_label": SOURCE_LABELS[SOURCE_LLM],
    }


def _pick(value: Any, table: dict[str, str], default: str) -> str:
    text = str(value or "").strip().lower()
    if text in table:
        return text
    # 模型有时回中文（「确认需要修复」），先按标签做双向包含，再按关键词兜。
    for key, label in table.items():
        if text and (text == label or text in label or label in text):
            return key
    for key, words in _VERDICT_KEYWORDS.items():
        if key in table and any(word in text for word in words):
            return key
    return default


# 中文同义说法 → 枚举。模型没照 prompt 回英文时，靠这张表兜住，
# 否则一条本来可用的分析会被降级成「疑似」（默认值）。
_VERDICT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "confirmed": ("确认", "确实是", "是缺陷"),
    "not_a_defect": ("符合预期", "不是缺陷", "非缺陷", "无需修复"),
    "needs_decision": ("决策", "拍板", "需确认产品", "语义变更"),
}


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strip_checkboxes(items: list[str]) -> list[str]:
    """``"- [ ] 复现"`` → ``"复现"``：验收项在分析段里是列表项，不该带复选框。

    ``lstrip("- [ ] ")`` 是错的做法 —— lstrip 按字符集剥离，会把以 ``-``/空格
    开头的内容一起吃进去（ruff B005 拦的就是这个）。
    """
    out: list[str] = []
    for item in items or []:
        text = str(item or "").strip()
        for prefix in ("- [ ] ", "- [x] ", "- ", "* "):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        if text:
            out.append(text)
    return out


def _as_list(value: Any) -> list[str]:
    """把 ``list`` / ``str`` / ``dict`` 统一成去空、去重、限长的字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [v for v in re.split(r"[\n；;]+", value)]
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value if str(v or "").strip()]
    elif isinstance(value, dict):
        items = [f"{k}：{v}" for k, v in value.items() if str(v or "").strip()]
    else:
        items = [str(value)]
    out: list[str] = []
    for item in items:
        text = _clip(item, LIST_ITEM_MAX_LEN)
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_LIST_ITEMS:
            break
    return out


# ---------------------------------------------------------------------------
# 兜底与渲染
# ---------------------------------------------------------------------------

def fallback_analysis(
    ticket: dict[str, Any] | None,
    comments: list[dict[str, Any]] | None,
    clues: dict[str, Any] | None,
    reason: str = "",
) -> dict[str, Any]:
    """不调模型时的分析（未配置 key / 调用失败 / 解析失败）。

    它**不是**假装的模型结果：``source=template`` 会在二审页明确写出
    「未调用模型」，避免管理员误以为根因已被模型确认过。
    """
    ticket = ticket if isinstance(ticket, dict) else {}
    category = str(ticket.get("category") or DEFAULT_CATEGORY)
    severity = {"data": "high", "bug": "medium"}.get(category, "low")
    gaps = ["未做模型分析" + (f"（{reason}）" if reason else "") + "：根因未经推演，需人工判断"]
    if not (clues and clues.get("matched")):
        gaps.append("没有可自动定位的页面路径")
    if not [c for c in (comments or []) if isinstance(c, dict) and c.get("is_admin")]:
        gaps.append("管理员一审未填意见，判定依据未知")
    return {
        "verdict": "suspected",
        "verdict_label": VERDICTS["suspected"],
        "severity": severity,
        "severity_label": SEVERITIES[severity],
        "summary": summarize(ticket.get("title") or "", 60) or "（无标题）",
        "root_cause": "",
        "affected": (
            [f"{clues.get('source')}:{clues.get('line')}"]
            if clues and clues.get("matched") and clues.get("source")
            else []
        ),
        "reproduce": "",
        "acceptance": _strip_checkboxes(render_acceptance(clues)),
        "gaps": gaps,
        "notes": "本条未经过模型分析，请二审时自行核对根因与验收项。",
        "source": SOURCE_TEMPLATE,
        "source_label": SOURCE_LABELS[SOURCE_TEMPLATE],
    }


def render_analysis(analysis: dict[str, Any]) -> str:
    """分析字典 → Issue 正文里的一节 markdown。

    二审页直接复用同一段渲染，保证「屏幕上看到的」与「Issue 里写的」一致 ——
    两个维护两套渲染是正文漂移的经典起点。
    """
    a = analysis if isinstance(analysis, dict) else {}
    lines = [
        f"- **结论**：{a.get('verdict_label') or VERDICTS[DEFAULT_VERDICT]}"
        f"（严重度 {a.get('severity_label') or SEVERITIES[DEFAULT_SEVERITY]}）",
    ]
    if a.get("source") == SOURCE_TEMPLATE:
        lines.append(f"- **分析来源**：{a.get('source_label')}")
    if a.get("summary"):
        lines.append(f"- **摘要**：{a['summary']}")
    if a.get("root_cause"):
        lines.append(f"- **根因假设**：{a['root_cause']}")
    for key, label in (("affected", "可疑位置"), ("reproduce", "复现"),
                       ("acceptance", "建议验收项"), ("gaps", "信息缺口"),
                       ("notes", "提醒")):
        value = a.get(key)
        if isinstance(value, list):
            for item in value:
                lines.append(f"- **{label}**：{item}")
        elif value:
            lines.append(f"- **{label}**：{value}")
    return "\n".join(lines)


def build_draft(
    ticket: dict[str, Any] | None,
    comments: list[dict[str, Any]] | None,
    clues: dict[str, Any] | None,
    analysis: dict[str, Any] | None,
    base_url: str = "",
) -> tuple[str, str]:
    """生成二审页的 (Issue 标题, Issue 正文) 初值。

    正文骨架仍走 ``build_issue_body``（四要素 / 闭环要求 / 验收标准有既有测试），
    模型分析只作为其中一节插入。这样「有分析」「没分析」两条路径产出同一套
    结构，开发侧不用分辨今天拿到的是哪种单子。
    """
    title = build_issue_title(ticket)
    ticket = dict(ticket or {})
    # 「分析用页面」若与提交时页面不同，就是管理员在一审里做的纠正。
    # 正文必须把两个都写出来（谁改的能追溯），所以转成渲染层认的 override 键。
    used = str(ticket.get("analysis_page_path") or "").strip()
    if used and used != str(ticket.get("page_path") or "").strip():
        ticket["page_path_override"] = used
    body = build_issue_body(
        ticket,
        comments,
        base_url=base_url,
        code_clues=clues,
        analysis_section=render_analysis(analysis) if analysis else "",
    )
    return title, body


def analysis_view(ticket: dict[str, Any] | None) -> dict[str, Any]:
    """从单据行里的 ``analysis_text`` 还原分析（fail-soft，坏的返回空字典）。

    渲染路径绝不能因为一段历史脏数据 500：解析失败就当没有分析，
    二审页仍能用模板正文派发。
    """
    ticket = ticket if isinstance(ticket, dict) else {}
    raw = str(ticket.get("analysis_text") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    verdict = _pick(data.get("verdict"), VERDICTS, DEFAULT_VERDICT)
    severity = _pick(data.get("severity"), SEVERITIES, DEFAULT_SEVERITY)
    source = str(data.get("source") or SOURCE_LLM)
    if source not in SOURCE_LABELS:
        source = SOURCE_LLM
    return {
        **data,
        "verdict": verdict,
        "verdict_label": VERDICTS[verdict],
        "severity": severity,
        "severity_label": SEVERITIES[severity],
        "source": source,
        "source_label": SOURCE_LABELS[source],
    }


def analysis_to_json(analysis: dict[str, Any]) -> str:
    """分析字典 → 落库字符串（ensure_ascii=False，便于人直接读库排查）。"""
    return json.dumps(analysis or {}, ensure_ascii=False, sort_keys=True)
