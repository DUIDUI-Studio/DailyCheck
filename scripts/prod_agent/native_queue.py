#!/usr/bin/env python3
"""Inert native-G Prod seam in the existing attempt domain.

No CLI, environment activation, installed policy, signer or real-host write
adapter is supplied here. A protected integration must supply authenticated
facts and an isolated signer after separate installation review. The only
concrete transport in this slice is explicitly scoped, credential-free loopback.
"""
from __future__ import annotations

import copy
import io
import json
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blue_green"))
import deploy_state  # noqa: E402
import publication_authority  # noqa: E402

from scripts.ci.merge_group import (  # noqa: E402
    NoRedirect,
    commit,
    digest,
    git,
    sha,
    validate_manifest,
)
from scripts.prod_agent.github_readiness import validate_head_ci, validate_native_ci  # noqa: E402


@dataclass(frozen=True)
class ProducerPolicy:
    actions_app_id: int
    workflow_id: int
    workflow_blob_sha: str
    prod_app_id: int
    prod_installation_id: int
    release_validity: str = "irrevocable_immutable_G"
    minimum_observation_seconds: int = 600
    producer_manifest_blob_sha: str | None = None

    def __post_init__(self):
        sha(self.workflow_blob_sha)
        if self.producer_manifest_blob_sha is not None:
            sha(self.producer_manifest_blob_sha)
        if (any(type(getattr(self, k)) is not int or getattr(self, k) <= 0 for k in (
                "actions_app_id", "workflow_id", "prod_app_id", "prod_installation_id"))
                or self.release_validity != "irrevocable_immutable_G"
                or self.minimum_observation_seconds < 600):
            raise ValueError("native policy pins missing")

    @property
    def sha256(self):
        return digest(asdict(self))


class FactsPort(Protocol):
    def read(self, candidate: dict) -> dict: ...
    def read_head(self, member: dict) -> dict: ...


class GatePort(Protocol):
    def success(self, payload: dict, *, guard_fd: int) -> dict: ...


class PipelinePort(Protocol):
    def execute(self, candidate: dict, *, guard_fd: int, attempt: dict) -> None: ...


class DisabledGate:
    def success(self, payload: dict, *, guard_fd: int) -> dict:
        raise RuntimeError("native Prod signer is not installed; activation is off")


@dataclass(frozen=True)
class LoopbackFixtureScope:
    """Explicit TEST ONLY scope. It carries zero authority at any real host."""
    root: Path
    endpoint: str

    def __post_init__(self):
        root = self.root.resolve()
        parsed = urlsplit(self.endpoint)
        if (not root.is_dir() or root in {Path("/"), Path("/var/lib/dailycheck-deploy")}
                or (root / ".native-queue-fixture").read_text() != "synthetic-owned-fixture\n"
                or parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or parsed.port is None):
            raise ValueError("explicit owned loopback fixture required")

    def get(self, path: str) -> dict:
        request = Request(self.endpoint + path, headers={"X-DailyCheck-Test-Scope": "native-queue"})
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            if response.url != request.full_url or response.status != 200:
                raise ValueError("untrusted fixture response")
            return json.loads(response.read(4 * 1024 * 1024 + 1))


class LoopbackFacts:
    def __init__(self, scope: LoopbackFixtureScope):
        self.scope = scope

    def read(self, candidate: dict) -> dict:
        return self.scope.get("/facts/" + sha(candidate["manifest"]["group_sha"]))

    def read_head(self, member: dict) -> dict:
        return self.scope.get(f"/head-facts/{member['number']}")


