"""Issue #16: the finite Lucy read runner (local, synthetic fixtures only).

These tests never touch the network, production, existing SSH config or any
real credential.  The complete consume lifecycle uses a real SQLite master and
warehouse, a real holder process for the active-business binding, and a real
MCP stdio child through the public SDK.  The relay tests inject a hermetic fake
``ssh`` executable through PATH.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

import scripts.mcp_lucy_read_once as runner

REPO_ROOT = Path(__file__).resolve().parents[1]
CODE = "wh_synth"
CORRELATION = "readonce-synthetic-1"


def _head_sha() -> str:
    return subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], text=True
    ).strip()


HEAD_SHA = _head_sha()


def _utc_iso(seconds: int) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _token_rows(master: Path) -> list[dict]:
    conn = sqlite3.connect(master)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, name, token_hash, revoked_at, allowed_read_paths_json, "
            "allowed_write_paths_json, allowed_warehouse_codes_json, rate_limit "
            "FROM agent_tokens ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _build_master(master: Path, warehouse_file: Path) -> None:
    import db

    conn = sqlite3.connect(master)
    try:
        conn.executescript(db.MASTER_SCHEMA)
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) "
            "VALUES ('synthetic-admin', 'x', 1, '2026-11-14')"
        )
        conn.execute(
            "INSERT INTO warehouses (code, name, db_path, created_at) "
            "VALUES (?, ?, ?, '2026-11-14')",
            (CODE, "Synthetic warehouse", str(warehouse_file)),
        )
        conn.execute(
            "INSERT INTO agent_tokens "
            "(name, token_hash, token_prefix, created_by, created_at, "
            "allowed_read_paths_json, allowed_write_paths_json, "
            "allowed_warehouse_codes_json) "
            "VALUES ('existing-agent', ?, 'existing', 1, '2026-11-14', ?, '[]', 'null')",
            (
                generate_password_hash("existing-secret", method="pbkdf2:sha256:1000"),
                json.dumps(["*"]),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _build_warehouse(warehouse_file: Path) -> None:
    import db

    conn = sqlite3.connect(warehouse_file)
    try:
        conn.executescript(db.WAREHOUSE_SCHEMA)
        conn.execute(
            "INSERT INTO categories (id, name, created_at) VALUES (1, '包材', '2026-11-14')"
        )
        conn.execute(
            "INSERT INTO items (id, sku, name, category_id, quantity, safety_stock, "
            "unit, unit_cost, gram_per_unit, updated_at) "
            "VALUES (1, 'SKU1', 'Synthetic item', 1, 33, 1, 'pcs', 12.5, 0, '2026-11-14')"
        )
        conn.commit()
    finally:
        conn.close()


def _start_time(pid: int) -> str:
    value = runner._process_start_time(pid)
    assert value, "could not read the holder start time"
    return value


@pytest.fixture
def env(tmp_path):
    root = tmp_path.resolve()
    warehouses = root / "warehouses"
    warehouses.mkdir()
    warehouse_file = warehouses / f"{CODE}.db"
    _build_warehouse(warehouse_file)
    master = root / "master.db"
    _build_master(master, warehouse_file)
    fence = root / "deploy.lock"
    fence.write_text("", encoding="utf-8")
    os.chmod(fence, 0o600)
    anchor = root / "deployed.json"
    anchor.write_text(
        json.dumps(
            {
                "source_sha": HEAD_SHA,
                "release_id": "synthetic-release-1",
                "deployment_root": str(root),
            }
        ),
        encoding="utf-8",
    )
    claims = root / "claims"
    claims.mkdir(mode=0o700)

    code = textwrap.dedent(
        """
        import sqlite3, sys, time
        conn = sqlite3.connect(sys.argv[1])
        conn.execute("SELECT 1 FROM users").fetchone()
        print("ready", flush=True)
        time.sleep(120)
        """
    )
    holder = subprocess.Popen(
        [sys.executable, "-B", "-c", code, str(master)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "ready"
    env = {
        "root": root,
        "warehouses": warehouses,
        "warehouse_file": warehouse_file,
        "master": master,
        "fence": fence,
        "anchor": anchor,
        "claims": claims,
        "holder": holder,
        "holder_start": _start_time(holder.pid),
    }
    try:
        yield env
    finally:
        if holder.poll() is None:
            holder.terminate()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=5)
        if holder.stdout is not None:
            holder.stdout.close()


def _plan_data(env, **overrides) -> dict:
    data = {
        "schema": runner.PLAN_SCHEMA,
        "version": 1,
        "correlation": CORRELATION,
        "identity": "synthetic",
        "created_at": _utc_iso(0),
        "expires_at": _utc_iso(7200),
        "budget_seconds": 30,
        "source": {
            "repository": str(REPO_ROOT),
            "git_sha": HEAD_SHA,
            "python": sys.executable,
            "module": "mcp_server",
        },
        "deploy_anchor": {"path": str(env["anchor"]), "source_sha": HEAD_SHA},
        "database": {
            "master_path": str(env["master"]),
            "warehouse_root": str(env["warehouses"]),
            "warehouse_code": CODE,
            "warehouse_file": str(env["warehouse_file"]),
        },
        "active_business": {"pid": env["holder"].pid, "start_time": env["holder_start"]},
        "fence": {"path": str(env["fence"])},
        "claim_dir": str(env["claims"]),
        "item": {"id": 1},
        "field_consent": {"item_detail": True, "unit_cost": True},
        "controls": {"issue_identity": True},
        "token": {"created_by_user_id": 1, "rate_limit": 30},
    }
    data.update(overrides)
    return data


def _write_plan(env, data: dict, name: str = "plan.json") -> tuple[Path, str]:
    path = env["root"] / name
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, 0o600)
    return path, _sha_file(path)


def _run_consume(capsys, plan: Path, sha: str, correlation: str = CORRELATION):
    code = runner.main(
        [
            "consume",
            "--plan",
            str(plan),
            "--plan-sha256",
            sha,
            "--correlation",
            correlation,
        ]
    )
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, captured.out
    return code, json.loads(lines[0]), captured


def test_consume_real_stdio_one_item_success(env, capsys):
    warehouse_before = _sha_file(env["warehouse_file"])
    existing_before = [row for row in _token_rows(env["master"]) if row["name"] == "existing-agent"]
    plan, sha = _write_plan(env, _plan_data(env))

    code, result, captured = _run_consume(capsys, plan, sha)

    assert code == runner.EXIT_OK, result
    assert result["status"] == "succeeded"
    assert result["correlation"] == CORRELATION
    assert result["plan_sha256"] == sha
    assert result["item"]["id"] == 1
    assert result["item"]["current_stock"] == 33
    assert result["item"]["unit_cost"] == 12.5
    assert result["declared_fields"] == list(runner.ITEM_DETAIL_FIELDS)
    assert result["mcp"]["server_returncode"] == 0
    assert result["mcp"]["child_waited"] is True
    assert isinstance(result["mcp"]["server_pid"], int)
    assert [call["tool"] for call in result["mcp"]["calls"]] == ["warehouse_list", "items_detail"]
    assert result["token"]["revoked"] is True
    assert result["token"]["revocation_verified"] is True
    assert result["claim"]["state"] == "succeeded"
    assert all(result["cleanup"].values())
    assert result["binding"]["kind"] in {"linux-proc", "synthetic-surrogate"}
    assert _sha_file(env["warehouse_file"]) == warehouse_before

    rows = _token_rows(env["master"])
    created = [row for row in rows if row["name"] == runner._token_name(CORRELATION)]
    assert len(created) == 1
    assert created[0]["id"] == result["token"]["id"]
    assert created[0]["revoked_at"] is not None
    assert json.loads(created[0]["allowed_read_paths_json"]) == list(runner.TOKEN_READ_PATHS)
    assert json.loads(created[0]["allowed_write_paths_json"]) == []
    assert json.loads(created[0]["allowed_warehouse_codes_json"]) == [CODE]
    assert [row for row in rows if row["name"] == "existing-agent"] == existing_before

    token_record = env["root"] / f"{CORRELATION}.claim.json"
    receipt = env["claims"] / f"{CORRELATION}.claim.json"
    assert not token_record.exists()
    assert receipt.exists()
    assert captured.err == ""


# ---------------------------------------------------------------------------
# Helpers for the remaining tests
# ---------------------------------------------------------------------------


def _created_rows(master: Path) -> list[dict]:
    return [row for row in _token_rows(master) if row["name"].startswith(runner.TOKEN_NAME_PREFIX)]


def _claim_file(env) -> Path:
    return env["claims"] / f"{CORRELATION}.claim.json"


def _hold_exclusive(path: Path):
    code = textwrap.dedent(
        """
        import fcntl, os, sys, time
        fd = os.open(sys.argv[1], os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        print("locked", flush=True)
        time.sleep(120)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", code, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "locked"
    return process


def _stop(process) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if process.stdout is not None:
        process.stdout.close()


# ---------------------------------------------------------------------------
# Replay barrier
# ---------------------------------------------------------------------------


def test_duplicate_plan_is_replay_rejected_without_second_grant_or_read(env, capsys):
    plan, sha = _write_plan(env, _plan_data(env))
    first_code, first, _ = _run_consume(capsys, plan, sha)
    assert first_code == runner.EXIT_OK
    rows_after_first = _created_rows(env["master"])
    assert len(rows_after_first) == 1
    warehouse_after_first = _sha_file(env["warehouse_file"])
    receipt_before = _claim_file(env).read_bytes()

    second_code, second, _ = _run_consume(capsys, plan, sha)

    assert second_code == runner.EXIT_CLAIM
    assert second["status"] == "replay_rejected"
    assert second["code"] == "already_claimed"
    assert "mcp" not in second
    assert "item" not in second or second["item"] is None
    assert len(_created_rows(env["master"])) == 1
    assert _sha_file(env["warehouse_file"]) == warehouse_after_first
    assert _claim_file(env).read_bytes() == receipt_before


# ---------------------------------------------------------------------------
# Preflight: fail before any grant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, expected_code, cli_hash, cli_correlation, expect_claim",
    [
        (lambda d: None, "plan_hash_mismatch", "b" * 64, CORRELATION, False),
        (lambda d: None, "plan_correlation_mismatch", None, "other-correlation", False),
        (lambda d: d.update(expires_at=_utc_iso(-60)), "plan_expired", None, CORRELATION, False),
        (lambda d: (d["source"].update(git_sha="0" * 40), d["deploy_anchor"].update(source_sha="0" * 40)), "source_sha_mismatch", None, CORRELATION, True),
        (lambda d: d["field_consent"].update(item_detail=False), "field_consent_missing", None, CORRELATION, False),
        (lambda d: d["controls"].update(issue_identity=False), "identity_issuance_not_permitted", None, CORRELATION, False),
        (lambda d: d["database"].update(warehouse_code="wh\nbad"), "database.warehouse_code_invalid", None, CORRELATION, False),
        (lambda d: d.update(budget_seconds=0), "budget_seconds_invalid", None, CORRELATION, False),
        (lambda d: d["item"].update(id=999), "item_not_found", None, CORRELATION, True),
    ],
)
def test_preflight_rejects_before_identity(
    env, capsys, mutate, expected_code, cli_hash, cli_correlation, expect_claim
):
    data = _plan_data(env)
    mutate(data)
    plan, sha = _write_plan(env, data)
    code, result, _ = _run_consume(
        capsys, plan, cli_hash or sha, cli_correlation
    )
    assert code != runner.EXIT_OK, result
    assert result["status"] in {"failed", "reconcile_required"}
    assert result["code"] == expected_code
    assert "item" not in result or result["item"] is None
    assert _created_rows(env["master"]) == []
    if expect_claim:
        assert _claim_file(env).exists()
        receipt = json.loads(_claim_file(env).read_text(encoding="utf-8"))
        assert receipt["state"] == "preflight-rejected"
    else:
        assert list(env["claims"].iterdir()) == []


