"""Deterministic merge-readiness gate tests (Issue #56)."""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "prod_agent"))

import merge_readiness  # noqa: E402

SHA = "a" * 40


def _response(pr: dict | None = None, statuses=None, *, state: str = "success") -> dict:
    return {
        "pr": pr
        if pr is not None
        else {
            "number": 56,
            "state": "open",
            "mergeable": True,
            "head": {"sha": SHA},
        },
        "status": {
            "state": state,
            "statuses": statuses
            if statuses is not None
            else [
                {
                    "context": "CI / test (pull_request)",
                    "status": "success",
                },
                {
                    "context": "CI / agent-framework-reports (pull_request)",
                    "status": "success",
                },
            ],
        },
    }


def _client(response: dict, *, required=("test", "agent-framework-reports")):
    def get_json(path: str):
        if "/pulls/56" in path:
            return response["pr"]
        if f"/commits/{SHA}/status" in path:
            return response["status"]
        raise AssertionError(path)

    return merge_readiness.GiteaReadinessClient(
        merge_readiness.ReadinessConfig(
            api_url="http://gitea.invalid",
            repo="eric/dailycheck",
            token="not-visible",
            required_contexts=tuple(required),
        ),
        get_json=get_json,
    )


def test_required_context_config_parses_exact_comma_separated_names():
    assert merge_readiness.parse_required_contexts(
        " test, agent-framework-reports "
    ) == ("test", "agent-framework-reports")


def test_config_reuses_existing_gitea_env_and_token_file_mechanism(tmp_path):
    file_secret = secrets.token_hex(16)
    env_secret = secrets.token_hex(16)
    token_file = tmp_path / "gitea-token"
    token_file.write_text(f"{file_secret}\n", encoding="utf-8")

    file_config = merge_readiness.ReadinessConfig.from_environment(
        {
            "GITEA_URL": "http://gitea.invalid",
            "GITEA_REPO": "eric/dailycheck",
            "GITEA_TOKEN_FILE": str(token_file),
            "DAILYCHECK_REQUIRED_CONTEXTS": "test,agent-framework-reports",
        }
    )
    env_config = merge_readiness.ReadinessConfig.from_environment(
        {
            "GITEA_URL": "http://gitea.invalid",
            "GITEA_TOKEN_FILE": str(token_file),
            "GITEA_TOKEN": env_secret,
        }
    )

    assert file_config.token == file_secret
    assert env_config.token == env_secret
    assert file_secret not in repr(file_config)
    assert env_secret not in repr(env_config)


@pytest.mark.parametrize("value", ["", "test,", ",test", "test,,reports", "test,test"])
def test_required_context_config_rejects_empty_or_duplicate_entries(value):
    with pytest.raises(ValueError):
        merge_readiness.parse_required_contexts(value)


def test_context_matching_is_exact_not_substring():
    response = _response(
        statuses=[
            {"context": "CI / integration-test (pull_request)", "status": "success"},
            {
                "context": "CI / not-agent-framework-reports (pull_request)",
                "status": "success",
            },
        ]
    )

    result = _client(response).check(56, SHA)

    assert result["ready"] is False
    assert result["reason"] == "checks_missing"
    assert result["contexts"] == {}


def test_all_required_checks_success_is_ready_and_ignores_unrequired_checks():
    result = _client(
        _response(
            statuses=[
                {"context": "CI / test (pull_request)", "status": "success"},
                {
                    "context": "CI / agent-framework-reports (pull_request)",
                    "status": "success",
                },
                {"context": "CI / deploy (pull_request)", "status": "skipped"},
            ]
        )
    ).check(56, SHA)

    assert result["ready"] is True
    assert result["reason"] == "ready"
    assert result["contexts"] == {
        "test": "success",
        "agent-framework-reports": "success",
    }


@pytest.mark.parametrize(
    ("statuses", "reason", "context_value"),
    [
        (
            [
                {"context": "CI / test (pull_request)", "status": "success"},
                {"context": "CI / agent-framework-reports (pull_request)", "status": "pending"},
            ],
            "checks_running",
            "pending",
        ),
        (
            [
                {"context": "CI / test (pull_request)", "status": "success"},
                {"context": "CI / agent-framework-reports (pull_request)", "status": "failure"},
            ],
            "checks_failed",
            "failure",
        ),
        (
            [{"context": "CI / test (pull_request)", "status": "success"}],
            "checks_missing",
            None,
        ),
    ],
)
def test_partial_running_failed_checks_are_not_ready(statuses, reason, context_value):
    result = _client(_response(statuses=statuses)).check(56, SHA)

    assert result["ready"] is False
    assert result["reason"] == reason
    if context_value is not None:
        assert result["contexts"]["agent-framework-reports"] == context_value


