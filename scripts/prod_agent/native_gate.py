"""Dedicated App check writers with separate authenticated H and G contexts.

Only the protected controller supplies operation contexts, never a public
arbitrary-payload endpoint. Its HTTP child inherits the kernel fence, including
after the controller is killed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.ci.merge_group import REPOSITORY, commit, digest
from scripts.prod_agent.github_readiness import validate_head_ci
from scripts.prod_agent.native_ports import (
    AppSignerIdentity,
    InstalledPolicy,
    OwnedFixture,
    read_protected,
)
from scripts.prod_agent.native_queue import NativeQueue, deploy_state, publication_authority


class GitHubProdGate:
    """The member and reader are protected controller dependencies, not RPC JSON."""
    def __init__(self, policy, member, facts):
        self.policy, self.member, self.facts = policy, dict(member), facts

    def success(self, payload, *, guard_fd):
        policy, member = self.policy, self.member
        if (type(guard_fd) is not int or guard_fd < 3
                or deploy_state.process_lock_fd() != guard_fd
                or len(commit(policy.location("repo"), member["head_sha"])[1]) != 1):
            raise ValueError("H signing requires the actual attempt fence and one-parent H")
        facts = self.facts.read_head(member)
        if (facts.get("repository_id") != 1448986421
                or facts.get("repository_node_id") != "R_kgDOVl3HNQ"
                or facts.get("member") != member):
            raise ValueError("unauthenticated H signing context")
        validate_head_ci(policy.location("repo"), member, policy.producer, facts["ci"])
        binding = publication_authority.resolve_source_binding(policy.location("directory"), member["number"])
        if binding.source_revision is None:
            raise ValueError("protected source revision required")
        expected = {"name": "dailycheck/prod-gate", "head_sha": member["head_sha"],
                    "status": "completed", "conclusion": "success",
                    "external_id": "H:" + digest({"member": member, "policy": policy.producer.sha256,
                                                   "binding": binding.binding_sha256})}
        if payload != expected or facts != self.facts.read_head(member):
            raise ValueError("H signing payload/evidence changed")
        return self._send(expected, guard_fd)

    def _send(self, payload, guard_fd):
        policy = self.policy
        # Keys are read only from protected custody and passed privately, not argv.
        read_protected(policy.location("key_file"), policy.owner, anchor=policy.anchor)
        key_fd = os.open(policy.location("key_file"), os.O_RDONLY | os.O_NOFOLLOW)
        config = {"policy": str(policy.path), "payload": payload,
                  "candidate": getattr(self, "candidate", None),
                  "fixture": ({"root": str(policy.fixture.root), "endpoint": policy.fixture.endpoint}
                              if policy.fixture else None)}
        bootstrap = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
                     "runpy.run_module('scripts.prod_agent.native_gate',run_name='__main__')")
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
               "DAILYCHECK_DEPLOY_STATE_DIR": str(policy.location("directory")),
               deploy_state.ATTEMPT_FD_ENV: str(guard_fd)}
        if hasattr(self, "candidate"):
            env[deploy_state.ATTEMPT_ENV] = os.environ[deploy_state.ATTEMPT_ENV]
        try:
            child = subprocess.Popen([str(policy.python), "-I", "-B", "-c", bootstrap,
                str(policy.authority), "--sign-g-child" if hasattr(self, "candidate") else "--sign-h-child",
                str(key_fd)], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, pass_fds=(key_fd, guard_fd))
            try:
                stdout, _ = child.communicate(json.dumps(config), timeout=75)
            except subprocess.TimeoutExpired as exc:
                child.kill()
                child.communicate()
                raise ValueError("App check request uncertain after child timeout") from exc
            if child.returncode != 0 or len(stdout) > 4096:
                raise ValueError("App check request uncertain; no automatic retry")
            return json.loads(stdout)
        finally:
            os.close(key_fd)


class GitHubGProdGate(GitHubProdGate):
    """Separate G capability, bound to the durable intent created by grant()."""
    def __init__(self, policy, candidate):
        self.policy, self.candidate = policy, candidate

    def validate(self, payload, guard_fd):
        policy = self.policy
        if policy.fixture is None:
            from scripts.prod_agent.native_pipeline import ProtectedPipeline

            ProtectedPipeline(policy)  # no installed G writer without the fixed executor
        if type(guard_fd) is not int or guard_fd < 3 or deploy_state.process_lock_fd() != guard_fd:
            raise ValueError("G signing requires original held fence")
        state = deploy_state.load_state(policy.location("directory"))
        active = deploy_state._native_owned(state)
        runner = NativeQueue(policy.location("repo"), policy.location("directory"), policy.producer, None)
        runner._bindings(self.candidate)
        proof = runner._validate_proof(active, self.candidate)
        grant = active["release_grant"]
        bootstrap = state.get("native_bootstrap") or {}
        if (active["candidate"] != self.candidate or active["phase"] != "grant_intent"
                or bootstrap.get("policy") != policy.fingerprint
                or bootstrap.get("epoch") != policy.value["epoch"]
                or self.candidate["policy_sha256"] != policy.producer.sha256
                or not grant or grant["last_http_result"] != "unknown" or grant["check_run_id"] is not None
                or grant["issuer_app_id"] != policy.producer.prod_app_id
                or grant["installation_id"] != policy.producer.prod_installation_id
                or grant["validity"] != policy.producer.release_validity
                or grant["evidence_sha256"] != proof["evidence_sha256"]
                or grant["group_sha"] != self.candidate["manifest"]["group_sha"]
                or payload != {"name": "dailycheck/prod-gate", "head_sha": grant["group_sha"],
                    "status": "completed", "conclusion": "success", "external_id": grant["intent_id"]}
                or digest(payload) != grant["success_request_sha256"]):
            raise ValueError("G request does not match exact protected durable intent")

    def success(self, payload, *, guard_fd):
        self.validate(payload, guard_fd)
        return self._send(payload, guard_fd)


def _child(key_fd, operation):
    value = json.loads(sys.stdin.read(65537))
    fixture = OwnedFixture(Path(value["fixture"]["root"]), value["fixture"]["endpoint"]) if value["fixture"] else None
    policy = InstalledPolicy(Path(value["policy"]), fixture=fixture)
    fd = deploy_state.process_lock_fd()
    if fd is None:
        raise ValueError("HTTP child missing inherited fence")
    payload = value["payload"]
    if operation == "--sign-g-child" and value.get("candidate") is not None:
        GitHubGProdGate(policy, value["candidate"]).validate(payload, fd)
    elif (operation != "--sign-h-child" or value.get("candidate") is not None
            or set(payload) != {"name", "head_sha", "status", "conclusion", "external_id"}
            or payload["name"] != "dailycheck/prod-gate" or payload["status"] != "completed"
            or payload["conclusion"] != "success" or not payload["external_id"].startswith("H:")
            or len(payload["external_id"]) != 66):
        raise ValueError("G/arbitrary request unavailable")
    with os.fdopen(key_fd, "rb") as key_file:
        key = key_file.read(65537)
    if len(key) > 65536:
        raise ValueError("invalid signing key")
    api, token, identity = AppSignerIdentity(policy).authenticate(key)
    result = api.request("POST", f"/repos/{REPOSITORY}/check-runs", token, payload, expected=201)
    if (any(result.get(k) != v for k, v in payload.items())
            or type(result.get("id")) is not int or result["id"] <= 0
            or result.get("app", {}).get("id") != identity["app_id"]):
        raise ValueError("App check response identity mismatch")
    print(json.dumps({**payload, "id": result["id"], **identity}), flush=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3 or sys.argv[1] not in {"--sign-h-child", "--sign-g-child"}:
            raise ValueError("private signer child only")
        _child(int(sys.argv[2]), sys.argv[1])
    except Exception:
        # HTTP/crypto error strings may contain credentials or returned payloads.
        print("protected App signer failed; request may be uncertain", file=sys.stderr)
        raise SystemExit(1) from None
