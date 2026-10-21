"""Owner 拍板提醒 → 企业微信「反馈通知群」推送。

复用 ``wecom_notify_pure`` 的发送链路（``send_webhook_markdown`` /
``mask_webhook_url``）与 ``feedback_notify`` 的 webhook 取值口径 ——
脱敏、超时、重试、「绝不抛异常」这些边界不写第二份。

为什么复用反馈群而不是新建一个群：
owner 2026-10-21 指定「发到反馈通知群，那个群是企微这边的管理员」，
群里本来就只有管理员，且反馈单本身也需要他们处理，同一批人同一个群。

Spec: docs/ARCHITECTURE.md §3.12。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import config
from blueprints.feedback_notify import feedback_webhook_url
from blueprints.owner_approvals_pure import build_pending_markdown
from blueprints.wecom_notify_pure import mask_webhook_url, send_webhook_markdown

logger = logging.getLogger(__name__)

NOTIFY_THREAD_NAME = "wecom-owner-approval"


def notify_pending(row: Any, background: bool = True) -> bool:
    """登记成功后推一条企微。返回「这条消息是否真的发出去了」。

    同步调用（``background=False``）时返回值可直接用于写 ``notified_at`` ——
    登记是低频操作（一天几次），多等一次 3 秒超时换一个确定的成败，划算。
    推送失败不影响队列里的数据：owner 下次打开页面照样能看到。
    """
    try:
        url = feedback_webhook_url()
        if not url:
            # 未注入 env = 该通道关闭，静默跳过（与 feedback_notify 同口径）
            return False
        markdown = build_pending_markdown(
            row, base_url=str(getattr(config, "PUBLIC_BASE_URL", "") or "")
        )
        pr = str((row or {}).get("pr_number", "")) if isinstance(row, dict) else ""
        if background:
            threading.Thread(
                target=_send_safely,
                args=(url, markdown, pr),
                name=NOTIFY_THREAD_NAME,
                daemon=True,
            ).start()
            return True
        return _send_safely(url, markdown, pr)
    except Exception:  # 兜底：通知失败不得冒泡到登记事务
        logger.warning(
            "wecom_owner_approval_error: pr=%s",
            (row or {}).get("pr_number", "") if isinstance(row, dict) else "",
            exc_info=True,
        )
        return False


def _send_safely(webhook_url: str, markdown: str, pr: str = "") -> bool:
    """后台线程体：吞掉一切异常，返回是否发送成功。

    成功与失败**都用 WARNING** —— 生产 root logger 阈值是 WARNING，
    对外消息是否真的出网必须可查（同 feedback_notify / ARCHITECTURE §13.9）。
    """
    masked = mask_webhook_url(webhook_url)
    try:
        sent = send_webhook_markdown(webhook_url, markdown)
        logger.warning(
            "wecom_owner_approval_%s: pr=%s url=%s",
            "ok" if sent else "failed", pr, masked,
        )
        return bool(sent)
    except Exception:  # 兜底：后台线程绝不冒泡
        logger.warning(
            "wecom_owner_approval_failed: pr=%s url=%s", pr, masked, exc_info=True
        )
        return False
