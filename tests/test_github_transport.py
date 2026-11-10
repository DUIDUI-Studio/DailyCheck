"""Black-box hosting contracts through an executable fake curl (no live writes)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.hosting import AccessRole, HostingClient, HostingConfig, HostingError
from scripts.prod_agent.github_readiness import GitHubReadinessClient

REPO = "DUIDUI-Studio/DailyCheck"
ROOT = f"/repos/{REPO}"
SHA, BASE = "a" * 40, "b" * 40
REPO_ID = 1448986421

FAKE_CURL = r"""#!PYTHON
import json, os, sys
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
args = sys.argv[1:]
config = sys.stdin.read()
url = urlsplit(args[-1])
query = parse_qs(url.query)
key = url.path + ("?page=" + query["page"][0] if "page" in query else "")
table = json.loads(Path(os.environ["FAKE_TABLE"]).read_text())
state_path = Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
row = table.get(key, {"status": 404, "data": {"message": "missing fake endpoint"}})
if isinstance(row, list):
    index = state.get(key, 0)
    state[key] = index + 1
    row = row[min(index, len(row)-1)]
state_path.write_text(json.dumps(state))
if key == "/graphql" and row.get("echo_mutation"):
    line = next(line for line in config.splitlines() if line.startswith("data = "))
    payload = json.loads(json.loads(line[7:]))
    row["data"] = {"data": {"updateRefs": {
        "clientMutationId": payload["variables"]["input"]["clientMutationId"]}}}
log = {"path": url.path, "query": query, "method": args[args.index("--request")+1],
       "bearer": "Authorization: Bearer " in config, "argv_has_token": "test-credential" in str(args),
       "config": config}