def test_anchor_source_mismatch_is_preflight_rejected(env, capsys):
    bad_anchor = env["root"] / "bad-anchor.json"
    bad_anchor.write_text(
        json.dumps(
            {
                "source_sha": "0" * 40,
                "release_id": "synthetic-release-1",
                "deployment_root": str(env["root"]),
            }
        ),
        encoding="utf-8",
    )
    data = _plan_data(env)
    data["deploy_anchor"]["path"] = str(bad_anchor)
    plan, sha = _write_plan(env, data)
    code, result, _ = _run_consume(capsys, plan, sha)
    assert code != runner.EXIT_OK
    assert result["code"] == "deploy_anchor_source_mismatch"
    assert _created_rows(env["master"]) == []
    assert json.loads(_claim_file(env).read_text(encoding="utf-8"))["state"] == "preflight-rejected"


def test_fence_busy_refuses_without_grant(env, capsys):
    holder = _hold_exclusive(env["fence"])
    try:
        plan, sha = _write_plan(env, _plan_data(env))
        code, result, _ = _run_consume(capsys, plan, sha)
    finally:
        _stop(holder)
    assert code != runner.EXIT_OK
    assert result["code"] == "fence_busy"
    assert _created_rows(env["master"]) == []
    assert json.loads(_claim_file(env).read_text(encoding="utf-8"))["state"] == "preflight-rejected"


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="non-Linux platform guard")
def test_production_identity_is_refused_on_non_linux(env, capsys):
    data = _plan_data(env)
    data["identity"] = "production"
    plan, sha = _write_plan(env, data)
    code, result, _ = _run_consume(capsys, plan, sha)
    assert code != runner.EXIT_OK
    assert result["code"] == "unsupported_platform"
    assert _created_rows(env["master"]) == []


