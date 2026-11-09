"""Owned-fixture behaviour and rejection tests for the native preparation adapter.

These are portable file/process/HTTP controls. They never claim a Linux UID
transition, a real 600-second window, an installed policy or production
acceptance; every synthetic record is explicitly owned-fixture input.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pwd
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from scripts.ci.merge_group import digest
from scripts.prod_agent.native_pipeline import literal_tree
from scripts.prod_agent.native_ports import (
    InstalledPolicy,
    ProtectedInitializer,
    ProtectedReview,
    ProtectedRuntime,
)
from scripts.prod_agent.native_prepare import (
    NativePreparer,
    PreparationPlan,
    _atomic_write,
    publish_review_result,
    render_launcher,
    render_preparation,
)
from scripts.prod_agent.native_queue import NativeQueue, deploy_state
from tests import test_native_live_bridge as bridge_tests

bridge = bridge_tests.bridge
git = bridge_tests.git


def private(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)
    path.chmod(0o600)


def render_policy(b, tmp_path: Path) -> InstalledPolicy:
    """Same protected fixture with the real App registration IDs."""
    value = {**b["value"], "producer": {**b["value"]["producer"],
             "prod_app_id": 5674104, "prod_installation_id": 180015273}}
    path = tmp_path / "policy-render.json"
    private(path, value)
    return InstalledPolicy(path, fixture=b["policy"].fixture)


def plan_document(b, policy: InstalledPolicy, records: Path) -> dict:
    repo = policy.location("repo")
    base = b["base"]
    tree = git(repo, "rev-parse", base + "^{tree}")
    reviewer_uid = int(next(iter(policy.value["reviewers"])))
    try:
        reviewer_gid = pwd.getpwuid(reviewer_uid).pw_gid
    except KeyError:
        reviewer_gid = 65531
    return {
        "version": 1,
        "source_pin": base,
        "source_manifest": {"scripts/prod_agent/native_prepare.py": "a" * 64,
                            "scripts/prod_agent/native_ports.py": "b" * 64},
        "authority": f"/usr/local/lib/dailycheck-native/{base}",
        "python": "/opt/dailycheck-native-control/bin/python3",
        "app_python": "/opt/dailycheck-native-app/bin/python3",
        "prod_app_id": 5674104,
        "prod_installation_id": 180015273,
        "app_uid": policy.value["app_uid"],
        "app_gid": 65532,
        "reviewer_uid": reviewer_uid,
        "reviewer_gid": reviewer_gid,
        "reviewer": policy.value["reviewers"][str(reviewer_uid)],
        "writer": policy.value["writer"],
        "epoch": policy.value["epoch"],
        "scope_approval": "Owner scope approval: local fixture only",
        "baseline": {"source_sha": "e" * 40, "source_outcome_sha256": "c" * 64,
                     "public_sha": base, "tree_sha": tree, "map_sha256": "d" * 64,
                     "map_row": {"source_sha": "e" * 40, "public_sha": base,
                                 "tree_sha": tree}},
        "paths": {
            "source_db": "/var/lib/dailycheck-production/db",
            "source_db_device": 64771, "source_db_inode": 963995,
            "green_db": "/var/lib/dailycheck-green/db",
            "blue_access_log": "/var/log/dailycheck-native-access/blue-access.log",
            "green_access_log": "/var/log/dailycheck-native-access/green-access.log",
            "app_environment_file": "/etc/dailycheck-native/application.env",
            "unit_directory": "/etc/systemd/system",
            "release_root": str(policy.location("release_root")),
            "records_directory": str(records),
            "review_directory": str(policy.location("review_directory")),
            "private_paths": ["/etc/dailycheck-native", str(records),
                              str(policy.location("review_directory"))],
        },
    }


def evidence_and_map(b, records: Path, repo: Path):
    base = b["base"]
    tree, entries = literal_tree(repo, base)
    deployed = {"sha": base, "pr": 1, "at": 1}
    map_doc = {"version": 1, "rows": [{"source_sha": base, "public_sha": base,
                                       "tree_sha": tree}]}
    evidence = {
        "version": 1, "source_sha": base,
        "source_outcome_sha256": digest(deployed), "public_sha": base, "tree_sha": tree,
        "map_sha256": digest(map_doc),
        "map_row": {"source_sha": base, "public_sha": base, "tree_sha": tree},
        "blob_manifest_sha256": digest(entries),
        "exclusions": [".git", ".venv"],
        "continuity": {"processes": "owned-fixture", "routes": "owned-fixture",
                       "database": "owned-fixture"},
        "observed_at": time.time(),
    }
    private(records / "source-public-map.json", map_doc)
    private(records / "baseline-evidence.json", evidence)
    private(records / "source-public-map.json", map_doc)
    return evidence, map_doc, deployed


def records_dir(b) -> Path:
    records = b["root"] / "records"
    records.mkdir(mode=0o700, exist_ok=True)
    return records


def preparer(b, *, policy=None, records=None, client=None):
    policy = policy or b["policy"]
    return NativePreparer(policy, records=records or records_dir(b),
                          repo=policy.location("repo"),
                          client=client or b["controller"].client,
                          observer=b["runtime"].observer)


def test_render_preparation_is_inert_and_complete(tmp_path, bridge):
    b = bridge
    policy = render_policy(b, tmp_path)
    records = records_dir(b)
    document = plan_document(b, policy, records)
    private(records / "preparation-plan.json", document)
    planned = NativePreparer(policy, records=records, repo=policy.location("repo"),
                             observer=b["runtime"].observer).plan()
    assert isinstance(planned, PreparationPlan)
    rendered = render_preparation(planned)
    assert rendered["status"] == "PREPARATION_ONLY_INERT" and rendered["state_mutation"] is False
    units = {row["name"]: row for row in rendered["units"]}
    assert set(units) == {"dailycheck.service", "dailycheck-mcp.service",
                          "dailycheck-green.service", "dailycheck-green-mcp.service"}
    blue = units["dailycheck.service"]["content"]
    green = units["dailycheck-green.service"]["content"]
    assert "--bind 127.0.0.1:8090" in blue and "DRY_RUN=false" in blue
    assert "--bind 127.0.0.1:8091" in green and "DRY_RUN=true" in green
    assert "--port 5100" in units["dailycheck-mcp.service"]["content"]
    assert "--port 5101" in units["dailycheck-green-mcp.service"]["content"]
    assert "ConditionPathIsMountPoint=/var/lib/dailycheck-production/db" in blue
    assert "ConditionPathIsMountPoint" not in green
    assert "s.st_dev,s.st_ino)==(int(sys.argv[2]),int(sys.argv[3]))" in blue
    assert "64771 963995" in blue
    assert f"--reuid={policy.value['app_uid']} --regid=65532 --clear-groups" in blue
    assert ("--bounding-set=-all --inh-caps=-all --ambient-caps=-all "
            "--no-new-privs") in blue
    assert "NoNewPrivileges=yes" in blue and "ProtectHome=yes" in blue
    assert "ProtectSystem=strict" in blue and "PrivateTmp=yes" in blue
    assert "EnvironmentFile=/etc/dailycheck-native/application.env" in blue
    assert "DAILYCHECK_DEPLOY_ATTEMPT" not in blue
    assert rendered["launcher"]["path"] == "/usr/local/sbin/dailycheck-native"
    launcher = rendered["launcher"]["content"]
    assert "/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8" in launcher
    assert " -I -B -c " in launcher and "PYTHONPATH=" not in launcher
    assert "/opt/dailycheck-native-control/bin/python3" in launcher
    assert f"/usr/local/lib/dailycheck-native/{b['base']}" in launcher
    assert len(rendered["dropins_remove"]) == 2
    assert rendered["generated_manifest"][rendered["launcher"]["path"]]["mode"] == "0755"
    for row in rendered["units"]:
        assert rendered["generated_manifest"][row["path"]]["sha256"] == row["sha256"]


@pytest.mark.parametrize("damage", ["python", "authority", "epoch", "reviewer", "app_id",
                                     "source_db", "missing"])
def test_render_preparation_refuses_unresolved_or_unapproved_facts(tmp_path, bridge, damage):
    b = bridge
    policy = render_policy(b, tmp_path)
    document = plan_document(b, policy, records_dir(b))
    if damage == "missing":
        document.pop("epoch")
    elif damage == "python":
        document["python"] = "REQUIRED_UNOBSERVED_CONTROL_PYTHON"
    elif damage == "authority":
        document["authority"] = "/usr/local/lib/dailycheck-native/not-the-pin"
    elif damage == "epoch":
        document["epoch"] = "not an epoch!"
    elif damage == "reviewer":
        document["reviewer"] = document["writer"]
    elif damage == "app_id":
        document["prod_app_id"] = 1
    else:
        document["paths"]["source_db"] = "/root/DailyCheck/db"
    with pytest.raises(ValueError):
        PreparationPlan.from_dict(document)


def test_plan_must_match_installed_policy(tmp_path, bridge):
    b = bridge
    policy = render_policy(b, tmp_path)
    records = records_dir(b)
    document = plan_document(b, policy, records)
    private(records / "preparation-plan.json", document)
    preparer_for_plan = NativePreparer(policy, records=records, repo=policy.location("repo"),
                                       observer=b["runtime"].observer)
    assert preparer_for_plan.plan().source_pin == b["base"]
    document["reviewer"] = "supplied-reviewer"
    private(records / "preparation-plan.json", document)
    with pytest.raises(ValueError, match="does not match installed policy"):
        preparer_for_plan.plan()


def test_export_baseline_creates_literal_release_without_state_change(bridge):
    b = bridge
    policy = b["policy"]
    repo = policy.location("repo")
    records = records_dir(b)
    _, _, deployed = evidence_and_map(b, records, repo)
    state_dir = policy.location("directory")
    state = deploy_state.load_state(state_dir)
    state["deployed"] = deployed
    deploy_state.save_state(state_dir, state)
    shutil.rmtree(policy.location("release_root") / b["base"], ignore_errors=True)
    state_before = (state_dir / deploy_state.STATE_FILENAME).read_bytes()
    head_before = git(repo, "rev-parse", "HEAD")
    runner = NativeQueue(repo, state_dir, policy.producer, None)
    with runner.fence():
        result = preparer(b).export_baseline()
    assert result["result"] == "created" and result["sha"] == b["base"]
    release = policy.location("release_root") / b["base"]
    assert json.loads((release / "release.json").read_text()) == {
        "sha": b["base"], "tree": git(repo, "rev-parse", b["base"] + "^{tree}")}
    assert (release.stat().st_mode & 0o222) == 0
    for row in git(repo, "ls-tree", "-r", b["base"]).splitlines():
        meta, name = row.split("\t", 1)
        oid = meta.split()[2]
        assert (release / name).read_bytes() == subprocess.check_output(
            ["git", "-C", str(repo), "cat-file", "blob", oid])
    assert (state_dir / deploy_state.STATE_FILENAME).read_bytes() == state_before
    assert git(repo, "rev-parse", "HEAD") == head_before
    with runner.fence():
        assert preparer(b).export_baseline()["result"] == "verified"


@pytest.mark.parametrize("damage", ["no-fence", "lock", "initialized", "outcome", "map",
                                     "blob", "existing", "symlink", "exclusion"])
def test_export_baseline_rejects_unproven_baseline(bridge, damage):
    b = bridge
    policy = b["policy"]
    repo = policy.location("repo")
    records = records_dir(b)
    evidence, map_doc, deployed = evidence_and_map(b, records, repo)
    state_dir = policy.location("directory")
    state = deploy_state.load_state(state_dir)
    state["deployed"] = deployed
    deploy_state.save_state(state_dir, state)
    shutil.rmtree(policy.location("release_root") / b["base"], ignore_errors=True)
    if damage == "lock":
        state["lock"] = {"synthetic": "legacy writer"}
        deploy_state.save_state(state_dir, state)
    elif damage == "initialized":
        state["native_queue"] = {"version": 1, "initialization_id": "synthetic",
                                 "active": None, "last_reconciled": None}
        deploy_state.native_guard(state_dir, {"version": 1,
            "initialization_id": "synthetic", "intent_id": None})
        deploy_state.save_state(state_dir, state)
    elif damage == "outcome":
        evidence["source_outcome_sha256"] = "f" * 64
        private(records / "baseline-evidence.json", evidence)
    elif damage == "map":
        map_doc["rows"][0]["public_sha"] = "f" * 40
        private(records / "source-public-map.json", map_doc)
    elif damage == "blob":
        evidence["blob_manifest_sha256"] = "f" * 64
        private(records / "baseline-evidence.json", evidence)
    elif damage == "existing":
        release = policy.location("release_root") / b["base"]
        release.mkdir()
        (release / "release.json").write_text('{"sha": "wrong", "tree": "wrong"}')
    elif damage == "symlink":
        os.symlink("target", repo / "synthetic-link")
        git(repo, "add", "synthetic-link")
        git(repo, "commit", "-qm", "synthetic symlink")
        link = git(repo, "rev-parse", "HEAD")
        tree = git(repo, "rev-parse", link + "^{tree}")
        map_doc["rows"].append({"source_sha": link, "public_sha": link, "tree_sha": tree})
        evidence.update(source_sha=link, public_sha=link, tree_sha=tree,
                        map_sha256=digest(map_doc),
                        map_row={"source_sha": link, "public_sha": link, "tree_sha": tree})
        private(records / "source-public-map.json", map_doc)
        private(records / "baseline-evidence.json", evidence)
    elif damage == "exclusion":
        evidence["exclusions"] = [".github/workflows/ci.yml"]
        private(records / "baseline-evidence.json", evidence)
    runner = NativeQueue(repo, state_dir, policy.producer, None)
    if damage == "no-fence":
        with pytest.raises(ValueError, match="fence"):
            preparer(b).export_baseline()
    else:
        with runner.fence(), pytest.raises(ValueError):
            preparer(b).export_baseline()
    release_root = policy.location("release_root")
    if damage == "existing":
        assert (release_root / b["base"] / "release.json").read_text() == (
            '{"sha": "wrong", "tree": "wrong"}')
    elif damage == "symlink":
        assert not (release_root / git(repo, "rev-parse", "HEAD")).exists()
    else:
        assert not (release_root / b["base"]).exists()


def _baseline_ledger(b, ledger_override=None):
    policy = b["policy"]
    record = b["runtime_record"]
    now = time.time()
    samples = [{"at": now - 600, "processes_sha256": digest(record["processes"]),
                "routes_sha256": digest(record["routes"])},
               {"at": now, "processes_sha256": digest(record["processes"]),
                "routes_sha256": digest(record["routes"])}]
    ledger = {"version": 1, "policy": policy.fingerprint, "epoch": policy.value["epoch"],
              "kind": "baseline", "sha": record["sha"], "tree": record["tree"],
              "processes": record["processes"], "routes": record["routes"],
              "source_outcome_sha256": digest({"sha": b["base"], "pr": 1, "at": 1}),
              "started_at": now - 600, "finished_at": now,
              "started_monotonic": 0.0, "finished_monotonic": 600.0,
              "observation_seconds": 600, "samples": samples, "incident": False}
    ledger.update(ledger_override or {})
    return ledger


def test_publish_runtime_baseline_writes_unchanged_consumer_record(bridge):
    b = bridge
    policy = b["policy"]
    records = records_dir(b)
    _, _, deployed = evidence_and_map(b, records, policy.location("repo"))
    state_dir = policy.location("directory")
    state = deploy_state.load_state(state_dir)
    state["deployed"] = deployed
    deploy_state.save_state(state_dir, state)
    result = preparer(b).publish_runtime_from_ledger(_baseline_ledger(b), kind="baseline")
    assert result["sha"] == b["base"] and result["observation_seconds"] == 600
    assert result["incident"] is False and set(result["routes"]) == {"app", "mcp"}
    assert ProtectedRuntime(policy, observer=b["runtime"].observer).read()["sha"] == b["base"]


@pytest.mark.parametrize("damage", ["short", "wall-clock-only", "drift", "route", "fence",
                                     "outcome", "epoch", "sample"])
def test_publish_runtime_baseline_rejects_unproven_continuity(bridge, damage):
    b = bridge
    policy = b["policy"]
    records = records_dir(b)
    _, _, deployed = evidence_and_map(b, records, policy.location("repo"))
    state_dir = policy.location("directory")
    state = deploy_state.load_state(state_dir)
    state["deployed"] = deployed
    deploy_state.save_state(state_dir, state)
    override = {}
    if damage == "short":
        override = {"observation_seconds": 599, "finished_monotonic": 599.0}
    elif damage == "wall-clock-only":
        now = time.time()
        override = {"started_at": now - 600, "finished_at": now,
                    "started_monotonic": 0.0, "finished_monotonic": 1.0,
                    "observation_seconds": 1}
    elif damage == "drift":
        processes = [dict(row) for row in b["runtime_record"]["processes"]]
        processes[0]["pid"] += 1
        override = {"processes": processes}
    elif damage == "route":
        override = {"routes": {"app": "http://foreign.invalid/health",
                               "mcp": "http://foreign.invalid/health"}}
    elif damage == "outcome":
        override = {"source_outcome_sha256": "f" * 64}
    elif damage == "epoch":
        override = {"epoch": "foreign-epoch"}
    else:
        samples = [dict(row) for row in _baseline_ledger(b)["samples"]]
        samples[0]["processes_sha256"] = "f" * 64
        override = {"samples": samples}
    ledger = _baseline_ledger(b, override)
    before = policy.location("runtime_file").read_bytes()
    runner = NativeQueue(policy.location("repo"), state_dir, policy.producer, None)
    if damage == "fence":
        with runner.fence(), pytest.raises(ValueError, match="fence"):
            preparer(b).publish_runtime_from_ledger(ledger, kind="baseline")
    else:
        with pytest.raises(ValueError):
            preparer(b).publish_runtime_from_ledger(ledger, kind="baseline")
    assert policy.location("runtime_file").read_bytes() == before


def _green_journal(clock=600):
    now = time.time()
    return [{"stage": stage, "started_monotonic": 0,
             "completed_monotonic": clock, "started_at": now - 700,
             "completed_at": now - 100, "completed": True, "rc": 0}
            for stage in deploy_state.NATIVE_STAGES]


def _green_state(b, journal):
    policy = b["policy"]
    state_dir = policy.location("directory")
    state = deploy_state.load_state(state_dir)
    tree = git(policy.location("repo"), "rev-parse", b["base"] + "^{tree}")
    state["native_queue"] = {"version": 1, "initialization_id": "synthetic-green",
                             "active": {"candidate": {"manifest": {
                                 "group_sha": b["base"], "tree_sha": tree}},
                                 "origin_attempt_id": "synthetic", "origin_generation": 0,
                                 "phase": "deploy", "journal": journal,
                                 "verified_evidence": None, "release_grant": None},
                             "last_reconciled": None}
    deploy_state.native_guard(state_dir, {"version": 1,
        "initialization_id": "synthetic-green", "intent_id": None})
    deploy_state.save_state(state_dir, state)


def test_publish_runtime_green_requires_completed_actual_journal(bridge):
    b = bridge
    policy = b["policy"]
    records_dir(b)
    _green_state(b, _green_journal())
    result = preparer(b).publish_runtime_green()
    assert result["sha"] == b["base"] and result["observation_seconds"] == 600
    assert ProtectedRuntime(policy, observer=b["runtime"].observer).read()["sha"] == b["base"]


@pytest.mark.parametrize("damage", ["short", "incomplete", "tree", "guard"])
def test_publish_runtime_green_rejects_incomplete_journal(bridge, damage):
    b = bridge
    policy = b["policy"]
    records_dir(b)
    journal = _green_journal()
    if damage == "short":
        journal = _green_journal(clock=599)
    elif damage == "incomplete":
        journal[-1]["completed"] = False
    _green_state(b, journal)
    if damage == "tree":
        state = deploy_state.load_state(policy.location("directory"))
        state["native_queue"]["active"]["candidate"]["manifest"]["tree_sha"] = "f" * 40
        deploy_state.save_state(policy.location("directory"), state)
    elif damage == "guard":
        (policy.location("directory") / deploy_state.NATIVE_GUARD_FILENAME).unlink()
    with pytest.raises(ValueError):
        preparer(b).publish_runtime_green()


def test_publish_baseline_emits_only_fresh_transition(bridge):
    b = bridge
    policy = b["policy"]
    records = records_dir(b)
    _, _, deployed = evidence_and_map(b, records, policy.location("repo"))
    state_dir = policy.location("directory")
    state = deploy_state.load_state(state_dir)
    state["deployed"] = deployed
    deploy_state.save_state(state_dir, state)
    state_before = (state_dir / deploy_state.STATE_FILENAME).read_bytes()
    runner = NativeQueue(policy.location("repo"), state_dir, policy.producer, None)
    with runner.fence():
        transition = preparer(b).publish_baseline()
    assert set(transition) == {"version", "policy", "epoch", "source_sha", "public_sha",
                               "tree_sha", "map_sha256", "source_outcome_sha256",
                               "runtime_sha256", "observed_at"}
    assert transition["public_sha"] == b["base"]
    assert transition["public_sha"] == policy.record("runtime_file")["sha"]
    assert transition["source_outcome_sha256"] == digest(deployed)
    assert transition["runtime_sha256"] == digest(policy.record("runtime_file"))
    assert (state_dir / deploy_state.STATE_FILENAME).read_bytes() == state_before
    assert policy.record("bootstrap_file") == transition
    assert b["server"].posts == []


@pytest.mark.parametrize("damage", ["main", "stale", "outcome", "initialized"])
def test_publish_baseline_rejects_changed_evidence(bridge, damage):
    b = bridge
    policy = b["policy"]
    records = records_dir(b)
    evidence, _, deployed = evidence_and_map(b, records, policy.location("repo"))
    state_dir = policy.location("directory")
    state = deploy_state.load_state(state_dir)
    state["deployed"] = deployed
    deploy_state.save_state(state_dir, state)
    if damage == "main":
        b["server"].routes[f"/repos/{bridge_tests.REPOSITORY}/branches/main"] = {
            "commit": {"sha": "f" * 40}}
    elif damage == "stale":
        private(policy.location("runtime_file"),
                {**b["runtime_record"], "observed_at": 1})
    elif damage == "outcome":
        evidence["source_outcome_sha256"] = "f" * 64
        private(records / "baseline-evidence.json", evidence)
    else:
        state["native_queue"] = {"version": 1, "initialization_id": "synthetic",
                                 "active": None, "last_reconciled": None}
        deploy_state.native_guard(state_dir, {"version": 1,
            "initialization_id": "synthetic", "intent_id": None})
        deploy_state.save_state(state_dir, state)
    runner = NativeQueue(policy.location("repo"), state_dir, policy.producer, None)
    with runner.fence(), pytest.raises(ValueError):
        preparer(b).publish_baseline()


def test_review_request_and_result_are_exactly_bound(bridge):
    b = bridge
    policy = b["policy"]
    preparer_for_review = preparer(b)
    candidate = {"synthetic": "candidate"}
    ci = {"synthetic": "ci"}
    request = preparer_for_review.prepare_review_request(candidate, ci)
    path = Path(request["request"])
    assert path.parent.name == request["request"].split("/")[-2]
    assert stat_mode(path) == 0o640
    assert stat_mode(path.parent) == 0o750
    (policy.location("review_directory") / str(os.geteuid())).mkdir(mode=0o700)
    record = publish_review_result(policy.location("review_directory"), digest(candidate),
                                   decision="pass")
    assert record["candidate_sha256"] == digest(candidate)
    assert record["ci_sha256"] == digest(ci) and record["verdict"] == "pass"
    assert stat_mode(policy.location("review_directory") / str(os.geteuid())
                     / (digest(candidate) + ".json")) == 0o600
    review = ProtectedReview(policy, fixture_ci=lambda _: ci)
    assert review.read(candidate)["reviewer"] == record["reviewer"]


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.parametrize("damage", ["no-request", "wrong-digest", "fail", "root",
                                     "tamper-ci", "already"])
def test_review_result_rejects_unsafe_or_second_publication(bridge, monkeypatch, damage):
    b = bridge
    policy = b["policy"]
    candidate = {"synthetic": "candidate"}
    ci = {"synthetic": "ci"}
    preparer_for_review = preparer(b)
    preparer_for_review.prepare_review_request(candidate, ci)
    directory = policy.location("review_directory")
    (directory / str(os.geteuid())).mkdir(mode=0o700)
    digest_value = digest(candidate)
    if damage in {"no-request", "wrong-digest"}:
        digest_value = "f" * 64
    elif damage == "fail":
        with pytest.raises(ValueError, match="pass"):
            publish_review_result(directory, digest_value, decision="fail")
        return
    elif damage == "root":
        monkeypatch.setattr(os, "geteuid", lambda: 0)
    elif damage == "tamper-ci":
        request_path = directory / "requests" / str(os.geteuid()) / (digest_value + ".json")
        request = json.loads(request_path.read_text())
        request["expected"]["ci_sha256"] = "f" * 64
        private(request_path, request)
    elif damage == "already":
        publish_review_result(directory, digest_value, decision="pass")
    with pytest.raises(ValueError):
        publish_review_result(directory, digest_value, decision="pass")


def test_atomic_record_publication_keeps_previous_bytes_on_interruption(tmp_path, monkeypatch):
    target = tmp_path / "record.json"
    private(target, {"state": "original"})
    before = target.read_bytes()

    def interrupted(*_args, **_kwargs):
        raise OSError("synthetic interruption")

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(OSError):
        _atomic_write(target, b'{"state":"partial"}', owner=os.geteuid(), mode=0o600,
                      anchor=tmp_path)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".record.json.tmp-*"))


# ── repair-1: CLI fence, initialized-P refresh, launcher controller routing ──

def _setup_baseline(b):
    policy = b["policy"]
    records = records_dir(b)
    _, _, deployed = evidence_and_map(b, records, policy.location("repo"))
    state = deploy_state.load_state(policy.location("directory"))
    state["deployed"] = deployed
    deploy_state.save_state(policy.location("directory"), state)
    return preparer(b)


def _fixture_child_command(b, operation, *extra):
    policy = b["policy"]
    return [str(policy.python), "-I", "-B", "-c",
            "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
            "runpy.run_module('scripts.prod_agent.native_prepare',run_name='__main__')",
            str(policy.authority), operation, "--fixture-root", str(b["root"]),
            "--fixture-endpoint", policy.fixture.endpoint,
            "--fixture-policy", str(policy.path), *extra]


def _write_fixture_processes(b):
    private(b["root"] / "fixture-processes.json", b["runtime_record"]["processes"])


def _initialized_baseline(b):
    """Positive setup: runtime + transition + the unchanged real initializer."""
    policy = b["policy"]
    preparer_obj = _setup_baseline(b)
    _write_fixture_processes(b)
    private(preparer_obj.records / "observation-ledger.json", _baseline_ledger(b))
    preparer_obj.publish_runtime_baseline()
    runner = NativeQueue(policy.location("repo"), policy.location("directory"),
                         policy.producer, None)
    with runner.fence():
        preparer_obj.publish_baseline()
    assert ProtectedInitializer(policy, b["runtime"], b["controller"].client).initialize()
    return preparer_obj


def test_baseline_cli_self_acquires_original_fence(bridge):
    b = bridge
    policy = b["policy"]
    preparer_obj = _setup_baseline(b)
    _write_fixture_processes(b)
    release = policy.location("release_root") / b["base"]
    shutil.rmtree(release, ignore_errors=True)
    result = subprocess.run(_fixture_child_command(b, "export-baseline"),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"] == "created"
    assert (release / "release.json").is_file()
    result = subprocess.run(_fixture_child_command(b, "publish-baseline"),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    transition = json.loads(result.stdout)
    assert transition["public_sha"] == b["base"]
    assert policy.record("bootstrap_file") == transition
    # The existing non-blocking attempt contract rejects a competing holder.
    runner = NativeQueue(policy.location("repo"), policy.location("directory"),
                         policy.producer, None)
    with runner.fence():
        release_before = (release / "release.json").read_bytes()
        bootstrap_before = policy.location("bootstrap_file").read_bytes()
        for operation in ("export-baseline", "publish-baseline"):
            result = subprocess.run(_fixture_child_command(b, operation),
                                    capture_output=True, text=True, timeout=30)
            assert result.returncode == 1
        assert (release / "release.json").read_bytes() == release_before
        assert policy.location("bootstrap_file").read_bytes() == bootstrap_before
    # The owned observer child never receives the fence or application env.
    captured = {}

    class FakeChild:
        returncode = 0

        def communicate(self, timeout=None):
            return ('{"sha": "synthetic"}', "")

        def kill(self):
            pass

    def fake_popen(command, **kwargs):
        captured.update(command=command, options=kwargs)
        return FakeChild()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subprocess, "Popen", fake_popen)
        assert preparer_obj.publish_runtime_process("green") == {"sha": "synthetic"}
    assert captured["options"]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert captured["options"]["close_fds"] is True
    assert "pass_fds" not in captured["options"]
    assert "DAILYCHECK_DEPLOY_ATTEMPT" not in captured["options"]["env"]
    assert "DAILYCHECK_DEPLOY_ATTEMPT_FD" not in captured["options"]["env"]


def test_initialized_matching_p_runtime_refresh(bridge):
    b = bridge
    policy = b["policy"]
    preparer_obj = _initialized_baseline(b)
    refreshed = preparer_obj.publish_runtime_baseline()
    assert refreshed["sha"] == b["base"]
    assert b["runtime"].read()["sha"] == b["base"]
    # Fresh-observation authorization path without a real 600s loop.
    (preparer_obj.records / "observation-ledger.json").unlink()
    observed = preparer_obj.publish_runtime_from_ledger(_baseline_ledger(b), kind="baseline")
    assert observed["sha"] == b["base"]
    state = deploy_state.load_state(policy.location("directory"))
    assert state["native_queue"]["active"] is None
    assert state["native_source_anchor"]["sha"] == b["base"]
    # True stale-record rejection remains in the unchanged consumer.
    current = policy.record("runtime_file")
    private(policy.location("runtime_file"), {**current, "observed_at": time.time() - 121})
    with pytest.raises(ValueError, match="unavailable/stale"):
        b["runtime"].read()


@pytest.mark.parametrize("damage", ["anchor-digest", "anchor-sha", "policy", "epoch",
                                     "deployed", "tree", "active", "partial", "missing-queue",
                                     "guard"])
def test_initialized_mismatch_still_denies_refresh(bridge, damage):
    b = bridge
    policy = b["policy"]
    preparer_obj = _initialized_baseline(b)
    state_dir = policy.location("directory")
    state = deploy_state.load_state(state_dir)
    if damage == "anchor-digest":
        state["native_source_anchor"]["pr"] = 999
    elif damage == "anchor-sha":
        state["native_source_anchor"]["sha"] = "f" * 40
    elif damage == "policy":
        state["native_bootstrap"]["policy"] = "f" * 64
    elif damage == "epoch":
        state["native_bootstrap"]["epoch"] = "foreign-epoch"
    elif damage == "deployed":
        state["deployed"]["sha"] = "f" * 40
    elif damage == "tree":
        state["native_bootstrap"]["tree_sha"] = "f" * 40
    elif damage == "active":
        state["native_queue"]["active"] = {"phase": "deploy", "origin_attempt_id": "x",
                                           "origin_generation": 0, "candidate": {},
                                           "journal": [], "verified_evidence": None,
                                           "release_grant": None}
    elif damage == "partial":
        state.pop("native_bootstrap")
    elif damage == "missing-queue":
        state.pop("native_queue")
    else:
        (state_dir / deploy_state.NATIVE_GUARD_FILENAME).unlink()
    deploy_state.save_state(state_dir, state)
    before = policy.location("runtime_file").read_bytes()
    with pytest.raises(ValueError):
        preparer_obj.publish_runtime_from_ledger(_baseline_ledger(b), kind="baseline")
    assert policy.location("runtime_file").read_bytes() == before


def test_rendered_launcher_routes_fixed_controller_lifecycle(bridge):
    b = bridge
    policy = b["policy"]
    plan = dataclasses.replace(
        PreparationPlan.from_dict(plan_document(b, policy, records_dir(b))),
        python=policy.python, authority=policy.authority)
    launcher = b["root"] / "fixture-native-launcher"
    launcher.write_text(render_launcher(plan))
    launcher.chmod(0o700)
    for operation, extra in (("status", []), ("initialize", []),
                             ("admit-head", ["--pr", "1"]), ("deploy-front", []),
                             ("recover", [])):
        result = subprocess.run(["/bin/sh", str(launcher), operation, *extra],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 1, (operation, result.stderr)
        assert "controller policy unavailable" in result.stderr
        assert "invalid choice" not in result.stderr
    fixture_args = ["--fixture-root", str(b["root"]),
                    "--fixture-endpoint", policy.fixture.endpoint,
                    "--fixture-policy", str(policy.path)]
    result = subprocess.run(["/bin/sh", str(launcher), "status", *fixture_args],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "uninitialized"
    result = subprocess.run(["/bin/sh", str(launcher), "recover", *fixture_args],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and "invalid choice" not in result.stderr
    result = subprocess.run(["/bin/sh", str(launcher), "bogus"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 2 and "invalid choice" in result.stderr
    result = subprocess.run(["/bin/sh", str(launcher), "status", "--policy", "/tmp/evil"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 2 and "unrecognized arguments" in result.stderr
    result = subprocess.run(["/bin/sh", str(launcher), "--help"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert "publish-runtime" in result.stdout and "deploy-front" in result.stdout


# ── repair-2: baseline publication binding and commit ordering ────────────

def _distinct_source_baseline(b, *, initialize=True):
    """Adapt the R1 review's real initializer with original source != public P."""
    policy = b["policy"]
    obj = _setup_baseline(b)
    _write_fixture_processes(b)
    source = {"sha": "e" * 40, "pr": 158, "at": 1}
    evidence = obj._record("baseline-evidence.json")
    mapping = obj._record("source-public-map.json")
    mapping["rows"][0]["source_sha"] = source["sha"]
    evidence.update(source_sha=source["sha"], source_outcome_sha256=digest(source),
                    map_sha256=digest(mapping), map_row=mapping["rows"][0])
    private(obj.records / "source-public-map.json", mapping)
    private(obj.records / "baseline-evidence.json", evidence)
    directory = policy.location("directory")
    with deploy_state.state_transaction(directory):
        state = deploy_state.load_state(directory)
        state["deployed"] = source
        deploy_state.save_state(directory, state)
    ledger = _baseline_ledger(b, {"source_outcome_sha256": digest(source)})
    private(obj.records / "observation-ledger.json", ledger)
    obj.publish_runtime_baseline()
    if initialize:
        runner = NativeQueue(policy.location("repo"), directory, policy.producer, None)
        with runner.fence():
            obj.publish_baseline()
        assert ProtectedInitializer(policy, b["runtime"], b["controller"].client).initialize()
        state = deploy_state.load_state(directory)
        assert state["native_source_anchor"] == source
        assert state["deployed"]["sha"] == b["base"] != source["sha"]
    return obj, ledger


