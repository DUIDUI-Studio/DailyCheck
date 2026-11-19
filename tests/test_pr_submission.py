import json
import shlex
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.agent_framework import pr_submission as gate

HEAD = "a" * 40
PR = {
    "number": 31, "state": "OPEN", "isDraft": True, "isCrossRepository": False,
    "baseRefName": "main", "headRefName": "feat/issue-30-shipment",
    "headRefOid": HEAD, "closingIssuesReferences": [
        {"url": f"https://github.com/{gate.CODE_REPO}/issues/30"},
    ],
}
BINDING = {
    "pr": 31,
    "public": {"provider": "github", "host": "github.com", "repository_id": gate.CODE_REPO_ID,
               "repository": gate.CODE_REPO},
    "source": {"provider": "github", "host": "github.com", "repository_id": gate.OPS_REPO_ID,
               "repository": gate.OPS_REPO, "kind": "issue", "number": 4},
    "issue_identities": {"public": {"number": 30, "id": 10030}, "source": {"id": 20004}},
}
ARGS = SimpleNamespace(pr=31, issue=30, ops_issue=4, expect_head=HEAD)


@pytest.fixture
def fixture(monkeypatch):
    data = SimpleNamespace(pr=deepcopy(PR), binding=deepcopy(BINDING), calls=[])

    def lookup(*args):
        data.calls.append(args)
        resource = args[-1]
        for repo, number, identity in [(gate.CODE_REPO, 30, 10030), (gate.OPS_REPO, 4, 20004)]:
            if resource == f"/repos/{repo}/issues/{number}":
                return {"number": number, "id": identity, "state": "open",
                        "repository_url": f"https://api.github.com/repos/{repo}"}
        raise AssertionError(args)

    def pr(number):
        assert number == 31
        return deepcopy(data.pr)

    def run(*args):
        data.calls.append(args)
        if args[:3] == ("gh", "pr", "ready"):
            data.pr["isDraft"] = False
        return "https://github.com/DUIDUI-Studio/DailyCheck/pull/31"

    monkeypatch.setattr(gate, "_json", lookup)
    monkeypatch.setattr(gate, "_pr", pr)
    monkeypatch.setattr(gate, "_binding", lambda *_: data.binding)
    monkeypatch.setattr(gate, "_run", run)
    return data


@pytest.mark.parametrize("failure", [
    "missing-public-link", "wrong-public-repo", "wrong-head", "fork", "missing-binding",
    "wrong-ops", "wrong-issue-id", "other-pr", "missing-ops-issue",
])
def test_ready_blocks_before_github_mutation(fixture, monkeypatch, failure):
    if failure == "missing-public-link":
        fixture.pr["closingIssuesReferences"] = []
    elif failure == "wrong-public-repo":
        fixture.pr["closingIssuesReferences"] = [{"url": "https://github.com/example/fork/issues/30"}]
    elif failure == "wrong-head":
        fixture.pr["headRefOid"] = "b" * 40
    elif failure == "fork":
        fixture.pr["isCrossRepository"] = True
    elif failure == "missing-binding":
        fixture.binding = {"error": "private_source_binding_missing"}
    elif failure == "wrong-ops":
        fixture.binding["source"]["number"] = 30
    elif failure == "wrong-issue-id":
        fixture.binding["issue_identities"]["source"]["id"] += 1
    elif failure == "other-pr":
        fixture.binding["pr"] = 30
    else:
        lookup = gate._json

        def missing(*args):
            if args[-1] == f"/repos/{gate.OPS_REPO}/issues/4":
                raise gate.SubmissionBlocked("404")
            return lookup(*args)

        monkeypatch.setattr(gate, "_json", missing)
    with pytest.raises(gate.SubmissionBlocked):
        gate.submit_for_review(ARGS)
    assert not any(call[:3] == ("gh", "pr", "ready") for call in fixture.calls)


