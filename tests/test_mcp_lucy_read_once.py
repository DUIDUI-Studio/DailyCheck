"""Issue #16: the finite Lucy read runner (local, synthetic fixtures only).

These tests never touch the network, production, existing SSH config or any
real credential.  The complete consume lifecycle uses a real SQLite master and
warehouse, a real holder process for the active-business binding, and a real
MCP stdio child through the public SDK.  The relay tests inject a hermetic fake
``ssh`` executable through PATH.
"""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import py_compile
import shutil
import sqlite3
import struct
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


@pytest.fixture(scope="session")
def source_snapshot(tmp_path_factory):
    """Commit a task-owned candidate snapshot; never stage source for a real consume."""
    root = tmp_path_factory.mktemp("read-once-source").resolve()
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO_ROOT).split(b"\0")
    for name in files:
        if not name:
            continue
        relative = os.fsdecode(name)
        source = REPO_ROOT / relative
        if source.is_file():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for args in (["init", "-q"], ["add", "."],
                 ["-c", "user.name=Synthetic", "-c", "user.email=synthetic@invalid",
                  "commit", "-qm", "synthetic source snapshot"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root


def _producer(env, *args):
    result = subprocess.run(
        [sys.executable, "-B", str(REPO_ROOT / "scripts/blue_green/deploy_state.py"), *args],
        env={**runner._tool_env(), "DAILYCHECK_DEPLOY_STATE_DIR": str(env["state_dir"])},
        capture_output=True, text=True, timeout=15,
    )
    return result


@pytest.fixture
def env(tmp_path, source_snapshot):
    root = tmp_path.resolve()
    warehouses = root / "warehouses"
    warehouses.mkdir()
    warehouse_file = warehouses / f"{CODE}.db"
    _build_warehouse(warehouse_file)
    master = root / "master.db"
    _build_master(master, warehouse_file)
    # Data, executable source and deployment state are three independent roots.
    state_dir = root / "separate-deployer-state"
    producer_env = {"state_dir": state_dir}
    source_sha = subprocess.check_output(
        ["git", "-C", str(source_snapshot), "rev-parse", "HEAD"], text=True,
    ).strip()
    assert _producer(producer_env, "acquire", "--pr", "17", "--sha", source_sha).returncode == 0
    assert _producer(producer_env, "release", "--result", "success", "--sha", source_sha).returncode == 0
    fence = state_dir / ".attempt.lock"
    anchor = state_dir / "state.json"
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
        "state_dir": state_dir,
        "source": source_snapshot,
        "source_sha": source_sha,
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
            "repository": str(env["source"]),
            "git_sha": env["source_sha"],
            "python": sys.executable,
            "module": "mcp_server",
        },
        "deploy_anchor": {"state_dir": str(env["state_dir"]), "source_sha": env["source_sha"]},
        "database": {
            "master_path": str(env["master"]),
            "warehouse_root": str(env["warehouses"]),
            "warehouse_code": CODE,
            "warehouse_file": str(env["warehouse_file"]),
        },
        "active_business": {"pid": env["holder"].pid, "start_time": env["holder_start"]},
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
    assert result["mcp"]["server_start_time"]
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
    state = json.loads(env["anchor"].read_text())
    state["deployed"]["sha"] = "0" * 40
    env["anchor"].write_text(json.dumps(state))
    data = _plan_data(env)
    plan, sha = _write_plan(env, data)
    code, result, _ = _run_consume(capsys, plan, sha)
    assert code != runner.EXIT_OK
    assert result["code"] == "deploy_state_invalid_or_unsettled"
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


# Repair 3: actual deployer compatibility and catchable-cancellation custody.

def _load(env):
    path, sha = _write_plan(env, _plan_data(env))
    return runner.load_plan(str(path), expected_sha256=sha, expected_correlation=CORRELATION)


def _contend(env):
    # This is the unchanged producer's real exclusive flock, not a fake lock.
    return _producer(env, "run", "--", sys.executable, "-B", "-c", "pass")


def test_real_producer_state_and_fence_have_independent_roots(env, capsys, monkeypatch):
    state_before = env["anchor"].read_bytes()
    inode_before = env["fence"].stat().st_ino
    events = []
    original_revoke = runner._revoke_identity
    original_finalize = runner._finalize_child
    original_remove = runner._remove_policy_dir
    original_release = runner._release_fence

    def blocked(phase):
        assert _contend(env).returncode == 10, phase
        assert env["fence"].stat().st_ino == inode_before
        events.append(phase)

    def revoke(plan, token_id):
        blocked("revoke")
        return original_revoke(plan, token_id)

    async def finalize(process, child):
        blocked("child_wait")
        assert _created_rows(env["master"])[0]["revoked_at"]
        await original_finalize(process, child)
        assert child.waited

    def remove(directory):
        blocked("temp")
        assert _created_rows(env["master"])[0]["revoked_at"]
        assert directory.exists()
        return original_remove(directory)

    def release(fd):
        blocked("release_last")
        return original_release(fd)

    monkeypatch.setattr(runner, "_revoke_identity", revoke)
    monkeypatch.setattr(runner, "_finalize_child", finalize)
    monkeypatch.setattr(runner, "_remove_policy_dir", remove)
    monkeypatch.setattr(runner, "_release_fence", release)
    path, sha = _write_plan(env, _plan_data(env))
    code, result, captured = _run_consume(capsys, path, sha)
    assert code == 0, result
    assert events == ["revoke", "child_wait", "temp", "release_last"]
    assert _contend(env).returncode == 0
    assert env["anchor"].read_bytes() == state_before
    assert "attempt_token" not in captured.out and "token_hash" not in captured.out
    assert result["anchor"]["release_id"] == "17"
    assert result["anchor"]["path"] == str(env["anchor"])


def _produce_outcome(env):
    # Run under producer-owned inherited descriptor and its exact attempt identity.
    code = '''
import json, os
from argparse import Namespace
from scripts.blue_green import deploy_state as d
assert d.main(["acquire", "--pr", "17", "--sha", SHA,
               "--deployment-provider", "github", "--deployment-host", "github.com",
               "--deployment-repository", "DUIDUI-Studio/DailyCheck"]) == 0
lock = json.loads((d._state_dir() / "state.json").read_text())["lock"]
os.environ[d.ATTEMPT_ENV] = json.dumps({k: lock[k] for k in ("token", "pr", "sha", "session")})
assert d.main(["release", "--result", "success", "--sha", SHA]) == 0
'''.replace('SHA', repr(env['source_sha']))
    result = subprocess.run(
        [sys.executable, "-B", str(REPO_ROOT / "scripts/blue_green/deploy_state.py"),
         "run", "--", sys.executable, "-B", "-c", code],
        env={**runner._tool_env(), "PYTHONPATH": str(REPO_ROOT),
             "DAILYCHECK_DEPLOY_STATE_DIR": str(env["state_dir"])},
        capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_real_producer_owned_outcome_is_validated_and_redacted(env, capsys):
    _produce_outcome(env)
    before = env["anchor"].read_bytes()
    state = json.loads(before)
    outcome_id = state["deployed"]["outcome_id"]
    outcome = state["outcomes"][outcome_id]
    path, sha = _write_plan(env, _plan_data(env))
    code, result, captured = _run_consume(capsys, path, sha)
    assert code == 0, result
    assert result["anchor"]["outcome_id"] == outcome_id
    assert result["anchor"]["generation"] == state["generation"]
    assert outcome["attempt_token"] not in captured.out + captured.err
    assert env["anchor"].read_bytes() == before


@pytest.mark.parametrize("mutation", [
    "sha", "pr", "result", "generation", "reference", "deployment", "version",
    "lease", "last_attempt", "native", "substitute", "duplicate", "missing",
])
def test_real_state_malformed_mismatched_or_unsettled_refuses(env, capsys, mutation):
    _produce_outcome(env)
    state = json.loads(env["anchor"].read_bytes())
    outcome = state["outcomes"][state["deployed"]["outcome_id"]]
    if mutation == "sha":
        outcome["sha"] = "0" * 40
    elif mutation == "pr":
        outcome["pr"] += 1
    elif mutation == "result":
        outcome["result"] = "failed"
    elif mutation == "generation":
        state["generation"] += 1
    elif mutation == "reference":
        state["deployed"]["outcome_id"] = "absent"
    elif mutation == "deployment":
        outcome["deployment"]["pr"] += 1
    elif mutation == "version":
        state["version"] = True
    elif mutation == "lease":
        state["lock"] = {"token": "SECRET-LEASE-CANARY"}
    elif mutation == "last_attempt":
        state["last_attempt"] = {"result": "failed"}
    elif mutation == "native":
        state["native_queue"] = {"active": None}
    elif mutation == "substitute":
        state = {"source_sha": env["source_sha"], "release_id": "17", "deployment_root": "/"}
    elif mutation == "missing":
        env["anchor"].unlink()
    if mutation != "missing":
        raw = json.dumps(state)
        if mutation == "duplicate":
            raw = raw[:-1] + ', "version":1}'
        env["anchor"].write_text(raw)
    path, sha = _write_plan(env, _plan_data(env))
    code, result, captured = _run_consume(capsys, path, sha)
    assert code != 0 and result["status"] != "succeeded"
    assert _created_rows(env["master"]) == []
    assert "SECRET-LEASE-CANARY" not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.parametrize("kind", ["missing", "symlink", "public", "directory", "inode", "owner"])
def test_existing_fence_identity_guards(env, capsys, monkeypatch, kind):
    fence = env["fence"]
    if kind == "missing":
        fence.unlink()
    elif kind == "symlink":
        original = fence.with_name("old-lock")
        fence.rename(original)
        fence.symlink_to(original)
    elif kind == "public":
        fence.chmod(0o644)
    elif kind == "directory":
        fence.unlink()
        fence.mkdir()
    else:
        original_open = runner.os.open
        def changed_open(path, flags, *args, **kwargs):
            if Path(path) == fence and kind == "inode":
                replacement = fence.with_name("replacement")
                replacement.write_bytes(b"")
                replacement.chmod(0o600)
                replacement.replace(fence)
            fd = original_open(path, flags, *args, **kwargs)
            if Path(path) == fence and kind == "owner":
                real_fstat = runner.os.fstat
                def alien_fstat(target):
                    info = real_fstat(target)
                    if target == fd:
                        import types
                        return types.SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid()+1,
                                                     st_dev=info.st_dev, st_ino=info.st_ino)
                    return info
                monkeypatch.setattr(runner.os, "fstat", alien_fstat)
            return fd
        monkeypatch.setattr(runner.os, "open", changed_open)
    path, sha = _write_plan(env, _plan_data(env))
    code, result, _ = _run_consume(capsys, path, sha)
    assert code != 0, result
    assert _created_rows(env["master"]) == []
    if kind == "missing":
        assert not fence.exists()  # consumer never creates the real producer lock


@pytest.mark.parametrize("kind", ["blueprint", "runner", "untracked", "ignored", "assume_unchanged"])
def test_executable_source_drift_refuses_before_import_or_grant(env, capsys, kind):
    source = env["source"]
    relative = "scripts/mcp_lucy_read_once.py" if kind == "runner" else "blueprints/consumption.py"
    if kind == "untracked":
        relative = "blueprints/untracked_canary.py"
    elif kind == "ignored":
        relative = "db/warehouses/ignored_canary.py"
    path = source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.read_bytes() if path.exists() else None
    marker = env["root"] / "source-canary"
    # Real imported module-scope side effect; red evidence proves the old child executes it.
    canary = f'\nfrom pathlib import Path as _CanaryPath\n_CanaryPath({str(marker)!r}).write_text("harmless")\n'.encode()
    ignore_path = source / ".git/info/exclude"
    ignore_before = ignore_path.read_bytes()
    try:
        if kind == "ignored":
            ignore_path.write_bytes(ignore_before + b"\n/db/warehouses/ignored_canary.py\n")
        if kind == "assume_unchanged":
            subprocess.run(["git", "-C", str(source), "update-index", "--assume-unchanged", relative], check=True)
        path.write_bytes((before or b"") + canary)
        plan_path, sha = _write_plan(env, _plan_data(env))
        code, result, _ = _run_consume(capsys, plan_path, sha)
        assert code != 0 and result["code"] == "source_tree_dirty", result
        assert not marker.exists()
        assert _created_rows(env["master"]) == []
    finally:
        if before is None:
            path.unlink()
        else:
            path.write_bytes(before)
            assert path.read_bytes() == before
        ignore_path.write_bytes(ignore_before)
        if kind == "assume_unchanged":
            subprocess.run(["git", "-C", str(source), "update-index", "--no-assume-unchanged", relative], check=True)
    assert subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"]) == b""


@pytest.fixture
def admission_env(env):
    """Every hostile layout has its own checkout, never a shared-source mutation."""
    isolated = env["root"] / "admission-source"
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(env["source"]), str(isolated)],
                   check=True, capture_output=True)
    env["source"] = isolated
    return env