# ---------------------------------------------------------------------------
# Timeout / revocation failure
# ---------------------------------------------------------------------------


def test_timeout_kills_owned_child_and_revokes_identity(env, capsys, monkeypatch):
    slow = env["root"] / "slow-python"
    slow.write_text("#!/bin/sh\nsleep 120\n", encoding="utf-8")
    slow.chmod(0o755)
    data = _plan_data(env)
    data["source"]["python"] = str(slow)
    data["budget_seconds"] = 1
    plan, sha = _write_plan(env, data)
    monkeypatch.setattr(
        runner,
        "_verify_source",
        lambda plan: {"git_sha": plan.source.git_sha, "repository": str(plan.source.repository)},
    )

    code, result, _ = _run_consume(capsys, plan, sha)

    assert code == runner.EXIT_TRANSPORT
    assert result["status"] == "failed"
    assert result["code"] == "mcp_timeout"
    assert result["child"]["pid"] is not None
    assert result["child"]["timed_out"] is True
    assert result["child"]["waited"] is True
    assert result["cleanup"]["token_revoked"] is True
    assert result["claim_state"] == "reconcile_required"
    assert result.get("item") is None
    with pytest.raises(ProcessLookupError):
        os.kill(result["child"]["pid"], 0)
    created = _created_rows(env["master"])
    assert len(created) == 1
    assert created[0]["revoked_at"] is not None


