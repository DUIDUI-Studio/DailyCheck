"""Orca role-model fallback routing tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "agent_framework"))

import model_routing  # noqa: E402


def _config() -> dict:
    return {
        "schema_version": 2,
        "fallback_policy": {
            "max_fallbacks_per_task": 2,
            "fallback_on": [
                "infrastructure_failure",
                "effective_model_mismatch",
            ],
        },
        "roles": {
            "developer": {
                "agent": "codex",
                "model": "primary-model",
                "effort": "high",
                "writes_code": True,
                "fallbacks": [
                    {
                        "agent": "codex",
                        "model": "backup-a",
                        "effort": "high",
                    },
                    {
                        "agent": "codex",
                        "model": "backup-b",
                        "effort": "max",
                    },
                ],
            },
            "reviewer": {
                "agent": "claude",
                "model": "review-primary",
                "effort": "high",
                "writes_code": False,
                "fallbacks": [
                    {
                        "agent": "claude",
                        "model": "review-backup",
                        "effort": "high",
                    }
                ],
            },
            "release": {
                "agent": "prod-agent",
                "model": None,
                "effort": None,
                "writes_code": False,
                "fallbacks": [],
            },
        },
    }


def test_production_routing_is_valid_and_every_llm_role_has_a_backup():
    routing = model_routing.load_routing(
        ROOT / "docs" / "agent-framework" / "orca-model-routing.json"
    )

    model_routing.validate_routing(routing)

    for role, route in routing["roles"].items():
        if role == "release":
            assert route["fallbacks"] == []
        else:
            assert route["fallbacks"], role


def test_resolve_role_route_returns_primary_then_ordered_fallbacks():
    attempts = model_routing.resolve_role_route(_config(), "developer")

    assert [(a.model, a.effort) for a in attempts] == [
        ("primary-model", "high"),
        ("backup-a", "high"),
        ("backup-b", "max"),
    ]
    assert [a.attempt_index for a in attempts] == [0, 1, 2]


def test_next_attempt_advances_only_to_the_declared_fallback_sequence():
    config = _config()

    assert model_routing.next_attempt(config, "developer", "primary-model").model == "backup-a"
    assert model_routing.next_attempt(config, "developer", "backup-a").model == "backup-b"
    assert model_routing.next_attempt(config, "developer", "backup-b") is None
    with pytest.raises(model_routing.RoutingError):
        model_routing.next_attempt(config, "developer", "not-in-route")


def test_retry_command_reuses_the_same_task_and_records_the_failed_dispatch():
    attempt = model_routing.resolve_role_route(_config(), "developer")[1]

    command = model_routing.build_worker_start_command(
        attempt,
        task="task_123",
        retry_of="ctx_failed",
        worktree="current",
        orca_command="orca-test",
    )

    assert command == [
        "orca-test",
        "orchestration",
        "worker-start",
        "--task",
        "task_123",
        "--retry-of",
        "ctx_failed",
        "--worktree",
        "current",
        "--agent",
        "codex",
        "--model",
        "backup-a",
        "--effort",
        "high",
        "--json",
    ]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            model_routing.LaunchResult(
                exit_code=1,
                payload={"ok": False, "status_code": 429},
            ),
            "infrastructure_failure",
        ),
        (
            model_routing.LaunchResult(
                exit_code=1,
                payload={"ok": False, "response": {"http_status": 503}},
            ),
            "infrastructure_failure",
        ),
        (
            model_routing.LaunchResult(
                exit_code=0,
                payload={"ok": True, "effective": "different-model"},
            ),
            "effective_model_mismatch",
        ),
        (
            model_routing.LaunchResult(
                exit_code=1,
                payload={"ok": False, "failure_class": "review_blocker"},
            ),
            "semantic_failure",
        ),
        (
            model_routing.LaunchResult(
                exit_code=1,
                payload={"ok": False, "failure_class": "ci_failure"},
            ),
            "semantic_failure",
        ),
        (
            model_routing.LaunchResult(
                exit_code=0, payload={"ok": True, "effective": "primary-model"}
            ),
            "success",
        ),
    ],
)
def test_launch_failure_classification_distinguishes_infrastructure_from_task_failure(
    result, expected
):
    assert model_routing.classify_launch_result(result, "primary-model") == expected


def test_unknown_failure_is_not_retried_on_another_model():
    config = _config()
    result = model_routing.LaunchResult(
        exit_code=1,
        payload={"ok": False, "failedStage": "unknown"},
    )
    kind = model_routing.classify_launch_result(result, "primary-model")

    assert kind == "unknown_failure"
    assert model_routing.should_fallback(config, kind) is False


def test_infrastructure_and_effective_mismatch_are_retryable():
    config = _config()

    assert model_routing.should_fallback(config, "infrastructure_failure") is True
    assert model_routing.should_fallback(config, "effective_model_mismatch") is True
    assert model_routing.should_fallback(config, "semantic_failure") is False


def test_release_role_cannot_be_started_as_an_llm_worker():
    with pytest.raises(model_routing.RoutingError):
        model_routing.resolve_role_route(_config(), "release")


def test_validation_rejects_duplicate_models_and_unknown_trigger_names():
    config = _config()
    config["roles"]["developer"]["fallbacks"][0]["model"] = "primary-model"
    config["fallback_policy"]["fallback_on"].append("quality_regression")

    with pytest.raises(model_routing.RoutingError) as exc:
        model_routing.validate_routing(config)

    assert "duplicate" in str(exc.value)
    assert "fallback trigger" in str(exc.value)


def test_cli_next_emits_a_ready_to_run_retry_command(tmp_path, capsys):
    config_path = tmp_path / "routing.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")

    rc = model_routing.main(
        [
            "next",
            "--config",
            str(config_path),
            "--role",
            "developer",
            "--failed-model",
            "primary-model",
            "--task",
            "task_123",
            "--retry-of",
            "ctx_failed",
            "--worktree",
            "current",
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["attempt_index"] == 1
    assert payload["model"] == "backup-a"
    assert payload["command"][-1] == "--json"


def test_cli_classify_marks_an_infrastructure_failure_as_fallback_allowed(tmp_path, capsys):
    config_path = tmp_path / "routing.json"
    result_path = tmp_path / "result.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    result_path.write_text(
        json.dumps({"ok": False, "status_code": 503}),
        encoding="utf-8",
    )

    rc = model_routing.main(
        [
            "classify",
            "--config",
            str(config_path),
            "--result-file",
            str(result_path),
            "--exit-code",
            "1",
            "--requested-model",
            "primary-model",
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "failure_kind": "infrastructure_failure",
        "fallback_allowed": True,
    }