def _admission_git(env, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@invalid",
         "-C", str(env["source"]), *args], stderr=subprocess.PIPE,
    )


def _repin_admission(env):
    _admission_git(env, "commit", "-qm", "synthetic executable layout")
    env["source_sha"] = _admission_git(env, "rev-parse", "HEAD").decode().strip()
    assert _producer(env, "acquire", "--pr", "17", "--sha", env["source_sha"]).returncode == 0
    assert _producer(env, "release", "--result", "success", "--sha", env["source_sha"]).returncode == 0


def _plant_admission_shape(env, kind, marker):
    source = env["source"]
    canary = f"\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    if kind in {"nested_git", "ignored_nested_git", "nested_gitfile", "directory_link", "tracked_gitlink"}:
        import anyio
        package = source / "anyio"
        shutil.copytree(Path(anyio.__file__).parent, package,
                        ignore=shutil.ignore_patterns("__pycache__"))
        with (package / "__init__.py").open("a") as stream:
            stream.write(canary)
        if kind == "directory_link":
            target = env["root"] / "external-anyio"
            package.rename(target)
            package.symlink_to(target, target_is_directory=True)
        else:
            options = ["--separate-git-dir", str(env["root"] / "nested-git")] if kind == "nested_gitfile" else []
            subprocess.run(["git", "init", "-q", *options, str(package)],
                           check=True, capture_output=True)
            if kind == "ignored_nested_git":
                with (source / ".git/info/exclude").open("a") as stream:
                    stream.write("\n/anyio/\n")
            if kind == "tracked_gitlink":
                _admission_git(env, "update-index", "--add", "--cacheinfo",
                               f"160000,{env['source_sha']},anyio")
                _repin_admission(env)
    elif kind in {"source_less", "timestamp_cache", "unchecked_cache"}:
        main = source / "mcp_server/main.py"
        original = main.read_text()
        payload = env["root"] / "bytecode-input.py"
        payload.write_text(canary if kind == "source_less" else original + canary)
        target = (source / "anyio.pyc" if kind == "source_less"
                  else Path(importlib.util.cache_from_source(str(main))))
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = (py_compile.PycInvalidationMode.UNCHECKED_HASH if kind == "unchecked_cache"
                else py_compile.PycInvalidationMode.TIMESTAMP)
        py_compile.compile(str(payload), cfile=str(target), doraise=True, invalidation_mode=mode)
        if kind == "timestamp_cache":
            cached = target.read_bytes()
            target.write_bytes(cached[:8] + struct.pack("<II", int(main.stat().st_mtime),
                                                       main.stat().st_size) + cached[16:])
    elif kind == "ancestor_link":
        package = source / "mcp_server"
        target = env["root"] / "external-mcp-server"
        package.rename(target)
        package.symlink_to(target, target_is_directory=True)
    elif kind == "tracked_link":
        (source / "source-link").symlink_to("mcp_server")
        _admission_git(env, "add", "source-link")
        _repin_admission(env)
    elif kind in {"dependency_link", "link_into_dependency"}:
        dependency = source / ".venv"
        if kind == "dependency_link":
            dependency.symlink_to(Path(sys.prefix), target_is_directory=True)
        else:
            dependency.mkdir()
            (source / "anyio").symlink_to(Path(sys.prefix), target_is_directory=True)
    elif kind == "native_extension":
        import markupsafe._speedups as native
        suffix = next(s for s in importlib.machinery.EXTENSION_SUFFIXES
                      if native.__file__.endswith(s))
        shutil.copyfile(native.__file__, source / ("anyio" + suffix))
    else:
        relative = {"ordinary": "extra.py", "ignored": "db/warehouses/extra.py",
                    "nested_fake_dependency": "pkg/.venv/shadow.py",
                    "fake_top_dependency": ".venv/shadow.py",
                    "fake_node_dependency": "node_modules/shadow.py"}[kind]
        path = source / relative
        if kind == "ignored":
            with (source / ".git/info/exclude").open("a") as stream:
                stream.write(f"\n/{relative}\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canary)


@pytest.mark.parametrize("kind", [
    "ordinary", "ignored", "nested_git", "ignored_nested_git", "nested_gitfile", "directory_link",
    "tracked_gitlink", "tracked_link", "ancestor_link", "source_less", "timestamp_cache",
    "unchecked_cache", "native_extension", "nested_fake_dependency", "fake_top_dependency",
    "fake_node_dependency", "dependency_link", "link_into_dependency",
])
def test_source_loading_forms_refuse_before_probe_grant_and_replay(admission_env, capsys,
                                                                  monkeypatch, kind):
    env = admission_env
    marker = env["root"] / "admission-canary"
    _plant_admission_shape(env, kind, marker)
    probes, grants, reads = [], [], []
    checked, issue, run_mcp = runner._run_checked, runner._issue_identity, runner._run_mcp

    def observe(argv, **kwargs):
        if "-c" in argv and "import json, mcp_server.main" in argv[-1]:
            probes.append(argv)
        return checked(argv, **kwargs)

    def issue_observer(*args):
        grants.append(True)
        return issue(*args)

    def read_observer(*args):
        reads.append(True)
        return run_mcp(*args)

    monkeypatch.setattr(runner, "_run_checked", observe)
    monkeypatch.setattr(runner, "_issue_identity", issue_observer)
    monkeypatch.setattr(runner, "_run_mcp", read_observer)
    warehouse_before = _sha_file(env["warehouse_file"])
    path, sha = _write_plan(env, _plan_data(env))
    code, result, _ = _run_consume(capsys, path, sha)
    assert probes == [], "application import subprocess started before source refusal"
    assert code == runner.EXIT_PREFLIGHT and result["stage"] == "source", result
    assert result["code"] == "source_tree_dirty", result
    assert not marker.exists()
    assert grants == reads == []
    assert _created_rows(env["master"]) == []
    second_code, second, _ = _run_consume(capsys, path, sha)
    assert second_code == runner.EXIT_CLAIM and second["code"] == "already_claimed"
    assert probes == grants == reads == []
    assert _created_rows(env["master"]) == []
    assert _sha_file(env["warehouse_file"]) == warehouse_before


def test_clean_installed_dependencies_and_runtime_files_are_usable(admission_env, capsys):
    env = admission_env
    source = env["source"]
    runtime_files = [source / "db/warehouses/runtime.db", source / "logs/access.log",
                     source / "node_modules/package/index.js"]
    for path in runtime_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ordinary ignored runtime bytes\x00\xff")
    before = {path: path.read_bytes() for path in runtime_files}
    for path in runtime_files:
        assert _admission_git(env, "check-ignore", str(path)).strip()
    path, sha = _write_plan(env, _plan_data(env))
    code, result, _ = _run_consume(capsys, path, sha)
    assert code == runner.EXIT_OK, result
    assert result["mcp"]["child_waited"] and result["mcp"]["server_returncode"] == 0
    assert [call["tool"] for call in result["mcp"]["calls"]] == ["warehouse_list", "items_detail"]
    assert result["item"]["id"] == 1 and result["token"]["revocation_verified"]
    assert all(result["cleanup"].values())
    rows = _created_rows(env["master"])
    assert len(rows) == 1 and rows[0]["revoked_at"]
    assert {path: path.read_bytes() for path in runtime_files} == before


@pytest.mark.parametrize("dependency_root", [".venv", "node_modules"])
def test_selected_installed_interpreter_root_is_trusted(admission_env, dependency_root):
    env = admission_env
    interpreter_root = env["source"] / dependency_root
    # A real venv, selected explicitly; a fake directory basename never suffices.
    subprocess.run([sys.executable, "-B", "-m", "venv", "--without-pip",
                    "--system-site-packages", str(interpreter_root)], check=True,
                   capture_output=True, timeout=30)
    data = _plan_data(env)
    data["source"]["python"] = str(interpreter_root / "bin/python")
    path, sha = _write_plan(env, data)
    # Use the public plan loading path, including the selected interpreter.
    plan = runner.load_plan(str(path), expected_sha256=sha, expected_correlation=CORRELATION)
    evidence = runner._verify_source(plan)
    assert evidence["import_returncode"] == 0
    # Metadata admission itself must not execute installed .pth startup code
    # before rejecting application drift (site sets venv prefixes on <=3.13).
    marker = env["root"] / "site-startup-canary"
    site_packages = next(interpreter_root.glob("lib/python*/site-packages"))
    (site_packages / "admission-canary.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n"
    )
    (env["source"] / "unverified.py").write_text("pass\n")
    with pytest.raises(runner.ReadOnceError, match="source_tree_dirty"):
        runner._verify_source(plan)
    assert not marker.exists(), "interpreter metadata query executed site startup before admission"


@pytest.mark.parametrize("window", ["policy", "ledger", "commit_return", "sdk", "cancel", "unexpected"])
def test_catchable_cancellation_revokes_settles_and_releases_last(env, capsys, monkeypatch, window):
    from mcp import ClientSession
    warehouse_before = env["warehouse_file"].read_bytes()
    existing_before = _token_rows(env["master"])[0]
    events = []
    directories = []
    original_revoke = runner._revoke_identity
    original_remove = runner._remove_policy_dir
    original_release = runner._release_fence
    original_finalize = runner._finalize_child
    exception_canary = "SECRET-INTERRUPTION-CANARY"

    def interrupt(*a, **kw):
        raise KeyboardInterrupt(exception_canary)

    if window == "policy":
        monkeypatch.setattr(runner, "_write_policy", interrupt)
    elif window == "ledger":
        original_update = runner.ClaimLedger.update
        def update(self, **changes):
            if changes.get("token_id"):
                raise OSError(exception_canary)
            return original_update(self, **changes)
        monkeypatch.setattr(runner.ClaimLedger, "update", update)
    elif window == "commit_return":
        original_issue = runner._issue_identity
        def issue(plan, token):
            original_issue(plan, token)
            interrupt()
        monkeypatch.setattr(runner, "_issue_identity", issue)
    else:
        async def read(self, name, arguments, *a, **kw):
            assert _contend(env).returncode == 10
            if window == "cancel":
                import asyncio
                raise asyncio.CancelledError(exception_canary)
            if window == "unexpected":
                raise RuntimeError(exception_canary)
            interrupt()
        monkeypatch.setattr(ClientSession, "call_tool", read)

    def revoke(plan, token_id):
        assert _contend(env).returncode == 10
        events.append("revoke")
        return original_revoke(plan, token_id)

    async def finalize(process, child):
        assert _created_rows(env["master"])[0]["revoked_at"]
        assert _contend(env).returncode == 10
        events.append("child")
        await original_finalize(process, child)
        assert child.waited

    def remove(directory):
        assert _created_rows(env["master"])[0]["revoked_at"]
        assert _contend(env).returncode == 10
        if directory:
            directories.append(directory)
        events.append("temp")
        return original_remove(directory)

    def release(fd):
        assert events[-1] == "temp"
        assert all(not directory.exists() for directory in directories)
        assert _contend(env).returncode == 10
        events.append("fence")
        return original_release(fd)

    monkeypatch.setattr(runner, "_revoke_identity", revoke)
    monkeypatch.setattr(runner, "_finalize_child", finalize)
    monkeypatch.setattr(runner, "_remove_policy_dir", remove)
    monkeypatch.setattr(runner, "_release_fence", release)
    path, sha = _write_plan(env, _plan_data(env))
    code, result, captured = _run_consume(capsys, path, sha)
    assert code != 0 and result["status"] != "succeeded", result
    assert result.get("item") is None
    assert all(result["cleanup"].values()), result
    assert exception_canary not in captured.out + captured.err
    assert _created_rows(env["master"])[0]["revoked_at"]
    assert len(_created_rows(env["master"])) == 1
    assert env["warehouse_file"].read_bytes() == warehouse_before
    assert _token_rows(env["master"])[0] == existing_before
    if window in {"sdk", "cancel", "unexpected"}:
        assert events == ["revoke", "child", "temp", "fence"]
        assert result["child"]["waited"]
        with pytest.raises(ProcessLookupError):
            os.kill(result["child"]["pid"], 0)
    else:
        assert events == ["revoke", "temp", "fence"]
    assert _contend(env).returncode == 0
    # Cleanup is not permission to replay, and no second grant/read occurs.
    monkeypatch.undo()
    again, replay, _ = _run_consume(capsys, path, sha)
    assert again == runner.EXIT_CLAIM and replay["status"] == "replay_rejected"
    assert len(_created_rows(env["master"])) == 1


def test_external_warehouse_symlink_remains_fail_closed(env, capsys):
    external = env["root"] / "external.db"
    env["warehouse_file"].rename(external)
    env["warehouse_file"].symlink_to(external)
    before = external.read_bytes()
    path, sha = _write_plan(env, _plan_data(env))
    code, result, _ = _run_consume(capsys, path, sha)
    assert code != 0 and result["status"] != "succeeded"
    assert _created_rows(env["master"]) == []
    assert external.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "writable", "missing"])
