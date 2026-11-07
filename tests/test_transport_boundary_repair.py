"""Raw boundary agreement and durable permanent-rejection progress through real entrypoints."""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TARGET = "DUIDUI-Studio/DailyCheck"
SHA = "a" * 40
SECRET = b"local-boundary-repair-fixture"


def deployment(pr=160, **extra):
    return {
        "delivery_id": f"delivery-{pr}",
        "event": "deploy_result",
        "action": "completed",
        "repo_full_name": "eric/dailycheck",
        "pr_number": pr,
        "pr_head_sha": SHA,
        "conclusion": "success",
        "deploy_rc": 0,
        "deploy_next": "closeout",
        **extra,
    }


@pytest.fixture
def rig(tmp_path, request):
    posts = []
    responses = [202]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            signature = "sha256=" + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
            posts.append(
                {
                    "raw": raw.decode("utf-8", errors="backslashreplace"),
                    "hmac_valid": hmac.compare_digest(
                        signature, self.headers.get("X-Hub-Signature-256", "")
                    ),
                    "path": self.path,
                }
            )
            status = responses.pop(0) if len(responses) > 1 else responses[0]
            self.send_response(status)
            if status == 429:
                self.send_header("Retry-After", "60")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    secret_file = tmp_path / "subscriptions.json"
    secret_file.write_text(
        json.dumps({r: {"secret": SECRET.decode()} for r in ("wdg-ci", "dailycheck-deploy")})
    )
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "GITEA_TOKEN": "local-unused-read-token",
        "DAILYCHECK_PYTHON": sys.executable,
        "DAILYCHECK_EVENT_REDUCER": str(ROOT / "scripts/prod_agent/event_reducer.py"),
        "DAILYCHECK_EVENT_REDUCER_DB": str(tmp_path / "state.sqlite3"),
        "DAILYCHECK_REDUCER_DEBOUNCE_MS": "0",
        "DAILYCHECK_HERMES_SENDER": str(ROOT / "scripts/prod_agent/hermes_webhook_sender.py"),
        "DAILYCHECK_HERMES_SUBSCRIPTIONS": str(secret_file),
        "DAILYCHECK_HERMES_WEBHOOK_URL": f"http://127.0.0.1:{server.server_port}",
        "TMPDIR": str(tmp_path),
    }
    env.pop("DAILYCHECK_HOSTING_PROVIDER", None)
    calls = []

    def run(args, raw=None):
        proc = subprocess.run(args, cwd=ROOT, env=env, input=raw, capture_output=True, timeout=30)
        calls.append(
            {
                "args": [str(x) for x in args],
                "exit": proc.returncode,
                "stdout": proc.stdout.decode(errors="backslashreplace"),
                "stderr": proc.stderr.decode(errors="backslashreplace"),
            }
        )
        return proc

    class Rig:
        db = tmp_path / "state.sqlite3"
        directory = tmp_path

        def sender(self, raw):
            body = tmp_path / "body.json"
            body.write_bytes(raw)
            return run(
                [
                    sys.executable,
                    env["DAILYCHECK_HERMES_SENDER"],
                    "--subscriptions",
                    env["DAILYCHECK_HERMES_SUBSCRIPTIONS"],
                    "--base-url",
                    env["DAILYCHECK_HERMES_WEBHOOK_URL"],
                    "--route",
                    "dailycheck-deploy",
                    "--event",
                    "deploy_result",
                    "--delivery-id",
                    "direct-fixture",
                    "--body-file",
                    str(body),
                ]
            )

        def guard(self, raw=b""):
            return run([sys.executable, env["DAILYCHECK_HERMES_SENDER"], "--production-guard"], raw)

        def adapter(self, raw=None):
            return run(
                [
                    "bash",
                    str(ROOT / "deploy/scripts/dc-notify-prod-agent.sh"),
                    *(["--drain"] if raw is None else []),
                ],
                raw,
            )

        def cli(self, command, raw=None, now=10000, *extra):
            return run(
                [
                    sys.executable,
                    env["DAILYCHECK_EVENT_REDUCER"],
                    "--db",
                    str(self.db),
                    "--now-ms",
                    str(now),
                    command,
                    *extra,
                ],
                raw,
            )

    obj = Rig()
    obj.posts, obj.responses, obj.env, obj.calls = posts, responses, env, calls
    obj.snapshots = []
    try:
        yield obj
    finally:
        dest = os.environ.get("DAILYCHECK_REPAIR_EVIDENCE")
        if dest:
            target = Path(dest)
            target.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
            observed = {
                "test": request.node.nodeid,
                "posts": posts,
                "calls": calls,
                "snapshots": obj.snapshots,
            }
            if obj.db.exists():
                with sqlite3.connect(obj.db) as conn:
                    conn.row_factory = sqlite3.Row
                    observed["tables"] = {
                        table: [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
                        for table in ("trigger_queue", "deliveries", "provider_state")
                    }
            (target / f"{name}.json").write_text(json.dumps(observed, indent=2))
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def seed(rig, rows):
    schema = re.search(
        r'SCHEMA = """(.*?)"""', (ROOT / "scripts/prod_agent/event_reducer.py").read_text(), re.S
    ).group(1)
    with sqlite3.connect(rig.db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(schema)
        for index, row in enumerate(rows):
            payload = row.get("payload", deployment(160 + index))
            raw = row.get("raw", json.dumps(payload))
            conn.execute(
                """INSERT INTO trigger_queue
              (trigger_id,event_key,repo,pr_number,head_sha,event,action,conclusion,route,priority,payload_json,state,first_seen_ms,due_at_ms,attempts,updated_at_ms)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"q-{index}",
                    row.get("key", f"key-{index}"),
                    row.get("repo", payload.get("repo_full_name", "eric/dailycheck")),
                    payload.get("pr_number", 160 + index),
                    SHA,
                    payload.get("event", "deploy_result"),
                    "completed",
                    "success",
                    "dailycheck-deploy",
                    30,
                    raw,
                    "pending",
                    index + 1,
                    1,
                    row.get("attempts", 2),
                    1,
                ),
            )
            conn.execute(
                "INSERT INTO deliveries VALUES (?,?,?,?,?)",
                (
                    payload["delivery_id"],
                    row.get("key", f"key-{index}"),
                    f"q-{index}",
                    1,
                    "deferred",
                ),
            )


def state(rig):
    with sqlite3.connect(rig.db) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in conn.execute("SELECT * FROM trigger_queue ORDER BY first_seen_ms,trigger_id")
        ]
        rig.snapshots.append(
            {
                "queue": rows,
                "deliveries": [dict(r) for r in conn.execute("SELECT * FROM deliveries")],
                "provider": [dict(r) for r in conn.execute("SELECT * FROM provider_state")],
            }
        )
        return rows


def test_original_mixed_queue_progress_without_cleanup(rig):
    rows = [{"repo": TARGET}, {"raw": '{"broken":'}, {}]
    seed(rig, rows)
    before = state(rig)
    for _ in range(3):
        rig.adapter()
    after = state(rig)
    assert len(rig.posts) == 1 and rig.posts[0]["hmac_valid"]
    assert [r["state"] for r in after] == ["done"] * 3
    assert [r["last_error"] for r in after[:2]] == ["shadow_rejected", "invalid_stored_payload"]
    for old, new in zip(before[:2], after[:2], strict=True):
        assert new["payload_json"] == old["payload_json"] and new["attempts"] == old["attempts"]
        assert new["completed_at_ms"] and new["updated_at_ms"] > old["updated_at_ms"]
        assert new["lease_token"] is None and new["lease_expires_ms"] is None
    with sqlite3.connect(rig.db) as conn:
        assert [
            r[0] for r in conn.execute("SELECT disposition FROM deliveries ORDER BY delivery_id")
        ][:2] == ["shadow_rejected", "invalid_stored_payload"]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"hosting_provider":"github",',
        json.dumps(deployment(repo_full_name=f" {TARGET} ")).encode(),
    ],
)
def test_original_direct_sender_rejects_invalid_or_padded_target_before_post(rig, raw):
    guard = rig.guard(raw)
    result = rig.sender(raw)
    assert guard.returncode != 0
    assert result.returncode != 0 and json.loads(result.stdout)["accepted"] is False
    assert not rig.posts


@pytest.mark.parametrize("alias", ["repository", "repo"])
def test_original_padded_conflicting_alias_through_actual_shell(rig, alias):
    supplied = (
        {"repository": {"full_name": f" {TARGET} "}}
        if alias == "repository"
        else {"repo": f" {TARGET} "}
    )
    raw = json.dumps(deployment(**supplied)).encode()
    rig.adapter(raw)
    assert not rig.posts and not rig.db.exists()
    rig.adapter()
    assert not rig.posts and not state(rig)


def configure(rig, mode):
    if mode:
        rig.env["DAILYCHECK_HOSTING_PROVIDER"] = mode
    rig.env["DAILYCHECK_GITHUB_SHADOW_DIR"] = str(rig.directory)


def result(proc):
    return json.loads(proc.stdout)


SHADOW_ALIASES = [
    {"repo_full_name": f" {TARGET.upper()} "},
    {"repository": {"full_name": f" {TARGET} "}},
    {"repo": f" {TARGET} "},
    {"payload": {"repo_full_name": f" {TARGET} "}},
    {"payload": {"payload": {"repository": {"full_name": f" {TARGET} "}}}},
    {"payload": {"repo": f" {TARGET} "}},
    {"hosting_provider": "github"},
    {"intake_mode": "shadow"},
]


@pytest.mark.parametrize("mode", [None, "gitea", "github"])
@pytest.mark.parametrize("aliases", SHADOW_ALIASES)
def test_target_policy_agrees_across_actual_boundaries(rig, mode, aliases):
    configure(rig, mode)
    raw = json.dumps(deployment(**aliases)).encode()
    # A nonexistent secret path also makes accidental credential access observable.
    rig.env["DAILYCHECK_HERMES_SUBSCRIPTIONS"] = str(rig.directory / "missing-secret")
    assert result(rig.guard(raw))["reason"] == "github_shadow_only"
    assert result(rig.sender(raw))["reason"] == "github_shadow_only"
    assert rig.cli("ingest", raw).returncode == 0
    assert rig.adapter(raw).returncode == 0
    assert not rig.posts and not rig.db.exists()
    assert not list(rig.directory.glob("state.sqlite3*"))


INVALID_BODIES = [
    b'{"broken":',
    b'{"same":1,"same":2}',
    b'{"n":NaN}',
    b'{"n":Infinity}',
    b'{"n":1e9999}',
    b'{"bad":"\\ud800"}',
    b'{"bad":"\xff"}',
    b'{"x":"' + b"x" * 1_048_576 + b'"}',
    b'{"x":' + b"[" * 101 + b"0" + b"]" * 101 + b"}",
    b"null",
    b"[]",
    b'"string"',
    b"42",
]


@pytest.mark.parametrize(
    "raw", INVALID_BODIES, ids=[f"invalid-{n}" for n in range(len(INVALID_BODIES))]
)
def test_strict_invalid_bodies_never_become_config_only_or_transport(rig, raw):
    guard = rig.guard(raw)
    sent = rig.sender(raw)
    reduced = rig.cli("ingest", raw)
    adapted = rig.adapter(raw)
    assert guard.returncode == sent.returncode == 5
    assert result(guard)["reason"] == result(sent)["reason"] == "invalid_payload"
    assert result(reduced)["reason"] == result(adapted)["reason"] == "invalid_payload"
    assert reduced.returncode == adapted.returncode == 0
    assert not rig.posts and not rig.db.exists()


@pytest.mark.parametrize("mode", [None, "gitea", "github", "unknown"])
def test_empty_guard_is_distinct_from_object_body_and_invalid_configuration(rig, mode):
    configure(rig, mode)
    empty = rig.guard()
    null = rig.guard(b"null")
    assert result(null)["reason"] == "invalid_payload" and null.returncode == 5
    if mode in (None, "gitea"):
        assert empty.returncode == 0 and not empty.stdout
        assert rig.adapter().returncode == 0 and state(rig) == []
    else:
        assert result(empty)["reason"] == (
            "github_shadow_only" if mode == "github" else "invalid_hosting_configuration"
        )
        assert result(rig.sender(b"{}"))["reason"] == result(empty)["reason"]
        assert rig.adapter().returncode == (0 if mode == "github" else 1)
        assert not rig.db.exists()
    assert not rig.posts


@pytest.mark.parametrize("mode", [None, "gitea"])
@pytest.mark.parametrize("raw", [b"{}", b'  { "one" : 1 }\n'])
def test_generic_sender_objects_keep_exact_signed_bytes(rig, mode, raw):
    configure(rig, mode)
    assert rig.guard(raw).returncode == 0
    assert rig.sender(raw).returncode == 0
    assert rig.posts == [
        {"raw": raw.decode(), "hmac_valid": True, "path": "/webhooks/dailycheck-deploy"}
    ]


@pytest.mark.parametrize("alias", [False, True])
def test_gitea_alias_precedence_and_replay_keep_one_healthy_post(rig, alias):
    data = deployment(repository={"full_name": "other/gitea"}, repo="another/source")
    if alias:
        data.pop("repo_full_name")
        data["repository"]["full_name"] = " ERIC/DailyCheck "
        data["number"] = data.pop("pr_number")
        data["head_sha"] = data.pop("pr_head_sha")
    raw = json.dumps(data).encode()
    assert rig.adapter(raw).returncode == 75
    assert rig.adapter().returncode == 0
    assert rig.adapter(raw).returncode == 0
    assert rig.adapter().returncode == 0
    assert len(rig.posts) == 1 and rig.posts[0]["hmac_valid"]
    assert json.loads(rig.posts[0]["raw"])["repo_full_name"].strip().casefold() == "eric/dailycheck"
    assert [r["state"] for r in state(rig)] == ["done"]


@pytest.mark.parametrize("n", [31, 32, 33, 97])
def test_rejection_budget_progresses_to_one_claim_without_cleanup(rig, n):
    rows = [{"repo": f" {TARGET} "} if i % 2 == 0 else {"raw": "[1]"} for i in range(n)]
    seed(rig, [*rows, {}, {}])
    before = state(rig)
    calls = n // 32 + 1
    for index in range(calls):
        response = rig.cli("drain", now=10000 + index)
        decision = result(response)
        assert len(decision.get("rejected", [])) <= 32
        if index < calls - 1:
            assert response.returncode == 75 and decision["reason"] == "rejection_budget"
        else:
            assert response.returncode == 0 and decision["decision"] == "trigger"
    after = state(rig)
    assert [r["state"] for r in after] == ["done"] * n + ["claimed", "pending"]
    for old, new in zip(before[:n], after[:n], strict=True):
        assert (new["payload_json"], new["event_key"], new["repo"], new["attempts"]) == (
            old["payload_json"],
            old["event_key"],
            old["repo"],
            old["attempts"],
        )
        assert new["last_error"] in {"shadow_rejected", "invalid_stored_payload"}
        assert new["completed_at_ms"] and new["lease_token"] is None
    with sqlite3.connect(rig.db) as conn:
        dispositions = [
            r[0]
            for r in conn.execute(
                "SELECT disposition FROM deliveries ORDER BY received_at_ms, rowid"
            )
        ]
        assert dispositions[:n] == [r["last_error"] for r in after[:n]]
    assert not rig.posts


def test_adapter_budget_continuation_and_at_most_one_post(rig):
    seed(rig, [*[{"repo": TARGET} for _ in range(32)], {}, {}])
    first = rig.adapter()
    assert first.returncode == 75 and result(first)["reason"] == "rejection_budget"
    assert len([r for r in state(rig) if r["state"] == "done"]) == 32
    assert not rig.posts
    assert rig.adapter().returncode == 0
    assert len(rig.posts) == 1 and rig.posts[0]["hmac_valid"]
    assert [r["state"] for r in state(rig)][-2:] == ["done", "pending"]


def canonical_key(payload):
    from scripts.prod_agent.event_reducer import event_key

    return event_key(payload)


def test_duplicate_and_equivalent_deploy_rejection_is_durable(rig):
    good = deployment()
    seed(rig, [{"raw": '{"bad":', "payload": good, "key": canonical_key(good)}, {}])
    first = rig.cli("ingest", json.dumps(good).encode())
    assert result(first)["reason"] == "invalid_stored_payload"
    for delivery in (good["delivery_id"], "equivalent-new"):
        assert (
            result(rig.cli("ingest", json.dumps({**good, "delivery_id": delivery}).encode()))[
                "reason"
            ]
            == "invalid_stored_payload"
        )
    assert state(rig)[0]["state"] == "done" and state(rig)[0]["attempts"] == 2
    with sqlite3.connect(rig.db) as conn:
        assert (
            conn.execute(
                "SELECT disposition FROM deliveries WHERE delivery_id='equivalent-new'"
            ).fetchone()[0]
            == "invalid_stored_payload"
        )
    assert rig.adapter().returncode == 0 and len(rig.posts) == 1


def test_equivalent_pending_and_non_equivalent_merge_do_not_overwrite_bad_raw(rig):
    good = deployment()
    seed(rig, [{"raw": "null", "payload": good, "key": canonical_key(good)}])
    assert (
        result(rig.cli("ingest", json.dumps({**good, "delivery_id": "new-equivalent"}).encode()))[
            "reason"
        ]
        == "invalid_stored_payload"
    )
    assert state(rig)[0]["payload_json"] == "null"
    different = {**good, "delivery_id": "different", "deploy_next": "envfix", "deploy_rc": 1}
    assert rig.cli("ingest", json.dumps(different).encode()).returncode == 75
    assert state(rig)[0]["payload_json"] == "null" and len(state(rig)) == 2
    assert rig.adapter().returncode == 0 and len(rig.posts) == 1


def test_bad_non_equivalent_pending_is_rejected_before_coalescing(rig):
    seed(rig, [{"raw": "[]"}])
    assert rig.cli("ingest", json.dumps(deployment(delivery_id="new")).encode()).returncode == 75
    assert (
        state(rig)[0]["payload_json"] == "[]"
        and state(rig)[0]["last_error"] == "invalid_stored_payload"
    )
    assert rig.adapter().returncode == 0 and len(rig.posts) == 1


def test_check_suite_duplicate_claim_and_equivalent_replay_retain_rejection(rig):
    good = deployment(event="check_suite", check_conclusion="success")
    seed(rig, [{"payload": good, "raw": "null", "key": canonical_key(good)}])
    with sqlite3.connect(rig.db) as conn:
        conn.execute(
            "INSERT INTO pr_state (repo,pr_number,head_sha,last_event,updated_at_ms,merge_ready_at_ms,merge_ready_trigger_id) VALUES (?,?,?,?,?,?,?)",
            ("eric/dailycheck", 160, SHA, "check_suite:completed:success", 1, 1, "q-0"),
        )
    for delivery in (good["delivery_id"], "new-ci-equivalent"):
        decision = result(rig.cli("ingest", json.dumps({**good, "delivery_id": delivery}).encode()))
        assert decision["reason"] == "invalid_stored_payload"
    with sqlite3.connect(rig.db) as conn:
        assert {r[0] for r in conn.execute("SELECT disposition FROM deliveries")} == {
            "invalid_stored_payload"
        }
    assert state(rig)[0]["attempts"] == 2 and not rig.posts


def test_rejection_preserves_live_lease_and_commits_expired_recovery(rig):
    seed(rig, [{"repo": TARGET}, {"raw": "null"}, {}])
    with sqlite3.connect(rig.db) as conn:
        conn.execute(
            "UPDATE trigger_queue SET state='claimed',lease_token='live-lease',lease_expires_ms=20000 WHERE trigger_id='q-0'"
        )
    live = state(rig)[0]
    first = result(rig.cli("drain", now=10000))
    assert first["decision"] == "trigger" and first["trigger_id"] == "q-2"
    assert state(rig)[0] == live
    # Complete the unrelated lease without going through a provider.
    # This tests bookkeeping only; healthy signed enqueue is tested separately.
    assert result(
        rig.cli("complete", None, 10001, "--trigger-id", "q-2", "--lease", first["lease"])
    )["completed"]
    recovered = result(rig.cli("drain", now=20001))
    assert recovered["recovered"] == ["q-0"]
    assert recovered["rejected"][0]["reason"] == "shadow_rejected"
    assert state(rig)[0]["state"] == "done" and state(rig)[0]["attempts"] == live["attempts"]
    assert not rig.posts


@pytest.mark.parametrize("status", [503, 429])
def test_real_adapter_retry_then_success_does_not_reset_health_on_rejection(rig, status):
    import time

    rig.responses[:] = [status, 202]
    rig.env["DAILYCHECK_REDUCER_COOLDOWN_MS"] = "2000"
    seed(rig, [{"raw": "null"}, {}])
    assert rig.adapter().returncode == 75
    rows = state(rig)
    assert rows[0]["last_error"] == "invalid_stored_payload" and rows[0]["attempts"] == 2
    assert rows[1]["state"] == "pending" and rows[1]["attempts"] == 3
    assert rows[1]["last_error"].startswith(f"http_status={status}")
    assert len(rig.posts) == 1
    assert rig.adapter().returncode in (0, 75) and len(rig.posts) == 1
    # Advance the fixture clock by making existing due/cooldown eligible; no deletion.
    now = int(time.time() * 1000)
    with sqlite3.connect(rig.db) as conn:
        if status == 429:
            health = conn.execute(
                "SELECT state,consecutive_failures FROM provider_state"
            ).fetchone()
            assert health == ("open", 1)
            conn.execute("UPDATE provider_state SET cooldown_until_ms=?", (now - 1,))
        conn.execute("UPDATE trigger_queue SET due_at_ms=? WHERE state='pending'", (now - 1,))
    assert rig.adapter().returncode == 0
    assert len(rig.posts) == 2 and all(p["hmac_valid"] for p in rig.posts)
    assert [r["state"] for r in state(rig)] == ["done", "done"]
    assert state(rig)[1]["attempts"] == 4
    assert rig.adapter().returncode == 0 and len(rig.posts) == 2


def test_permanent_rejection_does_not_fake_provider_recovery(rig):
    seed(rig, [{"raw": "null"}])
    with sqlite3.connect(rig.db) as conn:
        conn.execute("INSERT INTO provider_state VALUES ('hermes','open',7,9000,'prior failure',1)")
    assert result(rig.cli("drain", now=10000))["reason"] == "no_due"
    with sqlite3.connect(rig.db) as conn:
        assert conn.execute(
            "SELECT state,consecutive_failures,last_error FROM provider_state"
        ).fetchone() == ("half_open", 7, "prior failure")
    assert not rig.posts


@pytest.mark.parametrize("duplicate", [False, True])
def test_concurrent_actual_adapters_preserve_rejection_and_one_valid_enqueue(rig, duplicate):
    from concurrent.futures import ThreadPoolExecutor

    good = deployment(161)
    seed(rig, [{"raw": "null"}, {"payload": good, "key": canonical_key(good)}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [
            pool.submit(rig.adapter),
            pool.submit(rig.adapter, json.dumps(good).encode() if duplicate else None),
        ]
        outcomes = [p.result(timeout=30) for p in pending]
    assert all(p.returncode in (0, 75) for p in outcomes), [
        (p.returncode, p.stdout, p.stderr) for p in outcomes
    ]
    assert len(rig.posts) == 1 and rig.posts[0]["hmac_valid"]
    assert [r["state"] for r in state(rig)] == ["done", "done"]
    assert (
        state(rig)[0]["last_error"] == "invalid_stored_payload" and state(rig)[1]["attempts"] == 3
    )
    assert rig.adapter().returncode == 0 and len(rig.posts) == 1


def test_transaction_failure_rolls_back_rejections_claim_and_recovery_before_http(rig):
    seed(rig, [{"raw": "null"}, {}])
    with sqlite3.connect(rig.db) as conn:
        conn.execute(
            "UPDATE trigger_queue SET state='claimed',lease_token='expired',lease_expires_ms=1 WHERE trigger_id='q-1'"
        )
        conn.execute("""CREATE TRIGGER fixture_fail_claim BEFORE UPDATE OF state ON trigger_queue
                        WHEN NEW.state='claimed' BEGIN SELECT RAISE(ABORT,'fixture transaction failure'); END""")
    before = state(rig)
    failed = rig.adapter()
    assert failed.returncode == 1
    assert state(rig) == before and not rig.posts
    with sqlite3.connect(rig.db) as conn:
        assert [r[0] for r in conn.execute("SELECT disposition FROM deliveries")] == [
            "deferred",
            "deferred",
        ]
        conn.execute("DROP TRIGGER fixture_fail_claim")
    assert rig.adapter().returncode == 0
    assert len(rig.posts) == 1 and [r["state"] for r in state(rig)] == ["done", "done"]


def test_process_interruption_before_commit_has_no_http_and_retry_converges(rig):
    import signal
    import time

    seed(rig, [{"raw": "null"}, {}])
    before = state(rig)
    marker = rig.directory / "precommit.pid"
    wrapper = rig.directory / "interrupt-reducer.py"
    wrapper.write_text(
        """import os, sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, """
        + repr(str(ROOT))
        + """)
from scripts.prod_agent import event_reducer
original = sqlite3.connect
class Connection(sqlite3.Connection):
    changed = False
    def execute(self, statement, parameters=()):
        if statement.strip().upper() == 'COMMIT' and self.changed:
            Path(os.environ['FIXTURE_PRECOMMIT']).write_text(str(os.getpid()))
            time.sleep(30)
        result = super().execute(statement, parameters)
        if 'UPDATE trigger_queue' in statement:
            self.changed = True
        return result
def connect(*args, **kwargs):
    return original(*args, **kwargs, factory=Connection)
event_reducer.sqlite3.connect = connect
raise SystemExit(event_reducer.main())
"""
    )
    env = {**rig.env, "DAILYCHECK_EVENT_REDUCER": str(wrapper), "FIXTURE_PRECOMMIT": str(marker)}
    process = subprocess.Popen(
        ["bash", str(ROOT / "deploy/scripts/dc-notify-prod-agent.sh"), "--drain"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        assert state(rig) == before and not rig.posts
        os.kill(int(marker.read_text()), signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=10)
        rig.calls.append(
            {
                "interrupted_shell_exit": process.returncode,
                "stdout": stdout.decode(),
                "stderr": stderr.decode(),
            }
        )
        assert process.returncode != 0 and state(rig) == before and not rig.posts
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    assert rig.adapter().returncode == 0
    assert len(rig.posts) == 1 and [r["state"] for r in state(rig)] == ["done", "done"]


def test_invalid_and_shadow_do_not_read_secret_or_sign(monkeypatch, tmp_path):
    from scripts.prod_agent import hermes_webhook_sender as sender

    def forbidden(*args, **kwargs):
        pytest.fail("Rejected body reached secret/signing boundary")

    monkeypatch.setattr(sender, "_load_secret", forbidden)
    monkeypatch.setattr(sender.hmac, "new", forbidden)
    for raw in [*INVALID_BODIES, *[json.dumps(deployment(**x)).encode() for x in SHADOW_ALIASES]]:
        code, data = sender.send(
            subscriptions_path=tmp_path / "absent",
            base_url="http://127.0.0.1:1",
            route="dailycheck-deploy",
            event="deploy_result",
            delivery_id="fixture",
            body=raw,
        )
        assert code == 5 and data["accepted"] is False


def test_shell_output_guard_blocks_padded_nested_target_before_temp_or_secret(rig):
    wrapper = rig.directory / "fake-output.py"
    envelope = {
        "decision": "trigger",
        "route": "dailycheck-deploy",
        "trigger_id": "old",
        "lease": "old",
        "payload": deployment(repository={"full_name": f" {TARGET} "}),
    }
    wrapper.write_text("print(" + repr(json.dumps(envelope)) + ")\n")
    rig.env["DAILYCHECK_EVENT_REDUCER"] = str(wrapper)
    before = set(rig.directory.iterdir())
    assert rig.adapter().returncode == 0
    assert not rig.posts and set(rig.directory.iterdir()) == before


@pytest.mark.parametrize("event", ["deploy_result", "check_suite"])
def test_duplicate_rejection_precedes_debounce_and_cooldown_without_health_reset(rig, event):
    good = deployment(event=event, check_conclusion="success")
    seed(rig, [{"payload": good, "raw": "null", "key": canonical_key(good)}])
    with sqlite3.connect(rig.db) as conn:
        conn.execute("UPDATE trigger_queue SET due_at_ms=90000")
        conn.execute(
            "INSERT INTO provider_state VALUES ('hermes','open',7,90000,'prior failure',1)"
        )
        if event == "check_suite":
            conn.execute(
                "INSERT INTO pr_state (repo,pr_number,head_sha,last_event,updated_at_ms,merge_ready_at_ms,merge_ready_trigger_id) VALUES (?,?,?,?,?,?,?)",
                ("eric/dailycheck", 160, SHA, "ci", 1, 1, "q-0"),
            )
    assert (
        result(rig.cli("ingest", json.dumps(good).encode()))["reason"] == "invalid_stored_payload"
    )
    assert state(rig)[0]["state"] == "done" and state(rig)[0]["attempts"] == 2
    with sqlite3.connect(rig.db) as conn:
        assert conn.execute(
            "SELECT state,consecutive_failures,cooldown_until_ms,last_error FROM provider_state"
        ).fetchone() == ("open", 7, 90000, "prior failure")
    assert not rig.posts


def test_new_ci_delivery_rejects_preexisting_bad_merge_ready_row(rig):
    good = deployment(event="check_suite", check_conclusion="success")
    seed(rig, [{"payload": good, "raw": "null", "key": canonical_key(good)}])
    with sqlite3.connect(rig.db) as conn:
        conn.execute(
            "INSERT INTO pr_state (repo,pr_number,head_sha,last_event,updated_at_ms,merge_ready_at_ms,merge_ready_trigger_id) VALUES (?,?,?,?,?,?,?)",
            ("eric/dailycheck", 160, SHA, "ci", 1, 1, "q-0"),
        )
    assert (
        result(rig.cli("ingest", json.dumps({**good, "delivery_id": "new-ci"}).encode()))["reason"]
        == "invalid_stored_payload"
    )
    assert state(rig)[0]["state"] == "done" and not rig.posts


@pytest.mark.parametrize("pointer", [False, True])
def test_ci_sampling_race_retains_terminal_rejection_disposition(rig, pointer):
    from scripts.prod_agent.event_reducer import EventReducer, ReducerConfig

    good = deployment(event="check_suite", check_conclusion="success")
    reducer = None

    def sampled_ready(*args):
        key = reducer._merge_ready_key("eric/dailycheck", 160, SHA)
        seed(rig, [{"payload": {**good, "delivery_id": "prior-ci"}, "raw": "null", "key": key}])
        with sqlite3.connect(rig.db) as conn:
            conn.execute("UPDATE trigger_queue SET trigger_id=?", (reducer._trigger_id(key),))
            conn.execute("UPDATE deliveries SET trigger_id=?", (reducer._trigger_id(key),))
            if pointer:
                conn.execute(
                    "INSERT INTO pr_state (repo,pr_number,head_sha,last_event,updated_at_ms,merge_ready_at_ms,merge_ready_trigger_id) VALUES (?,?,?,?,?,?,?)",
                    ("eric/dailycheck", 160, SHA, "ci", 1, 1, reducer._trigger_id(key)),
                )
        return {"ready": True, "reason": "ready"}

    reducer = EventReducer(ReducerConfig(rig.db), readiness_checker=sampled_ready)
    decision = reducer.ingest(good, now_ms=10000)
    assert decision["reason"] == "invalid_stored_payload"
    assert state(rig)[0]["payload_json"] == "null" and state(rig)[0]["attempts"] == 2
    with sqlite3.connect(rig.db) as conn:
        assert {r[0] for r in conn.execute("SELECT disposition FROM deliveries")} == {
            "invalid_stored_payload"
        }
    assert not rig.posts