def test_revocation_failure_reports_reconcile_and_retains_barrier(env, capsys, monkeypatch):
    plan, sha = _write_plan(env, _plan_data(env))
    monkeypatch.setattr(runner, "_revoke_identity", lambda plan, token_id: False)

    code, result, _ = _run_consume(capsys, plan, sha)

    assert code == runner.EXIT_RECONCILE
    assert result["status"] == "reconcile_required"
    assert result["code"] == "cleanup_incomplete"
    assert result["cleanup"]["token_revoked"] is False
    assert result.get("item") is None
    assert result["claim_state"] == "reconcile_required"
    created = _created_rows(env["master"])
    assert len(created) == 1
    assert created[0]["revoked_at"] is None

    monkeypatch.undo()
    second_code, second, _ = _run_consume(capsys, plan, sha)
    assert second_code == runner.EXIT_CLAIM
    assert second["status"] == "replay_rejected"
    assert len(_created_rows(env["master"])) == 1


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


def test_output_never_exposes_raw_token_or_hash(env, capsys, monkeypatch):
    import secrets as real_secrets
    import types

    canary = "CANARY-" + "x" * 36
    fake = types.SimpleNamespace(
        token_urlsafe=lambda n: canary,
        token_hex=real_secrets.token_hex,
    )
    monkeypatch.setattr(runner, "secrets", fake)
    plan, sha = _write_plan(env, _plan_data(env))

    code, result, captured = _run_consume(capsys, plan, sha)

    assert code == runner.EXIT_OK
    assert canary not in captured.out
    assert canary not in captured.err
    created = _created_rows(env["master"])
    assert len(created) == 1
    assert created[0]["token_hash"] not in captured.out
    assert canary.encode() not in env["master"].read_bytes()
    for path in env["root"].rglob("*"):
        if path.is_file():
            assert canary.encode() not in path.read_bytes(), path
    assert "token_hash" not in json.dumps(result)


