"""Bot configuration console; fixed approved pool controls execution, discovery only display."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import config
from blueprints.audit_pure import append_admin_audit
from blueprints.bot_admin_pure import (
    EFFORT_LABELS,
    GET_PRICE_DIAGNOSTIC,
    build_tool_rows,
    switchable_tool_names,
)
from bot.agent_config import (
    KEY_REASONING_EFFORT,
    KEY_WRITE_ALLOWED_USERIDS,
    REASONING_EFFORTS,
    ConfigError,
    load_effective_config,
    normalize_reasoning_effort,
    normalize_userids,
    resolve_model_name,
)
from bot.usage import default_call_log_path, read_tool_call_counts
from db import get_master_db
from mcp_server.protocol.tools import get_tools
from permissions import require_platform_admin

bp = Blueprint("bot_admin", __name__)


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _actor() -> str | None:
    return g.user["username"] if g.user else None


def _upsert_config(db, updates: dict[str, str], actor: str | None, now: str) -> None:
    for key, value in updates.items():
        db.execute(
            """INSERT INTO bot_agent_config (key, value, updated_at, updated_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value,
                   updated_at=excluded.updated_at,
                   updated_by=excluded.updated_by""",
            (key, value, now, actor),
        )
    db.commit()


@bp.route("/admin/bot-agent", methods=["GET"])
@require_platform_admin
def index():
    # 每条请求（≈ 管理页的一次快照）读一次有效配置；bot 消息侧同理。
    effective = load_effective_config(config.MASTER_DB)
    counts = read_tool_call_counts(
        default_call_log_path(config.MASTER_DB)
    )
    rows = build_tool_rows(get_tools(), effective, counts)
    return render_template(
        "bot_agent.html",
        rows=rows,
        effective=effective,
        model_name=resolve_model_name(os.environ),
        efforts=REASONING_EFFORTS,
        effort_labels=EFFORT_LABELS,
        write_allowed_userids=", ".join(effective.write_allowed_userids),
        get_price_diagnostic=GET_PRICE_DIAGNOSTIC,
        config_path=str(Path(config.MASTER_DB)),
    )


@bp.route("/admin/bot-agent/save", methods=["POST"])
@require_platform_admin
def save_config():
    raw_effort = request.form.get("reasoning_effort", "")
    try:
        effort = normalize_reasoning_effort(raw_effort)
    except ConfigError:
        flash(
            "思考强度保存失败：只允许 low / medium / high / xhigh / max（"
            f"收到 {raw_effort!r}）。",
            "error",
        )
        return redirect(url_for("bot_admin.index"))

    updates: dict[str, str] = {KEY_REASONING_EFFORT: effort}
    disabled: list[str] = []
    for name in sorted(switchable_tool_names()):
        enabled = request.form.get(f"tool.{name}") is not None
        updates[f"tool.{name}"] = "1" if enabled else "0"
        if not enabled:
            disabled.append(name)

    userids = normalize_userids(request.form.get("write_allowed_userids", ""))
    updates[KEY_WRITE_ALLOWED_USERIDS] = ",".join(userids)

    actor = _actor()
    now = _timestamp()
    db = get_master_db()
    try:
        _upsert_config(db, updates, actor, now)
    except sqlite3.Error as exc:
        flash(f"Bot 配置保存失败：{exc}", "error")
        return redirect(url_for("bot_admin.index"))

    detail = (
        "update bot-agent config: "
        f"reasoning_effort={effort}; "
        f"tools_off={','.join(disabled) if disabled else '-'}; "
        f"write_allowed_userids={','.join(userids) if userids else '-'}"
    )
    append_admin_audit(actor, detail)
    flash("Bot 配置已保存，下一条消息生效。", "bot-config-saved")
    return redirect(url_for("bot_admin.index"))
