"""Issue #16: strict read-only policy validation and guarded SQLite access.

These are unit-level checks for ``mcp_server/readonly.py``.  The real MCP
protocol coverage lives in ``tests/mcp_server/test_protocol/``.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcp_server import readonly
from mcp_server.infra.errors import McpError


def _deadline(*, hours: int = 2) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).isoformat().replace("+00:00", "Z")


@pytest.fixture
def policy_files(tmp_path):
    root = tmp_path / "warehouses"
    root.mkdir()
    master = tmp_path / "master.db"
    with sqlite3.connect(master):
        pass
    warehouse = root / "wh_a.db"
    with sqlite3.connect(warehouse) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, quantity INTEGER)")
        conn.execute("INSERT INTO items (id, quantity) VALUES (1, 5)")
    return {"root": root.resolve(), "master": master.resolve(), "warehouse": warehouse.resolve()}


def _mapping(files, **overrides):
    data = {
        "schema_version": 1,
        "token_id": 3,
        "expires_at": _deadline(),
        "master_path": str(files["master"]),
        "warehouse_root": str(files["root"]),
        "warehouses": {"wh_a": str(files["warehouse"])},
    }
    data.update(overrides)
    return data


def _build(files, **overrides) -> readonly.ReadonlyPolicy:
    return readonly.ReadonlyPolicy.from_mapping(
        _mapping(files, **overrides), configured_master_path=files["master"]
    )


def test_valid_policy_round_trip(policy_files):
    policy = _build(policy_files)
    assert policy.token_id == 3
    assert policy.master_path == policy_files["master"]
    assert policy.warehouse_root == policy_files["root"]
    assert policy.approved_file("wh_a") == policy_files["warehouse"]
    assert policy.approved_file("wh_b") is None
    with pytest.raises(TypeError):
        policy.warehouses["wh_b"] = policy_files["warehouse"]  # immutable mapping


def test_loads_policy_file_and_uses_configured_master(tmp_path, monkeypatch, policy_files):
    import config

    monkeypatch.setattr(config, "MASTER_DB", policy_files["master"])
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_mapping(policy_files)), encoding="utf-8")
    policy = readonly.ReadonlyPolicy.load(path)
    assert policy.token_id == 3
    assert policy.approved_file("wh_a") == policy_files["warehouse"]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.pop("token_id"), "missing policy field"),
        (lambda d: d.update(extra="x"), "unknown policy field"),
        (lambda d: d.update(schema_version=2), "schema_version"),
        (lambda d: d.update(schema_version=True), "schema_version"),
        (lambda d: d.update(token_id=0), "token_id"),
        (lambda d: d.update(token_id=True), "token_id"),
        (lambda d: d.update(token_id="3"), "token_id"),
        (lambda d: d.update(expires_at="not-a-date"), "expires_at"),
        (lambda d: d.update(expires_at="2099-01-01T00:00:00"), "UTC offset"),
        (lambda d: d.update(expires_at=_deadline(hours=-1)), "past"),
        (lambda d: d.update(warehouses={}), "non-empty"),
        (lambda d: d.update(master_path="/nonexistent/master.db"), "master_path"),
    ],
)
def test_rejects_malformed_policy(policy_files, mutate, message):
    data = _mapping(policy_files)
    mutate(data)
    with pytest.raises(readonly.ReadonlyPolicyError, match=message):
        readonly.ReadonlyPolicy.from_mapping(data, configured_master_path=policy_files["master"])


def test_rejects_master_that_is_not_the_configured_master(policy_files):
    with pytest.raises(readonly.ReadonlyPolicyError, match="configured master"):
        readonly.ReadonlyPolicy.from_mapping(
            _mapping(policy_files, master_path=str(policy_files["warehouse"])),
            configured_master_path=policy_files["master"],
        )


def test_rejects_relative_and_missing_paths(policy_files):
    with pytest.raises(readonly.ReadonlyPolicyError, match="absolute"):
        _build(policy_files, warehouse_root="relative/dir")
    with pytest.raises(readonly.ReadonlyPolicyError, match="does not exist"):
        _build(policy_files, warehouses={"wh_a": str(policy_files["root"] / "absent.db")})


def test_rejects_root_escape_and_symlink_alias(policy_files, tmp_path):
    outside = tmp_path / "outside.db"
    with sqlite3.connect(outside) as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")
    outside = outside.resolve()
    with pytest.raises(readonly.ReadonlyPolicyError, match="escapes warehouse_root"):
        _build(policy_files, warehouses={"wh_a": str(outside)})

    link = policy_files["root"] / "link.db"
    link.symlink_to(outside)
    with pytest.raises(readonly.ReadonlyPolicyError, match="escapes warehouse_root"):
        _build(policy_files, warehouses={"wh_a": str(link)})


def test_rejects_duplicate_file_alias(policy_files):
    with pytest.raises(readonly.ReadonlyPolicyError, match="duplicate"):
        _build(
            policy_files,
            warehouses={
                "wh_a": str(policy_files["warehouse"]),
                "wh_b": str(policy_files["warehouse"]),
            },
        )


def test_rejects_non_file_paths(policy_files):
    with pytest.raises(readonly.ReadonlyPolicyError, match="not a file"):
        _build(policy_files, master_path=str(policy_files["root"]))
    with pytest.raises(readonly.ReadonlyPolicyError, match="not a file"):
        _build(policy_files, warehouses={"wh_a": str(policy_files["root"])})


def test_ensure_token_active_checks_expiry_and_revocation(policy_files):
    with sqlite3.connect(policy_files["master"]) as conn:
        conn.execute("CREATE TABLE agent_tokens (id INTEGER PRIMARY KEY, revoked_at TEXT)")
        conn.execute("INSERT INTO agent_tokens (id, revoked_at) VALUES (3, NULL)")
        conn.commit()
    policy = _build(policy_files)
    readonly.ensure_token_active(policy)
    expired = readonly.ReadonlyPolicy(
        token_id=policy.token_id,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        master_path=policy.master_path,
        warehouse_root=policy.warehouse_root,
        warehouses=policy.warehouses,
    )
    with pytest.raises(readonly.ReadonlyPolicyError):
        readonly.ensure_token_active(expired)
    with sqlite3.connect(policy_files["master"]) as conn:
        conn.execute("UPDATE agent_tokens SET revoked_at = datetime('now') WHERE id = 3")
        conn.commit()
    with pytest.raises(readonly.ReadonlyPolicyError):
        readonly.ensure_token_active(policy)


def test_cli_refuses_to_start_with_a_broken_policy(tmp_path):
    from click.testing import CliRunner

    from mcp_server import main

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    result = CliRunner().invoke(main.run, ["--readonly-policy", str(broken)])
    assert result.exit_code != 0
    assert "readonly policy rejected" in result.output


def test_deadline_and_argument_validation(policy_files):
    policy = _build(policy_files)
    policy.ensure_active()
    expires = policy.expires_at
    assert not policy.is_expired(now=expires - timedelta(seconds=1))
    assert policy.is_expired(now=expires)
    with pytest.raises(readonly.ReadonlyPolicyError):
        policy.ensure_active(now=expires)

    readonly.validate_strict_arguments("warehouse_list", None)
    readonly.validate_strict_arguments("items_list", {"warehouse_code": "wh_a"})
    readonly.validate_strict_arguments("items_detail", {"item_id": 1, "warehouse_code": "wh_a"})
    for name, arguments in [
        ("warehouse_list", {"extra": 1}),
        ("items_list", {"warehouse_code": "wh_a", "read_only": True}),
        ("items_list", {"warehouse_code": 5}),
        ("items_list", {}),
        ("items_detail", {"item_id": True, "warehouse_code": "wh_a"}),
        ("items_detail", {"item_id": "1", "warehouse_code": "wh_a"}),
        ("movements_list", {"warehouse_code": "wh_a"}),
    ]:
        with pytest.raises(McpError):
            readonly.validate_strict_arguments(name, arguments)


def _snapshot(path: Path) -> bytes:
    return path.read_bytes()


def test_guarded_connection_denies_writes_ddl_attach_and_keeps_bytes(tmp_path):
    for name in ("master.db", "warehouse.db"):
        path = tmp_path / name
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            conn.execute("INSERT INTO t (id, v) VALUES (1, 'a')")
            conn.commit()
        finally:
            conn.close()
        before = _snapshot(path)
        attach_target = tmp_path / f"attached-{name}"
        with readonly.readonly_connection(path) as conn:
            assert tuple(conn.execute("SELECT v FROM t WHERE id = 1").fetchone()) == ("a",)
            statements = [
                "UPDATE t SET v = 'b'",
                "INSERT INTO t (id, v) VALUES (2, 'b')",
                "DELETE FROM t",
                "CREATE TABLE x (id INTEGER)",
                "DROP TABLE t",
                f"ATTACH DATABASE '{attach_target.as_posix()}' AS other",
                "DETACH DATABASE other",
                "PRAGMA user_version = 9",
            ]
            for statement in statements:
                with pytest.raises(sqlite3.Error):
                    conn.execute(statement)
        assert _snapshot(path) == before
        assert not attach_target.exists()


def test_guarded_connection_encodes_uri_characters(tmp_path):
    path = tmp_path / "wh ?#.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (7)")
        conn.commit()
    finally:
        conn.close()
    with readonly.readonly_connection(path) as conn:
        assert tuple(conn.execute("SELECT id FROM t").fetchone()) == (7,)
