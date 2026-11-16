"""Agent token management (platform admin only).

CRITICAL SECURITY NOTE: The raw token is shown exactly once after creation.
There is no way to retrieve it again. Store it immediately.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import generate_password_hash

from config import AGENT_TOKEN_PREFIX_LEN, SECRET_KEY
from db import get_master_db
from permissions import require_platform_admin


def _fernet():
    import base64
    import hashlib

    from cryptography.fernet import Fernet
    # Derive a stable Fernet key from the app SECRET_KEY
    key = base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest())
    return Fernet(key)


def _encrypt_token(raw: str) -> str:
    return _fernet().encrypt(raw.encode()).decode()


def _decrypt_token(encrypted: str) -> str:
    return _fernet().decrypt(encrypted.encode()).decode()


def _ensure_encrypted_token_col(db):
    """Add encrypted_token column if it doesn't exist (zero-cost on repeated runs)."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(agent_tokens)")]
    if "encrypted_token" not in cols:
        db.execute("ALTER TABLE agent_tokens ADD COLUMN encrypted_token TEXT")
        db.commit()
        # Reload schema cache so subsequent SELECT in the same request sees the new column
        db.execute("PRAGMA schema_reload")


bp = Blueprint("agent_tokens", __name__)


def _parse_paths(s: str) -> str:
    s = s.strip()
    if not s:
        return "[]"
    if s == "*":
        return '["*"]'
    paths = list(dict.fromkeys(p.strip() for p in s.split(",") if p.strip()))
    if any(
        p != "*" and (not p.startswith("/api/v1/") or "?" in p or "#" in p
                      or ".." in p or any(c.isspace() for c in p)
                      or "*" in p.rstrip("*"))
        for p in paths
    ):
        raise ValueError("路径须为 /api/v1/ 开头的路径，可在末尾使用 *；或单独使用 *")
    return json.dumps(paths)


def _parse_warehouses(s: str) -> str:
    s = s.strip()
    if not s:
        return "null"
    return json.dumps([w.strip() for w in s.split(",") if w.strip()])


def _review_signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="agent-token-create-review-v1")


def _creation_acl(form, db):
    """Parse only new-token input; never rewrite an existing token's ACL."""
    name = form.get("name", "").strip()
    if not name or len(name) > 100:
        raise ValueError("Token 名称必填，最多 100 字符")
    read = _parse_paths(form.get("read_paths", ""))
    write = _parse_paths(form.get("write_paths", ""))
    # Old callers keep empty-input -> null. The new UI explicitly defaults to none.
    mode = form.get("warehouse_scope", "legacy")
    codes = list(dict.fromkeys(
        code.strip() for value in form.getlist("warehouses")
        for code in value.split(",") if code.strip()
    ))
    known = {r["code"] for r in db.execute("SELECT code FROM warehouses")}
    if not set(codes) <= known:
        raise ValueError("存在未知仓库，请重新选择")
    if mode == "legacy":
        warehouses = _parse_warehouses(",".join(codes))
    elif mode == "all" and not codes:
        warehouses = "null"
    elif mode == "none" and not codes:
        warehouses = "[]"
    elif mode == "selected" and codes:
        warehouses = json.dumps(codes)
    else:
        raise ValueError("仓库范围与选择不一致；指定仓库至少选择一项")
    return dict(name=name, read=read, write=write, warehouses=warehouses, actor=g.user["id"])


def _broad_acl(acl):
    return acl["warehouses"] == "null" or any(
        p == "*" or p.endswith("*") or p.rstrip("/") == "/api/v1"
        for key in ("read", "write") for p in json.loads(acl[key])
    )


@bp.route("/admin/agent-tokens", methods=["GET"])
@require_platform_admin
def list_tokens():
    db = get_master_db()
    _ensure_encrypted_token_col(db)
    rows = db.execute(
        """SELECT id, name, created_by, created_at, revoked_at,
                  allowed_read_paths_json, allowed_write_paths_json,
                  allowed_warehouse_codes_json, encrypted_token
           FROM agent_tokens
           ORDER BY created_at DESC"""
    ).fetchall()
    tokens = []
    for row in rows:
        token_value = None
        if row["encrypted_token"] and not row["revoked_at"]:
            try:
                token_value = _decrypt_token(row["encrypted_token"])
            except Exception:
                token_value = None
        tokens.append({**row, "token_value": token_value})
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
@require_platform_admin
def create_token():
    db = get_master_db()
    if "review" in request.form:
        try:
            acl = _review_signer().loads(request.form["review"], max_age=600)
        except BadSignature:
            abort(400, "权限确认已失效，请重新预览")
        if acl["actor"] != g.user["id"]:
            abort(403)
        # Revalidate warehouse existence at confirmation, without taking unsigned ACL fields.
        known = {r["code"] for r in db.execute("SELECT code FROM warehouses")}
        if not set(json.loads(acl["warehouses"]) or []) <= known:
            abort(400, "仓库已变更，请重新预览")
        if _broad_acl(acl) and request.form.get("confirm_broad") != "yes":
            abort(400, "请明确确认全部仓库或通配路径权限")
    else:
        try:
            acl = _creation_acl(request.form, db)
        except ValueError as error:
            return render_template("agent_token_review.html", error=str(error)), 400
        return render_template(
            "agent_token_review.html", acl=acl, review=_review_signer().dumps(acl),
            broad=_broad_acl(acl),
        )
    name = acl["name"]
    _ensure_encrypted_token_col(db)

    raw_token = secrets.token_urlsafe(32)
    token_hash = generate_password_hash(raw_token, method="pbkdf2:sha256")
    encrypted = _encrypt_token(raw_token)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        db.execute(
            """INSERT INTO agent_tokens
               (name, token_hash, token_prefix, encrypted_token, created_by, created_at,
                allowed_read_paths_json, allowed_write_paths_json,
                allowed_warehouse_codes_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                token_hash,
                raw_token[:AGENT_TOKEN_PREFIX_LEN],
                encrypted,
                g.user["id"],
                now,
                acl["read"],
                acl["write"],
                acl["warehouses"],
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash(f"Token 名称 '{name}' 已存在")
        return redirect(url_for("agent_tokens.list_tokens"))

    flash(f"已创建 Token '{name}'", "token-created")
    session["_new_token"] = raw_token
    session["_new_token_name"] = name
    _log_admin_action(f"create token '{name}' read={acl['read']} write={acl['write']} warehouses={acl['warehouses']}")
    return redirect(url_for("agent_tokens.list_tokens"))


@bp.route("/admin/agent-tokens/<int:token_id>/revoke", methods=["POST"])
@require_platform_admin
def revoke_token(token_id: int):
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
    """Append an admin action line via the single shared implementation."""
    from blueprints.audit_pure import append_admin_audit

    append_admin_audit(g.user["username"] if g.user else None, detail)
