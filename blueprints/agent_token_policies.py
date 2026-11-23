"""Platform-admin policy operations invoked by the existing Agent Tokens form."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import (
    abort,
    current_app,
    g,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from itsdangerous import BadData, URLSafeTimedSerializer

from blueprints.audit_pure import append_admin_audit
from mcp_server.protocol.registry import get_policy_actions
from mcp_server.service.policy import (
    SAFE_FIELDS,
    canonical_grants,
    encode_grants,
    storefront_scope,
)
from mcp_server.service.policy_issuance import insert_bound_token

_PRESET_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_PREVIEW_MAX_AGE_SECONDS = 600


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(detail: str) -> None:
    try:
        append_admin_audit(g.user["username"], detail)
    except OSError:
        # Durable provenance is in the same transaction as the operation.
        current_app.logger.warning("Could not append MCP policy admin audit")


def _operator() -> dict:
    csrf_session = session.get(current_app.config.get("WTF_CSRF_FIELD_NAME", "csrf_token"))
    if not isinstance(csrf_session, str) or not csrf_session:
        abort(400, "请重新打开 Token 管理页面后再试。")
    return {"user_id": g.user["id"], "session_binding": hashlib.sha256(csrf_session.encode()).hexdigest()}


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt="dailycheck-mcp-policy-confirmation-v1")


def _no_store(status: int = 200, **context):
    response = make_response(render_template("agent_tokens.html", **context), status)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _form(allowed: set[str]) -> None:
    csrf_field = current_app.config.get("WTF_CSRF_FIELD_NAME", "csrf_token")
    if request.form.keys() - allowed - {"policy_action", csrf_field}:
        abort(400, "无效的策略表单。")


def _one(name: str) -> str:
    values = request.form.getlist(name)
    if len(values) != 1 or not values[0]:
        abort(400, "无效的策略表单。")
    return values[0]


def _preset(db, name: str, version: int) -> str:
    row = db.execute(
        "SELECT grants_json FROM mcp_policy_presets WHERE name = ? AND version = ? AND disabled_at IS NULL",
        (name, version),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown or disabled preset")
    canonical = encode_grants(json.loads(row[0]))
    if row[0] != canonical:
        raise ValueError("Invalid preset")
    return canonical


def policy_tables_present(db) -> bool:
    return db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('mcp_policy_presets', 'mcp_policy_bindings')"
    ).fetchone()[0] == 2


def _effective_tools(grants_json: str) -> dict:
    try:
        grants = canonical_grants(json.loads(grants_json))
    except (TypeError, ValueError):
        return {}
    if encode_grants(grants) != grants_json:
        return {}
    return {
        name: {"action": action, "fields": grants[action]}
        for name, action in get_policy_actions().items() if action in grants
    }


def management_context(db) -> dict:
    if not policy_tables_present(db):
        return {}
    return {
        "policy_fields": SAFE_FIELDS,
        "policy_presets": db.execute(
            "SELECT name, version, disabled_at FROM mcp_policy_presets ORDER BY name, version DESC"
        ).fetchall(),
        "policy_bindings": {
            row["token_id"]: {**row, "tools": _effective_tools(row["grants_json"])} for row in db.execute(
                "SELECT token_id, preset_name, preset_version, warehouse_codes_json, "
                "grants_json, created_by, revoked_by, revoked_at FROM mcp_policy_bindings"
            )
        },
        "policy_storefronts": db.execute(
            "SELECT code, name FROM warehouses WHERE warehouse_type = 'storefront' ORDER BY code"
        ).fetchall(),
    }


def _publish(db):
    keys = {f"field_{action}" for action in SAFE_FIELDS}
    _form({"preset_name", *keys})
    name = _one("preset_name")
    if not _PRESET_NAME.fullmatch(name):
        abort(400, "预设名称须为小写字母、数字或连字符，以字母开头。")
    grants = {action: request.form.getlist(f"field_{action}") for action in SAFE_FIELDS
              if f"field_{action}" in request.form}
    try:
        snapshot = encode_grants(grants)
    except ValueError:
        abort(400, "选择至少一项读取权限及其允许字段。")
    try:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT MAX(version) FROM mcp_policy_presets WHERE name = ?", (name,)).fetchone()[0]
        version = (current or 0) + 1
        db.execute(
            "INSERT INTO mcp_policy_presets (name, version, grants_json, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, version, snapshot, g.user["id"], _utc_now()),
        )
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise
    _audit(f"publish MCP policy preset {name} v{version}")
    return redirect(url_for("agent_tokens.list_tokens"))


def _preview(db):
    _form({"token_name", "preset", "policy_warehouses"})
    name = _one("token_name").strip()
    preset, sep, version_text = _one("preset").partition(":")
    if not name or len(name) > 100 or not sep or not _PRESET_NAME.fullmatch(preset):
        abort(400, "无效的令牌名称或预设。")
    try:
        version = int(version_text)
        grants = _preset(db, preset, version)
        codes = storefront_scope(db, request.form.getlist("policy_warehouses"))
    except (ValueError, OverflowError):
        abort(400, "预设或门店范围无效。")
    snapshot = {
        **_operator(),
        "token_name": name,
        "preset": preset,
        "version": version,
        "grants_json": grants,
        "warehouses": list(codes),
        "confirmation_id": secrets.token_urlsafe(24),
    }
    return _no_store(
        policy_preview={**snapshot, "tools": _effective_tools(grants)},
        policy_confirmation=_serializer().dumps(snapshot),
    )


def _issue(db):
    _form({"confirmation"})
    try:
        snapshot = _serializer().loads(_one("confirmation"), max_age=_PREVIEW_MAX_AGE_SECONDS)
    except BadData:
        abort(400, "确认凭证无效或已过期。")
    operator = _operator()
    if not isinstance(snapshot, dict) or any(snapshot.get(key) != value for key, value in operator.items()):
        abort(403, "此确认凭证不属于当前会话。")

    try:
        db.execute("BEGIN IMMEDIATE")
        name = snapshot["token_name"]
        preset, version = snapshot["preset"], snapshot["version"]
        confirmed = _preset(db, preset, version)
        codes = storefront_scope(db, snapshot["warehouses"])
        if confirmed != snapshot["grants_json"] or list(codes) != snapshot["warehouses"]:
            raise ValueError("Confirmed scope changed")
        identity, raw = insert_bound_token(
            db, name=name, preset=preset, version=version, grants_json=confirmed,
            warehouses_json=json.dumps(codes), confirmation_id=snapshot["confirmation_id"], issuer=g.user["id"],
        )
        db.commit()
    except (KeyError, TypeError, ValueError, OverflowError, sqlite3.IntegrityError):
        db.rollback()
        abort(409, "确认已使用、名称仍有效或预设和门店范围已失效。")
    except sqlite3.Error:
        db.rollback()
        raise
    _audit(f"issue MCP policy token #{identity} using {preset} v{version}")
    return _no_store(201, new_token=raw, new_token_name=name, policy_issued_id=identity)


def handle_policy_action(db):
    action = _one("policy_action")
    if current_app.config.get("DRY_RUN") or not policy_tables_present(db):
        abort(409, "当前环境未启用策略令牌管理。")
    operations = {"publish": _publish, "preview": _preview, "confirm": _issue}
    if action not in operations:
        abort(400, "无效的策略表单。")
    return operations[action](db)


def revoke_policy_token(db, token_id: int, now: str) -> bool:
    """Commit the existing credential revocation and its policy provenance together."""
    try:
        db.execute("BEGIN IMMEDIATE")
        updates = db.execute(
            "UPDATE agent_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now, token_id),
        ).rowcount
        if updates and policy_tables_present(db):
            db.execute(
                "UPDATE mcp_policy_bindings SET revoked_at = ?, revoked_by = ? "
                "WHERE token_id = ? AND revoked_at IS NULL", (now, g.user["id"], token_id),
            )
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise
    if updates:
        _audit(f"revoke MCP policy token #{token_id}")
    return bool(updates)
