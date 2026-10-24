"""Owner 拍板队列的纯函数层：规范化 / 状态机 / 消息模板 / 令牌比对。

**为什么是「登记制」而不是站内反查代码托管平台**

判定的第一现场在 Prod Agent —— 它读 PR diff、对照红线清单，只有它知道这次
为什么被拦。站内反过来猜（open PR + CI 状态 + 评论措辞）等于把判据复制一份，
两份迟早不一致；而且反查要额外持读权限、每开一次页面打一轮 API，偏偏它是
用来兜底的。所以本表 = 待拍板的唯一事实来源，页面不依赖外网。

**为什么批准后不在这里 merge**

merge → 部署链路（release / 蓝绿）由 Prod Agent 持有，应用侧不该拿到写权限。
本模块只记录「owner 说可以」，谁执行、执行结果如何由 ``consumed_at`` /
``consume_result`` 回写。

Spec: docs/ARCHITECTURE.md §3.12（Owner 拍板队列）。
"""
from __future__ import annotations

import hmac
from typing import Any

# 状态机：pending ──┬→ approved ──┐
#                   └→ rejected ──┴→ consumed_at 非空（Agent 已执行完，status 不变）
#
# 「执行完」不设独立 status：status 记的是 owner 的**决定**，不是进展。
# 把 approved 改成 consumed 会让「当初批了还是驳了」这个信息永久丢失。
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# 站内可点的两个动作（消费由 Agent 写 consumed_at，不由页面写）
DECISIONS = (STATUS_APPROVED, STATUS_REJECTED)

# 字段长度上限：企微 markdown 单条 4096 字节，这里按「一个人能读完」定，
# 不按协议上限定 —— 超长原因 Prod Agent 应该写在自己的 issue 里，链过来即可。
MAX_TITLE = 200
MAX_REASON = 500
MAX_NOTE = 200
MAX_RISK_KIND = 60
MAX_URL = 300


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def normalize_registration(payload: Any) -> dict[str, Any]:
    """规范化 Prod Agent 的登记请求；不合法抛 ``ValueError``。

    只强制 ``pr``：其余字段缺就留空。宁可页面上少一句说明，也不能因为
    Prod Agent 少传一个字段，就把「有东西卡着」这件事整个丢掉。
    """
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 JSON 对象")

    raw_pr = payload.get("pr")
    # bool 是 int 的子类；float 截断后是「另一个 PR」—— 两者都必须单独挡掉。
    if isinstance(raw_pr, bool) or isinstance(raw_pr, float):
        raise ValueError("pr 必须是整数")
    try:
        pr = int(raw_pr)
    except (TypeError, ValueError):
        raise ValueError("pr 必须是整数") from None
    if pr <= 0:
        raise ValueError("pr 必须为正整数")

    url = _clip(payload.get("url"), MAX_URL)
    # 只收 http(s)：这个值最终要渲染进模板的 href，
    # 放行 javascript:/data: 等于给自己开一个存储型 XSS。
    if not url.startswith(("https://", "http://")):
        url = ""

    return {
        "pr_number": pr,
        "pr_title": _clip(payload.get("title"), MAX_TITLE),
        "pr_url": url,
        "risk_kind": _clip(payload.get("risk_kind"), MAX_RISK_KIND),
        "reason": _clip(payload.get("reason"), MAX_REASON),
    }


def normalize_note(value: Any) -> str:
    return _clip(value, MAX_NOTE)


def decide_allowed(status: str) -> bool:
    """只有 pending 能被打回重判。

    已 approved / rejected 再点同一个按钮属幂等（路由层直接返回「已是该状态」），
    点另一个按钮则必须拒绝 —— 否则 owner 会以为自己改主意了，实际 Agent 已经
    拿着旧决定去 merge 了。
    """
    return status == STATUS_PENDING


def target_status(decision: str) -> str:
    if decision not in DECISIONS:
        raise ValueError(f"不支持的决定：{decision!r}")
    return decision


def build_approvals_url(base_url: str = "") -> str:
    """待办页深链；``base_url`` 为空返回空串（相对链接在企微点不开）。"""
    base = str(base_url or "").strip().rstrip("/")
    return f"{base}/ops/approvals" if base else ""


def build_pending_markdown(row: Any, base_url: str = "") -> str:
    """渲染「有东西等你拍板」企微 markdown。

    模板锁定：PR 号 / 标题 / 红线类型 / 原因 / 链接。
    **不含**仓库内部路径、token、diff 内容 —— 群里可能不止一个人。
    """
    row = row if isinstance(row, dict) else {}
    pr = row.get("pr_number")
    lines: list[str] = [
        f"## 待拍板：PR #{pr}" if pr else "## 待拍板：新的一项"
    ]

    title = _clip(row.get("pr_title") or row.get("title"), 60)
    if title:
        lines.append(f"> 标题：{title}")

    kind = _clip(row.get("risk_kind"), 40)
    if kind:
        lines.append(f'> 红线：<font color="warning">{kind}</font>')

    reason = _clip(row.get("reason"), 200)
    if reason:
        lines.append(f"> 原因：{reason}")

    decided = _clip(row.get("decided_by"), 20)
    if decided:
        lines.append(f"> 已由 {decided} 批准，等 Prod Agent 执行")

    url = build_approvals_url(base_url)
    if url:
        lines.append(f"[去批准]({url})")

    return "\n".join(lines)


def token_matches(provided: Any, expected: Any) -> bool:
    """常量时间比对，长度不同也不提前返回。

    ``expected`` 为空表示该通道未启用 —— 一律 False，绝不「没配就放行」。
    """
    exp = str(expected or "")
    got = str(provided or "")
    if not exp or not got:
        return False
    return hmac.compare_digest(got.encode("utf-8"), exp.encode("utf-8"))
