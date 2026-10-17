"""企业微信群机器人推送（系统首个对外通知通道）。

设计要点：
- v1 只登记「门店新订货单提交」一个事件（``EVENT_ORDER_SUBMITTED``）。
  其他事件（审批 / 发货 / 取消）不推送，但事件→通道映射
  ``WEBHOOK_EVENT_MAP`` 是扩展位：新增事件只需在该表登记一行，
  发送链路（构建 → 发送 → 后台线程 → 日志脱敏）完全复用。
- 纯标准库（``urllib``），不引入 ``requests``。
- 本模块**绝不向上抛异常**：对外通道故障不得影响下单事务。含畸形 URL——
  ``urllib.request.Request()`` 会在构造期对缺 scheme / scheme 拼错的地址抛
  ``ValueError``，其文本带完整 URL（``key=`` 明文），所以它必须被就地降级，
  不能让它冒到日志里。
- webhook URL 含 `key=`（等同密钥），日志里一律经
  :func:`mask_webhook_url` 脱敏，禁止打印完整 URL。

Spec: docs/ARCHITECTURE.md §3.9 外部通知通道（企微群机器人）。
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import config
from blueprints.store_ordering_pure import EVENT_ORDER_SUBMITTED

logger = logging.getLogger(__name__)

# 企微群机器人 webhook：连接超时 3s（下单请求走后台线程，不阻塞用户）。
WECOM_TIMEOUT_SECONDS: float = 3.0
# 失败重试 1 次（等于最多 2 次尝试）。企微偶发网络抖动靠这一次兜住，
# 再失败就丢——订货单本身已经落库，通知只是锦上添花。
WECOM_MAX_RETRIES: int = 1

# 事件 → config 属性名（值 = webhook URL，空串 = 该事件未启用）。
# 扩展位：后续要做「审批通过 / 已发货」推送时，在这里加一行
# EVENT_ORDER_APPROVED: "WECOM_STORE_ORDER_WEBHOOK" 即可，无需改发送链路。
WEBHOOK_EVENT_MAP: dict[str, str] = {
    EVENT_ORDER_SUBMITTED: "WECOM_STORE_ORDER_WEBHOOK",
}


# ---------------------------------------------------------------------------
# 消息模板
# ---------------------------------------------------------------------------

def order_item_counts(order: dict[str, Any] | None) -> tuple[int | None, float | None]:
    """从订单字典的行项目推导 (品项数, 总件数)。取不到返回 (None, None)。

    ``order`` 通常是 ``store_ordering_pure.get_order_detail()`` 的返回，
    里面带 ``order_items``。没有该键（或为空）时不猜——调用方据此省略
    模板里的「品项」行，绝不编造计数。
    """
    if not isinstance(order, dict):
        return None, None
    items = order.get("order_items")
    if not isinstance(items, list) or not items:
        return None, None
    total = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            total += float(item.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
    return len(items), total


def build_store_order_markdown(
    order: dict[str, Any],
    item_count: int | None = None,
    total_qty: float | None = None,
    submitted_at: str | datetime | None = None,
    base_url: str = "",
) -> str:
    """渲染「新订货单待处理」企微 markdown。

    模板（锁定）：标题 + 单号 / 门店 / 品项 / 时间 + 单据链接。
    **不含金额**——订货金额属于敏感信息，工作群不需要。
    ``item_count`` / ``total_qty`` 任一缺失即省略对应文案；
    ``submitted_at`` 缺省时回退订单 ``created_at``，都解析不出来就省略该行。
    """
    order = order if isinstance(order, dict) else {}
    lines: list[str] = ["## 新订货单待处理"]

    order_no = str(order.get("order_no") or "").strip()
    if order_no:
        lines.append(f"> 单号：{order_no}")

    # 全站页面显示的门店标识就是 warehouse code；若调用方额外提供了
    # 门店名称（order["store_name"]）则优先用名称，没有就用 code。
    store_label = str(order.get("store_name") or order.get("store_warehouse_code") or "").strip()
    if store_label:
        lines.append(f"> 门店：{store_label}")

    if item_count is None and total_qty is None:
        item_count, total_qty = order_item_counts(order)
    counts = _format_counts(item_count, total_qty)
    if counts:
        lines.append(f"> 品项：{counts}")

    when = _parse_timestamp(submitted_at) or _parse_timestamp(order.get("created_at"))
    if when is not None:
        lines.append(f"> 时间：{when.strftime('%m-%d %H:%M')}")

    url = build_order_url(order.get("id"), base_url)
    if url:
        lines.append(f"[查看单据]({url})")

    return "\n".join(lines)


def build_order_url(order_id: Any, base_url: str = "") -> str:
    """拼单据详情链接；``base_url`` 或 ``order_id`` 缺失时返回空串。

    ``base_url`` 为空**不**退回相对路径：企微群里的消息是给人点的，
    ``/store-ordering/orders/42`` 这种相对链接在客户端点不开（无 host），
    发出去只会误导。宁可整行省略，等 ``PUBLIC_BASE_URL`` 配置好再带上链接。
    """
    if order_id is None or str(order_id).strip() == "":
        return ""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/store-ordering/orders/{order_id}"


def _format_counts(item_count: int | None, total_qty: Any) -> str:
    """「3 项 / 共 12 件」；只有一半信息时就只写那一半，都没有返回空串。"""
    parts: list[str] = []
    if item_count is not None:
        parts.append(f"{int(item_count)} 项")
    qty = _format_qty(total_qty)
    if qty:
        parts.append(f"共 {qty} 件")
    if not parts:
        return ""
    return " / ".join(parts)


def _format_qty(value: Any) -> str:
    """12.0 → "12"，1.50 → "1.5"；非法值返回空串。"""
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return ""
    if number == int(number):
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _parse_timestamp(value: Any) -> datetime | None:
    """容错解析时间戳。

    生产库里是 ``YYYY-MM-DD HH:MM:SS``；人工 SQL / 旧数据里还存在
    16 字符 ``YYYY-MM-DD HH:MM`` 形态（见 ARCHITECTURE §13.8），
    这里只用于展示，容忍多种形态、解析不了返回 None 让调用方省略。
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().replace("T", " ")
    if not text:
        return None
    text = text.split(".", 1)[0]  # 去掉可能的毫秒/微秒
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------