# ---------------------------------------------------------------------------
# Malicious plan inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("correlation", "bad\ncorrelation"),
        ("claim_dir", "/tmp/claims\nbad"),
    ],
)
def test_malicious_plan_fields_create_no_side_effects(env, capsys, field, value):
    data = _plan_data(env)
    data[field] = value
    plan, sha = _write_plan(env, data)
    code, result, _ = _run_consume(capsys, plan, sha)
    assert code != runner.EXIT_OK
    assert result["status"] == "failed"
    assert _created_rows(env["master"]) == []
    assert list(env["claims"].iterdir()) == []


# ---------------------------------------------------------------------------
# Linux /proc binding (CI only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc only")
def test_linux_proc_binding_matches_real_holder(env):
    plan, sha = _write_plan(env, _plan_data(env))
    loaded = runner.load_plan(str(plan), expected_sha256=sha, expected_correlation=CORRELATION)
    evidence = runner._verify_active_business(loaded)
    assert evidence["kind"] == "linux-proc"
    assert evidence["pid"] == env["holder"].pid
    assert evidence["master_fd_bound"] is True


# ---------------------------------------------------------------------------
# Relay: hermetic fake ssh injected through PATH
# ---------------------------------------------------------------------------


def _public_key() -> tuple[str, str]:
    import base64
    import struct

    key_type = b"ssh-ed25519"
    key = bytes(range(32))
    blob = (
        struct.pack(">I", len(key_type))
        + key_type
        + struct.pack(">I", len(key))
        + key
    )
    encoded = base64.b64encode(blob).decode("ascii")
    public_key = f"ssh-ed25519 {encoded} synthetic"
    return public_key, runner._fingerprint(public_key)


def _write_fake_ssh(
    env,
    *,
    mode: str = "ok",
    g_mode: str = "ok",
    g_host: str = "203.0.113.9",
    g_port: int = 2222,
):
    bin_dir = env["root"] / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = env["root"] / "ssh.log"
    result = env["root"] / "remote.json"
    script = bin_dir / "fake_ssh.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json, os, sys, time
            args = sys.argv[1:]
            with open({str(log)!r}, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(args) + "\\n")
            if "-G" in args:
                if {g_mode!r} == "fail":
                    sys.exit(255)
                print("hostname " + {g_host!r})
                print("port " + str({g_port!r}))
                print("user synthetic")
                sys.exit(0)
            mode = {mode!r}
            if mode == "denied":
                sys.stderr.write("Permission denied (publickey).\\n")
                sys.exit(255)
            if mode == "hostkey":
                sys.stderr.write("Host key verification failed.\\n")
                sys.exit(255)
            if mode == "hang":
                time.sleep(30)
                sys.exit(0)
            with open({str(result)!r}, "rb") as handle:
                sys.stdout.buffer.write(handle.read())
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    ssh = bin_dir / "ssh"
    ssh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    ssh.chmod(0o755)
    return bin_dir, log, result