class LoopbackGate:
    def __init__(self, scope: LoopbackFixtureScope):
        self.scope = scope

    def success(self, payload: dict, *, guard_fd: int) -> dict:
        # The real HTTP child inherits the kernel fence even if this parent dies.
        code = '''import json,sys,urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl): return None
request=urllib.request.Request(sys.argv[1],data=sys.stdin.buffer.read(),method="POST",
 headers={"Content-Type":"application/json","X-DailyCheck-Test-Scope":"native-queue"})
with urllib.request.build_opener(NoRedirect()).open(request,timeout=30) as response:
 if response.url != request.full_url or response.status != 201: raise ValueError("unexpected fixture response")
 print(response.read(65537).decode())
'''
        result = subprocess.run([sys.executable, "-c", code, self.scope.endpoint + "/checks"],
                                input=json.dumps(payload), text=True, capture_output=True,
                                timeout=35, pass_fds=(guard_fd,), check=True)
        return json.loads(result.stdout)


def candidate(repo: Path, manifest: dict, private_members: list[dict], policy: ProducerPolicy) -> dict:
    """Parse protected binding facts separately from public CI membership."""
    validate_manifest(repo, manifest)
    if not isinstance(private_members, list) or len(private_members) != len(manifest["members"]):
        raise ValueError("private membership missing")
    for public, private in zip(manifest["members"], private_members, strict=True):
        if (set(private) != {"pr", "source", "source_revision", "binding_sha256"}
                or private["pr"] != public["number"]):
            raise ValueError("private composite mapping mismatch")
        publication_authority._validate_binding_entry(
            {"pr": private["pr"], "source": private["source"], "public": deploy_state.PUBLIC_CODE_HOSTING,
             "source_revision": private["source_revision"]})
        for field in ("binding_sha256",):
            if not isinstance(private[field], str) or not re.fullmatch(r"[0-9a-f]{64}", private[field]):
                raise ValueError("private source-map digest missing")
    return {"version": 1, "kind": "github_merge_group", "manifest": copy.deepcopy(manifest),
            "private_members": copy.deepcopy(private_members),
            "private_composite_sha256": digest(private_members), "policy_sha256": policy.sha256}


def head_admission(repo: Path, head: str, checks: list[dict], policy: ProducerPolicy) -> dict:
    """Disjoint producer domain: this API can never release a merge commit."""
    if len(commit(repo, head)[1]) != 1:
        raise ValueError("multi-parent H cannot receive head admission")
    # H proof is validated in validate_native_ci with its authenticated PR and
    # full member facts. This function only constructs the typed admission fact.
    if (not checks or any(row.get("phase") != "H" or row.get("head_sha") != head
                          or row.get("conclusion") != "success" or row.get("app_id") != policy.actions_app_id
                          for row in checks)
            or {row["context"] for row in checks} != {"test", "agent-framework-reports"}):
        raise ValueError("complete trusted H evidence required")
    return {"phase": "H", "head_sha": head, "claim": "queue_eligible", "deployment": False}


