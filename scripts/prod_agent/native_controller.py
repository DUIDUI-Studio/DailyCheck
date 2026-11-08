"""Installed protected H/bootstrap CLI. No G executor or activation switch."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from scripts.ci.merge_group import REPOSITORY, QueueReader, digest
from scripts.hosting import AccessRole, HostingClient, HostingConfig, Provider
from scripts.prod_agent.github_readiness import NativeGitHubFacts
from scripts.prod_agent.native_gate import GitHubProdGate
from scripts.prod_agent.native_ports import (
    AppSignerIdentity,
    InstalledPolicy,
    ProtectedInitializer,
    ProtectedReview,
    ProtectedRuntime,
)
from scripts.prod_agent.native_queue import NativeQueue, deploy_state

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
        if not deploy_state.native_consistent(directory, state) or (state.get("native_queue") or {}).get("active"):
            raise ValueError("native recovery requires the future protected G controller")
        if "native_queue" in state:
            bootstrap = state.get("native_bootstrap")
            if (not isinstance(bootstrap, dict) or bootstrap.get("policy") != self.policy.fingerprint
                    or bootstrap.get("epoch") != self.policy.value["epoch"]):
                raise ValueError("installed policy/authority epoch changed")
        return {"state": "idle" if "native_queue" in state else "uninitialized",
                "activation": "OFF", "G_executor": "unavailable"}

    def preflight(self):
        status = self.status()
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
            self.status()
            return runner.admit_head(member)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "initialize", "admit-head", "status",
                                              "recover", "deploy-front", "grant", "release"))
    parser.add_argument("--pr", type=int)
    args = parser.parse_args(argv)
    if args.operation in {"deploy-front", "grant", "release"}:
        print("G executor/release unavailable; activation remains OFF", file=sys.stderr)
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
            value = getattr(controller, "status" if args.operation == "recover" else args.operation)()
        print(json.dumps(value))
        return 0
    except Exception:
        print("protected H/bootstrap prerequisites unavailable or inconsistent", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