def _relay_data(env) -> dict:
    public_key, fingerprint = _public_key()
    plan_sha = "a" * 64
    return {
        "schema": runner.RELAY_SCHEMA,
        "version": 1,
        "correlation": CORRELATION,
        "plan_sha256": plan_sha,
        "host": {
            "alias": runner.RELAY_HOST_ALIAS,
            "hostname": "203.0.113.9",
            "port": 2222,
            "public_key": public_key,
            "fingerprint": fingerprint,
        },
        "remote": {
            "python": "/usr/bin/python3",
            "repository": "/srv/dailycheck",
            "plan_path": "/srv/plans/plan.json",
            "module": runner.RELAY_MODULE,
            "correlation": CORRELATION,
            "plan_sha256": plan_sha,
        },
        "timeout_seconds": 30,
    }


def _write_request(env, data: dict) -> tuple[Path, str]:
    path = env["root"] / "relay-request.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, 0o600)
    return path, _sha_file(path)


def _remote_success(request: dict) -> dict:
    return {
        "schema": runner.RESULT_SCHEMA,
        "version": 1,
        "status": "succeeded",
        "correlation": request["correlation"],
        "plan_sha256": request["plan_sha256"],
        "item": {"id": 1, "sku": "SKU1", "name": "Synthetic item", "unit_cost": 12.5},
        "cleanup": {
            "token_revoked": True,
            "child_waited": True,
            "policy_removed": True,
            "fence_released": True,
        },
        "token": {"revoked": True, "revocation_verified": True},
        "mcp": {"server_returncode": 0, "child_waited": True},
    }


def _run_relay(capsys, request_path: Path, sha: str, correlation: str = CORRELATION):
    code = runner.main(
        [
            "relay",
            "--request",
            str(request_path),
            "--request-sha256",
            sha,
            "--correlation",
            correlation,
        ]
    )
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, captured.out
    return code, json.loads(lines[0]), captured