def test_ready_changes_draft_only_once(fixture):
    assert gate.submit_for_review(ARGS).endswith("/pull/31")
    assert gate.submit_for_review(ARGS).endswith("/pull/31")
    assert fixture.calls.count(("gh", "pr", "ready", "31", "-R", gate.GITHUB_REPO)) == 1


def test_head_change_before_mutation_is_rejected(fixture, monkeypatch):
    prs = iter([PR, {**PR, "headRefOid": "b" * 40}])
    monkeypatch.setattr(gate, "_pr", lambda *_: next(prs))
    with pytest.raises(gate.SubmissionBlocked):
        gate.submit_for_review(ARGS)
    assert not any(call[:3] == ("gh", "pr", "ready") for call in fixture.calls)


def test_draft_requires_real_issues_and_draft_flag(fixture, tmp_path):
    body = tmp_path / "body.md"
    args = SimpleNamespace(issue=30, ops_issue=4, body_file=body,
                           branch="feat/issue-30-shipment", title="Test")
    body.write_text("No-Issue: skip\n")
    with pytest.raises(gate.SubmissionBlocked, match="Closes #30"):
        gate.prepare_draft(args)
    body.write_text("```\nCloses #30\n```\n")
    with pytest.raises(gate.SubmissionBlocked, match="Closes #30"):
        gate.prepare_draft(args)
    body.write_text("    Closes #30\n```\nCloses #30\n```\n")
    with pytest.raises(gate.SubmissionBlocked, match="Closes #30"):
        gate.prepare_draft(args)
    assert not fixture.calls
    body.write_text("Closes #30\n")
    gate.prepare_draft(args)
    assert [call[-1] for call in fixture.calls[:2]] == [
        f"/repos/{gate.CODE_REPO}/issues/30", f"/repos/{gate.OPS_REPO}/issues/4",
    ]
    assert fixture.calls[-1][:3] == ("gh", "pr", "create")
    assert "--draft" in fixture.calls[-1]


def test_binding_reads_owned_paired_terminal_and_closes_on_error(monkeypatch):
    calls = []

    def orca(*args):
        calls.append(args)
        if args[0] == "create":
            return {"terminal": {"handle": "test-terminal"}}
        if args[0] == "read":
            raise gate.SubmissionBlocked("read failed")
        return {}

    monkeypatch.setattr(gate, "_orca", orca)
    with pytest.raises(gate.SubmissionBlocked, match="read failed"):
        gate._binding(31)
    assert gate.STATE in calls[0][-1]
    assert calls[-1] == ("close", "--terminal", "test-terminal")


def test_binding_reads_a_fresh_marker(monkeypatch):
    captured = {}

    def orca(*args):
        if args[0] == "create":
            _, marker, path, pr = shlex.split(args[-1])[-4:]
            captured["marker"] = marker
            assert (path, pr) == (gate.STATE, "31")
            return {"terminal": {"handle": "term-created"}}
        if args[0] == "read":
            return {"terminal": {"tail": [
                "DAILYCHECK_BINDING_stale=" + json.dumps({"pr": 99}),
                captured["marker"] + json.dumps(BINDING),
            ]}}
        assert args == ("close", "--terminal", "term-created")
        return {}

    monkeypatch.setattr(gate, "_orca", orca)
    assert gate._binding(31) == BINDING


@pytest.mark.parametrize("corrupt", [
    {**BINDING, "pr": 31.0},
    {**BINDING, "source_revision": None},
    {**BINDING, "unknown": True},
])
def test_paired_probe_uses_production_schema_for_entire_file(corrupt):
    scope = {}
    exec(gate._probe_schema(), scope)
    check = scope["checked_document"]
    valid = {"version": 1, "bindings": [BINDING]}
    assert check(json.dumps(valid).encode()) == valid
    for rows in ([corrupt], [BINDING, {**corrupt, "pr": 22.0}]):
        with pytest.raises(scope["HostingError"]):
            check(json.dumps({"version": 1, "bindings": rows}).encode())
