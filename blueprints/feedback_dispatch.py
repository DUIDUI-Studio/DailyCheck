"""反馈派发：把管理员的处理评论变成一张 Gitea Issue，交给开发闭环。

设计约定：
- **绝不抛异常**。派发失败时路由必须能降级（保留评论、提示人工建单），
  不能因为外部 API 故障丢掉管理员已经写好的处理意见。
- **绝不把 token 写进日志**。所有失败原因只记 HTTP 状态码或异常类型名。
- Gitea 保留既有 ``attach_issue`` / label ID 接口。
- GitHub 仅 ``deliver_private`` 接受持久 claim：丢响应只恢复，不盲目重发。
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
import sqlite3
import urllib.error
import urllib.request
from typing import Any

import config
from scripts.hosting import (
    AccessRole,
    HostingClient,
    HostingConfig,
    HostingError,
    Provider,
    provider,
)

from .feedback_issue_body import (  # noqa: F401 - 对外 re-export
    MIN_ADMIN_NOTE_LEN,
    MIN_BODY_LEN,
    admin_notes,
    build_issue_body,
    build_issue_title,
    detect_gaps,
)
from .feedback_pure import (
    begin_private_creation,
    claim_private_delivery,
    finalize_private_delivery,
    release_private_delivery,
)

logger = logging.getLogger(__name__)

DISPATCH_TIMEOUT_SECONDS = 10.0
# 反馈派发的标记标签：Hermes 侧据此识别「这是用户反馈，不是普通 issue」。
FEEDBACK_LABEL = "user-feedback"
FEEDBACK_LABEL_COLOR = "0e8a16"
FEEDBACK_LABEL_DESC = "用户在站内提交的使用问题，由管理员确认后派发"


def dispatch_enabled() -> bool:
    """token 为空 = 未启用派发（降级为「只存评论」）。"""
    try:
        if provider() is Provider.GITHUB:
            return bool(HostingConfig.from_environment(AccessRole.FEEDBACK).token)
        return bool(str(getattr(config, "FEEDBACK_GITEA_TOKEN", "") or "").strip())
    except HostingError:
        return False


def repo_slug() -> str:
    if provider() is Provider.GITHUB:
        return HostingConfig.from_environment(AccessRole.FEEDBACK).repo
    return str(getattr(config, "FEEDBACK_GITEA_REPO", "") or "").strip() or "eric/dailycheck"


def web_base() -> str:
    """给人点的链接基址。"""
    if provider() is Provider.GITHUB:
        return HostingConfig.from_environment(AccessRole.FEEDBACK).web_url
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
    try:
        if provider() is Provider.GITHUB:
            # Private creation requires the saved draft and durable claim below.
            raise HostingError("private_delivery_receipt_required")
    except HostingError as exc:
        logger.warning("feedback_dispatch_hosting_error: type=%s status=%s", type(exc).__name__, exc.status)
        return None
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


def _confirmed_private_issue(client: HostingClient, data: object, payload: dict) -> dict:
    c = client.config
    if (
        not isinstance(data, dict)
        or type(data.get("id")) is not int or data["id"] <= 0
        or type(data.get("number")) is not int or data["number"] <= 0
        or "pull_request" in data
        or data.get("repository_url") != f"{c.api_url}/repos/{c.repo}"
        or data.get("url") != f"{c.api_url}/repos/{c.repo}/issues/{data['number']}"
        or data.get("html_url") != f"{c.web_url}/{c.repo}/issues/{data['number']}"
        or data.get("title") != payload["title"] or data.get("body") != payload["body"]
    ):
        raise HostingError("private_issue_confirmation_mismatch")
    return {"id": data["id"], "number": data["number"], "url": data["html_url"]}


def deliver_private(db, ticket_id: int, admin_id: int,
                    comment_id: int | None = None) -> dict | None:
    """Only this caller can create GitHub feedback; uncertainty never re-POSTs."""
    claim = None
    try:
        client = HostingClient(HostingConfig.from_environment(AccessRole.FEEDBACK))
        if client.config.provider is not Provider.GITHUB:
            raise HostingError("private_github_destination_required")
        client.verify_repository()
        claim = claim_private_delivery(db, ticket_id, admin_id, comment_id)
        if claim is None:
            return None
        payload = json.loads(claim.payload_json)
        root = f"/repos/{client.config.repo}"
        if claim.action == "create":
            labels = client.pages(root + "/labels")
            if not any(row.get("name") == FEEDBACK_LABEL for row in labels):
                client.request("POST", root + "/labels", {
                    "name": FEEDBACK_LABEL, "color": FEEDBACK_LABEL_COLOR,
                    "description": FEEDBACK_LABEL_DESC,
                })
            if not begin_private_creation(db, claim):
                return None
            data = client.request("POST", root + "/issues", payload)[0]
        else:
            # No search-index lag or open-only lookup: enumerate the complete destination.
            rows = client.pages(root + "/issues", params={"state": "all"})
            marker = f"<!-- dailycheck-private-request:v1 {claim.request_id} -->"
            matches = [row for row in rows if marker in str(row.get("body") or "")]
            if len(matches) != 1:
                raise HostingError("private_creation_uncertain")
            data = matches[0]
        issue = _confirmed_private_issue(client, data, payload)
        # Recheck privacy during recovery as well as before every mutation.
        client.verify_repository()
        return issue if finalize_private_delivery(db, claim, issue) else None
    except (HostingError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        logger.warning("feedback_private_delivery_blocked: type=%s status=%s",
                       type(exc).__name__, getattr(exc, "status", 0))
        return None
    finally:
        if claim is not None:
            try:
                release_private_delivery(db, claim)
            except sqlite3.Error as exc:
                logger.warning("feedback_private_release_failed: type=%s", type(exc).__name__)


def _ensure_label(token: str, repo: str, name: str) -> int | None:
    """确保标签存在，返回其 id；失败返回 None（不影响建单）。

    ⚠️ 必须「先查再建」：Gitea 不约束标签同名，重复 POST 同样返回 201 并
    再生成一个同名标签（2026-10-24 实测：连发两次 → 出现 #5/#6 两个
    user-feedback）。靠 409/422 判幂等在 Gitea 上不成立。
    """
    for page in range(1, 51):
        path = f"/repos/{repo}/labels?limit=50" + (f"&page={page}" if page > 1 else "")
        try:
            code, data = _request(token, path, None, method="GET")
        except Exception:
            logger.warning("feedback_dispatch_label_error: label=%s", name)
            return None
        if code != 200 or not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            return None
        for item in data:
            if item.get("name") == name and type(item.get("id")) is int:
                return item["id"]
        if len(data) < 50:
            break
    else:
        return None
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
