"""Dedicated App check writer bound to authenticated H admission only.

The installed CLI exposes no arbitrary payload or G signing operation. Its HTTP
child inherits the kernel fence, including after the controller is killed.
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
from scripts.prod_agent.native_queue import deploy_state, publication_authority


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
                  "fixture": ({"root": str(policy.fixture.root), "endpoint": policy.fixture.endpoint}
                              if policy.fixture else None)}
        bootstrap = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
                     "runpy.run_module('scripts.prod_agent.native_gate',run_name='__main__')")
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
               "DAILYCHECK_DEPLOY_STATE_DIR": str(policy.location("directory")),
               deploy_state.ATTEMPT_FD_ENV: str(guard_fd)}
        try:
            child = subprocess.Popen([str(policy.python), "-I", "-B", "-c", bootstrap,
                str(policy.authority), "--sign-h-child", str(key_fd)], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, pass_fds=(key_fd, guard_fd))
            try:
                stdout, _ = child.communicate(json.dumps(config), timeout=75)
            except subprocess.TimeoutExpired as exc:
                child.kill()
                child.communicate()
                raise ValueError("H check request uncertain after child timeout") from exc
            if child.returncode != 0 or len(stdout) > 4096:
                raise ValueError("H check request uncertain; no automatic retry")
            return json.loads(stdout)
        finally:
            os.close(key_fd)


def _child(key_fd):
    value = json.loads(sys.stdin.read(65537))
    fixture = OwnedFixture(Path(value["fixture"]["root"]), value["fixture"]["endpoint"]) if value["fixture"] else None
    policy = InstalledPolicy(Path(value["policy"]), fixture=fixture)
    fd = deploy_state.process_lock_fd()
    if fd is None:
        raise ValueError("HTTP child missing inherited fence")
    # The protected parent binds the exact H request before this private process.
    payload = value["payload"]
    if (set(payload) != {"name", "head_sha", "status", "conclusion", "external_id"}
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
        raise ValueError("H check response identity mismatch")
    print(json.dumps({**payload, "id": result["id"], **identity}), flush=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "--sign-h-child":
            raise ValueError("private signer child only")
        _child(int(sys.argv[2]))
    except Exception:
        # HTTP/crypto error strings may contain credentials or returned payloads.
        print("protected H signer failed; request may be uncertain", file=sys.stderr)
        raise SystemExit(1) from None
