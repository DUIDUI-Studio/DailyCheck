"""Agent token management (platform admin only).

CRITICAL SECURITY NOTE: The raw token is shown exactly once after creation.
There is no way to retrieve it again. Store it immediately.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from config import MASTER_DB
from db import get_master_db
from permissions import require_login


bp = Blueprint("agent_tokens", __name__)


def _require_admin():
    if not g.user or not g.user["is_admin"]:
        abort(403)


def _parse_paths(s: str) -> str:
    s = s.strip()
    if not s:
        return "[]"
    if s == "*":
        return '["*"]'
    return json.dumps([p.strip() for p in s.split(",") if p.strip()])


def _parse_warehouses(s: str) -> str:
    s = s.strip()
    if not s:
        return "null"
    return json.dumps([w.strip() for w in s.split(",") if w.strip()])


@bp.route("/admin/agent-tokens", methods=["GET"])
@require_login
def list_tokens():
    _require_admin()
    db = get_master_db()
    tokens = db.execute(
        """SELECT id, name, created_by, created_at, revoked_at,
                  allowed_read_paths_json, allowed_write_paths_json,
                  allowed_warehouse_codes_json
           FROM agent_tokens
           ORDER BY created_at DESC"""
    ).fetchall()
    warehouses = db.execute(
        "SELECT id, code, name FROM warehouses ORDER BY id"
    ).fetchall()
    new_token = session.pop("_new_token", None)
    new_token_name = session.pop("_new_token_name", None)
    return render_template(
        "agent_tokens.html",
        tokens=tokens,
        warehouses=warehouses,
        new_token=new_token,
        new_token_name=new_token_name,
    )


@bp.route("/admin/agent-tokens/create", methods=["POST"])
@require_login
def create_token():
    _require_admin()

    name = request.form.get("name", "").strip()
    read_paths = request.form.get("read_paths", "*").strip()
    write_paths = request.form.get("write_paths", "").strip()
    warehouses = request.form.get("warehouses", "").strip()

    if not name:
        flash("Token 名称必填")
        return redirect(url_for("agent_tokens.list_tokens"))

    raw_token = secrets.token_urlsafe(32)
    token_hash = generate_password_hash(raw_token, method="pbkdf2:sha256")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db = get_master_db()
    try:
        db.execute(
            """INSERT INTO agent_tokens
               (name, token_hash, created_by, created_at,
                allowed_read_paths_json, allowed_write_paths_json,
                allowed_warehouse_codes_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                token_hash,
                g.user["id"],
                now,
                _parse_paths(read_paths),
                _parse_paths(write_paths),
                _parse_warehouses(warehouses),
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash(f"Token 名称 '{name}' 已存在")
        return redirect(url_for("agent_tokens.list_tokens"))

    flash(f"已创建 Token '{name}'", "token-created")
    session["_new_token"] = raw_token
    session["_new_token_name"] = name
    _log_admin_action(f"create token '{name}'")
    return redirect(url_for("agent_tokens.list_tokens"))


@bp.route("/admin/agent-tokens/<int:token_id>/revoke", methods=["POST"])
@require_login
def revoke_token(token_id: int):
    _require_admin()
    db = get_master_db()
    row = db.execute(
        "SELECT id, name, revoked_at FROM agent_tokens WHERE id=?", (token_id,)
    ).fetchone()
    if row is None:
        abort(404)
    if row["revoked_at"] is not None:
        flash(f"Token '{row['name']}' 已经是 revoked 状态")
        return redirect(url_for("agent_tokens.list_tokens"))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE agent_tokens SET revoked_at=? WHERE id=?",
        (now, token_id),
    )
    db.commit()
    flash(f"Token '{row['name']}' 已撤销", "token-revoked")
    _log_admin_action(f"revoke token '{row['name']}' #{token_id}")
    return redirect(url_for("agent_tokens.list_tokens"))


def _log_admin_action(detail: str) -> None:
    """Append an admin action line to admin_audit.log."""
    import json
    from pathlib import Path
    log = Path(__file__).resolve().parent.parent / "admin_audit.log"
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "actor": g.user["username"] if g.user else None,
            "detail": detail,
        }, ensure_ascii=False) + "\n")
