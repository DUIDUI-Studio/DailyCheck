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

Spec: docs/ARCHITECTURE.md §3.14 周度采购建议邮件。
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import smtplib
import threading
from contextlib import contextmanager
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
    stock_by_cid, consumed_by_cid, stores, failed_stores = store_stock_and_consumption(
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
    # 门店库读失败：这些店在「各门店库存」列显示「—」，不是 0
    name_by_code = {str(w.get("code")): w.get("name") for w in stores}
    for code in failed_stores:
        label = name_by_code.get(str(code)) or code
        warnings.append(f"门店「{label}」库读取失败，该店库存显示为「—」，请先排查。")

    digest = build_digest(
        day=day,
        rows=rows,
        store_stock=stock_by_cid,
        consumption_7d=consumed_by_cid,
        stores=stores,
        failed_store_codes=failed_stores,
        dc_name=str(dc_name),
        window_days=7,
        limit=limit,
        warnings=warnings,
        dc_configured=bool(dc_names),
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

    # 没有 DC 时清单必然为空，这是配置问题而不是「本周不用买」。该标记会
    # 一路传到邮件主题（build_subject），否则收件人只看到「无需采购」。
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


def _lock_path(day: date) -> Path:
    return _sentinel_path(day).with_suffix(".lock")


def _read_sentinel(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 读不出来按「没有可用的历史」处理，但留痕：坏文件会导致多发一封，
        # 比静默少发安全，且必须能在日志里查到发生过。
        logger.warning("procurement_digest_sentinel_unreadable: path=%s", path.name)
        return {}
    return data if isinstance(data, dict) else {}


def _write_sentinel(path: Path, payload: dict) -> bool:
    """原子写：先写 tmp 再 replace，避免半截 JSON 让下次读取判成空。

    返回是否写成功 —— 写失败必须让调用方知道：sentinel 没更新意味着
    下一次 tick 会以为「没发过」而重发一封。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("procurement_digest_sentinel_write_failed: path=%s", path.name)
        return False
    return True


@contextmanager
def _day_lock(day: date):
    """当日跨进程互斥锁（flock），非阻塞：拿不到就 yield False。

    为什么必须加锁：sentinel 的状态机是「读 → 判断 → 写」，只有 O_EXCL 的
    首次创建是原子的；失败重试与 pending 接管这两条路径都是读改写，两个
    worker 在同 60s tick 里可以同时判定「该我发」。生产是 `-w 2 --threads 4`
    （蓝绿并存时 4 条调度线程），这个窗口天天都在。flock 把整段
    「判定 + 投递 + 写结果」串起来，从根上消掉重复投递。
    """
    path = _lock_path(day)
    fd: int | None = None
    acquired = False
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
    except OSError as exc:
        logger.warning("procurement_digest_lock_failed: %s", type(exc).__name__)
        acquired = False
    try:
        yield acquired
    finally:
        if fd is not None:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass


def _claim(day: date) -> tuple[bool, str, int]:
    """判定今天还能不能发（**调用方必须已持有 `_day_lock`**）。

    返回 ``(是否可以发, 原因, 本次是第几次尝试)``。

    * 从未发过 → claimed，attempt=1
    * status=sent → already_sent
    * status=failed 且 attempts<MAX → retry，attempt=attempts+1
    * status=pending 且 claimed_at 在 5 分钟内 → in_progress（另一个进程在发）
    * status=pending 且已超时 → 视为「发送途中进程被杀」，接管，attempt=attempts+1
      **必须 +1**：否则「发到一半被重启」可以无限接管，MAX_SEND_ATTEMPTS 失效。
    * attempts 用尽 → attempts_exhausted
    """
    path = _sentinel_path(day)
    state = _read_sentinel(path) if path.exists() else {}
    status = str(state.get("status") or "")
    prior = int(state.get("attempts") or 0)

    if status == "sent":
        return False, "already_sent", prior
    if status == "pending":
        started = str(state.get("claimed_at") or "")
        try:
            age = (
                datetime.now() - datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
            ).total_seconds()
        except (TypeError, ValueError):
            age = 10_000.0
        if age < 300:
            return False, "in_progress", prior
    if prior >= MAX_SEND_ATTEMPTS:
        return False, "attempts_exhausted", prior

    attempt = prior + 1
    _write_sentinel(path, {
        "status": "pending",
        "claimed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "attempts": attempt,
    })
    return True, ("retry" if status else "claimed"), attempt


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
      sent      是否真的发出去了（**唯一**成功判据）
      reason    ok / not_configured / already_sent / in_progress /
                attempts_exhausted / lock_failed / build_failed / send_failed
      subject / rows / date / recipients（脱敏）

    `force=True` 是「手动补发」：忽略「今天已发」这个状态，但**仍然走同一把
    锁与同一个 attempts 计数** —— 否则双击按钮或与自动 tick 撞点就是两封信。
    """
    settings = digest_settings()
    if not settings["enabled"]:
        logger.warning(
            "procurement_digest_disabled: DIGEST_MAIL_TO/USER/PASSWORD 未配置完整"
        )
        return {"sent": False, "reason": "not_configured"}

    day = today or date.today()
    path = _sentinel_path(day)

    with _day_lock(day) as locked:
        if not locked:
            logger.warning("procurement_digest_skip: reason=in_progress date=%s", day)
            return {"sent": False, "reason": "in_progress", "date": day.isoformat()}

        if force:
            attempt = int(_read_sentinel(path).get("attempts") or 0) + 1
        else:
            allowed, why, attempt = _claim(day)
            if not allowed:
                logger.warning("procurement_digest_skip: reason=%s date=%s", why, day)
                return {"sent": False, "reason": why, "date": day.isoformat()}

        try:
            digest = collect_digest(today=day, limit=limit)
        except Exception:  # 兜底：装配失败也不能冒泡到调度线程外
            logger.warning("procurement_digest_build_failed", exc_info=True)
            _write_sentinel(path, {
                "status": "failed",
                "attempts": attempt,
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
                "procurement_digest_send_failed: date=%s attempt=%d error=%s",
                day, attempt, type(exc).__name__,
            )
            _write_sentinel(path, {
                "status": "failed",
                "attempts": attempt,
                "error": type(exc).__name__,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            return {"sent": False, "reason": "send_failed", "date": day.isoformat()}

        # sentinel 写失败不改变「已经发出去了」的事实，但必须让调用方看见：
        # 状态没落盘，下一次 tick 可能再发一封。
        persisted = _write_sentinel(path, {
            "status": "sent",
            "attempts": attempt,
            "subject": subject,
            "rows": digest["item_count_total"],
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    logger.warning(
        "procurement_digest_sent: date=%s rows=%d to=%s sentinel_ok=%s",
        day, digest["item_count_total"],
        ",".join(mask_address(r) for r in settings["recipients"]),
        persisted,
    )
    return {
        "sent": True,
        "reason": "ok",
        "date": day.isoformat(),
        "subject": subject,
        "rows": digest["item_count_total"],
        "recipients": [mask_address(r) for r in settings["recipients"]],
        "state_persisted": bool(persisted),
    }


# ---------------------------------------------------------------------------
# 调度（每周一次）
# ---------------------------------------------------------------------------

_scheduler_lock = threading.Lock()
_scheduler_started = False
_STOP = threading.Event()


def is_due(now: datetime | None = None, settings: dict | None = None) -> bool:
    """现在是否**进入当天的发送窗口**。抽成纯判定，测试不用等时钟。

    窗口是 `weekday 匹配 且 hour >= 配置小时`，不是「hour 恰好等于」。
    理由：生产发布/蓝绿切换就是 restart gunicorn，08:00–08:59 之间重启的
    概率不低；严格相等会让本周邮件**永久不发且无人知道**。放宽成「到点之后
    都算窗口」，配合当日 sentinel（发过就 already_sent）既能补发又不会重发。
    """
    cfg = settings or digest_settings()
    if not cfg.get("enabled"):
        return False
    moment = now or datetime.now()
    return moment.weekday() == cfg["weekday"] and moment.hour >= cfg["hour"]


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