def _runtime_child(b):
    result = subprocess.run(_fixture_child_command(b, "publish-runtime", "--kind", "baseline"),
                            capture_output=True, text=True, timeout=30)
    print(json.dumps({"operation": "publish-runtime --kind baseline",
                      "native_exit": result.returncode, "stderr": result.stderr.strip()}))
    return result


def test_uninitialized_distinct_p_runtime_child(bridge):
    b = bridge
    _distinct_source_baseline(b, initialize=False)
    directory = b["policy"].location("directory")
    state_before = (directory / deploy_state.STATE_FILENAME).read_bytes()
    result = _runtime_child(b)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sha"] == b["base"]
    assert b["runtime"].read()["sha"] == b["base"]
    assert (directory / deploy_state.STATE_FILENAME).read_bytes() == state_before
    assert not (directory / deploy_state.NATIVE_GUARD_FILENAME).exists()


@pytest.mark.parametrize("damage", ["active", "policy", "guard"])
def test_baseline_runtime_child_rejects_mid_probe_invalidation(bridge, damage):
    """Same owned health-peer schedules as R1, asserting the desired native effects."""
    b = bridge
    policy = b["policy"]
    _distinct_source_baseline(b)
    directory = policy.location("directory")
    initial = deploy_state.load_state(directory)
    state_before = (directory / deploy_state.STATE_FILENAME).read_bytes()
    guard = directory / deploy_state.NATIVE_GUARD_FILENAME
    guard_before = guard.read_bytes()
    valid = _runtime_child(b)
    assert valid.returncode == 0 and json.loads(valid.stdout)["sha"] == b["base"]
    assert b["runtime"].read()["sha"] == b["base"]
    assert (directory / deploy_state.STATE_FILENAME).read_bytes() == state_before
    assert guard.read_bytes() == guard_before
    before = policy.location("runtime_file").read_bytes()

    def invalidate():
        with deploy_state.state_transaction(directory):
            state = deploy_state.load_state(directory)
            if damage == "active":
                # The accepted R1 control models the coherent native-start write;
                # no deployment stages or state-machine success are manufactured.
                state["native_queue"]["active"] = {
                    "phase": "deploy", "origin_attempt_id": "owned-race", "origin_generation": 0,
                    "candidate": {"manifest": {"group_sha": b["head"], "tree_sha": git(
                        policy.location("repo"), "rev-parse", b["head"] + "^{tree}")}},
                    "journal": [], "verified_evidence": None, "release_grant": None}
            elif damage == "policy":
                state["native_bootstrap"]["policy"] = "f" * 64
            else:
                guard.unlink()
            deploy_state.save_state(directory, state)

    invalidate()
    entry = _runtime_child(b)
    assert entry.returncode == 1
    assert policy.location("runtime_file").read_bytes() == before
    with deploy_state.state_transaction(directory):
        deploy_state.save_state(directory, initial)
        deploy_state.native_guard(directory, {"version": 1,
            "initialization_id": initial["native_queue"]["initialization_id"], "intent_id": None})
    fired = []

    def health(_auth):
        if not fired:
            invalidate()
            fired.append(True)
        return 200, b'{"ok":true}'

    b["server"].health_handler = health
    try:
        result = _runtime_child(b)
    finally:
        b["server"].health_handler = None
    unchanged = policy.location("runtime_file").read_bytes() == before
    print(json.dumps({"schedule": damage, "valid_native_exit": valid.returncode,
                      "entry_native_exit": entry.returncode, "mid_probe_native_exit": result.returncode,
                      "old_runtime_preserved": unchanged, "health_writer_completed": bool(fired)}))
    assert fired and result.returncode == 1
    assert unchanged
    final = deploy_state.load_state(directory)
    assert final["native_source_anchor"] == initial["native_source_anchor"]
    if damage == "active":
        assert deploy_state.native_consistent(directory, final)
        assert final["native_queue"]["active"]["phase"] == "deploy"


