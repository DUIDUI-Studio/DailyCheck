"""Operator-local exact-H release Worker. The SSH port never gets GitHub auth."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from scripts.blue_green.deploy_report import (
    PublicDeployFacts,
    _payload_digest,
    publication_marker,
    render_public,
)
from scripts.blue_green.publication_authority import resolve_source_binding
from scripts.hosting import (
    GITHUB_REPOSITORY_NODE,
    AccessRole,
    HostingClient,
    HostingConfig,
    HostingError,
    Provider,
    github_ff_payload,
    strict_json_loads,
)
from scripts.prod_agent import relay_git, relay_github
from scripts.prod_agent.native_ports import protected_path
from scripts.prod_agent.relay_protocol import (
    COMMAND,
    MAX_CONTROL,
    REPO,
    SYSTEM_ENV,
    WORKER_PATH,
    canonical,
    checked_file,
    digest,
    hex_id,
    lock_file,
    positive,
    read_file,
    require,
    save_file,
    secure_profile,
    sign,
    target,
)
from scripts.prod_agent.relay_vps import MINIMUM, STAGES, exact_remote, observation


class Worker:
    def __init__(self, path, fixture_root):
        self.profile, self.fixture = secure_profile(path, fixture_root)
        self.expected = target(self.profile["target"])
        self.directory = self.protected_dir("worker_state")
        self.binding_dir = self.protected_dir("worker_binding")
        self.file = self.directory / (self.expected["attempt"] + ".json")
        self.root = "/repos/" + REPO
        self.pr = self.root + "/pulls/" + str(self.expected["pr"])
        self.main = self.root + "/git/ref/heads/main"
        self.grants = self.directory / (self.expected["attempt"] + ".grants.json")
        os.environ.clear()
        os.environ.update(SYSTEM_ENV, DAILYCHECK_GITHUB_LANE="ordinary",
                          DAILYCHECK_HOSTING_PROVIDER="github")

    def protected_dir(self, key):
        return protected_path(Path(self.profile[key]), os.geteuid(),
                              anchor=self.fixture, private=True)

    def pinned(self, key):
        return checked_file(self.profile[key], anchor=self.fixture, private=False,
                            pin=self.profile[key + "_sha256"])

    def client(self, phase, role=AccessRole.READ):
        signer = self.pinned("auth")
        request = {"phase": phase, "target": self.expected}
        result = subprocess.run([str(signer)], input=canonical(request), env=SYSTEM_ENV,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=35, check=True, cwd="/")
        grant = strict_json_loads(result.stdout, max_bytes=MAX_CONTROL)
        require(isinstance(grant, dict) and set(grant) ==
                {"phase", "target", "repo_id", "installation_id", "expires", "token"}
                and grant["phase"] == phase and grant["target"] == self.expected
                and grant["repo_id"] == 1448986421
                and grant["installation_id"] == self.profile["installation_id"]
                and type(grant["expires"]) is int and time.time() + 2 < grant["expires"] <= time.time() + 3700
                and isinstance(grant["token"], str)
                and re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", grant["token"]),
                "fresh scoped auth unavailable")
        token_key = hashlib.sha256(grant["token"].encode()).hexdigest()
        used = read_file(self.grants) if self.grants.exists() else []
        require(token_key not in used and len(used) < 128, "reused role credential")
        save_file(self.grants, [*used, token_key])
        curl = str(self.pinned("fixture_curl")) if self.fixture else "/usr/bin/curl"
        client = HostingClient(HostingConfig(Provider.GITHUB, "https://api.github.com", REPO,
                                role, grant["token"], curl=curl))
        return client, grant["expires"]

    def source(self):
        checked_file(self.binding_dir / "bindings.json", anchor=self.fixture)
        source = resolve_source_binding(self.binding_dir, self.expected["pr"])
        require(source.source[0] == "github" and source.issue_identities is not None
                and source.binding_sha256 == self.expected["source"], "work source mismatch")

    def preflight(self, client):
        self.source()
        e = self.expected
        facts = relay_github._github_preconditions(client, e["pr"], e["head"], e["base"])
        require(facts["head_ref"] == e["ref"] and facts["pr_node"] == e["pr_node"]
                and facts["pr_id"] == e["pr_id"]
                and facts["body_sha256"] == self.profile["approved_body_sha256"],
                "protected review and GitHub facts differ")
        self.ci_evidence = facts["ci"]
        return digest(facts)

    def ssh(self, operation, raw=b"", **fields):
        p = self.profile
        host, user, port = (p[k] for k in ("ssh_host", "ssh_user", "ssh_port"))
        require(isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host)
                and isinstance(user, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", user)
                and type(port) is int and 1 <= port <= 65535
                and (not self.fixture or host == "127.0.0.1"), "protected SSH endpoint")
        key = checked_file(p["ssh_identity"], anchor=self.fixture)
        known = checked_file(p["ssh_known_hosts"], anchor=self.fixture)
        cmd = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-p", str(port), "-i", str(key),
               "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}",
               "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ForwardAgent=no",
               "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=8",
               "--", user + "@" + host, COMMAND]
        envelope = {"op": operation, "target": self.expected, **fields}
        if operation not in {"status", "report_status"}:
            signing_key = checked_file(p["worker_signing_key"], anchor=self.fixture)
            require(signing_key != key, "separate authority and SSH keys required")
            envelope["signature"] = sign(envelope, signing_key)
        wire = canonical(envelope) + b"\n"
        result = subprocess.run(cmd, input=wire + raw, env=SYSTEM_ENV, timeout=72,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
        value = strict_json_loads(result.stdout, max_bytes=MAX_CONTROL)
        require(isinstance(value, dict) and value.get("target") == self.expected,
                "SSH receipt binding mismatch")
        return value

    def start(self):
        require(not self.file.exists() and self.ssh("status")["phase"] == "absent",
                "already submitted or outcome needs reconciliation")
        client, expires = self.client("preflight")
        ci = self.preflight(client)
        with tempfile.TemporaryDirectory(dir=self.directory, prefix="relay-send-") as directory:
            temp = Path(directory)
            repo = relay_git.make_store(temp / "git")
            settings = ({"fixture_root": self.fixture, "url": self.profile["fixture_git_url"],
                         "ca": self.pinned("fixture_ca")} if self.fixture else {})
            require(relay_git.fetch(repo, self.expected, **settings), "local anonymous exact fetch failed")
            raw = relay_git.make_bundle(repo, temp / "candidate.bundle", self.expected)
        require(time.time() < expires and self.preflight(client) == ci, "admission facts changed")
        save_file(self.file, {"target": self.expected, "ci": ci, "ci_evidence": self.ci_evidence,
                              "started_at": int(time.time())})
        return self.ssh("start", raw, ci=ci, bundle_size=len(raw),
                        bundle_sha256=hashlib.sha256(raw).hexdigest())

    def record(self):
        checked_file(self.file, anchor=self.fixture)
        result = read_file(self.file)
        require(result["target"] == self.expected, "local custody changed")
        return result

    def verified(self, row, record, phases):
        require(row["phase"] in phases and row["ci"] == record["ci"]
                and row.get("live") is True and row["observed"]["sha"] == self.expected["head"]
                and row["observed"]["route"] == "green" and hex_id(row["observed"]["identity"], 64)
                and row["fixture"] is (self.fixture is not None)
                and row["observation_seconds"] >= (1 if self.fixture else MINIMUM)
                and row["samples"] >= 2
                and row["source_method"] in {"incremental_bundle", "anonymous_https"}
                and [s["stage"] for s in row["journal"]] == [*STAGES, "observe"]
                and all(type(s.get("completed_at")) is int and s["completed_at"] >= s["started_at"]
                        for s in row["journal"]), "exact observation unproven")
        return observation(row)

    def remote(self, client, *, merged):
        expected = self.expected
        repo = client.verify_repository()
        pr = client.get(self.pr)
        main = client.get(self.main)
        require(repo.get("node_id") == GITHUB_REPOSITORY_NODE
                and pr["id"] == expected["pr_id"] and pr["node_id"] == expected["pr_node"]
                and pr["number"] == expected["pr"] and pr["head"]["sha"] == expected["head"]
                and "refs/heads/" + pr["head"]["ref"] == expected["ref"]
                and pr["base"]["ref"] == "main" and main["ref"] == "refs/heads/main"
                and all(pr[k]["repo"]["id"] == 1448986421 and
                        pr[k]["repo"]["full_name"] == REPO for k in ("head", "base")),
                "remote identity drift")
        require(pr["state"] == ("closed" if merged else "open")
                and pr["merged"] is merged
                and main["object"]["sha"] == expected["head" if merged else "base"],
                "remote outcome unconfirmed")
        if merged:
            require(pr["merge_commit_sha"] == expected["head"], "remote merge SHA mismatch")
            return {"pr_id": pr["id"], "pr_node": pr["node_id"], "head": pr["head"]["sha"],
                    "main": main["object"]["sha"], "merged_at": pr["merged_at"]}
        require(pr["draft"] is False and pr["base"]["sha"] == expected["base"], "remote base drift")
        return {"pr_id": pr["id"], "pr_node": pr["node_id"], "head": pr["head"]["sha"],
                "main": main["object"]["sha"], "merged": False}

    def finalize(self, guard, attempt):
        record = self.record()
        require("pre_arm_at" not in record and "post_at" not in record,
                "prior write intent requires read-only reconciliation")
        before = self.ssh("status")
        receipt = self.verified(before, record, {"verified"})
        client, _ = self.client("merge-read")
        require(self.preflight(client) == record["ci"], "approval, CI or protection changed")
        writer, expires = self.client("finalize", AccessRole.FINALIZE)
        require(self.preflight(writer) == record["ci"], "fresh finalization checks failed")
        record.update(pre_arm_at=int(time.time()), observation=receipt)
        save_file(self.file, record)
        armed = self.ssh("arm", observation=receipt, ci=record["ci"])
        require(self.verified(armed, record, {"armed"}) == receipt
                and hex_id(armed["intent"], 32), "unconfirmed write intent")
        readback = self.ssh("status")
        require(readback == armed, "fresh intent readback differs")
        record.update(intent=armed["intent"], observation=receipt)
        try:
            self.remote(writer, merged=False)
            self.source()
            require(time.time() + 2 < expires, "finalize credential expired before POST")
        except (HostingError, ValueError):
            record["definitive_rejection"] = "local_no_post"
            save_file(self.file, record)
            return self.reject(record)
        record["post_at"] = int(time.time())
        save_file(self.file, record)
        payload = github_ff_payload(self.expected["base"], self.expected["head"],
                                    self.expected["ref"], self.expected["attempt"])
        try:
            result, headers = writer.request("POST", "/graphql", payload,
                                            guard_fd=guard, attempt_fd=attempt)
            acknowledged = (isinstance(result, dict) and not result.get("errors")
                            and result.get("data", {}).get("updateRefs") ==
                            {"clientMutationId": self.expected["attempt"]})
            ids = re.findall(r"(?im)^x-github-request-id:[ \t]*([a-f0-9:]{8,80})\r?$", headers)
            record["post_acknowledged"] = acknowledged
            if len(ids) == 1:
                record["github_request_id"] = ids[0]
        except HostingError as error:
            record["post_acknowledged"] = False
            record["hosting_error_status"] = error.status
            record["hosting_error_reason"] = str(error)
            if error.status == 401 and str(error) == "hosting_http_error":
                record["definitive_rejection"] = "http_401"
        except (ValueError, OSError, TypeError, AttributeError):
            record["post_acknowledged"] = False
        save_file(self.file, record)
        return self.reconcile()

    def reconcile(self):
        record = self.record()
        if "definitive_rejection" in record:
            return self.reject(record)
        if "pre_arm_at" in record and "post_at" not in record:
            row = self.ssh("status")
            receipt = self.verified(row, record, {"armed"})
            require(receipt == record["observation"] and hex_id(row["intent"], 32)
                    and row["armed_at"] >= record["pre_arm_at"] - 5,
                    "lost arm receipt did not match recorded intent")
            record.update(intent=row["intent"], definitive_rejection="local_no_post")
            save_file(self.file, record)
            return self.reject(record)
        require("post_at" in record, "qualifying local write intent missing")
        row = self.ssh("status")
        receipt = self.verified(row, record, {"armed", "settlement_pending", "settled"})
        require(receipt == record["observation"] and row["intent"] == record["intent"],
                "wrong write attempt")
        client, _ = self.client("reconcile")
        remote = self.remote(client, merged=True)
        require(exact_remote(self.expected, remote)
                and row["armed_at"] - 5 <= record["post_at"] <= time.time() + 5
                and record["post_at"] - 5 <= datetime.fromisoformat(
                    remote["merged_at"].replace("Z", "+00:00")).timestamp() <= time.time() + 5,
                "remote merge timeline differs")
        if row["phase"] == "armed":
            require(self.remote(client, merged=True) == remote, "remote merge readback changed")
            self.ssh("settle", observation=receipt, intent=record["intent"], remote=remote)
            row = self.ssh("status")
        if row["phase"] == "settlement_pending":
            require(self.verified(row, record, {"settlement_pending"}) == receipt
                    and row["intent"] == record["intent"] and row["remote"] == remote
                    and self.remote(client, merged=True) == remote, "settlement readback differs")
            self.ssh("confirm", observation=receipt, intent=record["intent"], remote=remote)
            row = self.ssh("status")
        require(self.verified(row, record, {"settled"}) == receipt and row["remote"] == remote
                and self.remote(client, merged=True) == remote, "settlement proof differs")
        record.update(terminal_receipt=row["receipt"], remote=remote)
        save_file(self.file, record)
        if "github_request_id" in record:
            row["github_request_id"] = record["github_request_id"]
        row["post_acknowledged"] = record.get("post_acknowledged")
        row["ci_evidence"] = record["ci_evidence"]
        return row

    def reject(self, record):
        row = self.ssh("status")
        if row["phase"] == "rolled_back":
            require(row["intent"] == record["intent"] and row["live"] is True
                    and row["rejection"]["reason"] == record["definitive_rejection"],
                    "rollback identity mismatch")
            return row
        receipt = self.verified(row, record, {"armed"})
        require(receipt == record["observation"] and row["intent"] == record["intent"],
                "wrong rejected write attempt")
        client, _ = self.client("reject-read")
        remote = self.remote(client, merged=False)
        self.source()
        require(self.remote(client, merged=False) == remote, "rollback readback changed")
        self.ssh("reject", observation=receipt, intent=record["intent"],
                 reason=record["definitive_rejection"], remote=remote)
        row = self.ssh("status")
        require(row["phase"] == "rolled_back" and row["live"] is True, "blue rollback unconfirmed")
        return row

    def abort(self):
        record = self.record()
        require("post_at" not in record, "cannot roll back a possible GitHub write")
        row = self.ssh("status")
        receipt = self.verified(row, record, {"verified"})
        client, _ = self.client("abort-read")
        remote = self.remote(client, merged=False)
        self.source()
        self.ssh("abort", observation=receipt, remote=remote)
        row = self.ssh("status")
        require(row["phase"] == "rolled_back" and row["live"] is True, "blue rollback unconfirmed")
        return row

    def report(self, guard):
        self.source()
        client, _ = self.client("report", AccessRole.REPORT)
        terminal = self.ssh("status")
        snapshot = self.ssh("report_status")
        phase = snapshot["phase"]
        plan = snapshot["publication"]
        facts = PublicDeployFacts(**plan["facts"])
        require(phase == terminal["phase"] and phase in {"settled", "rolled_back"}
                and terminal.get("live") is True and facts.pr == self.expected["pr"]
                and facts.sha == self.expected["head"] and facts.receipt == terminal["receipt"]
                and (facts.rc == 0) == (phase == "settled")
                and terminal["publication"]["state"] == plan["state"], "report receipt differs")
        body = render_public(facts)
        require(plan["outcome"] == digest({"target": self.expected, "payload": _payload_digest(body),
                                            "receipt": facts.receipt}), "typed report payload differs")
        remote = self.remote(client, merged=phase == "settled")
        if phase == "settled":
            require(remote == terminal["remote"], "merge and deployment receipt differ")
        repo = client.verify_repository()
        require(repo.get("private") is False and repo.get("visibility") == "public",
                "report destination is not the public code repository")
        url = f"{self.root}/issues/{facts.pr}"
        route = url + "/comments"
        actor = self.profile["report_actor_id"]
        require(positive(actor), "protected report actor required")
        marker = publication_marker(facts)

        def prior():
            matches = [x for x in client.pages(route) if marker in str(x.get("body", ""))]
            require(len(matches) <= 1, "report marker ambiguous")
            if not matches:
                return None
            row = matches[0]
            require(row.get("body") == body and row.get("issue_url") == client.config.api_url + url
                    and row.get("user", {}).get("id") == actor, "report readback identity differs")
            individual = client.get(f"{self.root}/issues/comments/{row['id']}")
            require(all(row.get(k) == individual.get(k) for k in ("id", "body", "issue_url", "user")),
                    "report comment changed")
            return row["id"]

        comment = prior()
        if plan["state"] == "ready":
            armed = self.ssh("report_intent", outcome=plan["outcome"])["publication"]
            readback = self.ssh("report_status")["publication"]
            require(armed == readback and armed["state"] == "uncertain"
                    and armed["outcome"] == plan["outcome"] and hex_id(armed["intent"], 32),
                    "report intent receipt unconfirmed")
            plan = armed
            require(self.remote(client, merged=phase == "settled") == remote,
                    "report source facts changed before publication")
            # A prior exact marker is sufficient to link. Without it, this
            # invocation alone may POST after the durable VPS intent readback.
            comment = prior()
            if comment is None:
                try:
                    client.request("POST", route, {"body": body}, guard_fd=guard)
                except HostingError:
                    pass
                comment = prior()
        if comment is None:
            terminal["publication"] = {"state": plan["state"], "comment_id": 0}
            return terminal
        if plan["state"] != "linked":
            require(self.remote(client, merged=phase == "settled") == remote,
                    "report source facts changed before linking")
            self.ssh("report_link", outcome=plan["outcome"], intent=plan["intent"],
                     comment_id=comment, author=actor)
        terminal = self.ssh("status")
        require(terminal["phase"] == phase and terminal.get("live") is True
                and terminal["publication"] == {"state": "linked", "comment_id": comment}
                and prior() == comment, "linked report not corroborated")
        return terminal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("op", choices=("start", "status", "finalize", "reconcile", "abort", "report"))
    parser.add_argument("--fixture-profile", type=Path)
    parser.add_argument("--fixture-root")
    args = parser.parse_args()
    require(bool(args.fixture_profile) == bool(args.fixture_root), "fixture profile boundary")
    worker = Worker(args.fixture_profile or WORKER_PATH, args.fixture_root)
    if args.op == "status":
        output = worker.ssh("status")
    else:
        with (lock_file(worker.directory, ".guard.lock") as guard,
              lock_file(worker.directory, ".worker.lock") as attempt):
            fcntl.flock(attempt, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.op == "finalize":
                output = worker.finalize(guard.fileno(), attempt.fileno())
            elif args.op == "report":
                output = worker.report(guard.fileno())
            else:
                output = getattr(worker, args.op)()
    output["target"] = {k: v for k, v in output["target"].items() if k != "source"}
    output.pop("bundle", None)
    output.pop("intent", None)
    print(canonical(output).decode())
    if args.op in {"finalize", "reconcile"} and output["phase"] == "rolled_back":
        raise SystemExit(2)
    if args.op == "report" and output["publication"]["state"] != "linked":
        raise SystemExit(3)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"error":"worker_denied_or_reconciliation_required"}')
        raise SystemExit(1) from None
