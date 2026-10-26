"""External provider-quota breaker tests (Issue #54)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "prod_agent"))

import event_reducer  # noqa: E402
import provider_breaker  # noqa: E402


def _reducer(tmp_path: Path) -> event_reducer.EventReducer:
    return event_reducer.EventReducer(
        event_reducer.ReducerConfig(
            db_path=tmp_path / "events.sqlite3",
            debounce_ms=0,
            max_debounce_ms=10_000,
            lease_ttl_ms=1_200_000,
            provider_cooldown_ms=60_000,
        )
    )


def _payload(*, delivery_id: str = "breaker-pr") -> dict:
    return {
        "delivery_id": delivery_id,
        "event": "pull_request",
        "action": "opened",
        "repo_full_name": "eric/dailycheck",
        "pr_number": 54,
        "pr_head_sha": "a" * 40,
    }


def test_fresh_cursor_starts_at_eof_and_never_rescans_old_quota_lines(tmp_path):
    log = tmp_path / "errors.log"
    log.write_text(
        "2026-10-26 10:00:00 ERROR provider HTTP 429 quota exhausted\n"
        "2026-10-26 10:00:01 ERROR provider HTTP 429 quota exhausted\n"
        "2026-10-26 10:00:02 ERROR provider HTTP 429 quota exhausted\n",
        encoding="utf-8",
    )
    state = tmp_path / "breaker.json"
    reducer = _reducer(tmp_path)

    result = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=1_000_000,
    )

    assert result["tripped"] is False
    assert result["new_quota_events"] == 0
    assert json.loads(state.read_text())["cursor"]["offset"] == log.stat().st_size
    assert reducer.status(now_ms=1_000_000)["provider"]["state"] == "closed"


def test_log_created_after_missing_observation_is_scanned_as_new(tmp_path):
    log = tmp_path / "errors.log"
    state = tmp_path / "breaker.json"
    reducer = _reducer(tmp_path)
    missing = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=0,
    )
    assert missing["new_quota_events"] == 0

    with log.open("w", encoding="utf-8") as fh:
        for offset in (1, 2, 3):
            fh.write(f"provider HTTP 429 quota exhausted #{offset}\n")
    result = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=10_000,
    )

    assert result["new_quota_events"] == 3
    assert result["tripped"] is True


def test_three_provider_quota_lines_in_five_minutes_trip_fifteen_minute_cooldown(
    tmp_path,
):
    log = tmp_path / "errors.log"
    log.write_text("old non-quota line\n", encoding="utf-8")
    state = tmp_path / "breaker.json"
    reducer = _reducer(tmp_path)
    provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=1_000_000,
    )

    with log.open("a", encoding="utf-8") as fh:
        fh.write("provider HTTP 429 quota exhausted\n")
    first = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=1_010_000,
    )
    with log.open("a", encoding="utf-8") as fh:
        fh.write("provider usage limit reached\n")
    second = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=1_020_000,
    )
    with log.open("a", encoding="utf-8") as fh:
        fh.write("provider HTTP 429 rate limit\n")
    third = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=1_030_000,
    )

    assert first["new_quota_events"] == 1
    assert second["new_quota_events"] == 1
    assert third["tripped"] is True
    provider = reducer.status(now_ms=1_030_000)["provider"]
    assert provider["state"] == "open"
    assert provider["cooldown_until_ms"] == 1_930_000


def test_sliding_window_expires_old_quota_events(tmp_path):
    log = tmp_path / "errors.log"
    log.touch()
    state = tmp_path / "breaker.json"
    reducer = _reducer(tmp_path)
    provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=0,
    )

    for now_ms in (1_000, 2_000, 302_001, 303_000):
        with log.open("a", encoding="utf-8") as fh:
            fh.write("provider HTTP 429 quota exhausted\n")
        result = provider_breaker.scan_once(
            log_path=log,
            state_path=state,
            reducer=reducer,
            now_ms=now_ms,
        )

    assert result["tripped"] is False
    assert result["window_event_count"] == 2


def test_non_provider_rate_limit_lines_do_not_trip_breaker(tmp_path):
    log = tmp_path / "errors.log"
    log.touch()
    state = tmp_path / "breaker.json"
    reducer = _reducer(tmp_path)
    provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=0,
    )
    with log.open("a", encoding="utf-8") as fh:
        for offset in (1, 2, 3):
            fh.write(f"feishu rate limit while sending reminder #{offset}\n")

    result = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=10_000,
    )

    assert result["tripped"] is False
    assert reducer.status(now_ms=10_000)["provider"]["state"] == "closed"


def test_cooldown_blocks_ingest_claim_until_recovery(tmp_path):
    log = tmp_path / "errors.log"
    log.touch()
    state = tmp_path / "breaker.json"
    reducer = _reducer(tmp_path)
    provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=0,
    )
    for offset in (1, 2, 3):
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"provider HTTP 429 quota exhausted #{offset}\n")
    tripped = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=10_000,
    )
    assert tripped["tripped"] is True

    reducer.ingest(_payload(), now_ms=10_100)
    blocked = reducer.ingest(_payload(), now_ms=909_999)
    assert blocked["decision"] == "defer"
    assert blocked["reason"] == "provider_cooldown"
    assert reducer.drain(now_ms=909_999)["reason"] == "provider_cooldown"

    recovered = reducer.ingest(_payload(), now_ms=910_000)
    assert recovered["decision"] == "trigger"
    assert recovered["lease_expires_ms"] - 910_000 == 1_200_000


def test_retry_after_hint_extends_cooldown_and_is_capped_at_one_hour(tmp_path):
    log = tmp_path / "errors.log"
    log.touch()
    state = tmp_path / "breaker.json"
    reducer = _reducer(tmp_path)
    provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=0,
    )
    with log.open("a", encoding="utf-8") as fh:
        fh.write("provider HTTP 429 quota exhausted retry-after: 1800\n")
    first = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=5_000,
    )
    with log.open("a", encoding="utf-8") as fh:
        fh.write("provider HTTP 429 quota exhausted Retry-After: 172800\n")
    second = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=6_000,
    )
    with log.open("a", encoding="utf-8") as fh:
        fh.write("provider HTTP 429 quota exhausted\n")
    result = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=7_000,
    )

    assert first["tripped"] is False
    assert second["tripped"] is False
    assert result["tripped"] is True
    assert result["cooldown_until_ms"] == 3_606_000


def test_provider_reset_timestamp_extends_cooldown_and_is_capped_at_one_hour(tmp_path):
    log = tmp_path / "errors.log"
    log.touch()
    state = tmp_path / "breaker.json"
    reducer = _reducer(tmp_path)
    provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=0,
    )
    reset_at_s = 7_200
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"provider HTTP 429 quota exhausted X-RateLimit-Reset: {reset_at_s}\n")
        fh.write("provider HTTP 429 quota exhausted\n")
        fh.write("provider HTTP 429 quota exhausted\n")

    result = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=5_000,
    )

    assert result["tripped"] is True
    assert result["cooldown_until_ms"] == 3_605_000


def test_breaker_state_and_outputs_never_persist_log_secrets(tmp_path):
    log = tmp_path / "errors.log"
    log.touch()
    state = tmp_path / "breaker.json"
    reducer = _reducer(tmp_path)
    provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=0,
    )
    secret = "sk-live-never-persist-this"
    for offset in (1, 2, 3):
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"provider HTTP 429 quota exhausted token={secret} request={offset}\n")

    result = provider_breaker.scan_once(
        log_path=log,
        state_path=state,
        reducer=reducer,
        now_ms=10_000,
    )

    durable = (
        json.dumps(result)
        + state.read_text(encoding="utf-8")
        + json.dumps(reducer.status(now_ms=10_000))
    )
    assert secret not in durable


def test_breaker_systemd_units_are_oneshot_and_timer_driven():
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/systemd/dc-prod-agent-provider-breaker.service").read_text()
    timer = (root / "deploy/systemd/dc-prod-agent-provider-breaker.timer").read_text()

    assert "Type=oneshot" in service
    assert "provider_breaker.py" in service
    assert "ProtectHome=read-only" in service
    assert "ReadWritePaths=/var/lib/dailycheck-prod-agent" in service
    assert "OnUnitActiveSec=30s" in timer
