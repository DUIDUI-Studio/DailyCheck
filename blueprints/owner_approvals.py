"""Owner 拍板队列：站内页 + Prod Agent 接口。

路由分工（鉴权刻意分成两套）：

  ``GET  /ops/approvals``                   页面 —— 平台管理员 session
  ``POST /ops/approvals/<pr>/decide``       站内按钮 —— 同上 + CSRF
  ``POST /api/ops/approvals``               Agent 登记 —— Bearer 共享令牌
  ``GET  /api/ops/approvals``               Agent 取待执行 —— 同上
  ``POST /api/ops/approvals/<pr>/consume``  Agent 回写执行结果 —— 同上

为什么两套而不共用一套：页面是人在浏览器里点，必须吃 session + CSRF；
Agent 是机器调，没有 session，也拿不到 CSRF token。共用一套要么给机器发
cookie，要么给浏览器关 CSRF —— 两个都是错的。

Spec: docs/ARCHITECTURE.md §3.12。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import (
    Blueprint,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import config
from app import csrf
from blueprints import owner_approvals_pure as oap
from blueprints.owner_approvals_notify import notify_pending
from db import get_master_db
from permissions import require_platform_admin

bp = Blueprint("owner_approvals", __name__)

# 页面列表上限：待拍板项本来就是个位数，取 50 是为了「已决定」那头也不无限长。
LIST_LIMIT = 50

_COLUMNS = (
    "pr_number, pr_title, pr_url, risk_kind, reason, status, "
    "decided_by, decided_at, decision_note, consumed_at, consume_result, "
    "notified_at, created_at"
)

_STATUS_LABELS = {
    oap.STATUS_PENDING: "待拍板",
    oap.STATUS_APPROVED: "已批准",
    oap.STATUS_REJECTED: "已驳回",
}


def _now() -> str:
    """本地时间 ``YYYY-MM-DD HH:MM:SS``（与全站落库口径一致）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys()}
    data["status_label"] = _STATUS_LABELS.get(data.get("status", ""), data.get("status"))
    data["executed"] = bool(data.get("consumed_at"))
    return data


def _bearer_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return ""


def _agent_authorized() -> bool:
    return oap.token_matches(_bearer_token(), getattr(config, "OPS_AGENT_TOKEN", ""))


def _agent_denied():
    """401 与 403 分开：没令牌是 401，令牌不对/未启用是 403。

    未配置令牌时返回 403 而不是放行 —— 宁可页面空着，也不能让匿名请求
    往队列里塞东西。
    """
    if not _bearer_token():
        return jsonify({"ok": False, "error": "missing bearer token"}), 401
    return jsonify({"ok": False, "error": "invalid or disabled token"}), 403


# ---------------------------------------------------------------------------
# 站内页面（人）
# ---------------------------------------------------------------------------

@bp.route("/ops/approvals")
@require_platform_admin
def list_approvals():
    master = get_master_db()
    pending = [
        _row_to_dict(r)
        for r in master.execute(
            f"SELECT {_COLUMNS} FROM owner_approvals "
            "WHERE status = ? ORDER BY id DESC LIMIT ?",
            (oap.STATUS_PENDING, LIST_LIMIT),
        ).fetchall()
    ]
    decided = [
        _row_to_dict(r)
        for r in master.execute(
            f"SELECT {_COLUMNS} FROM owner_approvals "
            "WHERE status <> ? ORDER BY id DESC LIMIT ?",
            (oap.STATUS_PENDING, LIST_LIMIT),
        ).fetchall()
    ]
    return render_template(
        "ops_approvals.html",
        pending=pending,
        decided=decided,
        agent_enabled=bool(getattr(config, "OPS_AGENT_TOKEN", "")),
    )


