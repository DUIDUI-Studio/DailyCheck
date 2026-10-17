"""用户反馈 → 企业微信群机器人推送。

复用 ``wecom_notify_pure`` 的发送链路（``send_webhook_markdown`` /
``mask_webhook_url``），只新增模板与事件入口 —— 脱敏、超时、重试、
「绝不抛异常」这些安全边界不重写第二份。

Spec: docs/ARCHITECTURE.md §3.10（反馈闭环）。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import config
from blueprints.feedback_pure import summarize
from blueprints.wecom_notify_pure import mask_webhook_url, send_webhook_markdown

logger = logging.getLogger(__name__)

# 与订货单通知保持同一超时/重试口径（见 wecom_notify_pure 常量）。
FEEDBACK_THREAD_NAME = "wecom-feedback-notify"


def feedback_webhook_url() -> str:
    """反馈专用 webhook；空串 = 未配置 = 不推企微。

    调用时读 config（不在导入期绑定），便于测试替换与运行期判定。
    """
    return str(getattr(config, "WECOM_FEEDBACK_WEBHOOK", "") or "").strip()


def build_feedback_url(ticket_id: Any, base_url: str = "") -> str:
    """管理员处理页深链。``base_url`` 为空则整行省略（相对链接在企微点不开）。"""
    if ticket_id is None or str(ticket_id).strip() == "":
        return ""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/admin/feedback/{int(ticket_id)}"


def build_feedback_markdown(
    ticket: dict[str, Any] | None,
    base_url: str = "",
) -> str:
    """渲染「新的问题反馈」企微 markdown。

    模板锁定：标题 / 单号 / 提交人 / 分类 / 时间 / 正文摘要 / 链接。
    **不含**任何用户凭据或内部路径 —— 群里可能有人不该看到更深的东西。
    """
    ticket = ticket if isinstance(ticket, dict) else {}
    lines: list[str] = ["## 收到一条新的问题反馈"]

    ticket_no = str(ticket.get("ticket_no") or "").strip()
    if ticket_no:
        lines.append(f"> 单号：{ticket_no}")

    title = str(ticket.get("title") or "").strip()
    if title:
        lines.append(f"> 问题：**{summarize(title, 40)}**")

    category = str(ticket.get("category_label") or ticket.get("category") or "").strip()
    if category:
        lines.append(f"> 分类：{category}")

    submitter = str(ticket.get("submitter") or ticket.get("username") or "").strip()
    if submitter:
        lines.append(f"> 提交人：{submitter}")

    created = str(ticket.get("created_at") or "").strip()
    if created:
        lines.append(f"> 时间：{created}")

    url = build_feedback_url(ticket.get("id"), base_url)
    if url:
        lines.append(f"[去处理]({url})")

    return "\n".join(lines)


def notify_feedback_submitted(
    ticket: dict[str, Any] | None,
    background: bool = True,
) -> None:
    """反馈提交 → 企微群。绝不抛异常、绝不阻塞请求线程。"""
    try:
        url = feedback_webhook_url()
        if not url:
            # 未注入 env = 该通道关闭，静默跳过（不刷日志）
            return
        ticket_no = str((ticket or {}).get("ticket_no") or "")
        markdown = build_feedback_markdown(
            ticket,
            base_url=str(getattr(config, "PUBLIC_BASE_URL", "") or ""),
        )
        if background:
            threading.Thread(
                target=_send_safely,
                args=(url, markdown, ticket_no),
                name=FEEDBACK_THREAD_NAME,
                daemon=True,
            ).start()
        else:
            _send_safely(url, markdown, ticket_no)
    except Exception:  # 兜底：通知失败不得冒泡到提交事务
        logger.warning("wecom_feedback_error: ticket=%s",
                       (ticket or {}).get("ticket_no", ""), exc_info=True)


def _send_safely(webhook_url: str, markdown: str, ticket_no: str = "") -> None:
    """后台线程体：吞掉一切异常。

    成功与失败**都用 WARNING** —— 生产 root logger 阈值是 WARNING，
    对外消息是否真的出网必须可查（见 ARCHITECTURE §13.9）。
    """
    masked = mask_webhook_url(webhook_url)
    try:
        sent = send_webhook_markdown(webhook_url, markdown)
        logger.warning(
            "wecom_feedback_%s: ticket=%s url=%s",
            "ok" if sent else "failed", ticket_no, masked,
        )
    except Exception:  # 兜底：后台线程绝不冒泡
        logger.warning("wecom_feedback_failed: ticket=%s url=%s",
                       ticket_no, masked, exc_info=True)