def send_webhook_markdown(
    webhook_url: str,
    markdown: str,
    timeout: float = WECOM_TIMEOUT_SECONDS,
    retries: int = WECOM_MAX_RETRIES,
) -> bool:
    """POST 一条 markdown 到群机器人。成功返回 True，任何失败返回 False。

    绝不抛异常。失败判定：
    - 网络 / 超时 / HTTP 状态异常
    - 响应体不是 JSON，或缺 ``errcode``
    - ``errcode != 0``（企微的业务错误，如 key 失效、机器人被移出群）

    日志只落脱敏后的 URL（host + path，``key`` 打码）与 errcode。
    """
    url = (webhook_url or "").strip()
    if not url:
        logger.warning("wecom_notify_skip: webhook url empty")
        return False
    payload = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": markdown or ""}},
        ensure_ascii=False,
    ).encode("utf-8")
    attempts = max(0, int(retries)) + 1
    masked = mask_webhook_url(url)
    last_reason = ""
    for attempt in range(1, attempts + 1):
        try:
            ok, errcode, reason = _post_once(url, payload, timeout)
        except Exception as exc:  # 兜底：_post_once 已自吞，防未来改动回归
            # 注意：不得 exc_info=True —— traceback 会带出异常文本，
            # 而 URL 类异常文本包含完整 webhook（即 key 明文）。
            ok, errcode, reason = False, None, f"unexpected={type(exc).__name__}"
        if ok:
            return True
        last_reason = reason
        logger.warning(
            "wecom_notify_attempt_failed: url=%s attempt=%d/%d errcode=%s reason=%s",
            masked, attempt, attempts, errcode, reason,
        )
    logger.warning("wecom_notify_dropped: url=%s errcode=%s reason=%s",
                   masked, "n/a", last_reason)
    return False


def _post_once(
    url: str,
    payload: bytes,
    timeout: float,
) -> tuple[bool, int | None, str]:
    """单次 POST。返回 (是否成功, errcode, 失败原因)。**绝不抛异常**。

    ``Request`` 的构造也在 ``try`` 内——这是安全边界，不是风格问题：
    畸形 URL（缺 scheme、scheme 拼错、被引号包住、``ftp://``）会让
    ``urllib.request.Request()`` 抛 ``ValueError("unknown url type: '<完整 URL>'"``，
    异常文本里就是 ``key=`` 明文。它若冒出本函数，会一路冒到
    ``_send_safely()`` 的 ``exc_info=True`` → traceback 把密钥打进日志，
    违反模块自述「绝不抛异常 / 完整 URL 不允许出现在日志」。

    失败原因（reason）只放**异常类型名**或 HTTP/errcode 数字，
    绝不拼进 URL 片段。
    """
    # URL 来自 config（运维注入的企微地址），不是用户输入。
    try:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        return False, None, f"http_status={exc.code}"
    except urllib.error.URLError as exc:
        # 超时在 urllib 里也走这里（reason 通常是 timeout(...)）
        return False, None, f"urlerror={_short(exc.reason)}"
    except (TimeoutError, OSError) as exc:
        return False, None, f"oserror={type(exc).__name__}"
    except Exception as exc:  # 兜底：含 Request 构造的 ValueError（畸形 URL）
        return False, None, f"unexpected={type(exc).__name__}"

    errcode = _parse_errcode(body)
    if errcode is None:
        return False, None, "errcode_missing"
    if errcode != 0:
        return False, errcode, "errcode_nonzero"
    return True, errcode, ""