if url.path == "/graphql":
    retained = {}
    for name in (".attempt.lock", ".state.lock"):
        expected = (Path(os.environ["DAILYCHECK_DEPLOY_STATE_DIR"]) / name).stat()
        retained[name] = False
        for fd in range(3, 64):
            try:
                observed = os.fstat(fd)
                retained[name] |= ((expected.st_dev, expected.st_ino)
                                   == (observed.st_dev, observed.st_ino))
            except OSError: pass
    log["retained_fences"] = retained
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(log) + "\n")
headers = "HTTP/1.1 " + str(row.get("status", 200)) + "\r\n" + row.get("headers", "") + "\r\n"
Path(args[args.index("--dump-header")+1]).write_text(headers)
print(row.get("raw", json.dumps(row.get("data"))), end="")
print("\n" + str(row.get("status", 200)), end="")
sys.exit(row.get("exit", 0))
"""


@pytest.fixture
def api(tmp_path, monkeypatch):
    fake = tmp_path / "curl"
    fake.write_text(FAKE_CURL.replace("PYTHON", sys.executable))
    fake.chmod(0o755)
    for name in ("TABLE", "STATE", "LOG"):
        monkeypatch.setenv("FAKE_" + name, str(tmp_path / name.lower()))
    monkeypatch.setenv("DAILYCHECK_HOSTING_PROVIDER", "github")
    monkeypatch.setenv("DAILYCHECK_CURL", str(fake))
    monkeypatch.setenv("DAILYCHECK_GITHUB_READ_TOKEN", "test-credential")
    monkeypatch.delenv("DAILYCHECK_GITHUB_LANE", raising=False)
    monkeypatch.delenv("DAILYCHECK_GITHUB_FINALIZE_TOKEN_FILE", raising=False)
    monkeypatch.delenv("DAILYCHECK_GITHUB_READ_TOKEN_FILE", raising=False)
    monkeypatch.delenv("DAILYCHECK_GITHUB_FEEDBACK_TOKEN", raising=False)
    monkeypatch.delenv("DAILYCHECK_GITHUB_REPORT_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    class API:
        def write(self, table):
            (tmp_path / "table").write_text(json.dumps(table))
            (tmp_path / "state").unlink(missing_ok=True)

        def client(self, role=AccessRole.READ):
            return HostingClient(HostingConfig.from_environment(role))

        def log(self):
            p = tmp_path / "log"
            return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    return API()


def response(data, **extra):
    return {"data": data, **extra}


def pr(number=160):
    return {
        "id": number,
        "number": number,
        "title": "shadow",
        "state": "open",
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "head": {"sha": SHA, "ref": "chore/issue-160-test", "repo": {"full_name": REPO}},
        "base": {"sha": BASE, "ref": "main", "repo": {"id": REPO_ID, "full_name": REPO}},
    }


def run(ident=40, attempt=2):
    return {
        "id": ident,
        "run_number": ident,
        "workflow_id": 12,
        "path": ".github/workflows/ci.yml",
        "event": "pull_request",
        "head_sha": SHA,
        "run_attempt": attempt,
        "check_suite_id": ident + 100,
        "repository": {"id": REPO_ID},
        "head_repository": {"full_name": REPO},
        "status": "completed",
        "conclusion": "success",
        "updated_at": "2026-11-07T00:00:00Z",
        "pull_requests": [
            {"number": 160, "head": {"sha": SHA}, "base": {"sha": BASE, "ref": "main"}}
        ],
    }


def check(ident, name, state="success", suite=140):
    return {
        "id": ident,
        "name": name,
        "head_sha": SHA,
        "status": "completed" if state != "pending" else "in_progress",
        "conclusion": state if state != "pending" else None,
        "app": {"id": 15368, "slug": "github-actions"},
        "check_suite": {"id": suite},
    }


def evidence():
    checks = [check(51, "test"), check(52, "agent-framework-reports")]
    jobs = [
        {
            "id": c["id"] + 100,
            "name": c["name"],
            "run_id": 40,
            "run_attempt": 2,
            "head_sha": SHA,
            "status": c["status"],
            "conclusion": c["conclusion"],
            "check_run_url": f"https://api.github.com{ROOT}/check-runs/{c['id']}",
        }
        for c in checks
    ]
    return {
        ROOT: response({"id": REPO_ID, "full_name": REPO}),
        ROOT + "/pulls/160": response(pr()),
        ROOT + "/pulls?page=1": response([pr()]),
        ROOT + "/branches/main": response({"commit": {"sha": BASE}}),
        ROOT + "/actions/workflows/ci.yml": response(
            {"id": 12, "path": ".github/workflows/ci.yml", "state": "active"}
        ),
        ROOT + "/actions/workflows/12/runs?page=1": response(
            {"total_count": 1, "workflow_runs": [run()]}
        ),
        ROOT + "/actions/runs/40/jobs?page=1": response({"total_count": 2, "jobs": jobs}),
        ROOT + f"/commits/{SHA}/check-runs?page=1": response(
            {"total_count": 2, "check_runs": checks}
        ),
    }


def decide(api, table=None, **kw):
    api.write(evidence() if table is None else table)
    return GitHubReadinessClient(api.client()).check(160, SHA, **kw)


def test_success_uses_actual_jobs_checks_and_fresh_h_b(api):
    result = decide(api)
    assert result["ready"] is True, result
    assert result["base_sha"] == BASE and result["candidate_sha"] == SHA
    assert result["run_attempt"] == 2 and result["freshness"]
    assert result["finalization_supported"] is False and result["mode"] == "shadow"
    assert all(row["bearer"] and not row["argv_has_token"] for row in api.log())
    assert sum(row["path"].endswith("/pulls/160") for row in api.log()) == 2


@pytest.mark.parametrize(
    "state,ready",
    [
        ("success", True),
        ("pending", False),
        ("failure", False),
        ("cancelled", False),
        ("skipped", False),
        ("neutral", False),
    ],
)
def test_latest_trusted_attempt_replaces_old_success_and_failure(api, state, ready):
    table = evidence()
    checks = table[ROOT + f"/commits/{SHA}/check-runs?page=1"]["data"]["check_runs"]
    # Historical attempt of the SAME name/suite, opposite outcome, must not win.
    checks.extend(
        [check(31, "test", "failure" if ready else "success"), check(32, "agent-framework-reports")]
    )
    table[ROOT + f"/commits/{SHA}/check-runs?page=1"]["data"]["total_count"] = 4
    checks[0].update(
        status="in_progress" if state == "pending" else "completed",
        conclusion=None if state == "pending" else state,
    )
    job = table[ROOT + "/actions/runs/40/jobs?page=1"]["data"]["jobs"][0]
    job.update(status=checks[0]["status"], conclusion=checks[0]["conclusion"])
    result = decide(api, table)
    assert result["ready"] is ready, result
    assert result["contexts"]["test"] == (
        "success" if ready else "pending" if state == "pending" else "failure"
    )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("app", "untrusted_check_producer"),
        ("name", "untrusted_check_producer"),
        ("suite", "untrusted_check_producer"),
        ("workflow", "untrusted_workflow_run"),
        ("source", "untrusted_workflow_run"),
        ("event", "untrusted_workflow_run"),
        ("attempt", "untrusted_job"),
        ("missing", "checks_missing_or_ambiguous"),
        ("ambiguous", "checks_missing_or_ambiguous"),
        ("synthetic", "untrusted_workflow_run"),
        ("base", "stale_run_candidate"),
        ("bad_shape", "invalid_page_items"),
    ],
)
def test_wrong_producer_workflow_attempt_or_missing_evidence_blocks(api, mutation, reason):
    table = evidence()
    checks = table[ROOT + f"/commits/{SHA}/check-runs?page=1"]["data"]["check_runs"]
    runs = table[ROOT + "/actions/workflows/12/runs?page=1"]["data"]["workflow_runs"]
    jobs = table[ROOT + "/actions/runs/40/jobs?page=1"]["data"]["jobs"]
    if mutation == "app":
        checks[0]["app"]["id"] = 99
    if mutation == "name":
        checks[0]["name"] = "integration-test"
    if mutation == "suite":
        checks[0]["check_suite"]["id"] = 99
    if mutation == "workflow":
        runs[0]["path"] = ".github/workflows/spoof.yml"
    if mutation == "source":
        runs[0]["head_repository"]["full_name"] = "attacker/DailyCheck"
    if mutation == "event":
        runs[0]["event"] = "push"
    if mutation == "synthetic":
        runs[0]["head_sha"] = "c" * 40
    if mutation == "attempt":
        jobs[0]["run_attempt"] = 3
    if mutation == "base":
        runs[0]["pull_requests"][0]["base"]["sha"] = "c" * 40
    if mutation == "missing":
        jobs.pop()
        table[ROOT + "/actions/runs/40/jobs?page=1"]["data"]["total_count"] = 1
    if mutation == "ambiguous":
        jobs.append({**jobs[0], "id": 999})
        table[ROOT + "/actions/runs/40/jobs?page=1"]["data"]["total_count"] = 3
    if mutation == "bad_shape":
        jobs[0] = None
    result = decide(api, table)
    assert result["ready"] is False and result["reason"] == reason, result


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("head", "head_changed"),
        ("base", "base_changed"),
        ("branch", "base_changed"),
        ("draft", "pr_not_open_or_not_ready"),
        ("closed", "pr_not_open_or_not_ready"),
        ("mergeable", "not_mergeable"),
        ("run", "run_changed"),
    ],
)
def test_change_during_final_reread_blocks(api, mutation, reason):
    table = evidence()
    final = pr()
    if mutation == "head":
        final["head"]["sha"] = "c" * 40
    if mutation == "base":
        final["base"]["sha"] = "c" * 40
    if mutation == "draft":
        final["draft"] = True
    if mutation == "closed":
        final["state"] = "closed"
    if mutation == "mergeable":
        final["mergeable"] = None
    table[ROOT + "/pulls/160"] = [response(pr()), response(final)]
    if mutation == "branch":
        table[ROOT + "/branches/main"] = [
            response({"commit": {"sha": BASE}}),
            response({"commit": {"sha": "c" * 40}}),
        ]
    if mutation == "run":
        newer = run(attempt=3)
        table[ROOT + "/actions/workflows/12/runs?page=1"] = [
            response({"total_count": 1, "workflow_runs": [run()]}),
            response({"total_count": 1, "workflow_runs": [newer]}),
        ]
    result = decide(api, table)
    assert not result["ready"] and result["reason"] == reason, result


def test_expected_base_identity_is_explicit(api):
    assert decide(api, expected_base_sha="d" * 40)["reason"] == "base_changed"
    assert decide(api, repo="other/repo")["reason"] == "invalid_candidate"


def test_latest_new_run_pending_cannot_be_hidden_by_old_success(api):
    table = evidence()
    newer = run(41, 1)
    newer.update(status="queued", conclusion=None)
    table[ROOT + "/actions/workflows/12/runs?page=1"] = response(
        {"total_count": 2, "workflow_runs": [run(), newer]}
    )
    table[ROOT + "/actions/runs/41/jobs?page=1"] = response({"total_count": 0, "jobs": []})
    result = decide(api, table)
    assert not result["ready"] and result["run_id"] == 41


@pytest.mark.parametrize("code", [401, 403, 429, 500])
def test_auth_network_failure_is_visible_not_empty_success(api, code):
    api.write({ROOT: response([], status=code)})
    with pytest.raises(HostingError) as error:
        api.client().get(ROOT)
    assert error.value.status == code
    assert "test-credential" not in str(error.value)


def test_missing_credentials_never_fall_back_to_gitea_or_write_role(api, monkeypatch):
    monkeypatch.delenv("DAILYCHECK_GITHUB_READ_TOKEN")
    monkeypatch.setenv("GITEA_TOKEN", "not-a-github-credential")
    monkeypatch.setenv("DAILYCHECK_GITHUB_REPORT_TOKEN", "report-only")
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_REPO", "DUIDUI-Studio/DailyCheck-ops")
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_REPO_ID", "1449125557")
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE", "true")
    assert HostingConfig.from_environment().token == ""
    assert HostingConfig.from_environment(AccessRole.FEEDBACK).token == ""
    with pytest.raises(HostingError, match="credentials_unavailable"):
        api.client().get(ROOT)
    assert not api.log()


@pytest.mark.parametrize(
    "env",
    [
        {"DAILYCHECK_HOSTING_PROVIDER": "unknown"},
        {"DAILYCHECK_GITHUB_API": "http://gitea/api/v1"},
        {"DAILYCHECK_GITHUB_REPO": "attacker/repo"},
        {"DAILYCHECK_GITHUB_READ_TOKEN": "line\nbreak"},
    ],
)
def test_invalid_provider_endpoint_or_credentials_fail_closed(api, monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(HostingError):
        HostingConfig.from_environment()


def test_roles_reject_writes_and_finalization_before_network(api, monkeypatch):
    with pytest.raises(HostingError, match="read_role_cannot_write"):
        api.client().request("POST", ROOT + "/issues", {})
    monkeypatch.setenv("DAILYCHECK_GITHUB_REPORT_TOKEN", "report-only")
    for path in ("/pulls/160/merge", "/issues", "/git/refs/heads/main"):
        with pytest.raises(HostingError, match="unsupported_hosting_write"):
            api.client(AccessRole.REPORT).request("POST", ROOT + path, {})
    assert not api.log()


def test_pagination_follows_all_pages_and_fails_on_exhaustion(api, monkeypatch):
    client = api.client()
    monkeypatch.setattr(client, "PER_PAGE", 1)
    api.write(
        {
            ROOT + "/labels?page=1": response(
                [{"id": 1}],
                headers=f'Link: <https://api.github.com{ROOT}/labels?per_page=1&page=2>; rel="next"\r\n',
            ),
            ROOT + "/labels?page=2": response([{"id": 2}]),
            ROOT + "/labels?page=3": response([]),
        }
    )
    assert [r["id"] for r in client.pages(ROOT + "/labels")] == [1, 2]
    monkeypatch.setattr(client, "MAX_PAGES", 1)
    with pytest.raises(HostingError, match="pagination_exhausted"):
        client.pages(ROOT + "/labels")


@pytest.mark.parametrize("kind", ["malformed", "repeated", "cross_origin", "incomplete", "network"])
def test_malformed_or_partial_pagination_never_returns_partial_success(api, monkeypatch, kind):
    client = api.client()
    monkeypatch.setattr(client, "PER_PAGE", 1)
    table = {
        ROOT + "/labels?page=1": response([{"id": 1}]),
        ROOT + "/labels?page=2": response([{"id": 1}]),
    }
    kw = {}
    if kind == "malformed":
        table[ROOT + "/labels?page=1"] = {"raw": "not JSON"}
    if kind == "cross_origin":
        table[ROOT + "/labels?page=1"]["headers"] = (
            'Link: <https://evil.invalid/labels?page=2>; rel="next"\r\n'
        )
    if kind == "incomplete":
        table[ROOT + "/labels?page=1"] = response({"total_count": 3, "labels": []})
        kw = {"key": "labels"}
    if kind == "network":
        table[ROOT + "/labels?page=1"]["exit"] = 28
    api.write(table)
    with pytest.raises(HostingError):
        client.pages(ROOT + "/labels", **kw)


def test_feedback_raw_creation_without_private_receipt_is_rejected(api, monkeypatch):
    from blueprints import feedback_dispatch

    monkeypatch.setenv("DAILYCHECK_GITHUB_FEEDBACK_TOKEN", "test-credential")
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_REPO", "DUIDUI-Studio/DailyCheck-ops")
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_REPO_ID", "1449125557")
    monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE", "true")
    assert feedback_dispatch.dispatch_enabled()
    assert feedback_dispatch.create_issue("title", "body") is None
    assert not api.log()


def test_feedback_missing_or_invalid_readonly_credential_stays_disabled(api, monkeypatch):
    from blueprints import feedback_dispatch

    monkeypatch.setenv("GITEA_TOKEN", "old")
    assert not feedback_dispatch.dispatch_enabled()
    assert feedback_dispatch.create_issue("title", "body") is None
    monkeypatch.setenv("DAILYCHECK_GITHUB_FEEDBACK_TOKEN", "test-credential")
    table = evidence()
    table[ROOT + "/labels?page=1"] = response({}, status=403)
    api.write(table)
    assert feedback_dispatch.create_issue("title", "body") is None
    assert not any(r["method"] == "POST" for r in api.log())


def test_dashboard_github_shapes_and_failed_reads_are_not_zero(api):
    from scripts.dailycheck_dashboard import fetchers

    table = evidence()
    table[ROOT + "/issues?page=1"] = response(
        [
            {"id": 1, "number": 9, "state": "open", "labels": [{"name": "x"}]},
            {"id": 2, "number": 10, "pull_request": {}},
        ]
    )
    api.write(table)
    assert [row["number"] for row in fetchers.fetch_issues()] == [9]
    assert fetchers.fetch_prs()[0]["repo_id"] == "git:github.com/" + REPO
    assert all(
        row["query"].get("sort") == ["updated"]
        for row in api.log()
        if row["path"].endswith(("/issues", "/pulls"))
    )
    table[ROOT + "/issues?page=1"] = response([], status=403)
    api.write(table)
    with pytest.raises(fetchers.FetchError):
        fetchers.fetch_issues()


def test_poller_is_shadow_only_and_uses_checks_not_combined_status(api, monkeypatch):
    from scripts.prod_agent import pr_poll

    api.write(evidence())
    assert pr_poll._catchup_block() == ""
    text = pr_poll._pr_block()
    assert "PR #160" in text and "/pull/160" in text and "checks=ready" in text
    assert "shadow" in text.lower() and not any(r["path"].endswith("/status") for r in api.log())


def test_deploy_report_sha_only_lookup_and_untyped_edit_are_rejected(api, monkeypatch):
    from scripts.blue_green import deploy_report

    monkeypatch.setenv("DAILYCHECK_GITHUB_REPORT_TOKEN", "test-credential")
    with pytest.raises(HostingError):
        deploy_report.find_same_sha_comment(160, SHA)
    with pytest.raises(HostingError):
        deploy_report.gitea("PATCH", ROOT + "/issues/comments/2", {"body": "updated"})
    assert not api.log()


def test_github_finalization_is_rejected_without_transport(api):
    from scripts.blue_green import deploy_merge

    code, payload = deploy_merge.cmd_merge(160, SHA, False)
    assert code == deploy_merge.RC_API
    assert payload["reason"] == "github_finalization_unsupported"
    assert not api.log()


def ordinary(api, tmp_path, monkeypatch):
    token = tmp_path / "finalize-token"
    token.write_text("synthetic-prod-finalize")
    token.chmod(0o600)
    monkeypatch.setenv("DAILYCHECK_GITHUB_LANE", "ordinary")
    monkeypatch.setenv("DAILYCHECK_GITHUB_FINALIZE_TOKEN_FILE", str(token))
    monkeypatch.setenv("DAILYCHECK_DEPLOY_STATE_DIR", str(tmp_path / "deploy-state"))
    monkeypatch.setenv("DAILYCHECK_DEPLOY_EVENT_LOG", str(tmp_path / "private-events.jsonl"))
    table = evidence()
    table[ROOT]["data"]["node_id"] = "R_kgDOVl3HNQ"
    before = pr()
    before.update(node_id="PR_synthetic_160", merged=False)
    before["head"]["repo"]["id"] = REPO_ID
    table[ROOT + "/pulls/160"] = response(before)
    table[ROOT + "/compare/" + BASE + "..." + SHA] = response({
        "status": "ahead", "base_commit": {"sha": BASE}, "merge_base_commit": {"sha": BASE}})
    table[ROOT + "/git/ref/heads/main"] = response({
        "ref": "refs/heads/main", "object": {"sha": BASE}})
    table[ROOT + "/branches/main/protection"] = response({
        "enforce_admins": {"enabled": True},
        "required_status_checks": {"checks": [
            {"context": n, "app_id": 15368} for n in ("test", "agent-framework-reports")]},
        "required_pull_request_reviews": {
            "required_approving_review_count": 1, "dismiss_stale_reviews": True}})
    table[ROOT + "/pulls/160/reviews?page=1"] = response([
        {"id": 91, "user": {"id": 11}, "state": "APPROVED", "commit_id": SHA}])
    table["/graphql"] = {"echo_mutation": True}
    return table


def proof(name, data):
    destination = os.environ.get("DAILYCHECK_HOSTING_PROOF_DIR")
    if destination:
        path = Path(destination) / (name + ".json")
        path.write_text(json.dumps(data, indent=2) + "\n")
        path.chmod(0o600)


def merge_cli(tmp_path, *, command="merge", owned=True, base=BASE):
    root = Path(__file__).resolve().parents[1]
    cli = [sys.executable, "-B", str(root / "scripts/blue_green/deploy_merge.py"),
           command, "--pr", "160", "--sha", SHA, "--base", base]
    if owned and command == "merge":
        wrapper = tmp_path / "owned-finalize.py"
        wrapper.write_text(f'''import argparse, contextlib, io, json, os, sys
sys.path.insert(0, {str(root / "scripts/blue_green")!r})
import deploy_state as ds
if len(sys.argv) == 1:
    ds.cmd_run(argparse.Namespace(exec=[sys.executable, "-B", __file__, "held"]))
else:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ds.cmd_acquire(argparse.Namespace(pr=160, sha={SHA!r}, session="test-finalize",
            phase="merge-start", deployment_provider="github", deployment_host="github.com",
            deployment_repository={REPO!r}))
    assert rc == 0, out.getvalue()
    os.environ[ds.ATTEMPT_ENV] = out.getvalue()
    os.execv(sys.executable, {cli!r})
''')
        cli = [sys.executable, "-B", str(wrapper)]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               DAILYCHECK_DEPLOY_STATE_DIR=str(tmp_path / "deploy-state"))
    return subprocess.run(cli, env=env, text=True, capture_output=True, timeout=30)


def merged_table(table):
    before = table[ROOT + "/pulls/160"]
    after = json.loads(json.dumps(before))
    after["data"].update(merged=True, state="closed", merge_commit_sha=SHA)
    # Two full precondition reads, each readiness reads H/B twice plus target twice.
    table[ROOT + "/pulls/160"] = [before] * 8 + [after]
    main = table[ROOT + "/git/ref/heads/main"]
    table[ROOT + "/git/ref/heads/main"] = [main] * 2 + [response({
        "ref": "refs/heads/main", "object": {"sha": SHA}})]
    return table


def test_ordinary_finalization_cli_checks_and_atomic_exact_h_postfacts(api, tmp_path, monkeypatch):
    table = ordinary(api, tmp_path, monkeypatch)
    api.write(table)
    checked = merge_cli(tmp_path, command="check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert not [r for r in api.log() if r["method"] == "POST"]
    api.write(merged_table(table))
    result = merge_cli(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    out = json.loads(result.stdout)
    assert out["merged"] is True and out["merged_sha"] == out["main_sha"] == SHA
    posts = [r for r in api.log() if r["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["path"] == "/graphql"
    assert posts[0]["retained_fences"] == {".attempt.lock": True, ".state.lock": True}
    payload = json.loads(json.loads(next(
        line[7:] for line in posts[0]["config"].splitlines() if line.startswith("data = "))))
    updates = payload["variables"]["input"]["refUpdates"]
    assert updates == [
        {"name": "refs/heads/main", "beforeOid": BASE, "afterOid": SHA, "force": False},
        {"name": "refs/heads/chore/issue-160-test", "beforeOid": SHA,
         "afterOid": SHA, "force": False}]
    proof("finalization-positive", {"check_native_exit": checked.returncode,
          "check_stdout": checked.stdout, "merge_native_exit": result.returncode,
          "merge_stdout": out, "operation": payload,
          "http_child_retained_fences": posts[0]["retained_fences"],
          "post_read_paths": [r["path"] for r in api.log()[-3:]]})


@pytest.mark.parametrize("failure", ["head", "base", "foreign", "ancestry", "protection",
                                     "review", "stale_review", "ci", "no_fence", "absent_auth"])
def test_ordinary_finalization_bad_preconditions_never_write(api, tmp_path, monkeypatch, failure):
    table = ordinary(api, tmp_path, monkeypatch)
    if failure in {"head", "base", "foreign"}:
        p = table[ROOT + "/pulls/160"]["data"]
        if failure == "head":
            p["head"]["sha"] = "f" * 40
        elif failure == "base":
            p["base"]["sha"] = "f" * 40
        else:
            p["head"]["repo"]["id"] = 1
    if failure == "ancestry":
        table[ROOT + f"/compare/{BASE}...{SHA}"]["data"]["status"] = "diverged"
    if failure == "protection":
        table[ROOT + "/branches/main/protection"] = response({}, status=404)
    if failure in {"review", "stale_review"}:
        r = table[ROOT + "/pulls/160/reviews?page=1"]["data"][0]
        r.update(state="CHANGES_REQUESTED" if failure == "review" else "APPROVED",
                 commit_id=BASE)
    if failure == "ci":
        table[ROOT + "/actions/runs/40/jobs?page=1"]["data"]["jobs"][0]["conclusion"] = "failure"
    if failure == "absent_auth":
        monkeypatch.delenv("DAILYCHECK_GITHUB_FINALIZE_TOKEN_FILE")
    api.write(table)
    result = merge_cli(tmp_path, owned=failure != "no_fence")
    assert result.returncode == 32, result.stdout + result.stderr
    expected = {"head": "github_not_ready:head_changed", "base": "github_not_ready:base_changed",
        "foreign": "github_target_changed", "ancestry": "github_not_fast_forward",
        "protection": "hosting_http_error", "review": "github_review_required",
        "stale_review": "github_review_required", "ci": "github_not_ready:",
        "no_fence": "github_owned_verified_pipeline_required",
        "absent_auth": "explicit_finalization_credential_required"}
    assert json.loads(result.stdout)["reason"].startswith(expected[failure]), result.stdout
    assert not [r for r in api.log() if r["method"] == "POST"]
    proof("precondition-" + failure, {"native_exit": result.returncode,
          "stdout": json.loads(result.stdout), "post_count": 0})


@pytest.mark.parametrize("failure", ["permission", "graphql_error", "response_loss",
                                     "open_pr", "wrong_main", "changed_head_after", "duplicate"])
def test_ordinary_finalization_uncertain_result_never_succeeds(api, tmp_path, monkeypatch, failure):
    table = merged_table(ordinary(api, tmp_path, monkeypatch))
    if failure == "permission":
        table["/graphql"] = response({}, status=403)
    if failure == "graphql_error":
        table["/graphql"] = response({"errors": [{"message": "protected"}]})
    if failure == "response_loss":
        table["/graphql"]["exit"] = 28
    if failure == "open_pr":
        table[ROOT + "/pulls/160"][-1]["data"].update(state="open", merged=False)
    if failure == "wrong_main":
        table[ROOT + "/git/ref/heads/main"][-1]["data"]["object"]["sha"] = BASE
    if failure == "changed_head_after":
        table[ROOT + "/pulls/160"][-1]["data"]["head"]["sha"] = "f" * 40
    if failure == "duplicate":
        table[ROOT + "/pulls/160"] = table[ROOT + "/pulls/160"][-1]
    api.write(table)
    result = merge_cli(tmp_path)
    assert result.returncode == 32, result.stdout + result.stderr
    out = json.loads(result.stdout)
    assert out["merged"] is False and out["reconcile_required"] == (failure != "duplicate")
    posts = [r for r in api.log() if r["method"] == "POST"]
    assert len(posts) == (0 if failure == "duplicate" else 1)
    proof("finalization-" + failure, {"native_exit": result.returncode,
          "stdout": out, "post_count": len(posts)})


def test_ordinary_credential_is_file_only_separate_and_transport_is_finite(api, tmp_path, monkeypatch):
    ordinary(api, tmp_path, monkeypatch)
    token = Path(os.environ["DAILYCHECK_GITHUB_FINALIZE_TOKEN_FILE"])
    token.write_text("test-credential")
    with pytest.raises(HostingError, match="separate_finalization"):
        HostingConfig.from_environment(AccessRole.FINALIZE)
    token.write_text("synthetic-prod-finalize")
    client = api.client(AccessRole.FINALIZE)
    for path, body in (("/graphql", {"query": "mutation arbitrary"}),
                       (ROOT + "/git/refs/heads/main", {"sha": SHA}),
                       (ROOT + "/pulls/160/merge", {})):
        with pytest.raises(HostingError, match="unsupported_finalization"):
            client.request("POST", path, body)
    assert not api.log()


def test_ordinary_poller_cli_uses_readiness_and_missing_auth_alerts(api, tmp_path, monkeypatch):
    api.write(ordinary(api, tmp_path, monkeypatch))
    root = Path(__file__).resolve().parents[1]
    # No production checkout: unavailable catchup must alert, never silently mean zero work.
    monkeypatch.setenv("DAILYCHECK_PROD_REPO", str(tmp_path / "absent-prod"))
    command = [sys.executable, "-B", str(root / "scripts/prod_agent/pr_poll.py")]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "GitHub ordinary Prod" in result.stdout
    assert f"head={SHA} base={BASE} checks=ready" in result.stdout
    assert "_catchup_block" in result.stdout and "异常" in result.stdout
    monkeypatch.delenv("DAILYCHECK_GITHUB_FINALIZE_TOKEN_FILE")
    missing = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert missing.returncode == 0 and "异常" in missing.stdout and "checks=ready" not in missing.stdout
    proof("poll-cli", {"native_exit": result.returncode, "stdout": result.stdout,
                       "missing_auth_stdout": missing.stdout})


def test_ordinary_poll_real_catchup_and_private_report_failure(api, tmp_path, monkeypatch):
    table = ordinary(api, tmp_path, monkeypatch)
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "prod"
    repo.mkdir()
    bg = repo / "scripts/blue_green"
    bg.mkdir(parents=True)
    shutil.copy2(root / "scripts/blue_green/deploy_state.py", bg / "deploy_state.py")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("add", "scripts/blue_green/deploy_state.py")
    git("commit", "-qm", "base")
    deployed = git("rev-parse", "HEAD")
    (repo / "tiny").write_text("candidate")
    git("add", "tiny")
    git("commit", "-qm", "main")
    main = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", main)
    git("remote", "add", "origin", f"https://github.com/{REPO}.git")
    state = tmp_path / "deployment-state"
    state.mkdir()
    (state / "state.json").write_text(json.dumps({"deployed": {"sha": deployed}}))
    tools = tmp_path / "bin"
    tools.mkdir()
    real_git = shutil.which("git")
    fake = tools / "git"
    fake.write_text(f'''#!{sys.executable}
import os, sys
if "fetch" in sys.argv: sys.exit(0)
os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
''')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("DAILYCHECK_PROD_REPO", str(repo))
    monkeypatch.setenv("DAILYCHECK_DEPLOY_STATE_DIR", str(state))
    events = state / "events.jsonl"
    events.write_text(json.dumps({"hosting_provider": "github", "pr": 160, "sha": SHA,
                                 "publication_rc": 1}) + "\n")
    monkeypatch.setenv("DAILYCHECK_DEPLOY_EVENT_LOG", str(events))
    api.write(table)
    result = subprocess.run([sys.executable, "-B", str(root / "scripts/prod_agent/pr_poll.py")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '"status": "behind"' in result.stdout and main in result.stdout
    assert f"head={SHA} base={BASE} checks=ready" in result.stdout
    assert "report publication failed or unconfirmed" in result.stdout
    proof("poll-catchup", {"native_exit": result.returncode, "stdout": result.stdout,
                           "deployed": deployed, "main": main})
    # A broken schedule helper can exit zero without producing its required facts.
    (bg / "deploy_state.py").write_text("raise SystemExit(0)\n")
    missing = subprocess.run([sys.executable, "-B", str(root / "scripts/prod_agent/pr_poll.py")],
                             capture_output=True, text=True, timeout=30)
    assert missing.returncode == 0 and "invalid or absent facts" in missing.stdout
    proof("poll-no-output", {"native_exit": missing.returncode, "stdout": missing.stdout})


@pytest.fixture
def receiver(api, tmp_path, monkeypatch):
    from scripts.blue_green import webhook_receiver as receiver

    monkeypatch.setattr(receiver, "WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setattr(receiver, "INBOX_PATH", tmp_path / "shadow" / "inbox.jsonl")
    monkeypatch.setattr(receiver, "PROCESSED_PATH", tmp_path / "shadow" / "processed.jsonl")
    monkeypatch.setenv("DAILYCHECK_GITHUB_SHADOW_DIR", str(tmp_path / "shadow"))
    api.write(evidence())
    return receiver


def send(receiver, event, data, delivery="delivery-160", signature=True):
    raw = json.dumps(data).encode()
    sig = (
        "sha256=" + hmac.new(b"test-webhook-secret", raw, hashlib.sha256).hexdigest()
        if signature
        else "bad"
    )
    return receiver.app.test_client().post(
        "/webhook",
        data=raw,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sig,
        },
    )


def native(event):
    data = {"repository": {"id": REPO_ID, "full_name": REPO}, "action": "completed"}
    if event == "pull_request":
        data.update(action="synchronize", number=160, pull_request=pr())
    elif event == "status":
        data.update(sha=SHA, state="pending")
    else:
        data[event] = {"head_sha": SHA, "id": 9, "conclusion": "failure", "pull_requests": []}
    return data


@pytest.mark.parametrize(
    "event", ["pull_request", "check_suite", "check_run", "workflow_run", "status"]
)
def test_native_webhook_signed_association_and_duplicate_intake(receiver, event):
    data = native(event)
    assert send(receiver, event, data, signature=False).status_code == 401
    result = send(receiver, event, data)
    assert result.status_code == 202, result.data
    assert send(receiver, event, data).json["status"] == "duplicate"
    rows = [json.loads(line) for line in receiver.INBOX_PATH.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["pr_head_sha"] == SHA and rows[0]["pr_base_sha"] == BASE
    assert rows[0]["source_delivery_id"] == "delivery-160" and rows[0]["intake_mode"] == "shadow"
    if event != "pull_request":
        assert rows[0]["event"] == "check_suite" and rows[0]["source_event"] == event
        assert rows[0]["check_conclusion"] != "success"


def test_multiple_pr_associations_are_deliberate_and_retry_partial_append_is_idempotent(
    api, receiver, monkeypatch
):
    table = evidence()
    table[ROOT + "/pulls?page=1"] = response([pr(), pr(161)])
    api.write(table)
    real = receiver._append_inbox
    calls = []

    def append(record):
        calls.append(record)
        if len(calls) == 2:
            raise OSError("simulated partial delivery")
        real(record)

    monkeypatch.setattr(receiver, "_append_inbox", append)
    data = native("check_run")
    assert send(receiver, "check_run", data).status_code == 503
    assert send(receiver, "check_run", data).status_code == 202
    assert send(receiver, "check_run", data).json["status"] == "duplicate"
    rows = [json.loads(line) for line in receiver.INBOX_PATH.read_text().splitlines()]
    assert [r["pr_number"] for r in rows] == [160, 161]
    assert len({r["delivery_id"] for r in rows}) == 2


def test_webhook_production_inbox_or_missing_credentials_is_rejected(receiver, monkeypatch):
    monkeypatch.setattr(receiver, "INBOX_PATH", Path("/var/lib/dailycheck-prod-agent/inbox.jsonl"))
    assert send(receiver, "status", native("status")).status_code == 503
    assert not (Path(os.environ["DAILYCHECK_GITHUB_SHADOW_DIR"]) / "inbox.jsonl").exists()


def test_webhook_malformed_or_wrong_repository_and_api_error_are_not_queued(api, receiver):
    data = native("check_run")
    data["repository"]["id"] = 9
    assert send(receiver, "check_run", data).status_code == 400
    assert send(receiver, "check_run", []).status_code == 400
    table = evidence()
    table[ROOT + "/pulls?page=1"] = response([], status=403)
    api.write(table)
    assert send(receiver, "check_run", native("check_run")).status_code == 503
    assert not receiver.INBOX_PATH.exists()


def test_partial_rerun_reuses_latest_success_of_unaffected_job(api):
    table = evidence()
    jobs = table[ROOT + "/actions/runs/40/jobs?page=1"]["data"]["jobs"]
    jobs[0]["run_attempt"] = 1
    # Only reports was rerun in attempt 2; successful test from attempt 1 is inherited.
    result = decide(api, table)
    assert result["ready"], result
    assert result["check_evidence"][0]["run_attempt"] == 1
    assert result["run_attempt"] == 2


def test_latest_job_attempt_pending_overrides_historical_success(api):
    table = evidence()
    envelope = table[ROOT + "/actions/runs/40/jobs?page=1"]["data"]
    old = dict(envelope["jobs"][0])
    old.update(id=99, run_attempt=1)
    envelope["jobs"].append(old)
    envelope["total_count"] = 3
    envelope["jobs"][0].update(status="in_progress", conclusion=None)
    checks = table[ROOT + f"/commits/{SHA}/check-runs?page=1"]["data"]["check_runs"]
    checks[0].update(status="in_progress", conclusion=None)
    result = decide(api, table)
    assert not result["ready"] and result["contexts"]["test"] == "pending"


@pytest.mark.parametrize(
    "raw",
    [
        '{"id":1,"id":1448986421,"full_name":"DUIDUI-Studio/DailyCheck"}',
        '{"id":NaN}',
        '{"id":Infinity}',
    ],
)
def test_invalid_json_evidence_is_rejected(api, raw):
    api.write({ROOT: {"raw": raw}})
    with pytest.raises(HostingError):
        api.client().get(ROOT)


def test_reducer_cannot_admit_even_ready_github_shadow_evidence(api, tmp_path, monkeypatch):
    from scripts.prod_agent.event_reducer import EventReducer, ReducerConfig

    api.write(evidence())
    monkeypatch.setenv("DAILYCHECK_GITHUB_SHADOW_DIR", str(tmp_path))
    reducer = EventReducer(ReducerConfig(db_path=tmp_path / "reducer.sqlite3"))
    result = reducer._readiness_checker(REPO, 160, SHA)
    assert result["ready"] is False and result["reason"] == "github_shadow_only"
    assert result["contexts"] == {"test": "success", "agent-framework-reports": "success"}


@pytest.mark.parametrize(
    "root",
    ["/", "/var", "/var/lib", "/var/lib/dailycheck-prod-agent", "/var/lib/dailycheck-deploy"],
)
def test_shadow_root_cannot_overlap_production_state(api, monkeypatch, root):
    from scripts.hosting import require_shadow_path

    monkeypatch.setenv("DAILYCHECK_GITHUB_SHADOW_DIR", root)
    with pytest.raises(HostingError):
        require_shadow_path(Path(root) / "inbox.jsonl")


@pytest.mark.parametrize(
    "method,status,role,path",
    [
        ("GET", 202, AccessRole.READ, ROOT),
        ("GET", 201, AccessRole.READ, ROOT),
        ("POST", 200, AccessRole.FEEDBACK, "/repos/DUIDUI-Studio/DailyCheck-ops/issues"),
        ("PATCH", 201, AccessRole.REPORT, ROOT + "/issues/comments/1"),
    ],
)
def test_github_unexpected_success_code_is_not_terminal_success(
    api, monkeypatch, method, status, role, path
):
    monkeypatch.setenv("DAILYCHECK_GITHUB_FEEDBACK_TOKEN", "test-credential")
    monkeypatch.setenv("DAILYCHECK_GITHUB_REPORT_TOKEN", "test-credential")
    table = {path: response({"id": 1}, status=status)}
    payload = None
    if role is AccessRole.FEEDBACK:
        repo = "DUIDUI-Studio/DailyCheck-ops"
        monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_REPO", repo)
        monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_REPO_ID", "1449125557")
        monkeypatch.setenv("DAILYCHECK_GITHUB_OPERATIONS_EXPECT_PRIVATE", "true")
        table[f"/repos/{repo}"] = response({
            "id": 1449125557, "full_name": repo, "private": True, "visibility": "private",
            "has_issues": True, "archived": False, "disabled": False,
            "url": f"https://api.github.com/repos/{repo}", "html_url": f"https://github.com/{repo}",
        })
        payload = {"title": "synthetic", "body": "synthetic", "labels": ["user-feedback"]}
    api.write(table)
    with pytest.raises(HostingError) as error:
        api.client(role).request(method, path, payload)
    assert error.value.status == status


@pytest.fixture
def legacy_http(tmp_path):
    import http.server
    import threading

    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({"path": self.path, "auth": self.headers.get("Authorization")})
            if self.headers.get("Authorization") != "token legacy-file-token":
                self.send_error(401)
                return
            data = (
                [
                    {
                        "number": 42,
                        "title": "legacy fixture",
                        "head": {"sha": SHA, "ref": "fix/issue-160-test"},
                        "mergeable": True,
                        "draft": False,
                    }
                ]
                if "/pulls?" in self.path
                else {"statuses": [{"context": "CI / test (pull_request)", "status": "success"}]}
            )
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = tmp_path / "legacy-token"
    token.write_text("legacy-file-token\n")
    try:
        yield f"http://127.0.0.1:{server.server_port}", token, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("selected", [None, "gitea"])
@pytest.mark.parametrize("url_root", ["/", "/forge/"])
def test_legacy_launcher_preserves_source_url_auth_precedence_and_output(
    legacy_http, tmp_path, selected, url_root
):
    import os
    import subprocess

    root = Path(__file__).resolve().parents[1]
    host, token, requests = legacy_http
    source = subprocess.check_output(
        ["git", "show", "origin/main:deploy/scripts/dailycheck_pr_poll.py"], cwd=root, text=True
    )
    reference = tmp_path / "original-poller.py"
    reference.write_text(source)
    env = {
        **os.environ,
        "GITEA_URL": host + url_root,
        "GITEA_API": "http://127.0.0.1:1/ignored-by-legacy",
        "GITEA_TOKEN_FILE": str(token),
        "GITEA_TOKEN": "ignored-direct-token",
        "GITEA_REPO": "ignored/repo",
        "DCPOLL_REPO": "legacy/fixture",
    }
    env.pop("DAILYCHECK_HOSTING_PROVIDER", None)
    if selected is not None:
        env["DAILYCHECK_HOSTING_PROVIDER"] = selected
    before = subprocess.run(
        [sys.executable, str(reference)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    requests.clear()
    current = subprocess.run(
        [sys.executable, str(root / "deploy/scripts/dailycheck_pr_poll.py")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert current.returncode == before.returncode == 0
    assert current.stdout == before.stdout and "30 分钟兜底轮询" in current.stdout
    assert len(requests) == 2
    prefix = url_root.rstrip("/") + "/api/v1/repos/legacy/fixture/"
    assert all(
        r["path"].startswith(prefix) and r["auth"] == "token legacy-file-token" for r in requests
    )


def test_legacy_launcher_explicit_github_selects_canonical_without_gitea(
    api, legacy_http, monkeypatch
):
    import subprocess

    api.write(evidence())
    host, token, requests = legacy_http
    monkeypatch.setenv("GITEA_URL", host + "/forge/")
    monkeypatch.setenv("GITEA_TOKEN_FILE", str(token))
    root = Path(__file__).resolve().parents[1]
    current = subprocess.run(
        [sys.executable, str(root / "deploy/scripts/dailycheck_pr_poll.py")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert current.returncode == 0 and "GitHub shadow-only" in current.stdout
    assert "checks=ready" in current.stdout
    assert not requests
    assert api.log() and all(r["bearer"] for r in api.log())
