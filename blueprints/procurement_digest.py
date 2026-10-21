"""周度采购建议邮件 —— 取数装配 + SMTP 投递 + 周二定时调度。

为什么单独一个模块，而不是塞进 forecast.py
------------------------------------------
`forecast.py` 是「看板怎么算」；本模块是「算完之后按周发出去」。两者失败
面不同：看板坏了页面立刻可见，邮件坏了没人知道（这正是 Issue #145 想消灭的
盲区）。放一起会让「看板能开」被误当成「邮件能发」。

失败语义（与 wecom_notify_pure 一致）
------------------------------------
**绝不向上抛异常**：调度线程里的任何异常都只能进日志。邮件发不出去不能让
web 进程有任何感知，更不能让 gunicorn worker 崩掉。

去重（多 worker 下的「一周只发一封」）
--------------------------------------
gunicorn 每个 worker 都跑自己的调度线程，因此「今天发过没」必须落盘：
`<master.db 所在目录>/procurement-digest-YYYY-MM-DD.json`，用
`O_CREAT|O_EXCL` 抢占 —— 原子操作，抢到的那个 worker 负责发。发失败会
把 sentinel 标成 failed，仍在本小时内则允许重试，最多 3 次（避免一周内
因为一次网络抖动整周没报告，也避免无限重试把自己打成垃圾邮件源）。

Spec: docs/ARCHITECTURE.md §3.13 周度采购建议邮件。
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import threading
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

import config

from .procurement_digest_pure import (
    DEFAULT_LIMIT,
    build_digest,
    build_subject,
    render_html,
    render_text,
)

logger = logging.getLogger(__name__)

# 单日最多投递尝试次数（首次 + 2 次重试）。超过就不再试：一周报的时效
# 只有一天，当天 08 点发不出去、09 点还在重试没有意义，反而刷日志。
MAX_SEND_ATTEMPTS = 3

# SMTP 超时：邮件在调度线程里发，不占用户请求；15s 足够握手 + 投递，
# 又不至于把 sentinel 卡成 pending 太久。
SMTP_TIMEOUT_SECONDS = 15.0

_POLL_SECONDS = 60


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def digest_settings() -> dict:
    """在**调用时**读 config（不 import 期绑定），便于测试与运行期热改。

    `enabled` 的判据是「收件人 + 账号 + 授权码三者齐全」：缺任何一个都
    发不出去，此时静默降级为「功能关闭」，只在调度首次命中时记一条 WARNING。
    """
    host = str(getattr(config, "DIGEST_MAIL_HOST", "") or "").strip()
    user = str(getattr(config, "DIGEST_MAIL_USER", "") or "").strip()
    password = str(getattr(config, "DIGEST_MAIL_PASSWORD", "") or "")
    to = str(getattr(config, "DIGEST_MAIL_TO", "") or "").strip()
    sender = str(getattr(config, "DIGEST_MAIL_FROM", "") or "").strip() or user
    try:
        port = int(getattr(config, "DIGEST_MAIL_PORT", 465) or 465)
    except (TypeError, ValueError):
        port = 465
    try:
        weekday = int(getattr(config, "DIGEST_WEEKDAY", 1))
    except (TypeError, ValueError):
        weekday = 1
    try:
        hour = int(getattr(config, "DIGEST_HOUR", 8))
    except (TypeError, ValueError):
        hour = 8
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "sender": sender,
        "recipients": _split_recipients(to),
        "weekday": weekday % 7,
        "hour": max(0, min(23, hour)),
        "enabled": bool(host and user and password and sender and to),
    }


def _split_recipients(raw: str) -> list[str]:
    """`a@x.com, b@x.com` → 列表。支持逗号 / 分号 / 空白分隔。"""
    text = (raw or "").replace(";", ",").replace("\n", ",").replace(" ", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def mask_address(address: str) -> str:
    """日志用脱敏：`zhangsan@corp.com` → `z***@corp.com`。"""
    text = (address or "").strip()
    if "@" not in text:
        return "***"
    local, _, domain = text.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}"


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------


def collect_digest(today: date | None = None, limit: int | None = DEFAULT_LIMIT) -> dict:
    """跑一次看板装配，产出采购建议 digest。异常向上抛（由调用方兜底）。

    复用 `build_board()` 而不是另写一套公式：邮件和页面必须是同一份数，
    否则「看板上没有的东西出现在邮件里」会比不发更糟。
    """
    from .replenishment_data import (
        build_board,
        load_params,
        store_stock_and_consumption,
    )

    day = today or date.today()
    params = load_params()
    board = build_board(today=day, params=params)
    stock_by_cid, consumed_by_cid, stores = store_stock_and_consumption(
        days=7, today=day
    )

    dc_names = [w.get("name") for w in board.get("warehouses") or [] if w.get("is_dc")]
    dc_name = next((n for n in dc_names if n), "配送中心")

    rows = board.get("rows") or []
    dq = board.get("data_quality") or {}
    warnings: list[str] = []
    for wh in dq.get("skipped_warehouses") or []:
        warnings.append(f"仓「{wh.get('name') or wh.get('code')}」库文件缺失，未计入本次统计。")
    for wh in dq.get("failed_warehouses") or []:
        warnings.append(
            f"仓「{wh.get('name') or wh.get('code')}」读取失败，未计入本次统计。"
        )

    digest = build_digest(
        day=day,
        rows=rows,
        store_stock=stock_by_cid,
        consumption_7d=consumed_by_cid,
        stores=stores,
        dc_name=str(dc_name),
        window_days=7,
        limit=limit,
        warnings=warnings,
    )

    if not stores:
        digest["warnings"].append("没有可用的门店库，各门店库存一列全部缺失。")

    unbound = sum(1 for item in digest["items"] if not item["has_canonical"])
    if unbound:
        digest["warnings"].append(
            f"其中 {unbound} 个品项未绑定主数据（canonical_id 为空），"
            "无法跨门店汇总库存与消耗。"
        )

    # 研发中心按设计不参与补货计算（无实体库存流转）。这不是异常，但
    # 邮件里要留一句——否则对账时「少了一个仓」会被当成漏算。
    excluded = [w.get("name") or w.get("code") for w in (dq.get("excluded_warehouses") or [])]
    if excluded:
        digest["warnings"].append(
            "以下仓按类型不参与补货计算：" + "、".join(str(x) for x in excluded) + "。"
        )

    # 供调用方判断「DC 是否配置齐全」——没有 DC 时清单必然为空，
    # 这是配置问题而不是「本周不用买」，必须区分。
    digest["dc_configured"] = bool(dc_names)
    if not dc_names:
        digest["warnings"].append(
            "未找到配送中心仓（warehouse_type=distribution_center），采购建议无法生成。"
        )
    return digest


# ---------------------------------------------------------------------------
# 投递
# ---------------------------------------------------------------------------


def _sentinel_path(day: date) -> Path:
    """当日 sentinel 文件路径。放在 master.db 同目录（生产为 db/）。"""
    base = Path(getattr(config, "MASTER_DB", "master.db")).parent
    return base / f"procurement-digest-{day.isoformat()}.json"


def _read_sentinel(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_sentinel(path: Path, payload: dict) -> None:
    """原子写：先写 tmp 再 replace，避免半截 JSON 让下次读取判成空。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("procurement_digest_sentinel_write_failed: path=%s", path.name)


