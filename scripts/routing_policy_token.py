"""Issue an approved Gene routing credential from an existing v10 master."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

import click
from itsdangerous import BadData, URLSafeTimedSerializer
from werkzeug.security import check_password_hash

from blueprints.item_routing_pure import get_routing
from mcp_server.data.unit_of_work import require_policy_read_file
from mcp_server.service import routing_approvals
from mcp_server.service.policy import MAX_SQLITE_ID, approved_routing_pairs, encode_grants
from mcp_server.service.policy_issuance import insert_bound_token

_CONFIRMATION_AGE = 600


def _signer(salt="dailycheck-routing-issuance-v1"):
    secret = os.environ.get("DAILYCHECK_ROUTING_ISSUANCE_SECRET", "")
    if len(secret) < 32:
        raise click.ClickException("Routing issuance requires an independent confirmation secret")
    if os.environ.get("DRY_RUN", "").lower() == "true":
        raise click.ClickException("Routing issuance disabled in dry run")
    return URLSafeTimedSerializer(secret, salt=salt)


def _database_identity(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino]


def _open(path, *, mode):
    path = path.resolve(strict=True)
    if mode == "ro":
        require_policy_read_file(path)
    conn = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if mode == "ro":
        conn.execute("PRAGMA query_only=ON")
    return closing(conn)


def _validate_operator(conn, issuer, password):
    if conn.execute("PRAGMA user_version").fetchone()[0] < 10:
        raise ValueError("Master v10 required")
    operator = conn.execute(
        "SELECT password_hash FROM users WHERE id=? AND tenant_id=1 AND is_admin=1 AND is_active=1", (issuer,),
    ).fetchone()
    if operator is None or not check_password_hash(operator[0], password):
        raise ValueError("Authenticated active issuance operator required")


def _validate(conn, password, *, issuer, actor, name, preset, version, pairs):
    _validate_operator(conn, issuer, password)
    if not name or name != name.strip() or len(name) > 100:
        raise ValueError("Invalid credential name")
    allowed = approved_routing_pairs(conn, 1, actor)
    if not pairs or len(pairs) != len(set(pairs)) or not set(pairs) <= allowed.keys():
        raise ValueError("Active Gene principal and approved exact scope required")
    for cid, code in pairs:
        if get_routing(conn, cid) != code or conn.execute(
            "SELECT 1 FROM warehouses WHERE code=? AND tenant_id=1 AND warehouse_type='distribution_center'",
            (code,),
        ).fetchone() is None:
            raise ValueError("Approved warehouse must still be the original source")
    row = conn.execute(
        "SELECT grants_json FROM mcp_policy_presets WHERE name=? AND version=? AND disabled_at IS NULL",
        (preset, version),
    ).fetchone()
    if not row:
        raise ValueError("Active routing preset version required")
    grants = encode_grants(json.loads(row[0]), routing=True)
    if grants != row[0]:
        raise ValueError("Invalid routing preset snapshot")
    return grants, [[cid, code, allowed[(cid, code)]] for cid, code in pairs]


@click.group()
@click.option("--db", required=True, type=click.Path(path_type=Path, dir_okay=False, exists=True))
@click.pass_context
def main(ctx, db):
    """Use from an authorized host session. Preview and confirmation are separate commands."""
    ctx.obj = db


_ID = click.IntRange(1, MAX_SQLITE_ID)


@main.command()
@click.option("--issuer", required=True, type=_ID)
@click.option("--actor", required=True, type=_ID)
@click.option("--name", required=True)
@click.option("--preset", required=True)
@click.option("--version", required=True, type=_ID)
@click.option("--pair", "pairs", required=True, multiple=True, type=(_ID, str))
@click.pass_obj
def preview(path, **request):
    """Display and sign an exact, read-only authorization preview."""
    signer = _signer()
    password = click.prompt("Issuing operator password", hide_input=True, err=True)
    try:
        with _open(path, mode="ro") as conn:
            conn.execute("BEGIN")
            grants, approvals = _validate(conn, password, **request)
        snapshot = {
            **request, "grants_json": grants, "confirmation_id": secrets.token_urlsafe(24),
            "database": _database_identity(path), "approvals": approvals,
        }
        click.echo(json.dumps({**snapshot, "confirmation": signer.dumps(snapshot)}, ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise click.ClickException(str(exc)) from exc


def _credential_destination(path):
    parent = path.parent.resolve(strict=True)
    stat = parent.stat()
    if not parent.is_dir() or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
        raise ValueError("Credential directory must be owned by this operator and private")
    return parent / path.name


@main.command()
@click.option("--issuer", required=True, type=_ID)
@click.option("--confirmation", required=True)
@click.option("--token-file", required=True, type=click.Path(path_type=Path, dir_okay=False))
@click.pass_obj
def issue(path, issuer, confirmation, token_file):
    """Consume the confirmation and write the bearer once to a new private file."""
    signer = _signer()
    password = click.prompt("Issuing operator password", hide_input=True, err=True)
    created = None
    try:
        destination = _credential_destination(token_file)
        snapshot = signer.loads(confirmation, max_age=_CONFIRMATION_AGE)
        if snapshot["issuer"] != issuer or snapshot.pop("database") != _database_identity(path):
            raise ValueError("Confirmation belongs to another operator or database")
        grant = snapshot.pop("grants_json")
        approvals = snapshot.pop("approvals")
        confirmation_id = snapshot.pop("confirmation_id")
        snapshot["pairs"] = [tuple(pair) for pair in snapshot["pairs"]]
        with _open(path, mode="rw") as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if _validate(conn, password, **snapshot) != (grant, approvals):
                raise ValueError("Confirmed permission snapshot changed")
            token_id, raw = insert_bound_token(
                conn, name=snapshot["name"], preset=snapshot["preset"], version=snapshot["version"],
                grants_json=grant, warehouses_json="[]", confirmation_id=confirmation_id, issuer=issuer,
            )
            conn.execute(
                "INSERT INTO mcp_policy_routing_scopes (token_id, tenant_id, actor_id, pairs_json) VALUES (?, 1, ?, ?)",
                (token_id, snapshot["actor"], json.dumps(approvals, separators=(",", ":"))),
            )
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = destination
            with os.fdopen(fd, "w") as output:
                output.write(raw + "\n")
                output.flush()
                os.fsync(output.fileno())
    except (BadData, KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
        if created is not None:
            created.unlink(missing_ok=True)
        raise click.ClickException("Issuance refused: invalid authorization or unavailable private token file") from exc
    click.echo(json.dumps({"token_id": token_id}))


@main.command("authorization-preview")
@click.option("--operation", required=True, type=click.Choice(["install-preset", "approve", "revoke"]))
@click.option("--issuer", required=True, type=_ID)
@click.option("--actor", type=_ID)
@click.option("--pair", type=(_ID, str), multiple=True)
@click.option("--reference", required=True)
@click.pass_obj
def authorization_preview(path, issuer, operation, actor, pair, reference):
    """Preview a fixed preset or one exact Gene approval decision without writing."""
    if len(pair) > 1:
        raise click.BadParameter("Only one exact pair may be reviewed", param_hint="--pair")
    signer = _signer("dailycheck-routing-approval-v1")
    password = click.prompt("Issuing operator password", hide_input=True, err=True)
    decision = {"operation": operation, "actor": actor, "pair": list(pair[0]) if pair else None,
                "reference": reference}
    try:
        with _open(path, mode="ro") as conn:
            conn.execute("BEGIN")
            _validate_operator(conn, issuer, password)
            current = routing_approvals.preview(conn, path, **decision)
        snapshot = {
            **decision, "issuer": issuer, "state": current,
            "confirmation_id": secrets.token_urlsafe(24), "database": _database_identity(path),
        }
        click.echo(json.dumps({**snapshot, "confirmation": signer.dumps(snapshot)}, ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("authorization-confirm")
@click.option("--issuer", required=True, type=_ID)
@click.option("--confirmation", required=True)
@click.pass_obj
def authorization_confirm(path, issuer, confirmation):
    """Recheck and atomically record the confirmed authorization decision."""
    signer = _signer("dailycheck-routing-approval-v1")
    password = click.prompt("Issuing operator password", hide_input=True, err=True)
    try:
        snapshot = signer.loads(confirmation, max_age=_CONFIRMATION_AGE)
        if snapshot.pop("issuer") != issuer or snapshot.pop("database") != _database_identity(path):
            raise ValueError("Confirmation belongs to another operator or database")
        with _open(path, mode="rw") as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            _validate_operator(conn, issuer, password)
            result = routing_approvals.apply(conn, path, issuer=issuer, **snapshot)
    except (BadData, KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
        raise click.ClickException("Authorization refused: invalid, expired or changed decision") from exc
    click.echo(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
