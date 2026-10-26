"""Webhook event reducer tests (Issue #54).

The reducer is deliberately tested through its public state-machine contract:
ingest -> optional trigger -> complete/fail, plus the recovery and inspection
entry points used by the command-line adapter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "prod_agent"))

import event_reducer  # noqa: E402
import hermes_webhook_sender  # noqa: E402


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


def test_new_delivery_equivalent_key_is_suppressed_while_trigger_is_claimed(tmp_path):
    reducer = _reducer(tmp_path)

    reducer.ingest(_pr(delivery_id="delivery-a"), now_ms=1_000)
    claimed = reducer.drain(now_ms=3_000)
    equivalent = reducer.ingest(_pr(delivery_id="delivery-b"), now_ms=3_100)

    assert equivalent["decision"] == "suppress"
    assert equivalent["reason"] == "equivalent_active"
    assert equivalent["trigger_id"] == claimed["trigger_id"]
    assert len(reducer.inspect(pr=54)["queue"]) == 1


def test_new_delivery_equivalent_key_is_suppressed_after_trigger_is_done(tmp_path):
    reducer = _reducer(tmp_path)

    reducer.ingest(_pr(delivery_id="delivery-a"), now_ms=1_000)
    claimed = reducer.drain(now_ms=3_000)
    assert (
        reducer.complete(claimed["trigger_id"], claimed["lease"], now_ms=3_100)["completed"] is True
    )

    equivalent = reducer.ingest(_pr(delivery_id="delivery-b"), now_ms=3_200)

    assert equivalent["decision"] == "suppress"
    assert equivalent["reason"] == "equivalent_completed"
    assert equivalent["trigger_id"] == claimed["trigger_id"]
    assert len(reducer.inspect(pr=54)["queue"]) == 1


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

    assert reducer.complete(first["trigger_id"], first["lease"], now_ms=6_100)["completed"] is True
    second = reducer.drain(now_ms=6_200)
    assert second["decision"] == "trigger"
    assert second["payload"]["pr_head_sha"] == next_head


def test_retry_of_queued_work_while_other_head_active_stays_deferred(tmp_path):
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(), now_ms=1_000)
    reducer.drain(now_ms=3_000)
    queued_payload = _pr(delivery_id="delivery-sync", action="synchronize", sha="b" * 40)
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


def test_default_lease_ttl_is_literal_twenty_minutes():
    script = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "dc-notify-prod-agent.sh"

    assert event_reducer.DEFAULT_LEASE_TTL_MS == 1_200_000
    assert event_reducer.build_parser().parse_args(["status"]).lease_ttl_ms == 1_200_000
    assert "${DAILYCHECK_REDUCER_LEASE_TTL_MS:-1200000}" in script.read_text(encoding="utf-8")


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


def test_negative_retry_after_uses_default_quota_cooldown(tmp_path):
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(), now_ms=1_000)
    claimed = reducer.drain(now_ms=3_000)

    failed = reducer.fail(
        claimed["trigger_id"],
        claimed["lease"],
        now_ms=3_100,
        reason="HTTP 429",
        quota_exhausted=True,
        retry_after_ms=-1,
    )

    assert failed["decision"] == "defer"
    assert failed["due_at_ms"] == 63_100
    assert reducer.drain(now_ms=3_200)["reason"] == "provider_cooldown"


def test_cli_negative_retry_after_uses_default_quota_cooldown(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "prod_agent" / "event_reducer.py"
    db = tmp_path / "events.sqlite3"
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(), now_ms=1_000)
    claimed = reducer.drain(now_ms=3_000)

    failed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db",
            str(db),
            "--now-ms",
            "3100",
            "fail",
            "--trigger-id",
            claimed["trigger_id"],
            "--lease",
            claimed["lease"],
            "--reason",
            "HTTP 429",
            "--quota-exhausted",
            "--retry-after-ms",
            "-1",
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert failed.returncode == 75, failed.stderr.decode("utf-8", "replace")
    decision = json.loads(failed.stdout)
    assert decision["due_at_ms"] == 63_100
    assert decision["reason"] == "provider_cooldown"


def test_enqueue_completion_does_not_clear_an_active_external_cooldown(tmp_path):
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(), now_ms=1_000)
    claimed = reducer.drain(now_ms=3_000)
    reducer.trip_provider(
        now_ms=3_100,
        cooldown_ms=900_000,
        reason="provider_quota_threshold",
    )

    reducer.complete(claimed["trigger_id"], claimed["lease"], now_ms=3_200)

    provider = reducer.status(now_ms=3_200)["provider"]
    assert provider["state"] == "open"
    assert provider["cooldown_until_ms"] == 903_100


def test_half_open_probe_completion_returns_provider_to_closed(tmp_path):
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(), now_ms=1_000)
    reducer.trip_provider(
        now_ms=2_000,
        cooldown_ms=1_000,
        reason="provider_quota_threshold",
    )

    claimed = reducer.drain(now_ms=3_000)
    reducer.complete(claimed["trigger_id"], claimed["lease"], now_ms=3_100)

    assert reducer.status(now_ms=3_100)["provider"]["state"] == "closed"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"\xff",
        (
            b'{"delivery_id":"nan","event":"pull_request","action":"opened",'
            b'"repo_full_name":"eric/dailycheck","pr_number":1,'
            b'"pr_head_sha":"aaaaaaaa","extra":NaN}'
        ),
        (
            b'{"delivery_id":"inf","event":"pull_request","action":"opened",'
            b'"repo_full_name":"eric/dailycheck","pr_number":1,'
            b'"pr_head_sha":"aaaaaaaa","extra":Infinity}'
        ),
        (
            b'{"delivery_id":"neg-inf","event":"pull_request","action":"opened",'
            b'"repo_full_name":"eric/dailycheck","pr_number":1,'
            b'"pr_head_sha":"aaaaaaaa","extra":-Infinity}'
        ),
        (
            b'{"delivery_id":"overflow","event":"pull_request","action":"opened",'
            b'"repo_full_name":"eric/dailycheck","pr_number":1,'
            b'"pr_head_sha":"aaaaaaaa","extra":1e9999}'
        ),
        (
            b'{"delivery_id":"duplicate","delivery_id":"duplicate-2",'
            b'"event":"pull_request","action":"opened","repo_full_name":"eric/dailycheck",'
            b'"pr_number":1,"pr_head_sha":"aaaaaaaa"}'
        ),
        b'{"event":"pull_request","action":"opened","delivery_id":"\\ud800"}',
        (
            b'{"event":"pull_request","action":"opened","delivery_id":"x",'
            b'"pr_number":1,"repo_full_name":"eric/dailycheck",'
            b'"pr_head_sha":"aaaaaaaa","extra":"\\ud800"}'
        ),
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
    assert not (tmp_path / "events.sqlite3").exists()


def test_provider_failure_text_is_redacted_from_durable_state(tmp_path):
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(), now_ms=1_000)
    claimed = reducer.drain(now_ms=3_000)
    openai_secret = "sk-proj-super-secret-value"
    github_secret = "ghp_supersecretvalue1234567890"
    bearer_secret = "bearer-secret-value"

    reducer.fail(
        claimed["trigger_id"],
        claimed["lease"],
        now_ms=3_100,
        reason=(
            "HTTP 429 quota exhausted "
            f"Authorization: Bearer {bearer_secret} "
            f"api_key={openai_secret} token={github_secret}"
        ),
        quota_exhausted=True,
        retry_after_ms=60_000,
    )

    inspect = reducer.inspect(pr=54)
    status = reducer.status(now_ms=3_100)
    durable = json.dumps({"inspect": inspect, "status": status})

    assert inspect["queue"][0]["last_error"]
    assert status["provider"]["last_error"]
    assert "quota" in durable.lower()
    assert openai_secret not in durable
    assert github_secret not in durable
    assert bearer_secret not in durable


def _stress_event(index: int) -> dict:
    pr = index % 40 + 1
    sha = hashlib.sha256(f"pr-{pr}".encode()).hexdigest()
    event = ("pull_request", "check_suite", "deploy_result")[index % 3]
    row = _pr(
        delivery_id=f"stress-{index if index < 800 else index - 800}",
        pr=pr,
        sha=sha,
        action=("opened" if event == "pull_request" else "completed"),
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
        item
        for pr in range(1, 41)
        for item in reducer.inspect(pr=pr)["queue"]
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
        stale = reducer.complete(claimed["trigger_id"], "not-the-lease", now_ms=22_200)
        assert stale["reason"] == "stale_lease"
    assert reducer.drain(now_ms=22_300)["reason"] == "active_lease"


def test_notify_adapter_uses_only_approved_routes_and_distinct_exit_semantics():
    script = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "dc-notify-prod-agent.sh"
    content = script.read_text(encoding="utf-8")

    assert "wdg-pr" in content
    assert "wdg-ci" in content
    assert "dailycheck-deploy" in content
    assert re.search(r"\bdailycheck-(?:pr|ci|ops)\b", content) is None
    assert "exit 75" in content
    assert "suppress" in content
    assert "defer" in content
    assert "trigger" in content
    assert '--reason="$reason"' in content
    assert "webhook test" not in content


class _WebhookCapture(BaseHTTPRequestHandler):
    requests: list[dict] = []
    response_status = 202
    retry_after: str | None = None
    redirect_status: int | None = None

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.__class__.requests.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        if self.__class__.redirect_status is not None:
            self.send_response(self.__class__.redirect_status)
            self.send_header("Location", "/redirect-target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(self.__class__.response_status)
        if self.__class__.retry_after is not None:
            self.send_header("Retry-After", self.__class__.retry_after)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return


@pytest.fixture
def webhook_server():
    _WebhookCapture.requests = []
    _WebhookCapture.response_status = 202
    _WebhookCapture.retry_after = None
    _WebhookCapture.redirect_status = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WebhookCapture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _WebhookCapture, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _shell_env(tmp_path: Path, *, base_url: str, secret: str = "route-secret") -> tuple[dict, Path]:
    subscriptions = tmp_path / "webhook_subscriptions.json"
    subscriptions.write_text(
        json.dumps(
            {
                route: {"secret": secret, "events": []}
                for route in ("wdg-pr", "wdg-ci", "dailycheck-deploy")
            }
        ),
        encoding="utf-8",
    )
    subscriptions.chmod(0o600)
    env = os.environ.copy()
    env.update(
        {
            "DAILYCHECK_PYTHON": sys.executable,
            "DAILYCHECK_EVENT_REDUCER": str(
                Path(__file__).resolve().parents[1] / "scripts" / "prod_agent" / "event_reducer.py"
            ),
            "DAILYCHECK_EVENT_REDUCER_DB": str(tmp_path / "events.sqlite3"),
            "DAILYCHECK_REDUCER_DEBOUNCE_MS": "0",
            "DAILYCHECK_REDUCER_LEASE_TTL_MS": "1200000",
            "DAILYCHECK_HERMES_SENDER": str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "prod_agent"
                / "hermes_webhook_sender.py"
            ),
            "DAILYCHECK_HERMES_WEBHOOK_URL": base_url,
            "DAILYCHECK_HERMES_SUBSCRIPTIONS": str(subscriptions),
        }
    )
    return env, subscriptions


def test_notify_adapter_defer_then_http_enqueue_then_suppress(tmp_path, webhook_server):
    capture, base_url = webhook_server
    script = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "dc-notify-prod-agent.sh"
    env, _ = _shell_env(tmp_path, base_url=base_url)
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
    assert capture.requests == []

    triggered = subprocess.run(
        ["bash", str(script)],
        input=line.encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert triggered.returncode == 0, triggered.stderr.decode("utf-8", "replace")
    decision = json.loads(triggered.stdout)
    assert decision["decision"] == "trigger"
    assert len(capture.requests) == 1
    request = capture.requests[0]
    assert request["path"] == "/webhooks/wdg-pr"
    assert request["headers"]["x-github-event"] == "pull_request"
    assert request["headers"]["x-github-delivery"] == f"dailycheck-{decision['trigger_id']}"
    expected_body = json.dumps(
        decision["payload"],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert request["body"] == expected_body
    expected_sig = "sha256=" + hmac.new(b"route-secret", expected_body, hashlib.sha256).hexdigest()
    assert request["headers"]["x-hub-signature-256"] == expected_sig
    assert "route-secret" not in triggered.stdout.decode()
    assert "route-secret" not in triggered.stderr.decode()

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
    assert len(capture.requests) == 1


@pytest.mark.parametrize("status", [200, 202, 204])
def test_http_sender_accepts_all_2xx_enqueue_statuses(tmp_path, webhook_server, status):
    capture, base_url = webhook_server
    capture.response_status = status
    subscriptions = tmp_path / "webhook_subscriptions.json"
    subscriptions.write_text(
        json.dumps({"wdg-pr": {"secret": "route-secret"}}),
        encoding="utf-8",
    )
    body = b'{"one":1}'

    rc, result = hermes_webhook_sender.send(
        subscriptions_path=subscriptions,
        base_url=base_url,
        route="wdg-pr",
        event="pull_request",
        delivery_id="dailycheck-stable-id",
        body=body,
    )

    assert rc == 0
    assert result == {"accepted": True, "http_status": status}
    assert capture.requests[0]["body"] == body


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_http_sender_rejects_redirects_without_forwarding_signed_body(
    tmp_path, webhook_server, status
):
    capture, base_url = webhook_server
    capture.redirect_status = status
    subscriptions = tmp_path / "webhook_subscriptions.json"
    subscriptions.write_text(
        json.dumps({"wdg-pr": {"secret": "route-secret"}}),
        encoding="utf-8",
    )

    rc, result = hermes_webhook_sender.send(
        subscriptions_path=subscriptions,
        base_url=base_url,
        route="wdg-pr",
        event="pull_request",
        delivery_id="dailycheck-stable-id",
        body=b'{"one":1}',
    )

    assert rc == 4
    assert result == {"accepted": False, "http_status": status, "reason": "http_error"}
    assert len(capture.requests) == 1
    assert capture.requests[0]["path"] == "/webhooks/wdg-pr"


@pytest.mark.parametrize(
    ("route", "event"),
    [
        ("wdg-pr", "pull_request"),
        ("wdg-ci", "check_suite"),
        ("dailycheck-deploy", "deploy_result"),
    ],
)
def test_http_sender_uses_real_event_header_for_each_route(tmp_path, webhook_server, route, event):
    capture, base_url = webhook_server
    subscriptions = tmp_path / "webhook_subscriptions.json"
    subscriptions.write_text(
        json.dumps({route: {"secret": "route-secret"}}),
        encoding="utf-8",
    )

    rc, result = hermes_webhook_sender.send(
        subscriptions_path=subscriptions,
        base_url=base_url,
        route=route,
        event=event,
        delivery_id="dailycheck-stable-id",
        body=b"{}",
    )

    assert rc == 0
    assert result["accepted"] is True
    assert capture.requests[0]["path"] == f"/webhooks/{route}"
    assert capture.requests[0]["headers"]["x-github-event"] == event


def test_notify_adapter_drain_path_sends_due_work(tmp_path, webhook_server):
    capture, base_url = webhook_server
    script = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "dc-notify-prod-agent.sh"
    env, _ = _shell_env(tmp_path, base_url=base_url)
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
    assert len(capture.requests) == 1
    assert capture.requests[0]["path"] == "/webhooks/wdg-pr"


def test_notify_adapter_429_from_route_opens_cooldown(tmp_path, webhook_server):
    capture, base_url = webhook_server
    capture.response_status = 429
    capture.retry_after = "120"
    script = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "dc-notify-prod-agent.sh"
    env, _ = _shell_env(tmp_path, base_url=base_url)
    line = json.dumps(_pr()) + "\n"
    seeded = subprocess.run(
        ["bash", str(script)],
        input=line.encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert seeded.returncode == 75

    failed = subprocess.run(
        ["bash", str(script)],
        input=line.encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert failed.returncode == 75
    assert len(capture.requests) == 1
    assert "route-secret" not in failed.stdout.decode()
    assert "route-secret" not in failed.stderr.decode()

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
    provider = json.loads(status.stdout)["provider"]
    assert provider["state"] == "open"
    assert provider["last_error"] == "http_status=429; retry_after_ms=120000"


def test_notify_adapter_handles_nonquota_http_status_without_secret_output(
    tmp_path, webhook_server
):
    capture, base_url = webhook_server
    capture.response_status = 500
    script = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "dc-notify-prod-agent.sh"
    env, _ = _shell_env(tmp_path, base_url=base_url)
    seeded = subprocess.run(
        ["bash", str(script)],
        input=(json.dumps(_pr()) + "\n").encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert seeded.returncode == 75

    failed = subprocess.run(
        ["bash", str(script)],
        input=(json.dumps(_pr()) + "\n").encode(),
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )

    assert failed.returncode == 75
    assert "route-secret" not in failed.stdout.decode()
    assert "route-secret" not in failed.stderr.decode()
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
    provider = json.loads(status.stdout)["provider"]
    assert provider["state"] == "closed"
    assert provider["last_error"] == "http_status=500"


def test_concurrent_drainers_claim_only_one_active_trigger_per_pr(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "prod_agent" / "event_reducer.py"
    db = tmp_path / "events.sqlite3"
    reducer = _reducer(tmp_path)
    reducer.ingest(_pr(), now_ms=1_000)
    command = [
        sys.executable,
        str(script),
        "--db",
        str(db),
        "--now-ms",
        "3000",
        "drain",
    ]

    processes = [
        subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(16)
    ]
    completed = [process.communicate(timeout=20) for process in processes]
    decisions = []
    for index, process in enumerate(processes):
        stdout, stderr = completed[index]
        assert process.returncode == 0, stderr.decode("utf-8", "replace")
        decisions.append(json.loads(stdout))

    triggered = [item for item in decisions if item["decision"] == "trigger"]
    assert len(triggered) == 1
    assert all(item["decision"] == "silent" for item in decisions if item["decision"] != "trigger")
    queue = reducer.inspect(pr=54)["queue"]
    assert len(queue) == 1
    assert queue[0]["state"] == "claimed"