def _claim(day: date) -> tuple[bool, str, int]:
    """抢占当日发送权。返回 (是否可以发, 原因, 已尝试次数)。

    * 抢到 → ("claimed", 0)：本进程负责发。
    * 已有 sentinel 且 status=sent → 跳过。
    * 已有 sentinel 且 failed 且 attempts < MAX → 允许重试，并把 claimed_at
      刷成现在（防止两个 worker 同时重试）。
    * 已有 sentinel 且 failed 且 attempts 用尽 → 跳过。
    * pending 且 5 分钟内 → 视为另一个 worker 正在发，跳过；超时视为
      「进程在发送中途被杀」，允许重试。
    """
    path = _sentinel_path(day)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        state = _read_sentinel(path)
        status = str(state.get("status") or "")
        attempts = int(state.get("attempts") or 0)
        if status == "sent":
            return False, "already_sent", attempts
        if status == "pending":
            started = str(state.get("claimed_at") or "")
            try:
                age = (
                    datetime.now() - datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
                ).total_seconds()
            except (TypeError, ValueError):
                age = 10_000.0
            if age < 300:
                return False, "in_progress", attempts
        if attempts >= MAX_SEND_ATTEMPTS:
            return False, "attempts_exhausted", attempts
        _write_sentinel(path, {
            "status": "pending",
            "claimed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "attempts": attempts,
        })
        return True, "retry", attempts
    except OSError as exc:
        logger.warning("procurement_digest_claim_failed: %s", type(exc).__name__)
        return False, "claim_failed", 0
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "status": "pending",
                "claimed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "attempts": 0,
            }, ensure_ascii=False))
        return True, "claimed", 0


def send_mail(
    subject: str,
    text_body: str,
    html_body: str,
    settings: dict,
    timeout: float = SMTP_TIMEOUT_SECONDS,
) -> None:
    """投递一封 multipart/alternative 邮件。失败抛异常（由调用方决定语义）。

    465 走 SMTP_SSL（企业微信邮箱默认），其它端口走明文 + STARTTLS。
    日志里只出现脱敏后的收件人，**密码绝不进日志**。
    """
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["sender"]
    message["To"] = ", ".join(settings["recipients"])
    message["Date"] = formatdate(localtime=True)
    message.set_content(text_body, subtype="plain", charset="utf-8")
    message.add_alternative(html_body, subtype="html", charset="utf-8")

    host = settings["host"]
    port = int(settings["port"])
    if port == 465:
        client = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)
    try:
        if port != 465:
            client.starttls()
        client.login(settings["user"], settings["password"])
        client.send_message(message)
    finally:
        try:
            client.quit()
        except (smtplib.SMTPException, OSError):
            # quit 失败不影响「已投递」的事实；不掩盖真正的异常
            pass