class NativeQueue:
    """One production attempt; GitHub alone owns queue ordering and merge."""
    def __init__(self, repo: Path, directory: Path, policy: ProducerPolicy,
                 facts: FactsPort, gate: GatePort | None = None,
                 pipeline: PipelinePort | None = None):
        self.repo, self.directory = repo.resolve(), directory.resolve()
        self.policy, self.facts, self.gate = policy, facts, gate or DisabledGate()
        self.pipeline = pipeline

    @contextmanager
    def fence(self):
        old = {k: os.environ.get(k) for k in (
            "DAILYCHECK_DEPLOY_STATE_DIR", deploy_state.ATTEMPT_FD_ENV, deploy_state.ATTEMPT_ENV)}
        os.environ["DAILYCHECK_DEPLOY_STATE_DIR"] = str(self.directory)
        try:
            with deploy_state.attempt_lock(self.directory) as fd:
                os.set_inheritable(fd, True)
                os.environ[deploy_state.ATTEMPT_FD_ENV] = str(fd)
                yield fd
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def initialize_fixture(self, scope: LoopbackFixtureScope) -> None:
        """No real installer is supplied. Crash between marker/state blocks forever."""
        if (not self.directory.is_relative_to(scope.root.resolve())
                or not self.repo.is_relative_to(scope.root.resolve())
                or self.directory == scope.root.resolve()):
            raise ValueError("native fixture repository and state must be inside owned scope")
        with self.fence(), deploy_state.state_transaction(self.directory):
            state = deploy_state.load_state(self.directory)
            if not deploy_state.native_consistent(self.directory, state):
                raise ValueError("native initialization mismatch")
            if "native_queue" in state:
                return
            initialization = secrets.token_hex(16)
            deploy_state.native_guard(self.directory, {"version": 1, "initialization_id": initialization, "intent_id": None})
            state["native_queue"] = {"version": 1, "initialization_id": initialization, "active": None,
                                     "last_reconciled": None}
            deploy_state.save_state(self.directory, state)

    def _bindings(self, value: dict) -> None:
        if value["private_composite_sha256"] != digest(value["private_members"]):
            raise ValueError("private composite changed")
        for member in value["private_members"]:
            binding = publication_authority.resolve_source_binding(self.directory, member["pr"])
            if (binding.source_revision != member["source_revision"]
                    or binding.binding_sha256 != member["binding_sha256"]
                    or binding.source != tuple(member["source"][k] for k in publication_authority.SOURCE_KEYS)):
                raise ValueError("source binding changed")

    def inspect(self, value: dict, *, front: bool) -> tuple[dict, list[dict]]:
        manifest = value["manifest"]
        if candidate(self.repo, manifest, value["private_members"], self.policy) != value:
            raise ValueError("native candidate shape/identity changed")
        if value["policy_sha256"] != self.policy.sha256:
            raise ValueError("native policy changed")
        self._bindings(value)
        facts = self.facts.read(value)
        if (facts["repository_id"] != 1448986421 or facts["repository_node_id"] != "R_kgDOVl3HNQ"
                or facts["repository"] != "DUIDUI-Studio/DailyCheck"
                or facts["candidate_sha256"] != digest(value)
                or facts["policy_sha256"] != self.policy.sha256
                or facts["manifest"] != manifest or facts["dequeued"] is not False
                or facts["incident"] is not False or facts["observation_complete"] is not True):
            raise ValueError("native candidate changed/dequeued")
        ci = validate_native_ci(self.repo, manifest, self.policy, facts["ci"])
        review = facts["review"]
        if (review["role"] != "independent_prod" or review["verdict"] != "pass"
                or review["candidate_sha256"] != digest(value) or not review["reviewer"]
                or review["reviewer"] == review["writer"]
                or not re.fullmatch(r"[0-9a-f]{64}", review["record_sha256"] or "")):

            raise ValueError("independent Prod review missing")
        if front:
            state = deploy_state.load_state(self.directory)
            predecessor = manifest["expected_predecessor_sha"]
            if (facts["front_entry_id"] != manifest["subject_entry_id"] or facts["main_sha"] != predecessor
                    or facts["production_sha"] != predecessor
                    or (state.get("deployed") or {}).get("sha") != predecessor):
                raise ValueError("awaiting live front/main/deployed predecessor")
        predecessors = manifest["members"][:-1]
        merged = facts["merged_predecessors"]
        if merged != [{"entry_id": m["queue_entry_id"], "pr": m["number"],
                       "merged": True, "merge_sha": m["integration_sha"], "anchored_sha": m["integration_sha"]}
                      for m in predecessors]:
            raise ValueError("predecessor is not merged and anchored")
        return facts, ci

    def admit_head(self, member: dict) -> dict:
        if deploy_state.process_lock_fd() is None or isinstance(self.gate, DisabledGate):
            raise RuntimeError("head admission needs an isolated signer and process fence")
        # Parent count is checked before reading evidence or sending any check.
        if len(commit(self.repo, member["head_sha"])[1]) != 1:
            raise ValueError("multi-parent H cannot receive head admission")
        binding = publication_authority.resolve_source_binding(self.directory, member["number"])
        if binding.source_revision is None:
            raise ValueError("native source revision mapping missing")
        authenticated = self.facts.read_head(member)
        if (authenticated["repository_id"] != 1448986421
                or authenticated["repository_node_id"] != "R_kgDOVl3HNQ"
                or authenticated["member"] != member):
            raise ValueError("head read identity mismatch")
        checks = validate_head_ci(self.repo, member, self.policy, authenticated["ci"])
        receipt = head_admission(self.repo, member["head_sha"], checks, self.policy)
        if authenticated != self.facts.read_head(member):
            raise ValueError("head evidence changed before admission")
        payload = {"name": "dailycheck/prod-gate", "head_sha": member["head_sha"],
                   "status": "completed", "conclusion": "success",
                   "external_id": "H:" + digest({"member": member, "policy": self.policy.sha256,
                                                  "binding": binding.binding_sha256})}
        response = self.gate.success(payload, guard_fd=deploy_state.process_lock_fd())
        if (any(response.get(k) != payload[k] for k in payload)
                or response.get("app_id") != self.policy.prod_app_id
                or response.get("installation_id") != self.policy.prod_installation_id):
            raise ValueError("head admission response uncertain")
        return receipt

    def run(self, value: dict, *, wait_seconds: int = 1800, pipeline_args: tuple[str, ...] = ()) -> str:
        with self.fence():
            self.start(value, pipeline_args=pipeline_args)
            self.seal(value)
            self.grant(value)
            deadline = time.monotonic() + wait_seconds
            while True:
                result = self.reconcile()
                if result == "anchored" or time.monotonic() >= deadline:
                    return result
                time.sleep(min(1, max(0, deadline - time.monotonic())))

    def resume_verified(self) -> dict:
        """Explicit pre-intent recovery; an existing intent can only reconcile."""
        if deploy_state.process_lock_fd() is None:
            raise ValueError("verified recovery requires process fence")
        with deploy_state.state_transaction(self.directory):
            state = deploy_state.load_state(self.directory)
            if not deploy_state.native_consistent(self.directory, state):
                raise ValueError("native state/guard mismatch")
            active, lock = state["native_queue"]["active"], state["lock"]
            if (not active or active["phase"] != "verified" or active["release_grant"] is not None
                    or not lock or lock["attempt_id"] != active["origin_attempt_id"]
                    or lock["generation"] != state["generation"]):
                raise ValueError("only complete pre-intent proof can resume")
            os.environ[deploy_state.ATTEMPT_ENV] = json.dumps({k: lock[k] for k in ("token", "pr", "sha", "session")})
            return copy.deepcopy(active["candidate"])

    def start(self, value: dict, *, pipeline_args: tuple[str, ...] = ()) -> None:
        """Caller holds fence through pipeline, grant and reconciliation."""
        if deploy_state.process_lock_fd() is None:
            raise ValueError("native start requires process fence")
        state = deploy_state.load_state(self.directory)
        if ("native_queue" not in state or not deploy_state.native_consistent(self.directory, state)):
            raise ValueError("protected native initialization is absent")
        self.inspect(value, front=True)
        manifest = value["manifest"]
        # Reuse the existing generation/lease creation, never a second queue.
        import argparse
        with redirect_stdout(io.StringIO()):
            rc = deploy_state._acquire(argparse.Namespace(
            pr=manifest["subject_pr_number"], sha=manifest["group_sha"], session="native-queue",
            phase="native-deploy", deployment_provider="github", deployment_host="github.com",
            deployment_repository="DUIDUI-Studio/DailyCheck"))
        if rc:
            raise ValueError("native admission barrier")
        with deploy_state.state_transaction(self.directory):
            state = deploy_state.load_state(self.directory)
            lock = state["lock"]
            identity = {k: lock[k] for k in ("token", "pr", "sha", "session")}
            os.environ[deploy_state.ATTEMPT_ENV] = json.dumps(identity)
            state["native_queue"]["active"] = {
                "candidate": copy.deepcopy(value), "origin_attempt_id": lock["attempt_id"],
                "origin_generation": lock["generation"], "phase": "deploy", "journal": [],
                "verified_evidence": None, "release_grant": None}
            deploy_state.save_state(self.directory, state)
        if self.pipeline is not None:
            if pipeline_args:
                raise ValueError("protected pipeline does not accept caller commands/options")
            self.pipeline.execute(value, guard_fd=deploy_state.process_lock_fd(), attempt=identity)
        elif isinstance(self.facts, LoopbackFacts):
            # Explicit owned-fixture dispatch. Ordinary Gitea runs its existing
            # pipeline directly; real GitHub G never falls back to this shell.
            subprocess.run(["bash", str(self.repo / "scripts/blue_green/deploy_pipeline.sh"),
                            str(manifest["subject_pr_number"]), manifest["group_sha"],
                            "--native-candidate", *pipeline_args], cwd=self.repo, env=os.environ.copy(),
                           pass_fds=(deploy_state.process_lock_fd(),), check=True)
        else:
            raise RuntimeError("installed protected G pipeline required; no checkout-shell fallback")

    def _validate_proof(self, active: dict, value: dict) -> dict:
        proof = active["verified_evidence"]
        journal = active["journal"]
        if (not isinstance(proof, dict) or proof["candidate_sha256"] != digest(value)
                or proof["evidence_sha256"] != digest({k: v for k, v in proof.items() if k != "evidence_sha256"})
                or proof["verification_sha256"] != digest(journal)
                or proof["verification_result"] != "normal" or proof["review_role"] != "independent_prod"
                or proof["production_sha"] != value["manifest"]["group_sha"]
                or len(journal) != len(deploy_state.NATIVE_STAGES)
                or [r["stage"] for r in journal] != list(deploy_state.NATIVE_STAGES)
                or any(r.get("completed") is not True or r.get("rc") != 0 for r in journal)
                or proof["observation_seconds"] < self.policy.minimum_observation_seconds
                or journal[-1]["completed_monotonic"] - journal[-1]["started_monotonic"] != proof["observation_seconds"]):
            raise ValueError("native verified proof corrupt/incomplete")
        return proof

    def seal(self, value: dict) -> None:
        # Re-read the authenticated host, bindings, latest CI and independent review.
        facts, ci = self.inspect(value, front=False)
        group = value["manifest"]["group_sha"]
        if facts["production_sha"] != group or git(self.repo, "rev-parse", "HEAD") != group:
            raise ValueError("runtime/checkout is not verified G")
        with deploy_state.state_transaction(self.directory):
            state = deploy_state.load_state(self.directory)
            active = deploy_state._native_owned(state)
            if active["phase"] != "deploy" or active["candidate"] != value:
                raise ValueError("native proof phase mismatch")
            journal = active["journal"]
            if (len(journal) != len(deploy_state.NATIVE_STAGES)
                    or [r["stage"] for r in journal] != list(deploy_state.NATIVE_STAGES)
                    or any(r.get("completed") is not True or r.get("rc") != 0 for r in journal)):
                raise ValueError("verification stages missing/skipped/failed")
            observation = journal[-1]
            seconds = observation["completed_monotonic"] - observation["started_monotonic"]
            if seconds < self.policy.minimum_observation_seconds:
                raise ValueError("short observation cannot grant")
            proof = {"candidate_sha256": digest(value), "ci": ci,
                     "review_record_sha256": facts["review"]["record_sha256"], "review_role": "independent_prod",
                     "verification_result": "normal", "verification_sha256": digest(journal),
                     "production_sha": group, "observation_started_at": observation["started_at"],
                     "observation_completed_at": observation["completed_at"],
                     "observation_seconds": seconds, "live_snapshot_sha256": digest(facts)}
            proof["evidence_sha256"] = digest(proof)
            active.update(phase="verified", verified_evidence=proof)
            deploy_state.save_state(self.directory, state)

    def grant(self, value: dict) -> None:
        if isinstance(self.gate, DisabledGate):
            raise RuntimeError("native Prod signer is not installed; activation is off")
        # Last mandatory live re-read before durable intent. It is not a host CAS.
        facts, ci = self.inspect(value, front=False)
        manifest = value["manifest"]
        state = deploy_state.load_state(self.directory)
        predecessor = manifest["expected_predecessor_sha"]
        if (facts["front_entry_id"] != manifest["subject_entry_id"]
                or facts["main_sha"] != predecessor or facts["production_sha"] != manifest["group_sha"]
                or (state.get("deployed") or {}).get("sha") != predecessor):
            raise ValueError("front/runtime/main changed before intent")
        with deploy_state.state_transaction(self.directory):
            state = deploy_state.load_state(self.directory)
            active = deploy_state._native_owned(state)
            proof = self._validate_proof(active, value)
            if (active["candidate"] != value or active["phase"] != "verified"
                    or proof["ci"] != ci or proof["review_record_sha256"] != facts["review"]["record_sha256"]
                    or proof["verification_sha256"] != digest(active["journal"])
                    or active["release_grant"] is not None):
                raise ValueError("native proof stale")
            intent_id = secrets.token_hex(16)
            payload = {"name": "dailycheck/prod-gate", "head_sha": manifest["group_sha"],
                       "status": "completed", "conclusion": "success", "external_id": intent_id}
            grant = {"intent_id": intent_id, "intent_at": deploy_state.now_epoch(),
                     "issuer_app_id": self.policy.prod_app_id,
                     "installation_id": self.policy.prod_installation_id,
                     "group_sha": manifest["group_sha"], "evidence_sha256": proof["evidence_sha256"],
                     "success_request_sha256": digest(payload), "validity": self.policy.release_validity,
                     "last_http_result": "unknown", "check_run_id": None}
            # Guard first: either crash window is admission-blocking, never healed.
            deploy_state.native_guard(self.directory, {"version": 1,
                "initialization_id": state["native_queue"]["initialization_id"], "intent_id": intent_id})
            active.update(phase="grant_intent", release_grant=grant)
            deploy_state.save_state(self.directory, state)
        try:
            response = self.gate.success(payload, guard_fd=deploy_state.process_lock_fd())
            if (response.get("name") != payload["name"] or response.get("head_sha") != payload["head_sha"]
                    or response.get("external_id") != intent_id or response.get("app_id") != self.policy.prod_app_id
                    or response.get("installation_id") != self.policy.prod_installation_id
                    or response.get("conclusion") != "success" or type(response.get("id")) is not int
                    or response["id"] <= 0):
                raise ValueError("grant response uncertain")
        except Exception:
            with deploy_state.state_transaction(self.directory):
                state = deploy_state.load_state(self.directory)
                active = deploy_state._native_owned(state)
                active["phase"] = "grant_uncertain"
                deploy_state.save_state(self.directory, state)
            raise
        with deploy_state.state_transaction(self.directory):
            state = deploy_state.load_state(self.directory)
            active = deploy_state._native_owned(state)
            active["release_grant"].update(last_http_result="accepted", check_run_id=response["id"])
            active["phase"] = "grant_accepted"
            deploy_state.save_state(self.directory, state)

    def reconcile(self) -> str:
        """Recovery reads host/runtime facts under OS fence; never deploys or POSTs."""
        if deploy_state.process_lock_fd() is None:
            raise ValueError("recovery requires process fence")
        state = deploy_state.load_state(self.directory)
        if not deploy_state.native_consistent(self.directory, state):
            raise ValueError("native state/guard mismatch; operator reconciliation required")
        active = state["native_queue"]["active"]
        if active is None:
            return "reconciled" if state["native_queue"]["last_reconciled"] else "idle"
        value, grant, proof = active["candidate"], active["release_grant"], active["verified_evidence"]
        if not grant or not proof:
            return "reconcile_required"
        facts = self.facts.read(value)
        group = value["manifest"]["group_sha"]
        try:
            self._bindings(value)
            self._validate_proof(active, value)
            ci = validate_native_ci(self.repo, value["manifest"], self.policy, facts["ci"])
            valid = (facts["repository_id"] == 1448986421 and facts["repository_node_id"] == "R_kgDOVl3HNQ"
                     and facts["repository"] == "DUIDUI-Studio/DailyCheck"
                     and facts["candidate_sha256"] == digest(value) and facts["policy_sha256"] == self.policy.sha256
                     and proof["candidate_sha256"] == digest(value)
                     and proof["verification_sha256"] == digest(active["journal"])
                     and proof["ci"] == ci and facts["incident"] is False
                     and facts["observation_complete"] is True
                     and facts["merged_pr"] == {"number": value["manifest"]["subject_pr_number"],
                         "head_sha": value["manifest"]["members"][-1]["head_sha"],
                         "merged": True, "merge_sha": group}
                     and facts["merged"] is True and facts["merge_sha"] == group
                     and facts["main_sha"] == group and facts["production_sha"] == group
                     and facts["prod_check"]["head_sha"] == group
                     and facts["prod_check"]["app_id"] == self.policy.prod_app_id
                     and facts["prod_check"]["installation_id"] == self.policy.prod_installation_id
                     and facts["prod_check"]["external_id"] == grant["intent_id"]
                     and facts["prod_check"]["conclusion"] == "success"
                     and facts["prod_check"]["status"] == "completed"
                     and type(facts["prod_check"]["id"]) is int and facts["prod_check"]["id"] > 0
                     and (grant["check_run_id"] is None or grant["check_run_id"] == facts["prod_check"]["id"]))
        except (ValueError, KeyError, TypeError, publication_authority.HostingError):
            valid = False
        if (facts.get("merged") is False and facts.get("main_sha") == value["manifest"]["expected_predecessor_sha"]
                and facts.get("production_sha") == group and facts.get("incident") is False
                and active["phase"] != "incident"):
            return "reconcile_required"
        with deploy_state.state_transaction(self.directory):
            state = deploy_state.load_state(self.directory)
            if (not deploy_state.native_consistent(self.directory, state)
                    or state["native_queue"]["active"] != active):
                raise ValueError("native state changed during reconciliation")
            if not valid or active["phase"] == "incident":
                state["native_queue"]["active"]["phase"] = "incident"
                deploy_state.save_state(self.directory, state)
                return "reconcile_required"
            lock = state["lock"]
            if (not lock or lock["attempt_id"] != active["origin_attempt_id"]
                    or lock["generation"] != active["origin_generation"]
                    or state["generation"] != active["origin_generation"]):
                raise ValueError("native recovery origin mismatch")
            os.environ[deploy_state.ATTEMPT_ENV] = json.dumps({k: lock[k] for k in ("token", "pr", "sha", "session")})
            outcome = deploy_state._mint_outcome(state, lock, group, lock["pr"], deploy_state.now_epoch(),
                                                 "success", 0, "complete", "normal")
            if outcome is None:
                raise ValueError("native outcome issuance failed")
            outcome["candidate"] = copy.deepcopy(value)
            outcome["native_evidence_sha256"] = proof["evidence_sha256"]
            state["deployed"] = {"sha": group, "pr": lock["pr"], "at": deploy_state.now_epoch(),
                                 "generation": lock["generation"], "outcome_id": outcome["attempt_id"]}
            state["last_attempt"], state["lock"] = None, None
            state["native_queue"].update(active=None, last_reconciled={"group_sha": group,
                "outcome_id": outcome["attempt_id"], "intent_id": grant["intent_id"]})
            deploy_state.save_state(self.directory, state)
            # Crash after anchor before guard retirement blocks, without reminting.
            deploy_state.native_guard(self.directory, {"version": 1,
                "initialization_id": state["native_queue"]["initialization_id"], "intent_id": None})
            deploy_state._remove_token_file(self.directory)
        return "anchored"