def _parse_errcode(body: bytes) -> int | None:
    """从响应体取 errcode；取不到返回 None（按失败处理，不假绿）。"""
    try:
        data = json.loads((body or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        errcode = data.get("errcode")
        if isinstance(errcode, bool):  # bool 是 int 的子类，排除掉
            return None
        if isinstance(errcode, int):
            return errcode
        if isinstance(errcode, str) and errcode.strip().lstrip("-").isdigit():
            return int(errcode.strip())
    return None


def mask_webhook_url(url: str) -> str:
    """脱敏 webhook URL：保留 host + path，``key`` 等查询参数全部打码。

    供日志使用——``key=`` 就是密钥，完整 URL 不允许出现在任何日志 / 报错里。
    """
    text = (url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return "***"
    if not parts.scheme and not parts.netloc:
        return f"***{parts.path}" if parts.path else "***"
    masked = f"{parts.scheme}://{parts.netloc}{parts.path}"
    if parts.query:
        masked = f"{masked}?key=***"
    return masked


def _short(value: Any, limit: int = 80) -> str:
    """压缩异常信息，避免日志被超长 reason 撑爆。"""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


# ---------------------------------------------------------------------------
# 业务入口
# ---------------------------------------------------------------------------

def webhook_for_event(event_type: str) -> str:
    """按事件类型取 webhook URL（读 config 属性，空串 = 该事件未启用）。

    在**调用时**读 config 而非导入期绑定：便于测试替换，也让
    「未配置 = 功能关闭」在运行期可判定，不依赖导入顺序。
    """
    attr = WEBHOOK_EVENT_MAP.get(event_type or "")
    if not attr:
        return ""
    return str(getattr(config, attr, "") or "").strip()


def notify_store_order_submitted(
    order: dict[str, Any],
    item_count: int | None = None,
    total_qty: float | None = None,
    background: bool = True,
) -> None:
    """门店新订货单提交 → 企微群。绝不抛异常、绝不阻塞请求线程。"""
    notify_store_order_event(
        EVENT_ORDER_SUBMITTED,
        order,
        item_count=item_count,
        total_qty=total_qty,
        background=background,
    )


def notify_store_order_event(
    event_type: str,
    order: dict[str, Any],
    item_count: int | None = None,
    total_qty: float | None = None,
    background: bool = True,
) -> None:
    """事件 → 通道分发入口（扩展位：新事件登记进 WEBHOOK_EVENT_MAP 即可）。"""
    order_no = ""
    try:
        order = order if isinstance(order, dict) else {}
        order_no = str(order.get("order_no") or "")
        webhook_url = webhook_for_event(event_type)
        if not webhook_url:
            # 未注入 env = 功能关闭，静默跳过（不问、不警告，避免刷日志）
            return
        if item_count is None and total_qty is None:
            item_count, total_qty = order_item_counts(order)
        markdown = build_store_order_markdown(
            order,
            item_count=item_count,
            total_qty=total_qty,
            base_url=str(getattr(config, "PUBLIC_BASE_URL", "") or ""),
        )
        if background:
            threading.Thread(
                target=_send_safely,
                args=(webhook_url, markdown, order_no),
                name="wecom-store-order-notify",
                daemon=True,
            ).start()
        else:
            _send_safely(webhook_url, markdown, order_no)
    except Exception:  # 兜底：通知失败不得冒泡到下单事务
        logger.warning("wecom_notify_error: event=%s order_no=%s",
                       event_type, order_no, exc_info=True)


def _send_safely(webhook_url: str, markdown: str, order_no: str = "") -> None:
    """后台线程体：吞掉一切异常，只记订单号。

    成功与失败**都用 WARNING**：app 不设 ``logging.basicConfig``，生产 root
    logger 阈值是 WARNING，INFO 级会被静默吞掉（见 ARCHITECTURE §13.9）。
    推送成功是「必须留痕」的运维事实（对外消息已出网，事后要能证明发过），
    所以不能是 INFO。
    """
    try:
        sent = send_webhook_markdown(webhook_url, markdown)
        if sent:
            logger.warning("wecom_notify_ok: order_no=%s", order_no)
        else:
            logger.warning("wecom_notify_failed: order_no=%s", order_no)
    except Exception:  # 兜底：后台线程绝不冒泡
        logger.warning("wecom_notify_failed: order_no=%s", order_no, exc_info=True)
