#!/usr/bin/env python3
"""Native MERGE graph and member gates. Queue names and webhook bodies are not authority.

The public manifest contains authenticated public membership, never private source tuples.
Prod adds the separately protected private bindings and source-map digest to its candidate.
No signer, deployment port, or hosting write is reachable from this module.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

REPOSITORY = "DUIDUI-Studio/DailyCheck"
REPOSITORY_ID = 1448986421
REPOSITORY_NODE = "R_kgDOVl3HNQ"
SHA = re.compile(r"[0-9a-f]{40}")
CONTEXTS = ("test", "agent-framework-reports")
# A fixed read-only query; caller input is confined to GraphQL variables.
QUEUE_QUERY = """query($cursor:String) {
 repository(owner:"DUIDUI-Studio",name:"DailyCheck") {
  id databaseId nameWithOwner mergeQueue(branch:"main") {
   id entries(first:100,after:$cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id position headCommit { oid } baseCommit { oid }
     pullRequest { id number headRefOid baseRefOid headRefName body merged
      mergeCommit { oid } repository { id databaseId nameWithOwner } }
    }
   }
  }
 }
}"""


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   text=True, stderr=subprocess.PIPE).strip()


def sha(value: object) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("full Git identity required")
    return value


def commit(repo: Path, value: str) -> tuple[str, list[str]]:
    sha(value)
    row = git(repo, "show", "-s", "--format=%T %P", value).split()
    return row[0], row[1:]


def ancestor(repo: Path, parent: str, child: str) -> bool:
    sha(parent)
    sha(child)
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", parent, child],
                          capture_output=True).returncode == 0


def validate_manifest(repo: Path, value: object) -> dict:
    keys = {"version", "repository", "repository_id", "repository_node_id", "queue_id",
            "subject_entry_id", "subject_pr_number", "creation_base_sha", "group_sha", "tree_sha",
            "parents", "expected_predecessor_sha", "group_ref", "event_delivery_id", "members"}
    if (not isinstance(value, dict) or set(value) != keys or value["version"] != 1
            or value["repository"] != REPOSITORY or value["repository_id"] != REPOSITORY_ID
            or value["repository_node_id"] != REPOSITORY_NODE):
        raise ValueError("untrusted repository/manifest")
    for key in ("queue_id", "subject_entry_id", "group_ref", "event_delivery_id"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError("missing authenticated queue/event identity")
    group, base, predecessor = (sha(value[key]) for key in (
        "group_sha", "creation_base_sha", "expected_predecessor_sha"))
    tree, parents = commit(repo, group)
    if (len(parents) < 2 or len(parents) != len(set(parents))
            or parents != value["parents"] or tree != value["tree_sha"]
            or predecessor != parents[0] or not ancestor(repo, base, predecessor)):
        raise ValueError("native MERGE graph mismatch")
    members = value["members"]
    if not isinstance(members, list) or not members:
        raise ValueError("null membership")
    seen_pr, seen_entry = set(), set()
    for position, member in enumerate(members, 1):
        if (not isinstance(member, dict) or set(member) != {
                "number", "node_id", "head_sha", "base_sha", "head_ref", "body_sha256",
                "queue_entry_id", "position_at_creation", "integration_sha"}
                or type(member["number"]) is not int or member["number"] <= 0
                or member["number"] in seen_pr or member["queue_entry_id"] in seen_entry
                or member["position_at_creation"] != position
                or any(not isinstance(member[k], str) or not member[k] for k in (
                    "node_id", "head_ref", "queue_entry_id"))
                or not re.fullmatch(r"[0-9a-f]{64}", member["body_sha256"] or "")):
            raise ValueError("ambiguous membership")
        seen_pr.add(member["number"])
        seen_entry.add(member["queue_entry_id"])
        if (len(commit(repo, member["head_sha"])[1]) != 1
                or len(commit(repo, member["integration_sha"])[1]) < 2
                or not ancestor(repo, member["head_sha"], member["integration_sha"])
                or not ancestor(repo, member["integration_sha"], group)
                or not commit(repo, member["base_sha"])):
            raise ValueError("member H is outside its MERGE producer domain")
        if position > 1 and commit(repo, member["integration_sha"])[1][0] != members[position-2]["integration_sha"]:
            raise ValueError("disconnected integration ordering")
    subject = members[-1]
    if (subject["number"] != value["subject_pr_number"]
            or subject["queue_entry_id"] != value["subject_entry_id"]
            or subject["integration_sha"] != group):
        raise ValueError("subject is not final integrated member")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class QueueReader:
    """Authenticated paginated query reader. It cannot issue a mutation.

    This port reads graph facts only. The production signing/installation port is
    deliberately absent; constructing a reader never enables native release.
    """
    def __init__(self, token: str):
        if not token or any(ord(c) < 33 or ord(c) == 127 for c in token):
            raise ValueError("read credential required")
        self._token = token

    def query(self, cursor: str | None) -> dict:
        request = Request("https://api.github.com/graphql", method="POST",
                          data=json.dumps({"query": QUEUE_QUERY, "variables": {"cursor": cursor}}).encode(),
                          headers={"Authorization": "Bearer " + self._token,
                                   "Content-Type": "application/json", "User-Agent": "dailycheck-native-read/1"})
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            if response.status != 200 or response.url != request.full_url:
                raise ValueError("untrusted graph response")
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("oversized graph")
        return json.loads(raw)

    def entries(self) -> tuple[str, list[dict]]:
        cursor, seen, entries, queue_id = None, set(), [], None
        for _ in range(50):
            document = self.query(cursor)
            if document.get("errors"):
                raise ValueError("queue query failed")
            repo = document["data"]["repository"]
            if (repo["id"], repo["databaseId"], repo["nameWithOwner"]) != (
                    REPOSITORY_NODE, REPOSITORY_ID, REPOSITORY):
                raise ValueError("queue repository mismatch")
            queue = repo["mergeQueue"]
            if queue_id is not None and queue["id"] != queue_id:
                raise ValueError("queue changed during pagination")
            queue_id = queue["id"]
            connection = queue["entries"]
            for entry in connection["nodes"]:
                if not isinstance(entry, dict) or entry["id"] in seen:
                    raise ValueError("ambiguous queue entry")
                seen.add(entry["id"])
                entries.append(entry)
            page = connection["pageInfo"]
            if page["hasNextPage"] is False:
                return queue_id, entries
            following = page["endCursor"]
            if not isinstance(following, str) or not following or following == cursor:
                raise ValueError("incomplete queue pagination")
            cursor = following
        raise ValueError("queue page limit")

    def public_pr(self, number: int) -> dict:
        request = Request(f"https://api.github.com/repos/{REPOSITORY}/pulls/{number}",
                          headers={"Authorization": "Bearer " + self._token,
                                   "Accept": "application/vnd.github+json"})
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError("member unavailable")
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("oversized member")
        return json.loads(raw)

    def revalidate(self, repo: Path, original: dict, event: dict, delivery_id: str) -> tuple[dict, dict[int, str]]:
        """Retain authenticated creation membership across a predecessor merging.

        Reports receives the manifest with the bound upstream job output. A
        predecessor disappearing from the live queue must be independently
        confirmed merged at its recorded integration SHA, never silently dropped.
        """
        validate_manifest(repo, original)
        group = event["merge_group"]
        identity = event["repository"]
        if (event["action"] != "checks_requested" or group["base_ref"] != "refs/heads/main"
                or (identity["id"], identity["node_id"], identity["full_name"]) != (
                    REPOSITORY_ID, REPOSITORY_NODE, REPOSITORY)
                or (original["group_sha"], original["creation_base_sha"], original["group_ref"], original["event_delivery_id"]) != (
                    group["head_sha"], group["base_sha"], group["head_ref"], delivery_id)):
            raise ValueError("upstream event/manifest mismatch")
        queue_id, entries = self.entries()
        by_id = {entry["id"]: entry for entry in entries}
        subject = by_id.get(original["subject_entry_id"])
        if (queue_id != original["queue_id"] or subject is None
                or subject["headCommit"]["oid"] != original["group_sha"]
                or subject["pullRequest"]["number"] != original["subject_pr_number"]):
            raise ValueError("group rebuilt/dequeued")
        bodies, positions = {}, []
        for member in original["members"]:
            pr = self.public_pr(member["number"])
            if (pr["number"] != member["number"] or pr["node_id"] != member["node_id"]
                    or pr["head"]["sha"] != member["head_sha"]
                    or pr["head"]["repo"]["id"] != REPOSITORY_ID
                    or pr["base"]["repo"]["id"] != REPOSITORY_ID
                    or digest(pr["body"]) != member["body_sha256"]):
                raise ValueError("member changed during CI")
            entry = by_id.get(member["queue_entry_id"])
            if entry is None:
                if pr["merged"] is not True or pr["merge_commit_sha"] != member["integration_sha"]:
                    raise ValueError("missing predecessor is not merged at recorded G")
            else:
                if (entry["headCommit"]["oid"] != member["integration_sha"]
                        or entry["pullRequest"]["number"] != member["number"]):
                    raise ValueError("member entry rebuilt")
                positions.append(entries.index(entry))
            bodies[member["number"]] = pr["body"]
        if positions != sorted(positions):
            raise ValueError("member ordering changed")
        return original, bodies

    def manifest(self, repo: Path, event: dict, delivery_id: str) -> tuple[dict, dict[int, str]]:
        group = event["merge_group"]
        identity = event["repository"]
        if (event["action"] != "checks_requested" or group["base_ref"] != "refs/heads/main"
                or (identity["id"], identity["node_id"], identity["full_name"]) != (
                    REPOSITORY_ID, REPOSITORY_NODE, REPOSITORY)):
            raise ValueError("invalid merge_group event")
        queue_id, entries = self.entries()
        # GitHub leaves head/base null while an entry is queued but not built.
        # Only the prefix ending at this event's built G supplies membership.
        matches = [i for i, row in enumerate(entries)
                   if row["headCommit"] is not None
                   and sha(row["headCommit"]["oid"]) == sha(group["head_sha"])]
        if len(matches) != 1:
            raise ValueError("group absent/ambiguous in authenticated queue")
        included, members, bodies = entries[:matches[0]+1], [], {}
        for row in included:
            if row["headCommit"] is None or row["baseCommit"] is None:
                raise ValueError("unbuilt entry inside included prefix")
            integration = sha(row["headCommit"]["oid"])
            if sha(row["baseCommit"]["oid"]) != commit(repo, integration)[1][0]:
                raise ValueError("included queue base differs from MERGE predecessor")
            pr = row["pullRequest"]
            if (pr["repository"]["id"], pr["repository"]["databaseId"], pr["repository"]["nameWithOwner"]) != (
                    REPOSITORY_NODE, REPOSITORY_ID, REPOSITORY):
                raise ValueError("foreign member")
            body = pr["body"]
            if not isinstance(body, str):
                raise ValueError("missing member body")
            bodies[pr["number"]] = body
            members.append({"number": pr["number"], "node_id": pr["id"],
                            "head_sha": pr["headRefOid"], "base_sha": pr["baseRefOid"],
                            "head_ref": pr["headRefName"], "body_sha256": digest(body),
                            "queue_entry_id": row["id"], "position_at_creation": row["position"],
                            "integration_sha": row["headCommit"]["oid"]})
        tree, parents = commit(repo, group["head_sha"])
        value = {"version": 1, "repository": REPOSITORY, "repository_id": REPOSITORY_ID,
                 "repository_node_id": REPOSITORY_NODE, "queue_id": queue_id,
                 "subject_entry_id": included[-1]["id"], "subject_pr_number": members[-1]["number"],
                 "creation_base_sha": group["base_sha"], "group_sha": group["head_sha"],
                 "tree_sha": tree, "parents": parents, "expected_predecessor_sha": parents[0],
                 "group_ref": group["head_ref"], "event_delivery_id": delivery_id, "members": members}
        return validate_manifest(repo, value), bodies


def member_gates(repo: Path, manifest: dict, bodies: dict[int, str], evidence: Path, text: Path) -> None:
    """Full G coverage is reused only after the caller compared its tagged binding.

    Each member's body, MCP impact, redlines and hygiene run on that exact H in
    a detached owned Git worktree. G's complete coverage/quality/preview remains
    required for each member's four-report gate.
    """
    validate_manifest(repo, manifest)
    if git(repo, "rev-parse", "HEAD") != manifest["group_sha"]:
        raise ValueError("member report controller must be checked out at G")
    python = sys.executable
    subprocess.run([python, "-I", "-c",
                    "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));"
                    "runpy.run_module('scripts.mcp_contracts',run_name='__main__')",
                    str(repo), "--check"], cwd=repo, check=True)
    with tempfile.TemporaryDirectory(prefix="dailycheck-members-", dir=os.environ.get("CI_ROOT")) as temp:
        for member in manifest["members"]:
            body = bodies[member["number"]]
            if digest(body) != member["body_sha256"]:
                raise ValueError("member body changed")
            root = Path(temp) / str(member["number"])
            body_file = Path(temp) / f'{member["number"]}.md'
            body_file.write_text(body, encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(root), member["head_sha"]], check=True)
            try:
                for script in ("check_mcp_impact.py", "check_redlines.py", "check_pr_hygiene.py"):
                    command = [python, "-I", str(repo / "scripts/agent_framework" / script),
                               "--base", member["base_sha"], "--body-file", str(body_file)]
                    if script == "check_pr_hygiene.py":
                        command += ["--branch", member["head_ref"]]
                    subprocess.run(command, cwd=root, check=True)
            finally:
                subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(root)], check=True)
            env = {**os.environ, "PR_NUM": str(member["number"]), "PR_BODY_FILE": str(body_file),
                   "BASE_SHA": manifest["expected_predecessor_sha"], "HEAD_SHA": manifest["group_sha"],
                   "PYTEST_REUSE_FILE": str(text.resolve()), "PYTEST_REUSE_EVIDENCE": str(evidence.resolve())}
            subprocess.run(["bash", "scripts/agent_framework/generate_reports.sh"], cwd=repo, env=env, check=True)
            subprocess.run(["bash", "scripts/agent_framework/preview.sh", str(member["number"])], cwd=repo, env=env, check=True)
            subprocess.run([python, "scripts/agent_framework/check_reports.py"], cwd=repo, env=env, check=True)


def make_attestation(envelope: dict, members: list[dict], context: str) -> dict:
    """Reports attests its gates and the exact test proof it consumed, not a new session."""
    from scripts.ci.candidate_binding import parse_binding
    from scripts.ci.pytest_summary import decode_result

    binding = parse_binding(envelope["binding"])
    decode_result(envelope, binding)
    if binding.get("version") != 3 or context not in CONTEXTS:
        raise ValueError("tagged native attestation required")
    obligations = {"full_suite": True, "members": [
        {key: member[key] for key in ("number", "head_sha", "body_sha256")} for member in members]}
    artifact = {"envelope": envelope, "obligations": obligations}
    if context == "agent-framework-reports":
        test_artifact_sha256 = digest(artifact)
        # The final approved workflow step runs only after every report gate passed.
        obligations.update({k: True for k in ("report01", "report02", "report03", "report04",
                           "mcp_contracts", "mcp_impact", "redlines", "hygiene", "upstream_success")})
        artifact["pytest_source"] = {"context": "test", "artifact_sha256": test_artifact_sha256,
                                     "envelope_sha256": digest(envelope)}
    return {**artifact, "artifact_sha256": digest(artifact)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["manifest", "reports", "attest"])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--context", choices=CONTEXTS)
    args = parser.parse_args()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if args.command == "attest":
        from scripts.ci.candidate_binding import parse_binding
        from scripts.ci.pytest_summary import decode_result
        envelope = json.loads(Path(os.environ["CI_ROOT"], "envelope.json").read_text())
        binding = parse_binding(envelope["binding"])
        decode_result(envelope, binding)
        if binding.get("version") != 3 or not args.context:
            raise ValueError("tagged native attestation required")
        if binding["phase"] == "G":
            value = validate_manifest(Path.cwd(), json.loads(args.manifest.read_text()))
            if digest(value) != binding["member_manifest_sha256"]:
                raise ValueError("attestation manifest mismatch")
            members = value["members"]
        else:
            pr = event["pull_request"]
            members = [{"number": pr["number"], "head_sha": pr["head"]["sha"],
                        "body_sha256": digest(pr["body"])}]
            if members[0]["head_sha"] != binding["CI_HEAD_SHA"]:
                raise ValueError("attestation head mismatch")
        record = make_attestation(envelope, members, args.context)
        print("DAILYCHECK_NATIVE_EVIDENCE=" + base64.b64encode(json.dumps(record, sort_keys=True).encode()).decode())
        return 0
    reader = QueueReader(os.environ["NATIVE_QUEUE_READ_TOKEN"])
    upstream = os.environ.get("UPSTREAM_MEMBER_MANIFEST_B64")
    if args.command == "reports":
        original = json.loads(args.manifest.read_text())
    elif upstream:
        original = json.loads(base64.b64decode(upstream, validate=True))
    else:
        original = None
    value, bodies = (reader.revalidate(Path.cwd(), original, event, os.environ["CI_RUN_ID"])
                     if original is not None else reader.manifest(Path.cwd(), event, os.environ["CI_RUN_ID"]))
    if args.command == "manifest":
        args.manifest.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        if os.environ.get("GITHUB_OUTPUT"):
            encoded = base64.b64encode(json.dumps(value, sort_keys=True).encode()).decode()
            if len(encoded) > 48 * 1024:
                raise ValueError("member manifest too large for complete job output")
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write("member_manifest_b64=" + encoded + "\n")
    else:
        expected = json.loads(args.manifest.read_text())
        if value != expected:
            raise ValueError("membership changed between jobs")
        member_gates(Path.cwd(), value, bodies, Path(os.environ["PYTEST_REUSE_EVIDENCE"]), Path(os.environ["PYTEST_REUSE_FILE"]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("native queue CI obligation failed", file=sys.stderr)
        raise SystemExit(1) from None