def _ssh_invocations(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def test_relay_success_pins_hostkey_and_cleans_temp(env, capsys, monkeypatch):
    import tempfile

    bin_dir, log, result_file = _write_fake_ssh(env)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    data = _relay_data(env)
    result_file.write_text(json.dumps(_remote_success(data)), encoding="utf-8")
    request_path, sha = _write_request(env, data)
    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(runner.tempfile, "mkdtemp", recording_mkdtemp)

    code, payload, captured = _run_relay(capsys, request_path, sha)

    assert code == runner.EXIT_OK, payload
    assert payload["status"] == "succeeded"
    assert payload["item"]["id"] == 1
    assert captured.err == ""
    assert created and not Path(created[0]).exists()
    calls = _ssh_invocations(log)
    assert len(calls) == 2
    main = next(call for call in calls if "-G" not in call)
    assert "StrictHostKeyChecking=yes" in main
    assert "BatchMode=yes" in main
    assert "UpdateHostKeys=no" in main
    assert "GlobalKnownHostsFile=/dev/null" in main
    assert any(arg.startswith("UserKnownHostsFile=") for arg in main)
    known_hosts = next(arg.split("=", 1)[1] for arg in main if arg.startswith("UserKnownHostsFile="))
    assert "dailycheck-read-once-relay-" in known_hosts
    assert "-p" in main and main[main.index("-p") + 1] == "2222"
    assert runner.RELAY_HOST_ALIAS in main
    assert "-A" not in main
    assert "-L" not in main
    assert "-R" not in main
    assert "ControlMaster=yes" not in main
    remote_command = main[-1]
    assert "consume" in remote_command
    assert "/srv/plans/plan.json" in remote_command
    assert "exec" in remote_command


def test_relay_missing_or_false_trusted_key_fails_before_ssh(env, capsys, monkeypatch):
    bin_dir, log, result_file = _write_fake_ssh(env)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    for mutation, expected in (
        (lambda data: data["host"].pop("public_key"), "host_keys_invalid"),
        (lambda data: data["host"].update(fingerprint="SHA256:" + "A" * 43), "host.fingerprint_mismatch"),
    ):
        data = _relay_data(env)
        mutation(data)
        request_path, sha = _write_request(env, data)
        code, payload, _ = _run_relay(capsys, request_path, sha)
        assert code != runner.EXIT_OK
        assert payload["code"] == expected
        assert _ssh_invocations(log) == []


def test_relay_alias_destination_mismatch_has_no_fallback(env, capsys, monkeypatch):
    bin_dir, log, result_file = _write_fake_ssh(env, g_host="198.51.100.7")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    data = _relay_data(env)
    request_path, sha = _write_request(env, data)

    code, payload, _ = _run_relay(capsys, request_path, sha)

    assert code == runner.EXIT_RELAY
    assert payload["code"] == "ssh_destination_mismatch"
    calls = _ssh_invocations(log)
    assert len(calls) == 1
    assert "-G" in calls[0]


@pytest.mark.parametrize(
    "mode, expected",
    [("denied", "ssh_failed"), ("hostkey", "ssh_hostkey_rejected")],
)
def test_relay_ssh_rejection_fails_closed(env, capsys, monkeypatch, mode, expected):
    bin_dir, log, result_file = _write_fake_ssh(env, mode=mode)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    data = _relay_data(env)
    request_path, sha = _write_request(env, data)

    code, payload, _ = _run_relay(capsys, request_path, sha)

    assert code == runner.EXIT_RELAY
    assert payload["code"] == expected
    assert "item" not in payload or payload["item"] is None
    calls = _ssh_invocations(log)
    assert len(calls) == 2
    assert "-G" in calls[0]
    main = calls[1]
    assert "StrictHostKeyChecking=yes" in main
    assert len([call for call in calls if "-G" not in call]) == 1


def test_relay_timeout_is_unknown_without_replay(env, capsys, monkeypatch):
    bin_dir, log, result_file = _write_fake_ssh(env, mode="hang")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    data = _relay_data(env)
    data["timeout_seconds"] = 1
    request_path, sha = _write_request(env, data)

    code, payload, _ = _run_relay(capsys, request_path, sha)

    assert code == runner.EXIT_TRANSPORT
    assert payload["status"] == "unknown"
    assert payload["code"] == "transport_timeout"
    calls = _ssh_invocations(log)
    assert len(calls) == 2
    assert len([call for call in calls if "-G" not in call]) == 1


def test_relay_remote_reconcile_required_is_nonzero(env, capsys, monkeypatch):
    bin_dir, log, result_file = _write_fake_ssh(env)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    data = _relay_data(env)
    remote = {
        "schema": runner.RESULT_SCHEMA,
        "version": 1,
        "status": "reconcile_required",
        "correlation": data["correlation"],
        "plan_sha256": data["plan_sha256"],
    }
    result_file.write_text(json.dumps(remote), encoding="utf-8")
    request_path, sha = _write_request(env, data)

    code, payload, _ = _run_relay(capsys, request_path, sha)

    assert code == runner.EXIT_RECONCILE
    assert payload["status"] == "reconcile_required"
    assert len(_ssh_invocations(log)) == 2


def test_relay_malicious_remote_path_has_no_side_effect(env, capsys, monkeypatch, tmp_path):
    bin_dir, log, result_file = _write_fake_ssh(env)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    target = tmp_path / "pwned"
    data = _relay_data(env)
    data["remote"]["plan_path"] = f"/srv/plan.json; touch {target}"
    request_path, sha = _write_request(env, data)

    code, payload, _ = _run_relay(capsys, request_path, sha)

    assert code != runner.EXIT_OK
    assert payload["code"] == "remote.plan_path_invalid"
    assert not target.exists()
    assert _ssh_invocations(log) == []


def test_relay_request_hash_and_correlation_are_pinned(env, capsys, monkeypatch):
    bin_dir, log, result_file = _write_fake_ssh(env)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    data = _relay_data(env)
    request_path, sha = _write_request(env, data)

    code, payload, _ = _run_relay(capsys, request_path, "b" * 64)
    assert code != runner.EXIT_OK
    assert payload["code"] == "request_hash_mismatch"
    assert _ssh_invocations(log) == []

    code, payload, _ = _run_relay(capsys, request_path, sha, "other-correlation")
    assert code != runner.EXIT_OK
    assert payload["code"] == "request_correlation_mismatch"
    assert _ssh_invocations(log) == []