def send_digest(
    *,
    today: date | None = None,
    force: bool = False,
    limit: int | None = DEFAULT_LIMIT,
) -> dict:
    """发一次周报。**绝不抛异常**，结果用 dict 表达（含中文 reason）。

    返回字段：
      sent      是否真的发出去了
      reason    ok / not_configured / already_sent / in_progress /
                attempts_exhausted / claim_failed / build_failed / send_failed
      subject / rows / date / recipients（脱敏）
    """
    settings = digest_settings()
    if not settings["enabled"]:
        logger.warning(
            "procurement_digest_disabled: DIGEST_MAIL_TO/USER/PASSWORD 未配置完整"
        )
        return {"sent": False, "reason": "not_configured"}

    day = today or date.today()
    path = _sentinel_path(day)

    if force:
        # 手动补发：不抢占、不被 sentinel 阻止，但发送完把 sentinel 刷成
        # 本次结果，避免「手动发过之后自动又发一遍」。
        attempts = int(_read_sentinel(path).get("attempts") or 0)
    else:
        allowed, why, attempts = _claim(day)
        if not allowed:
            logger.warning("procurement_digest_skip: reason=%s date=%s", why, day)
            return {"sent": False, "reason": why, "date": day.isoformat()}

    try:
        digest = collect_digest(today=day, limit=limit)
    except Exception:  # 兜底：装配失败也不能冒泡到调度线程外
        logger.warning("procurement_digest_build_failed", exc_info=True)
        _write_sentinel(path, {
            "status": "failed",
            "attempts": attempts + 1,
            "error": "build_failed",
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return {"sent": False, "reason": "build_failed", "date": day.isoformat()}

    subject = build_subject(digest)
    text_body = render_text(digest)
    html_body = render_html(digest)

    try:
        send_mail(subject, text_body, html_body, settings)
    except Exception as exc:  # 兜底：SMTP / 网络 / 认证全部在这一层收口
        logger.warning(
            "procurement_digest_send_failed: date=%s error=%s",
            day, type(exc).__name__,
        )
        _write_sentinel(path, {
            "status": "failed",
            "attempts": attempts + 1,
            "error": type(exc).__name__,
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return {"sent": False, "reason": "send_failed", "date": day.isoformat()}

    _write_sentinel(path, {
        "status": "sent",
        "attempts": attempts + 1,
        "subject": subject,
        "rows": digest["item_count"],
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    logger.warning(
        "procurement_digest_sent: date=%s rows=%d to=%s",
        day, digest["item_count"],
        ",".join(mask_address(r) for r in settings["recipients"]),
    )
    return {
        "sent": True,
        "reason": "ok",
        "date": day.isoformat(),
        "subject": subject,
        "rows": digest["item_count"],
        "recipients": [mask_address(r) for r in settings["recipients"]],
    }


# ---------------------------------------------------------------------------
# 调度（每周一次）
# ---------------------------------------------------------------------------

_scheduler_lock = threading.Lock()
_scheduler_started = False
_STOP = threading.Event()


def is_due(now: datetime | None = None, settings: dict | None = None) -> bool:
    """现在是否该发。抽成纯判定，测试不用等时钟。"""
    cfg = settings or digest_settings()
    if not cfg.get("enabled"):
        return False
    moment = now or datetime.now()
    return moment.weekday() == cfg["weekday"] and moment.hour == cfg["hour"]


def tick(now: datetime | None = None) -> dict | None:
    """调度线程的一次心跳。到点则发，不到点返回 None。"""
    cfg = digest_settings()
    moment = now or datetime.now()
    if not is_due(moment, cfg):
        return None
    return send_digest(today=moment.date())


def _scheduler_loop() -> None:
    while not _STOP.is_set():
        try:
            tick()
        except Exception:  # 兜底：调度线程绝不因单次失败退出
            logger.warning("procurement_digest_tick_failed", exc_info=True)
        if _STOP.wait(_POLL_SECONDS):
            return


def start_scheduler() -> None:
    """幂等启动周报调度线程（create_app 调用，测试可被 env 关掉）。"""
    global _scheduler_started
    if os.environ.get("DAILYCHECK_DISABLE_SCHEDULER") == "1":
        logger.info("procurement digest scheduler disabled via DAILYCHECK_DISABLE_SCHEDULER")
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    thread = threading.Thread(
        target=_scheduler_loop, name="procurement-digest-scheduler", daemon=True
    )
    thread.start()


def stop_scheduler() -> None:
    """仅供测试 / 优雅退出使用。"""
    _STOP.set()
