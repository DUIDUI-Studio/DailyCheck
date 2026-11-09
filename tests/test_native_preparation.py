"""Owned-fixture behaviour and rejection tests for the native preparation adapter.

These are portable file/process/HTTP controls. They never claim a Linux UID
transition, a real 600-second window, an installed policy or production
acceptance; every synthetic record is explicitly owned-fixture input.
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from scripts.ci.merge_group import digest
from scripts.prod_agent.native_pipeline import literal_tree
from scripts.prod_agent.native_ports import InstalledPolicy, ProtectedReview, ProtectedRuntime
from scripts.prod_agent.native_prepare import (
    NativePreparer,
    PreparationPlan,
    _atomic_write,
    publish_review_result,
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


@pytest.mark.parametrize("damage", ["short", "drift", "route", "fence", "outcome", "epoch",
                                     "sample"])
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
        override = {"observation_seconds": 599}
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