def test_baseline_runtime_replace_orders_real_lease_writer(bridge, monkeypatch):
    """Start the actual acquire caller in the final check-to-os.replace interval."""
    b = bridge
    policy = b["policy"]
    obj, ledger = _distinct_source_baseline(b)
    directory = policy.location("directory")
    target = policy.location("runtime_file")
    before = target.read_bytes()
    generation = deploy_state.load_state(directory)["generation"]
    # Only the child's flock is instrumented: try the same kernel lock without
    # blocking first, then retain the real blocking semantics of _write_lock.
    script = r'''
import argparse, contextlib, fcntl, hashlib, io, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.prod_agent.native_queue import deploy_state
directory, runtime, candidate = map(Path, sys.argv[2:])
os.environ['DAILYCHECK_DEPLOY_STATE_DIR'] = str(directory)
original = fcntl.flock
def emit(value):
    print(json.dumps(value), file=sys.__stdout__, flush=True)
def flock(fd, operation):
    if operation == fcntl.LOCK_EX:
        try:
            original(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            emit({'event': 'blocked-at-state-lock'})
            original(fd, operation)
        emit({'event': 'acquired-state-lock',
              'runtime_sha256': hashlib.sha256(runtime.read_bytes()).hexdigest()})
        return
    return original(fd, operation)
fcntl.flock = flock
args = argparse.Namespace(pr=1, sha=str(candidate), session='owned-commit-order', phase='native-deploy')
with contextlib.redirect_stdout(io.StringIO()):
    # Keep flock events visible while suppressing the fixture lease token.
    rc = deploy_state.cmd_acquire(args)
print(json.dumps({'event': 'lease-result', 'native_exit': rc}), flush=True)
raise SystemExit(rc)
'''
    original_replace = os.replace
    captured = {}
    child = None

    def replace(source, destination):
        nonlocal child
        if Path(destination) == target:
            captured["published_sha256"] = hashlib.sha256(Path(source).read_bytes()).hexdigest()
            child = subprocess.Popen(
                [str(policy.python), "-I", "-B", "-c", script, str(policy.authority),
                 str(directory), str(target), b["head"]],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, close_fds=True)
            assert select.select([child.stdout], [], [], 10)[0], "writer did not reach state lock"
            captured["first_event"] = json.loads(child.stdout.readline())
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    try:
        result = obj.publish_runtime_from_ledger(ledger, kind="baseline")
    finally:
        if child is not None:
            stdout, stderr = child.communicate(timeout=30)
    assert child is not None and child.returncode == 0, stderr
    events = [captured["first_event"], *[json.loads(row) for row in stdout.splitlines()]]
    print(json.dumps({"linearization": "runtime os.replace", "writer_native_exit": child.returncode,
                      "events": events, "published_sha256": captured["published_sha256"]}))
    assert events[0] == {"event": "blocked-at-state-lock"}
    assert events[1] == {"event": "acquired-state-lock",
                         "runtime_sha256": captured["published_sha256"]}
    assert events[2] == {"event": "lease-result", "native_exit": 0}
    assert target.read_bytes() != before
    assert result["sha"] == b["base"] == b["runtime"].read()["sha"]
    state = deploy_state.load_state(directory)
    assert state["generation"] == generation + 1 and state["lock"]["sha"] == b["head"]
    assert deploy_state.native_consistent(directory, state)