def test_state_directory_requires_trusted_explicit_identity(env, capsys, kind):
    data = _plan_data(env)
    if kind == "symlink":
        alias = env["root"] / "state-alias"
        alias.symlink_to(env["state_dir"], target_is_directory=True)
        data["deploy_anchor"]["state_dir"] = str(alias)
    elif kind == "writable":
        env["state_dir"].chmod(0o777)
    else:
        data["deploy_anchor"]["state_dir"] = str(env["root"] / "absent-state")
    path, sha = _write_plan(env, data)
    code, result, _ = _run_consume(capsys, path, sha)
    assert code != 0 and result["status"] != "succeeded"
    assert _created_rows(env["master"]) == []


def test_init_only_producer_state_is_not_successful_release(env, capsys):
    env["anchor"].unlink()
    assert _producer(env, "init", "--sha", env["source_sha"]).returncode == 0
    path, sha = _write_plan(env, _plan_data(env))
    code, result, _ = _run_consume(capsys, path, sha)
    assert code != 0 and result["code"] == "deploy_state_invalid_or_unsettled"
    assert _created_rows(env["master"]) == []


def test_actual_ledger_replace_failure_after_issue_still_cleans(env, capsys, monkeypatch):
    original_replace = runner.os.replace
    def refuse_claim_write(source, target):
        if Path(target) == _claim_file(env) and _created_rows(env["master"]):
            raise OSError("SECRET-LEDGER-WRITE-CANARY")
        return original_replace(source, target)
    monkeypatch.setattr(runner.os, "replace", refuse_claim_write)
    path, sha = _write_plan(env, _plan_data(env))
    code, result, captured = _run_consume(capsys, path, sha)
    assert code != 0 and result["status"] != "succeeded"
    assert all(result["cleanup"].values()), result
    assert _created_rows(env["master"])[0]["revoked_at"]
    assert list(env["claims"].iterdir()) == [_claim_file(env)]
    assert "SECRET-LEDGER-WRITE-CANARY" not in captured.out + captured.err
    assert _contend(env).returncode == 0
