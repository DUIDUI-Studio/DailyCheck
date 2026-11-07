"""Producer boundary tests. Synthetic facts below test parsers, not isolation.

The opt-in fixture drives the literal approved native workflow entrypoint on the
Coordinator-owned runtime. It is never an application full-suite invocation.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

import pytest
import yaml

from scripts.ci.trusted_producer import (
    attest,
    blob_id,
    digest,
    identity,
    make_test_envelope,
    parse_manifest,
    validate_producer,
)

ROOT = Path(__file__).resolve().parents[1]


def approved():
    raw = (ROOT / "scripts/ci/producer_manifest.json").read_bytes()
    pin = blob_id(raw)
    return parse_manifest(raw, pin), pin


def test_workflow_pins_executed_closure_independently_of_candidate():
    manifest, pin = approved()
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert re.findall(r"CI_PRODUCER_MANIFEST_BLOB: ([0-9a-f]{40})", workflow) == [pin, pin]
    assert ".github/workflows/ci.yml" not in manifest["files"]
    assert "scripts/ci/producer_manifest.json" not in manifest["files"]
    for path, expected in manifest["files"].items():
        assert blob_id((ROOT / path).read_bytes()) == expected, path
    value = yaml.load(workflow, Loader=yaml.BaseLoader)
    for context, job in value["jobs"].items():
        native = next(s for s in job["steps"] if s["name"] == "Execute approved isolated native producer")
        assert native["env"]["NATIVE_CONTEXT"] == context
        assert "python3 -I" in native["run"] and "git\", \"cat-file\", \"blob" in native["run"]
        for step in job["steps"]:
            if step is native or "run" not in step:
                continue
            if any(p in step["run"] for p in ("scripts/ci/", "scripts/check_known_failures.py", "scripts/agent_framework/", "venv/bin/", "-m pytest")):
                assert "env.CI_NATIVE_PRODUCER != 'true'" in step.get("if", ""), step["name"]


def synthetic_proof():
    """Parser input only. Actual OS provenance is tested by the opt-in fixture."""
    manifest, pin = approved()
    binding = {"version": 3, "phase": "H", "CI_REPOSITORY": "DUIDUI-Studio/DailyCheck",
               "CI_RUN_ID": "41", "CI_RUN_ATTEMPT": "1", "CI_EVENT": "pull_request",
               "CI_HEAD_SHA": "1" * 40, "CI_EVENT_SHA": "2" * 40, "CI_BASE_SHA": "3" * 40,
               "tested_sha": "1" * 40, "CI_GROUP_SHA": None, "member_manifest_sha256": None,
               "workflow_path": ".github/workflows/ci.yml", "workflow_blob_sha": "4" * 40}
    session = {"schema_version": 1, "pytest_exit": 0, "selected": 2, "selected_unique": 2,
               "completed": 2, "uncompleted": [], "unexpected": [], "failed_nodeids": [], "error_nodeids": [],
               "counts": {"passed": 2, "failed": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "error": 0}}
    raw = b".. [100%]\n2 passed in 0.01s\n"
    import hashlib

    capture = {"version": 1, "container_id": "5" * 64, "image": manifest["runtime"]["image"],
               "kind": "pytest", "context": "test", "binding_sha256": digest(binding),
               "native_exit": 0, "stdout_sha256": hashlib.sha256(raw).hexdigest(),
               "session_sha256": digest(session), "exited": True, "removed": True, "oom_killed": False}
    envelope = make_test_envelope(raw, session, capture, binding)
    producer = {"identity": identity(manifest, pin), "test_capture": capture,
                "lint_capture": {**capture, "kind": "lint", "session_sha256": None}, "gates": []}
    return manifest, pin, envelope, producer


@pytest.mark.parametrize("damage", ["none", "missing", "manifest", "closure", "runtime", "native",
                                    "stdout", "session", "attempt", "run", "live", "removed", "oom", "lint"])
def test_native_provenance_rejects_foreign_or_rewritten_capture(damage):
    manifest, pin, envelope, producer = synthetic_proof()
    if damage == "missing":
        producer = None
    elif damage in {"manifest", "closure"}:
        producer["identity"][{"manifest": "manifest_blob_sha", "closure": "closure_sha256"}[damage]] = "0" * (40 if damage == "manifest" else 64)
    elif damage == "runtime":
        producer["identity"]["runtime"] = {**manifest["runtime"], "image": "sha256:" + "0" * 64}
    elif damage == "native":
        producer["test_capture"]["native_exit"] = 1
    elif damage in {"stdout", "session"}:
        producer["test_capture"][damage + "_sha256"] = "0" * 64
    elif damage in {"attempt", "run"}:
        envelope["binding"]["CI_RUN_ATTEMPT" if damage == "attempt" else "CI_RUN_ID"] = "2"
    elif damage in {"live", "removed", "oom"}:
        producer["test_capture"][{"live": "exited", "removed": "removed", "oom": "oom_killed"}[damage]] = damage == "oom"
    elif damage == "lint":
        producer["lint_capture"]["native_exit"] = 1
    if damage == "none":
        validate_producer(producer, manifest, pin, envelope, "test")
    else:
        with pytest.raises(ValueError):
            validate_producer(producer, manifest, pin, envelope, "test")


def test_reports_links_exact_native_capture_and_real_gate_exits():
    manifest, pin, envelope, producer = synthetic_proof()
    members = [{"number": 1, "head_sha": "1" * 40, "body_sha256": "6" * 64}]
    test = attest(envelope, members, "test", producer)
    producer = copy.deepcopy(producer)
    producer["gates"] = [{**producer["test_capture"], "kind": kind, "context": "agent-framework-reports",
                          "session_sha256": None} for kind in ("report-generate", "report-verify")]
    validate_producer(producer, manifest, pin, envelope, "agent-framework-reports")
    reports = attest(envelope, members, "agent-framework-reports", producer)
    assert reports["pytest_source"]["artifact_sha256"] == test["artifact_sha256"]
    assert reports["producer"]["test_capture"] == test["producer"]["test_capture"]
    for damage in ("missing", "failed"):
        bad = copy.deepcopy(producer)
        if damage == "missing":
            bad["gates"].pop()
        else:
            bad["gates"][1]["native_exit"] = 1
        with pytest.raises(ValueError):
            validate_producer(bad, manifest, pin, envelope, "agent-framework-reports")


def test_changed_producer_requires_new_explicit_pin():
    manifest, old_pin = approved()
    updated = copy.deepcopy(manifest)
    updated["files"]["scripts/ci/trusted_producer.py"] = "a" * 40
    raw = json.dumps(updated, sort_keys=True).encode()
    with pytest.raises(ValueError, match="unapproved"):
        parse_manifest(raw, old_pin)
    new_pin = blob_id(raw)
    assert parse_manifest(raw, new_pin) == updated
    # Parsing a proposed pin does not install it. The protected policy and
    # approved workflow must both be updated independently after review.
    assert new_pin not in (ROOT / ".github/workflows/ci.yml").read_text()


@pytest.mark.skipif(not os.environ.get("DAILYCHECK_NATIVE_PRODUCER_FIXTURE_DRIVER"),
                    reason="requires exact Coordinator-owned local runtime grant")
def test_actual_isolated_producer_fixture():
    import runpy

    # The owned driver is kept with command/log evidence, not an installed tool.
    runpy.run_path(os.environ["DAILYCHECK_NATIVE_PRODUCER_FIXTURE_DRIVER"], run_name="__main__")
