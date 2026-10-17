"""反馈派发：把管理员的处理评论变成一张 GitHub Issue，交给开发闭环。

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

GITHUB_API = "https://api.github.com"
DISPATCH_TIMEOUT_SECONDS = 10.0
# 反馈派发的标记标签：Hermes 侧据此识别「这是用户反馈，不是普通 issue」。
FEEDBACK_LABEL = "user-feedback"
FEEDBACK_LABEL_COLOR = "0e8a16"
FEEDBACK_LABEL_DESC = "用户在站内提交的使用问题，由管理员确认后派发"


def dispatch_enabled() -> bool:
    """token 为空 = 未启用派发（降级为「只存评论」）。"""
    return bool(str(getattr(config, "FEEDBACK_GITHUB_TOKEN", "") or "").strip())


def repo_slug() -> str:
    return str(getattr(config, "FEEDBACK_GITHUB_REPO", "") or "").strip() or "ericmr1981/DailyCheck"


# ---------------------------------------------------------------------------
# GitHub API
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
    token = str(getattr(config, "FEEDBACK_GITHUB_TOKEN", "") or "").strip()
    if not token:
        logger.warning("feedback_dispatch_skip: no token configured")
        return None
    repo = repo_slug()
    labels: list[str] = []
    if label:
        if _ensure_label(token, repo, label):
            labels = [label]
        else:
            logger.warning("feedback_dispatch_label_unavailable: label=%s", label)

    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    data = _post_json(token, f"/repos/{repo}/issues", payload)
    if not isinstance(data, dict):
        return None
    number = data.get("number")
    url = str(data.get("html_url") or "").strip()
    if not isinstance(number, int):
        logger.warning("feedback_dispatch_failed: reason=no_issue_number")
        return None
    return {"number": number, "url": url}


def _ensure_label(token: str, repo: str, name: str) -> bool:
    """确保标签存在。201=新建成功，422=已存在（也算成功），其余算失败。"""
    payload = {
        "name": name,
        "color": FEEDBACK_LABEL_COLOR,
        "description": FEEDBACK_LABEL_DESC,
    }
    try:
        code, _ = _request(token, f"/repos/{repo}/labels", payload)
    except Exception:  # 兜底：标签失败不影响建单
        logger.warning("feedback_dispatch_label_error: label=%s", name)
        return False
    if code in (200, 201, 422):
        return True
    logger.warning("feedback_dispatch_label_http: label=%s status=%s", name, code)
    return False


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
    token: str, path: str, payload: dict[str, Any]
) -> tuple[int, Any]:
    """发一次 GitHub API 请求，返回 ``(status_code, parsed_json)``。

    只抛网络层异常（由调用方吞掉并只记类型名）。HTTP 错误码在这里
    变成返回值而不是异常，因为 422（标签已存在）是**预期分支**，不是错误。
    """
    request = urllib.request.Request(
        GITHUB_API + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            # Go 之外的 UA 也必须带：GitHub 对无 UA 的请求直接 403。
            "User-Agent": "DailyCheck-feedback-dispatch",
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
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
