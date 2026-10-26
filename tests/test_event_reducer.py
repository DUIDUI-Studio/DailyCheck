"""Webhook event reducer tests (Issue #54).

The reducer is deliberately tested through its public state-machine contract:
ingest -> optional trigger -> complete/fail, plus the recovery and inspection
entry points used by the command-line adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "prod_agent"))

import event_reducer  # noqa: E402


def _pr(
    *,
    delivery_id: str = "delivery-pr-1",
    action: str = "opened",
    pr: int = 54,
    sha: str = "a" * 40,
    repo: str = "eric/dailycheck",
) -> dict:
    return {
        "delivery_id": delivery_id,
        "event": "pull_request",
        "action": action,
        "repo_full_name": repo,
        "pr_number": pr,
        "pr_head_sha": sha,
        "pr_title": "event reducer",
        "pr_author": "agent",
        "received_at_ms": 1_000,
    }


def _check(
    *,
    delivery_id: str = "delivery-ci-1",
    conclusion: str = "success",
    pr: int = 54,
    sha: str = "a" * 40,
) -> dict:
    row = _pr(delivery_id=delivery_id, pr=pr, sha=sha)
    row.update(
        {
            "event": "check_suite",
            "action": "completed",
            "check_conclusion": conclusion,
        }
    )
    return row


def _deploy(
    *,
    delivery_id: str = "delivery-deploy-1",
    rc: int = 0,
    pr: int = 54,
    sha: str = "a" * 40,
) -> dict:
    row = _pr(delivery_id=delivery_id, pr=pr, sha=sha)
    row.update(
        {
            "event": "deploy_result",
            "action": "completed",
            "deploy_rc": rc,
            "deploy_next": "none" if rc == 0 else "envfix",
        }
    )
    return row


def _reducer(tmp_path: Path, **overrides) -> event_reducer.EventReducer:
    config = {
        "db_path": tmp_path / "events.sqlite3",
        "debounce_ms": 2_000,
        "max_debounce_ms": 10_000,
        "lease_ttl_ms": 30_000,
        "provider_cooldown_ms": 60_000,
    }
    config.update(overrides)
    return event_reducer.EventReducer(event_reducer.ReducerConfig(**config))


def test_ingest_defers_first_event_and_routes_pull_request(tmp_path):
    reducer = _reducer(tmp_path)

    decision = reducer.ingest(_pr(), now_ms=1_000)

    assert decision["decision"] == "defer"
    assert decision["reason"] == "debounce"
    assert decision["route"] == "wdg-pr"
    assert decision["due_at_ms"] == 3_000
    assert decision["payload"]["pr_number"] == 54
    assert decision["payload"]["pr_head_sha"] == "a" * 40
    assert decision["payload"]["repo_full_name"] == "eric/dailycheck"
    json.dumps(decision, ensure_ascii=True)


@pytest.mark.parametrize(
    ("payload", "route", "status_field", "expected"),
    [
        (_check(), "wdg-ci", "checks_status", "success"),
        (_deploy(), "dailycheck-deploy", "deploy_status", "success"),
    ],
)
def test_ingest_routes_ci_and_deploy_and_updates_pr_state(
    tmp_path, payload, route, status_field, expected
):
    reducer = _reducer(tmp_path)

    decision = reducer.ingest(payload, now_ms=1_000)
    state = reducer.inspect(pr=54)["pr_state"]

    assert decision["route"] == route
    assert state["head_sha"] == "a" * 40
    assert state[status_field] == expected


def test_same_delivery_retry_becomes_due_trigger_instead_of_being_suppressed(tmp_path):
    reducer = _reducer(tmp_path)
    payload = _pr()

    first = reducer.ingest(payload, now_ms=1_000)
    retried = reducer.ingest(payload, now_ms=3_000)

    assert first["decision"] == "defer"
    assert retried["decision"] == "trigger"
    assert retried["reason"] == "due"
    assert retried["trigger_id"] == first["trigger_id"]
    assert retried["lease"]


def test_equivalent_events_collapse_into_one_pending_trigger(tmp_path):
    reducer = _reducer(tmp_path)

    reducer.ingest(_pr(delivery_id="delivery-a"), now_ms=1_000)
    collapsed = reducer.ingest(_pr(delivery_id="delivery-b"), now_ms=1_100)
    queue = reducer.inspect(pr=54)["queue"]

    assert collapsed["decision"] == "defer"
    assert collapsed["reason"] == "coalesced"
    assert len(queue) == 1
    assert queue[0]["collapsed_count"] == 2


def test_rapid_pr_ci_deploy_burst_coalesces_to_one_deploy_trigger(tmp_path):
    reducer = _reducer(tmp_path)

    reducer.ingest(_pr(), now_ms=1_000)
    reducer.ingest(_check(), now_ms=1_100)
    reducer.ingest(_deploy(), now_ms=1_200)
    decision = reducer.drain(now_ms=3_200)

    assert decision["decision"] == "trigger"
    assert decision["route"] == "dailycheck-deploy"
    assert decision["payload"]["event"] == "deploy_result"
    assert len(reducer.inspect(pr=54)["queue"]) == 1


def test_one_active_trigger_per_pr_serializes_later_work(tmp_path):
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(), now_ms=1_000)
    first = reducer.drain(now_ms=3_000)

    next_head = "b" * 40
    reducer.ingest(
        _pr(delivery_id="delivery-sync", action="synchronize", sha=next_head),
        now_ms=3_100,
    )
    blocked = reducer.drain(now_ms=6_000)
    assert blocked["decision"] == "silent"
    assert blocked["reason"] == "active_lease"

    assert reducer.complete(
        first["trigger_id"], first["lease"], now_ms=6_100
    )["completed"] is True
    second = reducer.drain(now_ms=6_200)
    assert second["decision"] == "trigger"
    assert second["payload"]["pr_head_sha"] == next_head


def test_retry_of_queued_work_while_other_head_active_stays_deferred(tmp_path):
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(), now_ms=1_000)
    reducer.drain(now_ms=3_000)
    queued_payload = _pr(
        delivery_id="delivery-sync", action="synchronize", sha="b" * 40
    )
    reducer.ingest(queued_payload, now_ms=3_100)

    retried = reducer.ingest(queued_payload, now_ms=6_000)

    assert retried["decision"] == "defer"
    assert retried["reason"] == "active_lease"


def test_expired_lease_is_recovered_atomically(tmp_path):
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(), now_ms=1_000)
    first = reducer.drain(now_ms=3_000)

    recovered = reducer.drain(now_ms=33_001)

    assert recovered["decision"] == "trigger"
    assert recovered["trigger_id"] == first["trigger_id"]
    assert recovered["lease"] != first["lease"]
    assert recovered["attempts"] == 2
    assert first["trigger_id"] in recovered["recovered"]
    assert reducer.inspect(pr=54)["queue"][0]["last_error"] == "lease_expired"


def test_quota_exhaustion_opens_cooldown_and_blocks_drain(tmp_path):
    reducer = _reducer(tmp_path)
    payload = _check(delivery_id="delivery-quota")
    payload.update(
        {
            "provider_status": 429,
            "provider_error": "quota exhausted",
            "retry_after_s": 60,
        }
    )

    deferred = reducer.ingest(payload, now_ms=1_000)
    blocked = reducer.drain(now_ms=2_000)
    status = reducer.status(now_ms=2_000)
    resumed = reducer.drain(now_ms=61_000)

    assert deferred["decision"] == "defer"
    assert deferred["reason"] == "provider_cooldown"
    assert deferred["due_at_ms"] == 61_000
    assert blocked["decision"] == "silent"
    assert blocked["reason"] == "provider_cooldown"
    assert status["provider"]["state"] == "open"
    assert status["provider"]["cooldown_until_ms"] == 61_000
    assert resumed["decision"] == "trigger"


def test_stale_ci_for_old_head_is_suppressed(tmp_path):
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(sha="a" * 40), now_ms=1_000)
    reducer.ingest(
        _pr(delivery_id="delivery-sync", action="synchronize", sha="b" * 40),
        now_ms=1_100,
    )

    stale = reducer.ingest(
        _check(delivery_id="delivery-stale", sha="a" * 40),
        now_ms=1_200,
    )

    assert stale["decision"] == "suppress"
    assert stale["reason"] == "stale_head"


def test_normalizes_raw_gitea_pull_request_shape(tmp_path):
    reducer = _reducer(tmp_path)
    raw = {
        "delivery_id": "raw-delivery",
        "action": "opened",
        "number": 77,
        "pull_request": {
            "state": "open",
            "head": {"sha": "c" * 40},
        },
        "repository": {"full_name": "eric/dailycheck"},
    }
    raw["event"] = "pull_request"

    decision = reducer.ingest(raw, now_ms=1_000)

    assert decision["payload"]["repo_full_name"] == "eric/dailycheck"
    assert decision["payload"]["pr_number"] == 77
    assert decision["payload"]["pr_head_sha"] == "c" * 40
    assert decision["route"] == "wdg-pr"


def test_provider_failure_with_quota_opens_cooldown(tmp_path):
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(), now_ms=1_000)
    claimed = reducer.drain(now_ms=3_000)

    failed = reducer.fail(
        claimed["trigger_id"],
        claimed["lease"],
        now_ms=3_100,
        reason="HTTP 429 quota exhausted",
        quota_exhausted=True,
        retry_after_ms=90_000,
    )

    assert failed["decision"] == "defer"
    assert failed["due_at_ms"] == 93_100
    assert reducer.status(now_ms=3_100)["provider"]["state"] == "open"
    assert reducer.drain(now_ms=3_200)["reason"] == "provider_cooldown"
    assert reducer.drain(now_ms=93_100)["decision"] == "trigger"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"\xff",
        b'{"event":"pull_request","action":"opened","delivery_id":"\\ud800"}',
        b'{"event":"pull_request","action":"opened","delivery_id":"x","pr_number":1,'
        b'"repo_full_name":"eric/dailycheck","pr_head_sha":"a" * 40}',
        ("[" * 2_000 + "0" + "]" * 2_000).encode(),
    ],
)
def test_cli_corrupt_input_is_silent_and_non_crashing(tmp_path, raw):
    script = Path(__file__).resolve().parents[1] / "scripts" / "prod_agent" / "event_reducer.py"
    env = os.environ.copy()
    env["DAILYCHECK_EVENT_REDUCER_DB"] = str(tmp_path / "events.sqlite3")

    run = subprocess.run(
        [sys.executable, str(script), "ingest"],
        input=raw,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )

    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    decision = json.loads(run.stdout)
    assert decision["decision"] == "suppress"
    assert decision["reason"] == "invalid_payload"
    assert run.stderr == b""


def _stress_event(index: int) -> dict:
    pr = index % 40 + 1
    sha = hashlib.sha256(f"pr-{pr}".encode()).hexdigest()
    event = ("pull_request", "check_suite", "deploy_result")[index % 3]
    row = _pr(
        delivery_id=f"stress-{index if index < 800 else index - 800}",
        pr=pr,
        sha=sha,
        action=(
            "opened"
            if event == "pull_request"
            else "completed"
        ),
    )
    if event == "check_suite":
        row["event"] = "check_suite"
        row["check_conclusion"] = ("success", "failure")[index % 2]
    elif event == "deploy_result":
        row["event"] = "deploy_result"
        row["deploy_rc"] = (0, 21)[index % 2]
        row["deploy_next"] = ("none", "envfix")[index % 2]
    return row


def test_1000_event_dedupe_coalesce_stress_has_one_active_wake_per_pr(tmp_path):
    reducer = _reducer(tmp_path, max_debounce_ms=20_000)
    order = sorted(
        range(1_000),
        key=lambda index: hashlib.sha256(f"shuffle-{index}".encode()).digest(),
    )
    decisions = [
        reducer.ingest(_stress_event(index), now_ms=1_000 + position)
        for position, index in enumerate(order)
    ]

    assert all(item["decision"] in {"defer", "suppress"} for item in decisions)
    assert all(item.get("route") in event_reducer.ROUTES.values() for item in decisions)
    pending = [
        item for pr in range(1, 41) for item in reducer.inspect(pr=pr)["queue"]
        if item["state"] == "pending"
    ]
    assert len(pending) == 40

    active_prs: set[int] = set()
    for offset in range(40):
        decision = reducer.drain(now_ms=22_000 + offset)
        assert decision["decision"] == "trigger"
        assert decision["payload"]["pr_number"] not in active_prs
        active_prs.add(decision["payload"]["pr_number"])
    assert len(active_prs) == 40
    assert reducer.drain(now_ms=22_100)["reason"] == "active_lease"

    for pr in range(1, 41):
        claimed = next(
            item for item in reducer.inspect(pr=pr)["queue"] if item["state"] == "claimed"
        )
        state = reducer.inspect(pr=pr)["pr_state"]
        assert claimed["head_sha"] == state["head_sha"]

    decisions.clear()
    for pr in range(1, 41):
        claimed = next(
            item for item in reducer.inspect(pr=pr)["queue"] if item["state"] == "claimed"
        )
        # Completing by a stale lease must not settle a claimed row.
        stale = reducer.complete(
            claimed["trigger_id"], "not-the-lease", now_ms=22_200
        )
        assert stale["reason"] == "stale_lease"
    assert reducer.drain(now_ms=22_300)["reason"] == "active_lease"


def test_notify_adapter_uses_only_approved_routes_and_distinct_exit_semantics():
    script = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "scripts"
        / "dc-notify-prod-agent.sh"
    )
    content = script.read_text(encoding="utf-8")

    assert "wdg-pr" in content
    assert "wdg-ci" in content
    assert "dailycheck-deploy" in content
    assert re.search(r"\bdailycheck-(?:pr|ci|ops)\b", content) is None
    assert "exit 75" in content
    assert "suppress" in content
    assert "defer" in content
    assert "trigger" in content


def _shell_env(
    tmp_path: Path, *, hermes_rc: int = 0, hermes_output: str = ""
) -> tuple[dict, Path]:
    fake_hermes = tmp_path / "hermes"
    fake_hermes.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['HERMES_CALLS'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(os.environ.get('HERMES_OUTPUT', ''))\n"
        "sys.exit(int(os.environ.get('HERMES_RC', '0')))\n",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)
    calls = tmp_path / "hermes-calls.jsonl"
    env = os.environ.copy()
    env.update(
        {
            "DAILYCHECK_PYTHON": sys.executable,
            "DAILYCHECK_EVENT_REDUCER": str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "prod_agent"
                / "event_reducer.py"
            ),
            "DAILYCHECK_EVENT_REDUCER_DB": str(tmp_path / "events.sqlite3"),
            "DAILYCHECK_REDUCER_DEBOUNCE_MS": "0",
            "DAILYCHECK_HERMES": str(fake_hermes),
            "HERMES_CALLS": str(calls),
            "HERMES_RC": str(hermes_rc),
            "HERMES_OUTPUT": hermes_output,
        }
    )
    return env, calls


def test_notify_adapter_defer_then_trigger_then_suppress(tmp_path):
    script = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "scripts"
        / "dc-notify-prod-agent.sh"
    )
    env, calls = _shell_env(tmp_path)
    line = json.dumps(_pr()) + "\n"

    deferred = subprocess.run(
        ["bash", str(script)],
        input=line.encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert deferred.returncode == 75
    assert json.loads(deferred.stdout)["decision"] == "defer"
    assert not calls.exists()

    triggered = subprocess.run(
        ["bash", str(script)],
        input=line.encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert triggered.returncode == 0, triggered.stderr.decode("utf-8", "replace")
    assert json.loads(triggered.stdout)["decision"] == "trigger"
    call = json.loads(calls.read_text(encoding="utf-8").splitlines()[0])
    assert call[:3] == ["webhook", "test", "wdg-pr"]
    assert json.loads(call[call.index("--payload") + 1])["pr_number"] == 54

    suppressed = subprocess.run(
        ["bash", str(script)],
        input=line.encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert suppressed.returncode == 0
    assert json.loads(suppressed.stdout)["decision"] == "suppress"
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1


def test_notify_adapter_drain_path_sends_due_work(tmp_path):
    script = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "scripts"
        / "dc-notify-prod-agent.sh"
    )
    env, calls = _shell_env(tmp_path)
    seeded = subprocess.run(
        ["bash", str(script)],
        input=(json.dumps(_pr()) + "\n").encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert seeded.returncode == 75

    drained = subprocess.run(
        ["bash", str(script), "--drain"],
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )

    assert drained.returncode == 0, drained.stderr.decode("utf-8", "replace")
    assert json.loads(drained.stdout)["decision"] == "trigger"
    assert calls.exists()


def test_notify_adapter_429_from_provider_opens_cooldown(tmp_path):
    script = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "scripts"
        / "dc-notify-prod-agent.sh"
    )
    env, calls = _shell_env(
        tmp_path,
        hermes_rc=1,
        hermes_output="provider HTTP 429 quota exhausted",
    )
    line = json.dumps(_pr()) + "\n"
    subprocess.run(
        ["bash", str(script)],
        input=line.encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )

    failed = subprocess.run(
        ["bash", str(script)],
        input=line.encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert failed.returncode == 75
    assert calls.exists()

    status = subprocess.run(
        [
            env["DAILYCHECK_PYTHON"],
            env["DAILYCHECK_EVENT_REDUCER"],
            "--db",
            env["DAILYCHECK_EVENT_REDUCER_DB"],
            "status",
        ],
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert status.returncode == 0
    assert json.loads(status.stdout)["provider"]["state"] == "open"

