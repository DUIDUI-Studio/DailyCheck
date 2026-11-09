"""Installed protected H/G controller. Installation/activation remains separate."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

from scripts.ci.merge_group import REPOSITORY, QueueReader, digest, sha, validate_manifest
from scripts.hosting import AccessRole, HostingClient, HostingConfig, Provider
from scripts.prod_agent.github_readiness import NativeGitHubFacts
from scripts.prod_agent.native_gate import GitHubGProdGate, GitHubProdGate
from scripts.prod_agent.native_pipeline import ProtectedPipeline
from scripts.prod_agent.native_ports import (
    AppSignerIdentity,
    InstalledPolicy,
    ProtectedInitializer,
    ProtectedReview,
    ProtectedRuntime,
)
from scripts.prod_agent.native_prepare import NativePreparer
from scripts.prod_agent.native_queue import (
    NativeQueue,
    candidate,
    deploy_state,
    publication_authority,
)

POLICY_PATH = Path("/etc/dailycheck-native/policy.json")


class NativeController:
    def __init__(self, policy, *, client=None, queue_reader=None, runtime=None):
        self.policy = policy
        if policy.fixture is None and any(x is not None for x in (client, queue_reader, runtime)):
            raise ValueError("installed authority ports cannot be replaced")
        self.client = client or HostingClient(HostingConfig(Provider.GITHUB, "https://api.github.com",
            REPOSITORY, AccessRole.READ, policy.read_token(), actions_app_id=policy.producer.actions_app_id))
        self.queue_reader = queue_reader or QueueReader(policy.read_token())
        self.runtime = runtime or ProtectedRuntime(policy)
        self.review = ProtectedReview(policy)
        self.identity = AppSignerIdentity(policy)
        self.facts = NativeGitHubFacts(self.client, self.queue_reader, policy.producer,
            runtime=self.runtime, review=self.review, directory=policy.location("directory"),
            signer_identity=self.identity)
        self.review.facts = self.facts

    def status(self):
        directory = self.policy.location("directory")
        state = deploy_state.load_state(directory)
        if not deploy_state.native_consistent(directory, state):
            raise ValueError("native state/guard mismatch; operator reconciliation required")
        if "native_queue" in state:
            bootstrap = state.get("native_bootstrap")
            if (not isinstance(bootstrap, dict) or bootstrap.get("policy") != self.policy.fingerprint
                    or bootstrap.get("epoch") != self.policy.value["epoch"]):
                raise ValueError("installed policy/authority epoch changed")
        active = (state.get("native_queue") or {}).get("active")
        return {"state": active["phase"] if active else "idle" if "native_queue" in state else "uninitialized",
                "activation": "OFF", "G_executor": "configured" if "pipeline" in self.policy.value else "unavailable"}

    def preflight(self):
        status = self.status()
        if status["state"] not in {"idle", "uninitialized"}:
            raise ValueError("active native attempt must reconcile before admission")
        runtime = self.runtime.read()
        repo = self.client.verify_repository()
        main = self.client.get(f"/repos/{REPOSITORY}/branches/main")["commit"]["sha"]
        if (repo.get("node_id") != "R_kgDOVl3HNQ" or main != runtime["sha"]
                or runtime["incident"] is not False or runtime["observation_complete"] is not True):
            raise ValueError("current baseline runtime/main mismatch")
        return {**status, "identity": self.identity.read(), "baseline_sha": main,
                "boundary": "fixture_transport_only" if self.policy.fixture else "installed_linux"}

    def initialize(self):
        self.preflight()
        return ProtectedInitializer(self.policy, self.runtime, self.client).initialize()

    def admit_head(self, number):
        if type(number) is not int or number <= 0:
            raise ValueError("positive public PR identity required")
        self.preflight()
        directory = self.policy.location("directory")
        state = deploy_state.load_state(directory)
        if "native_queue" not in state or "native_bootstrap" not in state:
            raise ValueError("protected baseline initialization required")
        pr = self.client.get(f"/repos/{REPOSITORY}/pulls/{number}")
        member = {"number": number, "node_id": pr["node_id"], "head_sha": pr["head"]["sha"],
                  "base_sha": pr["base"]["sha"], "head_ref": pr["head"]["ref"],
                  "body_sha256": digest(pr["body"])}
        runner = NativeQueue(self.policy.location("repo"), directory, self.policy.producer,
                            self.facts, GitHubProdGate(self.policy, member, self.facts))
        with runner.fence():
            if self.status()["state"] != "idle":
                raise ValueError("reconcile active native attempt before H admission")
            self.preflight()
            return runner.admit_head(member)

    def _candidate(self, group):
        """Recover the original public manifest from the approved reports run.

        The fixed workflow exposes this upstream job output in its step env log.
        Its digest is bound by both authenticated producer envelopes, not by a
        webhook or caller JSON. Revalidation retains already merged lookahead.
        """
        run = self.facts._run("G", group, None)
        reports = [job for job in run["jobs"] if job["name"] == "agent-framework-reports"]
        if len(reports) != 1:
            raise ValueError("authenticated reports job required")
        log = self.facts.job_log(reports[0]["id"])
        encoded = set(re.findall(r"UPSTREAM_MEMBER_MANIFEST_B64: ([A-Za-z0-9+/=]+)(?:\s|$)", log))
        if len(encoded) != 1:
            raise ValueError("original authenticated queue manifest unavailable/ambiguous")
        manifest = validate_manifest(self.policy.location("repo"),
                                     json.loads(base64.b64decode(encoded.pop(), validate=True)))
        binding = reports[0]["envelope"]["binding"]
        if (manifest["group_sha"] != group or manifest["creation_base_sha"] != binding["CI_BASE_SHA"]
                or digest(manifest) != binding["member_manifest_sha256"]
                or manifest["event_delivery_id"] != str(run["run"]["id"])):
            raise ValueError("original G manifest/run digest mismatch")
        raw = self.client.get(f"/repos/{REPOSITORY}/actions/runs/{run['run']['id']}")
        if (raw.get("head_sha") != group or raw.get("head_branch") != manifest["group_ref"].removeprefix("refs/heads/")
                or raw.get("run_attempt") != run["run"]["attempt"]):
            raise ValueError("G event ref/run changed")
        event = {"action": "checks_requested", "repository": {"id": 1448986421,
            "node_id": "R_kgDOVl3HNQ", "full_name": REPOSITORY}, "merge_group": {
                "base_ref": "refs/heads/main", "base_sha": manifest["creation_base_sha"],
                "head_sha": group, "head_ref": manifest["group_ref"]}}
        self.queue_reader.revalidate(self.policy.location("repo"), manifest, event, manifest["event_delivery_id"])
        from scripts.prod_agent.native_ports import read_protected

        directory = self.policy.location("directory")
        read_protected(directory / "bindings.json", self.policy.owner, anchor=self.policy.anchor)
        members = []
        for member in manifest["members"]:
            binding = publication_authority.resolve_source_binding(directory, member["number"])
            members.append({"pr": member["number"], "source_revision": binding.source_revision,
                "source": dict(zip(publication_authority.SOURCE_KEYS, binding.source, strict=True)),
                "binding_sha256": binding.binding_sha256})
        return candidate(self.policy.location("repo"), manifest, members, self.policy.producer)

    def recover(self):
        runner = NativeQueue(self.policy.location("repo"), self.policy.location("directory"),
                             self.policy.producer, self.facts)
        with runner.fence():
            self._initialized()
            return {"result": runner.reconcile(), "activation": "OFF"}

    def _initialized(self):
        self.status()  # epoch and guard validation also apply during recovery
        state = deploy_state.load_state(self.policy.location("directory"))
        if ("native_bootstrap" not in state or "native_source_anchor" not in state
                or "native_queue" not in state):
            raise ValueError("protected baseline/source initialization required")
        return state

    def review_ci(self, value):
        """Authenticated CI facts, exactly the ones the protected review consumer rebinds."""
        from scripts.prod_agent.github_readiness import validate_native_ci

        manifest = value["manifest"]
        runs = [self.facts._run("G", manifest["group_sha"], None)]
        runs += [self.facts._run("H", m["head_sha"], m["number"]) for m in manifest["members"]]
        return validate_native_ci(self.policy.location("repo"), manifest,
                                  self.policy.producer, runs)

    def prepare_review_request(self, value):
        """Authenticated root side only binds request facts; it never decides a pass."""
        return NativePreparer(self.policy).prepare_review_request(value, self.review_ci(value))

    def publish_g_runtime(self, value):
        """One bounded independent publisher; fixture mode uses the injected observer."""
        preparer = NativePreparer(self.policy, observer=self.runtime.observer
                                  if self.policy.fixture else None)
        if self.policy.fixture:
            return preparer.publish_runtime_green()
        return preparer.publish_runtime_process("green")

    def deploy_front(self, group=None):
        # Mandatory dependency check precedes candidate work or attempt admission.
        runner = NativeQueue(self.policy.location("repo"), self.policy.location("directory"),
                             self.policy.producer, self.facts)
        with runner.fence():
            state = self._initialized()
            if state["native_queue"]["active"] is not None:
                return {"result": runner.reconcile(), "activation": "OFF"}
            runner.pipeline = ProtectedPipeline(self.policy)
            self.preflight()
            _, entries = self.queue_reader.entries()
            if not entries or entries[0]["headCommit"] is None:
                raise ValueError("built live front required")
            actual = sha(entries[0]["headCommit"]["oid"])
            if group is not None and sha(group) != actual:
                raise ValueError("event hint is not current live G")
            value = self._candidate(actual)
            self.prepare_review_request(value)
            runner.gate = GitHubGProdGate(self.policy, value)
            runner.start(value)
            # start() returns only after the actual stage journal (including its
            # >=600s observation) is complete. The bounded independent publisher
            # reads that journal and writes the runtime record; it never re-runs
            # deployment or owns the attempt fence. Positive child exit is the only
            # completion proof.
            self.publish_g_runtime(value)
            runtime = self.runtime.read()
            if runtime != {"sha": actual, "incident": False, "observation_complete": True}:
                raise ValueError("independent G runtime publisher unavailable; reconcile required")
            runner.seal(value)
            runner.grant(value)
            return {"result": runner.reconcile(), "activation": "OFF"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "initialize", "admit-head", "status",
                                              "recover", "deploy-front", "grant", "release"))
    parser.add_argument("--pr", type=int)
    args = parser.parse_args(argv)
    if args.operation in {"grant", "release"}:
        print("standalone grant/release unavailable; use protected deploy-front/recover", file=sys.stderr)
        return 2
    try:
        # The installed launcher uses isolated Python and this protected module;
        # no inherited token, attempt, PYTHONPATH or arbitrary-policy flag exists.
        os.environ.clear()
        os.environ.update(PATH="/usr/bin:/bin", LANG="C.UTF-8")
        controller = NativeController(InstalledPolicy(POLICY_PATH))
        if args.operation == "admit-head":
            value = controller.admit_head(args.pr)
        else:
            value = getattr(controller, args.operation.replace("-", "_"))()
        print(json.dumps(value))
        return 0
    except Exception:
        print("protected H/G prerequisites unavailable or inconsistent; reconcile active attempts", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
