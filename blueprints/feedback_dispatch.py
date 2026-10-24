"""反馈派发：把管理员的处理评论变成一张 Gitea Issue，交给开发闭环。

设计约定：
- **绝不抛异常**。派发失败时路由必须能降级（保留评论、提示人工建单），
  不能因为外部 API 故障丢掉管理员已经写好的处理意见。
- **绝不把 token 写进日志**。所有失败原因只记 HTTP 状态码或异常类型名。
- 幂等由调用方保证：``feedback_pure.attach_issue`` 只在
  ``issue_number IS NULL`` 时更新，重复点「发送」不会造出第二张单。
- 纯标准库（``urllib``），与 wecom_notify_pure 的口径一致。
- **正文渲染不在本模块**：见 ``feedback_issue_body``。这里 re-export 是为了让
  调用方（路由与测试）继续按 ``feedback_dispatch.build_issue_body`` 访问，
  避免为一次纯重构改动所有引用点。

平台：自托管 Gitea（2026-10-22 GitHub 账号封禁后迁移）。
Spec: docs/ARCHITECTURE.md §3.10（反馈闭环）。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

import config

from .feedback_issue_body import (  # noqa: F401 - 对外 re-export
    MIN_ADMIN_NOTE_LEN,
    MIN_BODY_LEN,
    admin_notes,
    build_issue_body,
    build_issue_title,
    detect_gaps,
)

logger = logging.getLogger(__name__)

DISPATCH_TIMEOUT_SECONDS = 10.0
# 反馈派发的标记标签：Hermes 侧据此识别「这是用户反馈，不是普通 issue」。
FEEDBACK_LABEL = "user-feedback"
FEEDBACK_LABEL_COLOR = "0e8a16"
FEEDBACK_LABEL_DESC = "用户在站内提交的使用问题，由管理员确认后派发"


def dispatch_enabled() -> bool:
    """token 为空 = 未启用派发（降级为「只存评论」）。"""
    return bool(str(getattr(config, "FEEDBACK_GITEA_TOKEN", "") or "").strip())


def repo_slug() -> str:
    return str(getattr(config, "FEEDBACK_GITEA_REPO", "") or "").strip() or "eric/dailycheck"


def web_base() -> str:
    """给人点的链接基址。"""
    return str(getattr(config, "FEEDBACK_GITEA_WEB_BASE", "") or "").strip().rstrip("/")


def issue_url(number: int, data: dict[str, Any] | None = None) -> str:
    """站内回填的 Issue 链接。

    ⚠️ 不能直接信 ``html_url``：它由 Gitea 实例的 ROOT_URL 决定，生产指向内网
    172.28.129.216:3000，办公端打不开（2026-10-24 实测）。所以按
    ``FEEDBACK_GITEA_WEB_BASE`` 自己拼；该键为空时才退回 html_url。
    """
    base = web_base()
    if base:
        return f"{base}/{repo_slug()}/issues/{number}"
    return str((data or {}).get("html_url") or "").strip()


# ---------------------------------------------------------------------------
# Gitea API
# ---------------------------------------------------------------------------

def create_issue(
    title: str,
    body: str,
    label: str | None = FEEDBACK_LABEL,
) -> dict[str, Any] | None:
    """创建 Issue，成功返回 ``{"number", "url"}``，任何失败返回 None。

    label 处理：先尽力确保标签存在（已存在不算错），确保不了就不带标签建单 ——
    标签是「给自动化识别用」的增强，不是创建 Issue 的前提。
    """
    token = str(getattr(config, "FEEDBACK_GITEA_TOKEN", "") or "").strip()
    if not token:
        logger.warning("feedback_dispatch_skip: no token configured")
        return None
    repo = repo_slug()
    label_ids: list[int] = []
    if label:
        lid = _ensure_label(token, repo, label)
        if lid:
            label_ids = [lid]
        else:
            logger.warning("feedback_dispatch_label_unavailable: label=%s", label)

    # ⚠️ Gitea 的 CreateIssueOption.labels 收的是**标签 id**，不是名字
    #    （GitHub 收名字）。传名字会 422，且失败信息里看不到原因。
    payload: dict[str, Any] = {"title": title, "body": body}
    if label_ids:
        payload["labels"] = label_ids
    data = _post_json(token, f"/repos/{repo}/issues", payload)
    if not isinstance(data, dict):
        return None
    number = data.get("number")
    if not isinstance(number, int):
        logger.warning("feedback_dispatch_failed: reason=no_issue_number")
        return None
    return {"number": number, "url": issue_url(number, data)}


def _ensure_label(token: str, repo: str, name: str) -> int | None:
    """确保标签存在，返回其 id；失败返回 None（不影响建单）。

    ⚠️ 必须「先查再建」：Gitea 不约束标签同名，重复 POST 同样返回 201 并
    再生成一个同名标签（2026-10-24 实测：连发两次 → 出现 #5/#6 两个
    user-feedback）。靠 409/422 判幂等在 Gitea 上不成立。
    """
    try:
        code, data = _request(token, f"/repos/{repo}/labels?limit=50", None, method="GET")
    except Exception:
        logger.warning("feedback_dispatch_label_error: label=%s", name)
        return None
    if code == 200 and isinstance(data, list):
        for item in data:
            if item.get("name") == name and isinstance(item.get("id"), int):
                return item["id"]
    payload = {
        "name": name,
        "color": FEEDBACK_LABEL_COLOR,
        "description": FEEDBACK_LABEL_DESC,
    }
    try:
        code, data = _request(token, f"/repos/{repo}/labels", payload)
    except Exception:  # 兜底：标签失败不影响建单
        logger.warning("feedback_dispatch_label_error: label=%s", name)
        return None
    if code in (200, 201) and isinstance(data, dict) and isinstance(data.get("id"), int):
        return data["id"]
    logger.warning("feedback_dispatch_label_http: label=%s status=%s", name, code)
    return None


def _post_json(token: str, path: str, payload: dict[str, Any]) -> Any:
    """POST JSON 并解析响应；失败返回 None（绝不抛异常）。"""
    try:
        code, data = _request(token, path, payload)
    except Exception as exc:  # 兜底：含 Request 构造期异常
        logger.warning("feedback_dispatch_error: path=%s type=%s",
                       path, type(exc).__name__)
        return None
    if code not in (200, 201):
        logger.warning("feedback_dispatch_http: path=%s status=%s", path, code)
        return None
    return data


def _request(
    token: str, path: str, payload: dict[str, Any] | None, method: str = "POST"
) -> tuple[int, Any]:
    """发一次 Gitea API 请求，返回 ``(status_code, parsed_json)``。

    只抛网络层异常（由调用方吞掉并只记类型名）。HTTP 错误码在这里
    变成返回值而不是异常，因为 422（标签已存在）是**预期分支**，不是错误。
    """
    request = urllib.request.Request(
        str(getattr(config, "FEEDBACK_GITEA_API", "") or "").rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "User-Agent": "DailyCheck-feedback-dispatch",
            "Accept": "application/json",
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=DISPATCH_TIMEOUT_SECONDS) as resp:
            return resp.status, _parse_json(resp.read())
    except urllib.error.HTTPError as exc:
        # 读掉响应体避免连接泄漏；失败原因只留状态码，不回显响应文本
        try:
            exc.read()
        except Exception:
            pass
        return exc.code, None


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads((body or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