@bp.route("/ops/approvals/<int:pr>/decide", methods=["POST"])
@require_platform_admin
def decide(pr: int):
    try:
        target = oap.target_status((request.form.get("decision") or "").strip())
    except ValueError:
        flash("无效的操作")
        return redirect(url_for("owner_approvals.list_approvals"))

    note = oap.normalize_note(request.form.get("note"))
    master = get_master_db()
    row = master.execute(
        "SELECT status FROM owner_approvals WHERE pr_number = ?", (pr,)
    ).fetchone()
    if row is None:
        flash(f"PR #{pr} 不在待拍板队列里")
        return redirect(url_for("owner_approvals.list_approvals"))
    if row["status"] == target:
        # 重复点同一个按钮：幂等，不改 decided_at —— 否则审计时间会被刷新成
        # 「最后一次手抖的时刻」，看不出真正决定是什么时候下的。
        flash(f"PR #{pr} 已经是「{_STATUS_LABELS[target]}」，无需重复操作")
        return redirect(url_for("owner_approvals.list_approvals"))
    if not oap.decide_allowed(row["status"]):
        flash(f"PR #{pr} 已由他人拍板，不能改判")
        return redirect(url_for("owner_approvals.list_approvals"))

    master.execute(
        "UPDATE owner_approvals SET status = ?, decided_by = ?, decided_at = ?, "
        "decision_note = ? WHERE pr_number = ?",
        (target, g.user["username"], _now(), note or None, pr),
    )
    master.commit()

    if target == oap.STATUS_APPROVED:
        flash(f"已批准 PR #{pr}，Prod Agent 下次巡检会执行 merge")
    else:
        flash(f"已驳回 PR #{pr}")
    return redirect(url_for("owner_approvals.list_approvals"))


# ---------------------------------------------------------------------------
# Agent 接口（机器）
# ---------------------------------------------------------------------------

@bp.route("/api/ops/approvals", methods=["POST"])
def api_register():
    if not _agent_authorized():
        return _agent_denied()
    try:
        data = oap.normalize_registration(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    master = get_master_db()
    now = _now()
    # 返工后重新登记：整行重置回 pending，清掉上一轮的决定与推送标记。
    master.execute(
        """INSERT INTO owner_approvals
             (pr_number, pr_title, pr_url, risk_kind, reason, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(pr_number) DO UPDATE SET
             pr_title      = excluded.pr_title,
             pr_url        = excluded.pr_url,
             risk_kind     = excluded.risk_kind,
             reason        = excluded.reason,
             status        = excluded.status,
             decided_by    = NULL,
             decided_at    = NULL,
             decision_note = NULL,
             consumed_at   = NULL,
             consume_result= NULL,
             notified_at   = NULL,
             created_at    = excluded.created_at""",
        (
            data["pr_number"], data["pr_title"], data["pr_url"],
            data["risk_kind"], data["reason"], oap.STATUS_PENDING, now,
        ),
    )
    master.commit()

    row = master.execute(
        f"SELECT {_COLUMNS} FROM owner_approvals WHERE pr_number = ?",
        (data["pr_number"],),
    ).fetchone()
    payload = _row_to_dict(row)

    # 同步发：登记是低频操作，多等一次超时换一个确定的成败，值得。
    notified = notify_pending(payload, background=False)
    if notified:
        master.execute(
            "UPDATE owner_approvals SET notified_at = ? WHERE pr_number = ?",
            (now, data["pr_number"]),
        )
        master.commit()

    return jsonify({"ok": True, "item": payload, "notified": notified})


@bp.route("/api/ops/approvals")
def api_list():
    if not _agent_authorized():
        return _agent_denied()
    master = get_master_db()
    rows = master.execute(
        f"SELECT {_COLUMNS} FROM owner_approvals "
        "WHERE status IN (?, ?) AND consumed_at IS NULL ORDER BY id",
        (oap.STATUS_APPROVED, oap.STATUS_REJECTED),
    ).fetchall()
    return jsonify({"ok": True, "items": [_row_to_dict(r) for r in rows]})


@bp.route("/api/ops/approvals/<int:pr>/consume", methods=["POST"])
def api_consume(pr: int):
    if not _agent_authorized():
        return _agent_denied()
    body = request.get_json(silent=True) or {}
    result = oap.normalize_note(body.get("result")) or "done"

    master = get_master_db()
    cur = master.execute(
        "UPDATE owner_approvals SET consumed_at = ?, consume_result = ? "
        "WHERE pr_number = ? AND consumed_at IS NULL",
        (_now(), result, pr),
    )
    master.commit()
    if cur.rowcount == 0:
        return jsonify({"ok": False, "error": "not found or already consumed"}), 404
    return jsonify({"ok": True, "pr": pr, "result": result})


# ---------------------------------------------------------------------------
# CSRF：只豁免 Agent 的三个接口
# ---------------------------------------------------------------------------
# 页面上的 POST 必须带 CSRF（浏览器会自动带 cookie，攻击者能借用户的身份
# 点「批准」）。Agent 接口走 Bearer 头，浏览器不会自动附带，CSRF 在此不成立，
# 反向也要豁免 —— 机器拿不到 session 里的 token。
for _view in (api_register, api_list, api_consume):
    csrf.exempt(_view)