@pytest.mark.parametrize(
    ("pr_overrides", "reason"),
    [
        ({"state": "closed"}, "pr_not_open"),
        ({"head": {"sha": "b" * 40}}, "head_changed"),
        ({"mergeable": False}, "not_mergeable"),
        ({"mergeable": None}, "not_mergeable"),
    ],
)
def test_pr_gate_requires_open_unchanged_mergeable_head(pr_overrides, reason):
    response = _response()
    response["pr"].update(pr_overrides)

    result = _client(response).check(56, SHA)

    assert result["ready"] is False
    assert result["reason"] == reason


@pytest.mark.parametrize(
    "response",
    [
        {"pr": [], "status": {"statuses": []}},
        {"pr": {"state": "open"}, "status": {"statuses": "bad"}},
        {"pr": {"state": "open"}, "status": [{"status": "success"}]},
        {
            "pr": {
                "state": "open",
                "mergeable": True,
                "head": {"sha": SHA},
            },
            "status": {"statuses": [{"context": "CI / test", "status": []}]},
        },
    ],
)
def test_malformed_gitea_response_fails_closed(response):
    result = _client(response).check(56, SHA)

    assert result["ready"] is False
    assert result["reason"] == "invalid_response"


def test_transport_error_is_fail_closed_and_does_not_echo_credentials():
    secret = secrets.token_hex(16)

    def broken_get(path: str):
        raise OSError(f"Authorization: token {secret}")

    client = merge_readiness.GiteaReadinessClient(
        merge_readiness.ReadinessConfig(
            api_url="http://gitea.invalid",
            repo="eric/dailycheck",
            token=secret,
            required_contexts=("test", "agent-framework-reports"),
        ),
        get_json=broken_get,
    )

    result = client.check(56, SHA)
    rendered = json.dumps(result)

    assert result["ready"] is False
    assert result["reason"] == "gitea_unavailable"
    assert secret not in rendered


def test_curl_transport_keeps_token_out_of_argv_and_output(tmp_path, monkeypatch):
    secret = secrets.token_hex(16)
    pr_file = tmp_path / "pr.json"
    status_file = tmp_path / "status.json"
    argv_file = tmp_path / "argv.txt"
    stdin_file = tmp_path / "stdin.txt"
    fake_curl = tmp_path / "fake-curl"
    response = _response()
    pr_file.write_text(json.dumps(response["pr"]), encoding="utf-8")
    status_file.write_text(json.dumps(response["status"]), encoding="utf-8")
    fake_curl.write_text(
        f"""#!{sys.executable}
import os
import pathlib
import sys

pathlib.Path(os.environ["FAKE_CURL_ARGV"]).write_text("\\n".join(sys.argv), encoding="utf-8")
pathlib.Path(os.environ["FAKE_CURL_STDIN"]).write_text(sys.stdin.read(), encoding="utf-8")
source = os.environ["FAKE_CURL_PR" if "/pulls/" in sys.argv[-1] else "FAKE_CURL_STATUS"]
sys.stdout.write(pathlib.Path(source).read_text(encoding="utf-8"))
sys.stdout.write("\\n200")
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    monkeypatch.setenv("FAKE_CURL_ARGV", str(argv_file))
    monkeypatch.setenv("FAKE_CURL_STDIN", str(stdin_file))
    monkeypatch.setenv("FAKE_CURL_PR", str(pr_file))
    monkeypatch.setenv("FAKE_CURL_STATUS", str(status_file))

    config = merge_readiness.ReadinessConfig(
        api_url="http://gitea.invalid",
        repo="eric/dailycheck",
        token=secret,
        required_contexts=("test", "agent-framework-reports"),
        curl_path=str(fake_curl),
    )
    result = merge_readiness.GiteaReadinessClient(config).check(56, SHA)

    assert result["ready"] is True
    assert secret not in argv_file.read_text(encoding="utf-8")
    assert secret not in json.dumps(result)
    assert f"Authorization: token {secret}" in stdin_file.read_text(encoding="utf-8")
