"""反馈的 LLM 预分析：调模型 → 落库 → 二审前把结果摆在管理员面前。

链路位置（2026-10-25 二审制）：

    一审通过 ─→ [本模块后台线程] ─→ analyzed（等二审） ─→ 派发 / 忽略
                    └─ 失败 ─→ open + analysis_error（可重试，可模板兜底派发）

四个刻意的取舍：

1. **后台线程而不是同步等**：一次推理实测 5~30 秒，同步会把「点通过」变成
   一段不明所以的等待，超时了页面还只能报错。线程里跑，端点立刻 302 回详情页，
   页面显示「分析中」并自动刷新，失败也只是这一张单退回去重试。
2. **成功失败都落库，绝不静默**：分析结果、模型名、失败原因全部写进
   ``feedback_tickets``。运营侧要能回答「这条到底分析过没有、谁分析的、
   为什么失败」——只写日志等于事后无法追溯。
3. **失败不阻塞派发**：模型挂了不等于这条反馈不值得修。退回 ``open`` 后
   管理员可以直接「跳过分析派发」，正文走模板渲染（``analysis_section`` 为空）。
4. **绝不把 key 写进日志**：失败原因只记 HTTP 状态码或异常类型名，与
   ``feedback_dispatch`` 同一口径。

纯标准库（``urllib``），与 ``feedback_dispatch`` / ``wecom_notify_pure`` 一致；
提示词构造与输出解析在 ``feedback_analysis_pure``（纯函数、可单测）。

Spec: docs/ARCHITECTURE.md §3.10（反馈闭环 / 二审制与 LLM 预分析）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import config
import db as db_module

from . import feedback_introspect as introspect
from .feedback_analysis_pure import (
    SOURCE_TEMPLATE,
    analysis_to_json,
    build_draft,
    build_messages,
    fallback_analysis,
    parse_analysis,
)
from .feedback_pure import (
    STATUS_ANALYZING,
    fail_analysis,
    get_ticket,
    list_comments,
    save_analysis,
)

logger = logging.getLogger(__name__)

# ⚠️ 这个预算不是「正文长度」，是「推理 + 正文」的总预算。
#
# 生产默认模型 MiniMax-M3.1-Flash 是**推理模型**，回复里先有 ``reasoning_content``
# 再有 ``content``。2026-10-25 生产事故（ticket 9 / FB-20261024-0001）：1600 被
# reasoning 吃光（1599 reasoning_tokens），推理没收敛就撞 ``finish_reason=length``，
# 此时上游返回的 ``message`` 里**只有 reasoning_content、没有 content 键**，
# 取值直接 KeyError → 被笼统报成「响应结构不可解析」，一审降级成模板兜底。
#
# 实测同一张单（prompt 2012 tokens）三档对照：
#   1600 → length，reasoning 1599，无 content
#   4096 → length，reasoning 4095，无 content
#   8192 → stop，  reasoning 3325，content 1805 字符（合法 JSON）
# 推理长度波动很大，所以取 8192 而不是刚刚够的 4096；真被截断时下面的错误
# 文案会带上实际 reasoning 用量，便于判断要不要再抬。
#
# ⚠️ 改这个数必须同时看 ``_timeout()``：预算越大生成越久，8192 下单次实测 108s。
# 只抬预算不抬超时的后果与「预算不够」完全同形——都落 analysis_model=template。
MAX_OUTPUT_TOKENS = 8192
MODEL_TEMPLATE_LABEL = "template"


class LLMError(RuntimeError):
    """模型侧不可用（未配置 / 网络 / 额度 / 解析失败）。上层据此降级。"""


def llm_enabled() -> bool:
    """key 为空 = 不调模型（一审降级为模板兜底分析）。"""
    return bool(str(getattr(config, "FEEDBACK_LLM_API_KEY", "") or "").strip())


def _model_name() -> str:
    return str(getattr(config, "FEEDBACK_LLM_MODEL", "") or "").strip() or "unknown"


def _timeout() -> float:
    """单次调用总超时（秒）。

    默认 300 而不是 90：8192 输出预算下推理模型真实耗时实测 108s（见
    ``MAX_OUTPUT_TOKENS`` 上方注释）。90 会让「预算够了」的单子死在超时上，
    且两种降级在库里长得一样（``analysis_model=template``），排查极易走错方向。
    """
    try:
        return max(5.0, float(getattr(config, "FEEDBACK_LLM_TIMEOUT", 300) or 300))
    except (TypeError, ValueError):
        return 300.0


# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------

def analyze(
    ticket: dict[str, Any] | None,
    comments: list[dict[str, Any]] | None,
    clues: dict[str, Any] | None,
    snippets: str = "",
) -> dict[str, Any]:
    """调一次模型并解析成归一化分析；任何失败 raise ``LLMError``。"""
    messages = build_messages(ticket, comments, clues, snippets)
    raw = _chat(messages)
    try:
        return parse_analysis(raw)
    except ValueError as exc:
        raise LLMError(str(exc)) from exc


def _chat(messages: list[dict[str, str]]) -> str:
    """OpenAI 兼容的 ``/v1/chat/completions``，返回 message.content。"""
    key = str(getattr(config, "FEEDBACK_LLM_API_KEY", "") or "").strip()
    if not key:
        raise LLMError("未配置 LLM_API_KEY")
    base = str(getattr(config, "FEEDBACK_LLM_BASE_URL", "") or "").rstrip("/")
    payload = {
        "model": _model_name(),
        "messages": messages,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.2,   # 分析要稳定复现，不是创意写作
        "stream": False,
    }
    request = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": "DailyCheck-feedback-analyze",
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:  # noqa: BLE001 - 读掉响应体避免连接泄漏，失败无所谓
            pass
        # 只留状态码：响应体可能回显部分请求头，日志里不能出现凭据片段。
        raise LLMError(f"HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - 网络/超时/SSL 一律归为不可用
        # 超时单独给文案：默认超时被撞破时，光看 "TimeoutError" 分不清是网络
        # 不稳还是「模型还没说完，是我们等得不够久」——把上限和已等时长写进去，
        # 下次再撞一眼就知道该抬哪一头。
        if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
            raise LLMError(
                f"调用超时（上限 {_timeout():.0f}s，已等 {time.monotonic() - started:.0f}s）"
            ) from exc
        raise LLMError(f"调用失败（{type(exc).__name__}）") from exc

    try:
        data = json.loads((body or b"").decode("utf-8"))
        choice = data["choices"][0]
        if not isinstance(choice, dict):
            raise TypeError("choices[0] 不是对象")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise TypeError("message 不是对象")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError("响应结构不可解析") from exc

    # content 用 get 不用下标：推理模型撞 length 时这个键**根本不存在**，
    # 拿它当结构异常会让「预算不够」伪装成「上游返回物怪异」，把排查引到错方向。
    text = str(message.get("content") or "").strip()
    if text:
        return text

    finish = str(choice.get("finish_reason") or "")
    if finish == "length":
        raise LLMError(
            f"输出被 max_tokens={MAX_OUTPUT_TOKENS} 截断"
            f"（reasoning 已用 {_reasoning_tokens(data)} tokens，正文未生成）"
        )
    raise LLMError(f"模型返回空内容（finish_reason={finish or '未提供'}）")


def _reasoning_tokens(data: Any) -> int:
    """从 usage 里抠 reasoning token 数，钻不出来就返 0（只用于告警文案）。"""
    try:
        usage = data.get("usage") if isinstance(data, dict) else None
        details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
        return int((details or {}).get("reasoning_tokens") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


# ---------------------------------------------------------------------------
# 后台执行
# ---------------------------------------------------------------------------

def launch(app: Any, ticket_id: int) -> bool:
    """起一个 daemon 线程跑分析。返回是否真的起了线程。

    调用方（路由）负责先把状态置 ``analyzing`` 并把「本次分析用哪个页面」写进
    ``analysis_page_path``；本函数不起线程的唯一情况是 DRY_RUN —— 那种实例
    写库是被拦截的，跑分析只会白烧 token 并留下假状态。
    """
    if app is not None and bool(app.config.get("DRY_RUN")):
        logger.warning("feedback_analyze_skip: reason=dry_run ticket=%s", ticket_id)
        return False
    thread = threading.Thread(
        target=_run,
        args=(app, int(ticket_id)),
        name=f"feedback-analyze-{int(ticket_id)}",
        daemon=True,
    )
    thread.start()
    return True


def _run(app: Any, ticket_id: int) -> None:
    """线程主体：读单 → 定位 → 分析 → 落库。任何异常都退回 open。"""
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect()
        ticket = get_ticket(conn, ticket_id)
        if ticket is None:
            logger.warning("feedback_analyze_skip: reason=not_found ticket=%s", ticket_id)
            return
        if ticket.get("status") != STATUS_ANALYZING:
            # 分析期间管理员已经忽略 / 派发 / 重跑过一次，这次结果作废。
            logger.info("feedback_analyze_skip: reason=status_%s ticket=%s",
                        ticket.get("status"), ticket_id)
            return

        comments = list_comments(conn, ticket_id)
        # 定位口径：一审时管理员纠正过的实际页面 > 用户提交时所在页面。
        locate_from = ticket.get("analysis_page_path") or ticket.get("page_path")
        clues = introspect.collect(app, locate_from)
        snippets = introspect.collect_snippets(clues)

        try:
            analysis = analyze(ticket, comments, clues, snippets)
        except LLMError as exc:
            # 单次失败不重试：线程里重试要么拖长占单时间，要么撞限流。
            # 管理员点「重新分析」比自动重试更可控。
            logger.warning("feedback_analyze_failed: ticket=%s reason=%s", ticket_id, exc)
            analysis = fallback_analysis(ticket, comments, clues, reason=str(exc))
            degraded = True
        else:
            degraded = False

        model = MODEL_TEMPLATE_LABEL if degraded or (
            analysis.get("source") == SOURCE_TEMPLATE) else _model_name()
        title, body = build_draft(
            ticket, comments, clues, analysis, base_url=_base_url())
        ok = save_analysis(
            conn, ticket_id, analysis_to_json(analysis), model, title, body)
        if not ok:
            logger.warning("feedback_analyze_stale: ticket=%s", ticket_id)
        elif degraded:
            # 降级也要落库：二审页必须能看出「这次没跑成模型」，
            # 否则管理员会把模板话术当成模型结论去签字。
            logger.warning("feedback_analyze_degraded: ticket=%s", ticket_id)
    except Exception:  # noqa: BLE001 - 线程里绝不能把异常抛给解释器
        logger.exception("feedback_analyze_crashed: ticket=%s", ticket_id)
        _fail_safely(ticket_id, "分析线程异常，请点「重新分析」重试")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 关闭失败无关紧要
                pass


def _fail_safely(ticket_id: int, error: str) -> None:
    """退回 open 并写错误原因；自己再失败就只记日志（不能二次抛）。"""
    try:
        conn = _connect()
        try:
            fail_analysis(conn, ticket_id, error)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 兜底路径不允许再炸
        logger.warning("feedback_analyze_fail_safely_error: ticket=%s", ticket_id)


def _connect() -> sqlite3.Connection:
    """线程内独立连接。

    ⚠️ 不能复用请求的 ``g.master_db``：线程跑起来时请求早结束了，Flask 会把
    那个连接关掉。这里直接连 ``db.MASTER_DB``（模块属性，测试可 monkeypatch）。
    也因此**必须**由调用方保证不是 DRY_RUN —— 裸连接绕过了 dry-run 包装器。
    """
    conn = sqlite3.connect(str(db_module.MASTER_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _base_url() -> str:
    return str(getattr(config, "PUBLIC_BASE_URL", "") or "")
