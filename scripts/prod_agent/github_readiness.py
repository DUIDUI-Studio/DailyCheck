"""Read-only GitHub candidate evidence. No admission, merge or deployment authority."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import quote

from scripts.hosting import HostingClient, HostingError

_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Candidate:
    pr: int
    head: str
    base: str
    head_ref: str


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise HostingError("invalid_response")
    return value


def _positive(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise HostingError("invalid_response")
    return value


class GitHubReadinessClient:
    def __init__(
        self, client: HostingClient, required: tuple[str, ...] = ("test", "agent-framework-reports")
    ):
        self.client = client
        self.config = client.config
        self.required = required
        self.root = f"/repos/{self.config.repo}"

    def _candidate(self, number: int, expected: str) -> Candidate:
        pr = _object(self.client.get(f"{self.root}/pulls/{number}"))
        if pr.get("number") != number or pr.get("state") != "open" or pr.get("draft") is not False:
            raise HostingError("pr_not_open_or_not_ready")
        head, base = _object(pr.get("head")), _object(pr.get("base"))
        if (
            base.get("ref") != self.config.base_ref
            or _object(base.get("repo")).get("id") != self.config.repo_id
            or _object(base.get("repo")).get("full_name") != self.config.repo
            or _object(head.get("repo")).get("full_name") != self.config.repo
        ):
            raise HostingError("pr_identity_mismatch")
        if head.get("sha") != expected:
            raise HostingError("head_changed")
        base_sha = base.get("sha")
        if not isinstance(base_sha, str) or not _SHA.fullmatch(base_sha):
            raise HostingError("invalid_base_sha")
        if pr.get("mergeable") is not True or pr.get("mergeable_state") not in {
            "clean",
            "unstable",
            "has_hooks",
        }:
            raise HostingError("not_mergeable")
        branch = _object(
            self.client.get(f"{self.root}/branches/{quote(self.config.base_ref, safe='')}")
        )
        if _object(branch.get("commit")).get("sha") != base_sha:
            raise HostingError("base_changed")
        if not isinstance(head.get("ref"), str) or not head["ref"]:
            raise HostingError("invalid_head_ref")
        return Candidate(number, expected, base_sha, head["ref"])

    def _latest_run(self, candidate: Candidate, workflow_id: int) -> dict:
        runs = self.client.pages(
            f"{self.root}/actions/workflows/{workflow_id}/runs",
            key="workflow_runs",
            params={"head_sha": candidate.head, "event": "pull_request"},
        )
        eligible = []
        for run in runs:
            if (
                run.get("workflow_id") != workflow_id
                or run.get("path") != self.config.workflow_path
                or run.get("event") != "pull_request"
                or run.get("head_sha") != candidate.head
                or _object(run.get("repository")).get("id") != self.config.repo_id
                or _object(run.get("head_repository")).get("full_name") != self.config.repo
            ):
                raise HostingError("untrusted_workflow_run")
            associations = run.get("pull_requests")
            if not isinstance(associations, list) or any(
                not isinstance(pr, dict) for pr in associations
            ):
                raise HostingError("invalid_pr_association")
            if any(
                isinstance(pr, dict) and pr.get("number") == candidate.pr for pr in associations
            ):
                _positive(run.get("check_suite_id"))
                _positive(run.get("run_number"))
                _positive(run.get("run_attempt"))
                eligible.append(run)
        if not eligible:
            raise HostingError("checks_missing")
        run = max(eligible, key=lambda row: (row["run_number"], row["id"]))
        associations = [pr for pr in run["pull_requests"] if pr.get("number") == candidate.pr]
        if len(associations) != 1:
            raise HostingError("invalid_pr_association")
        pr = associations[0]
        if (
            _object(pr.get("head")).get("sha") != candidate.head
            or _object(pr.get("base")).get("sha") != candidate.base
            or _object(pr.get("base")).get("ref") != self.config.base_ref
        ):
            raise HostingError("stale_run_candidate")
        return run

    def check(
        self,
        pr_number: int,
        expected_head_sha: str,
        *,
        repo: str | None = None,
        expected_base_sha: str | None = None,
    ) -> dict:
        result = {
            "ready": False,
            "reason": "invalid_candidate",
            "provider": "github",
            "mode": "shadow",
            "finalization_supported": False,
            "expected_head": expected_head_sha,
            "actual_head": None,
            "base_sha": None,
            "required_contexts": list(self.required),
            "contexts": {},
            "observed_at_ms": int(time.time() * 1000),
        }
        try:
            if (
                type(pr_number) is not int
                or pr_number <= 0
                or not _SHA.fullmatch(expected_head_sha)
                or repo is not None
                and repo != self.config.repo
            ):
                raise HostingError("invalid_candidate")
            self.client.verify_repository()
            candidate = self._candidate(pr_number, expected_head_sha)
            result.update(
                actual_head=candidate.head, base_sha=candidate.base, pr_state="open", mergeable=True
            )
            if expected_base_sha is not None and expected_base_sha != candidate.base:
                raise HostingError("base_changed")
            workflow = _object(
                self.client.get(
                    f"{self.root}/actions/workflows/{quote(self.config.workflow_path.rsplit('/', 1)[-1])}"
                )
            )
            workflow_id = _positive(workflow.get("id"))
            if (
                workflow.get("path") != self.config.workflow_path
                or workflow.get("state") != "active"
            ):
                raise HostingError("untrusted_workflow")
            run = self._latest_run(candidate, workflow_id)
            attempt = _positive(run.get("run_attempt"))
            result.update(
                workflow_id=workflow_id,
                run_id=run["id"],
                run_attempt=attempt,
                candidate_sha=candidate.head,
                workflow_head_sha=run["head_sha"],
                check_evidence=[],
            )
            jobs = self.client.pages(
                f"{self.root}/actions/runs/{run['id']}/jobs", key="jobs", params={"filter": "all"}
            )
            checks = self.client.pages(
                f"{self.root}/commits/{candidate.head}/check-runs",
                key="check_runs",
                params={"filter": "all"},
            )
            by_id = {check["id"]: check for check in checks}
            for name in self.required:
                matches = [job for job in jobs if job.get("name") == name]
                if not matches:
                    raise HostingError("checks_missing_or_ambiguous")
                for job in matches:
                    if (
                        job.get("run_id") != run["id"]
                        or _positive(job.get("run_attempt")) > attempt
                        or job.get("head_sha") != candidate.head
                    ):
                        raise HostingError("untrusted_job")
                latest_attempt = max(job["run_attempt"] for job in matches)
                latest = [job for job in matches if job["run_attempt"] == latest_attempt]
                if len(latest) != 1:
                    raise HostingError("checks_missing_or_ambiguous")
                job = latest[0]
                check_url = job.get("check_run_url")
                prefix = f"{self.config.api_url}{self.root}/check-runs/"
                if (
                    not isinstance(check_url, str)
                    or not check_url.startswith(prefix)
                    or not check_url[len(prefix) :].isdigit()
                ):
                    raise HostingError("invalid_check_run_url")
                check_id = int(check_url[len(prefix) :])
                check = _object(by_id.get(check_id))
                app = _object(check.get("app"))
                if (
                    app.get("id") != self.config.actions_app_id
                    or app.get("slug") != "github-actions"
                    or check.get("name") != name
                    or check.get("head_sha") != candidate.head
                    or _object(check.get("check_suite")).get("id") != run.get("check_suite_id")
                ):
                    raise HostingError("untrusted_check_producer")
                if job.get("status") != check.get("status") or job.get("conclusion") != check.get(
                    "conclusion"
                ):
                    raise HostingError("inconsistent_check_evidence")
                status = (
                    "success"
                    if check.get("status") == "completed" and check.get("conclusion") == "success"
                    else "pending"
                    if check.get("status")
                    in {"queued", "in_progress", "waiting", "pending", "requested"}
                    else "failure"
                )
                result["contexts"][name] = status
                result["check_evidence"].append(
                    {
                        "name": name,
                        "job_id": job["id"],
                        "check_run_id": check_id,
                        "app_id": app["id"],
                        "run_attempt": job["run_attempt"],
                        "state": status,
                    }
                )
            # Detect a rerun or newer workflow beginning during collection, then re-read H/B.
            final_run = self._latest_run(candidate, workflow_id)
            if any(
                final_run.get(k) != run.get(k)
                for k in ("id", "run_attempt", "status", "conclusion", "updated_at")
            ):
                raise HostingError("run_changed")
            if self._candidate(pr_number, expected_head_sha) != candidate:
                raise HostingError("base_changed")
            result["freshness"] = "pr_head_base_and_latest_run_reread"
            result["verified_at_ms"] = int(time.time() * 1000)
            values = result["contexts"].values()
            if "failure" in values:
                raise HostingError("checks_failed")
            if (
                "pending" in values
                or run.get("status") != "completed"
                or run.get("conclusion") != "success"
            ):
                raise HostingError("checks_running")
            result.update(ready=True, reason="ready")
        except HostingError as exc:
            result["reason"] = str(exc)
        return result


# Native producers are deliberately separate from the legacy shadow readiness
# result. Nothing in check() or the reducer can turn these facts into a grant.
def validate_native_ci(repo, manifest: dict, policy, runs: list[dict]) -> list[dict]:
    """Validate authenticated producer facts and hash-bound full-job handoffs.

    The read port supplies raw authenticated run/job/check/log facts, never a
    same-name status or webhook assertion. Callers pin the independently
    approved workflow blob; this function performs no writes.
    """
    from scripts.ci.merge_group import validate_manifest

    validate_manifest(repo, manifest)
    expected = [("G", None, manifest["group_sha"], manifest["members"])]
    expected += [("H", m["number"], m["head_sha"], [m]) for m in manifest["members"]]
    return _validate_native_runs(repo, manifest, policy, runs, expected)


def validate_head_ci(repo, member: dict, policy, runs: list[dict]) -> list[dict]:
    return _validate_native_runs(repo, {}, policy, runs,
                                 [("H", member["number"], member["head_sha"], [member])])


def _validate_native_runs(repo, manifest, policy, runs, expected):
    from scripts.ci.candidate_binding import parse_binding
    from scripts.ci.merge_group import CONTEXTS, commit, digest, git
    from scripts.ci.pytest_summary import decode_result
    from scripts.ci.trusted_producer import parse_manifest, validate_producer

    producer_pin = policy.producer_manifest_blob_sha
    if producer_pin is None:
        raise ValueError("approved executed producer pin required")
    import subprocess
    producer_manifest = parse_manifest(subprocess.check_output(
        ["git", "-C", str(repo), "cat-file", "blob", producer_pin]), producer_pin)

    if not isinstance(runs, list) or len(runs) != len(expected):
        raise ValueError("missing H/G evidence")
    proof = []
    for phase, pr, tested, members in expected:
        matches = [r for r in runs if r.get("phase") == phase and r.get("pr") == pr]
        if len(matches) != 1:
            raise ValueError("ambiguous native CI evidence")
        evidence = matches[0]
        if evidence.get("producer_manifest") != producer_manifest:
            raise ValueError("missing authenticated approved producer manifest")
        run, workflow, jobs = evidence["run"], evidence["workflow"], evidence["jobs"]
        event = "merge_group" if phase == "G" else "pull_request"
        if (phase == "H" and len(commit(repo, tested)[1]) != 1
                or workflow != {"id": policy.workflow_id, "path": ".github/workflows/ci.yml",
                                "blob_sha": policy.workflow_blob_sha}
                or git(repo, "rev-parse", tested + ":.github/workflows/ci.yml") != policy.workflow_blob_sha
                or run["workflow_id"] != policy.workflow_id or run["head_sha"] != tested
                or run["event"] != event or run["repository_id"] != 1448986421
                or run["repository_node_id"] != "R_kgDOVl3HNQ"
                or run["status"] != "completed" or run["conclusion"] != "success"
                or run["id"] != evidence["latest_run_id"]
                or run["attempt"] != evidence["latest_attempt"]
                or any(type(run[k]) is not int or run[k] <= 0 for k in ("id", "attempt", "suite_id"))
                or len(jobs) != 2 or {j["name"] for j in jobs} != set(CONTEXTS)):
            raise ValueError("untrusted/latest native workflow evidence")
        bindings = []
        for job in jobs:
            check = job["check"]
            if (type(job["id"]) is not int or job["id"] <= 0
                    or type(check["id"]) is not int or check["id"] <= 0
                    or job["run_id"] != run["id"] or job["attempt"] != run["attempt"]
                    or job["head_sha"] != tested or check["head_sha"] != tested
                    or check["suite_id"] != run["suite_id"]
                    or check["name"] != job["name"] or check["app_id"] != policy.actions_app_id
                    or check["app_slug"] != "github-actions"
                    or any(row.get("status") != "completed" or row.get("conclusion") != "success"
                           for row in (job, check))):
                raise ValueError("untrusted native check producer/attempt")
            envelope = job["envelope"]
            binding = parse_binding(envelope["binding"])
            if (binding.get("version") != 3 or binding.get("phase") != phase
                    or binding["CI_REPOSITORY"] != "DUIDUI-Studio/DailyCheck"
                    or binding["tested_sha"] != tested or binding["CI_EVENT"] != event
                    or binding["CI_RUN_ID"] != str(run["id"])
                    or binding["CI_RUN_ATTEMPT"] != str(run["attempt"])
                    or binding["workflow_path"] != workflow["path"]
                    or binding["workflow_blob_sha"] != workflow["blob_sha"]
                    or phase == "H" and binding["CI_BASE_SHA"] != run["historical_base_sha"]
                    or phase == "G" and (
                        binding["CI_BASE_SHA"] != manifest["creation_base_sha"]
                        or binding["member_manifest_sha256"] != digest(manifest))):
                raise ValueError("native binding is stale or from another domain")
            decode_result(envelope, binding)
            validate_producer(job.get("producer"), producer_manifest, producer_pin, envelope, job["name"])
            artifact = {"envelope": envelope, "obligations": job["obligations"], "producer": job["producer"]}
            if "pytest_source" in job:
                artifact["pytest_source"] = job["pytest_source"]
            if job["artifact_sha256"] != digest(artifact):
                raise ValueError("native artifact digest mismatch")
            obligations = job["obligations"]
            expected_members = [{"number": m["number"], "head_sha": m["head_sha"],
                                 "body_sha256": m["body_sha256"]} for m in members]
            if (obligations.get("members") != expected_members or obligations.get("full_suite") is not True
                    or job["name"] == "agent-framework-reports" and any(
                        obligations.get(k) is not True for k in (
                            "report01", "report02", "report03", "report04", "mcp_contracts",
                            "mcp_impact", "redlines", "hygiene", "upstream_success"))):
                raise ValueError("native member/report obligations incomplete")
            bindings.append(binding)
            proof.append({"phase": phase, "pr": pr, "context": job["name"],
                          "app_id": policy.actions_app_id, "workflow_id": policy.workflow_id,
                          "workflow_blob_sha": policy.workflow_blob_sha, "run_id": run["id"],
                          "run_attempt": run["attempt"], "job_id": job["id"],
                          "check_run_id": check["id"], "head_sha": tested, "event": event,
                          "conclusion": "success", "binding_sha256": digest(binding),
                          "artifact_sha256": job["artifact_sha256"]})
        if bindings[0] != bindings[1]:
            raise ValueError("native jobs tested different identities")
        by_context = {job["name"]: job for job in jobs}
        test, reports = (by_context[name] for name in CONTEXTS)
        if ("pytest_source" in test or reports["envelope"] != test["envelope"]
                or {k: v for k, v in reports["producer"].items() if k != "gates"}
                    != {k: v for k, v in test["producer"].items() if k != "gates"}
                or reports.get("pytest_source") != {
                    "context": "test", "artifact_sha256": test["artifact_sha256"],
                    "envelope_sha256": digest(test["envelope"])}):
            raise ValueError("reports did not reuse the exact trusted test attestation")
    return proof


class NativeGitHubFacts:
    """Read-only native producer/queue reader; no signing or deployment authority.

    Runtime and independent review are protected Prod ports, not request JSON,
    environment flags, PR bodies or CI outputs. No implementation/installation
    of those privileged ports is supplied by the migration slice.
    """
    def __init__(self, client: HostingClient, queue_reader, policy, *, runtime, review, directory, signer_identity=None):
        if client.config.provider.value != "github":
            raise ValueError("native facts require GitHub")
        self.client, self.queue_reader, self.policy = client, queue_reader, policy
        self.runtime, self.review, self.directory = runtime, review, directory
        self.signer_identity = signer_identity
        self.root = f"/repos/{client.config.repo}"

    def job_log(self, job_id: int) -> str:
        """Follow only the authenticated log API's presigned download, without auth."""
        from urllib.parse import urlsplit
        from urllib.request import HTTPRedirectHandler, Request, build_opener

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        from urllib.error import HTTPError
        request = Request(self.client.config.api_url + self.root + f"/actions/jobs/{job_id}/logs",
                          headers={"Authorization": "Bearer " + self.client.config.token,
                                   "Accept": "application/vnd.github+json"})
        try:
            response = build_opener(NoRedirect()).open(request, timeout=30)
        except HTTPError as exc:
            if exc.code != 302:
                raise ValueError("job log unavailable") from exc
            location = exc.headers.get("Location", "")
            parsed = urlsplit(location)
            if (parsed.scheme != "https" or parsed.username or parsed.password
                    or not parsed.hostname or not parsed.hostname.endswith((
                        ".actions.githubusercontent.com", ".blob.core.windows.net"))):
                raise ValueError("untrusted job log redirect") from exc
            response = build_opener(NoRedirect()).open(Request(location), timeout=30)
        with response:
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024 or response.status != 200:
                raise ValueError("incomplete job log")
        return raw.decode("utf-8")

    def _run(self, phase: str, head: str, pr: int | None) -> dict:
        import base64
        import json

        from scripts.ci.merge_group import digest
        from scripts.ci.trusted_producer import parse_manifest, validate_producer

        event = "merge_group" if phase == "G" else "pull_request"
        workflow = _object(self.client.get(self.root + "/actions/workflows/ci.yml"))
        if (workflow.get("id") != self.policy.workflow_id or workflow.get("state") != "active"
                or workflow.get("path") != ".github/workflows/ci.yml"):
            raise ValueError("unapproved native workflow")
        blob = self.client.get(self.root + "/contents/.github/workflows/ci.yml?ref=" + head)
        if blob.get("sha") != self.policy.workflow_blob_sha:
            raise ValueError("unapproved workflow content")
        pin = self.policy.producer_manifest_blob_sha
        if pin is None:
            raise ValueError("approved executed producer pin required")
        approved = self.client.get(self.root + "/git/blobs/" + pin)
        if approved.get("sha") != pin or approved.get("encoding") != "base64":
            raise ValueError("missing authenticated producer manifest blob")
        producer_manifest = parse_manifest(base64.b64decode(approved["content"], validate=True), pin)
        rows = self.client.pages(self.root + f"/actions/workflows/{self.policy.workflow_id}/runs",
                                 key="workflow_runs", params={"event": event, "head_sha": head})
        rows = [r for r in rows if r.get("head_sha") == head and r.get("event") == event
                and (pr is None or any(p.get("number") == pr for p in r.get("pull_requests", [])))]
        if not rows:
            raise ValueError("native CI run absent")
        run = max(rows, key=lambda r: (r["run_number"], r["id"]))
        if (run.get("workflow_id") != self.policy.workflow_id
                or run.get("path") != ".github/workflows/ci.yml"
                or run.get("repository", {}).get("id") != self.client.config.repo_id
                or run.get("head_repository", {}).get("full_name") != self.client.config.repo):
            raise ValueError("native run repository mismatch")
        jobs = self.client.pages(self.root + f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", key="jobs")
        checks = self.client.pages(self.root + f"/commits/{head}/check-runs", key="check_runs", params={"filter": "all"})
        normalized = []
        for context in self.policy_contexts:
            matches = [j for j in jobs if j.get("name") == context]
            if len(matches) != 1:
                raise ValueError("native job missing/ambiguous")
            job = matches[0]
            prefix = self.client.config.api_url + self.root + "/check-runs/"
            url = job["check_run_url"]
            if not url.startswith(prefix) or not url[len(prefix):].isdigit():
                raise ValueError("native check URL mismatch")
            matches = [c for c in checks if c["id"] == int(url[len(prefix):])]
            if len(matches) != 1:
                raise ValueError("native check absent/ambiguous")
            check = matches[0]
            markers = re.findall(r"(?:^|\s)DAILYCHECK_NATIVE_EVIDENCE=([A-Za-z0-9+/=]+)\s*$",
                                 self.job_log(job["id"]), flags=re.MULTILINE)
            if len(markers) != 1:
                raise ValueError("native attestation log missing/ambiguous")
            record = json.loads(base64.b64decode(markers[0], validate=True))
            validate_producer(record.get("producer"), producer_manifest, pin, record["envelope"], context)
            artifact = {"envelope": record["envelope"], "obligations": record["obligations"], "producer": record["producer"]}
            if "pytest_source" in record:
                artifact["pytest_source"] = record["pytest_source"]
            if record["artifact_sha256"] != digest(artifact):
                raise ValueError("native log evidence corrupted")
            normalized.append({"id": job["id"], "name": context, "run_id": job["run_id"],
                "attempt": job["run_attempt"], "head_sha": job["head_sha"], "status": job["status"],
                "conclusion": job["conclusion"], "check": {"id": check["id"], "name": check["name"],
                "head_sha": check["head_sha"], "suite_id": check["check_suite"]["id"],
                "app_id": check["app"]["id"], "app_slug": check["app"]["slug"],
                "status": check["status"], "conclusion": check["conclusion"]},
                "envelope": record["envelope"], "obligations": record["obligations"],
                "artifact_sha256": record["artifact_sha256"],
                "producer": record["producer"],
                **({"pytest_source": record["pytest_source"]} if "pytest_source" in record else {})})
        associations = [p for p in run.get("pull_requests", []) if p.get("number") == pr]
        historical_base = associations[0]["base"]["sha"] if len(associations) == 1 else None
        if phase == "H" and (len(associations) != 1 or associations[0]["head"]["sha"] != head
                             or associations[0]["base"]["ref"] != "main"):
            raise ValueError("native H association mismatch")
        return {"phase": phase, "pr": pr, "workflow": {"id": workflow["id"],
                "path": workflow["path"], "blob_sha": blob["sha"]},
                "run": {"id": run["id"], "attempt": run["run_attempt"], "workflow_id": run["workflow_id"],
                        "head_sha": head, "event": event, "repository_id": run["repository"]["id"],
                        "repository_node_id": run["repository"]["node_id"], "status": run["status"],
                        "conclusion": run["conclusion"], "suite_id": run["check_suite_id"],
                        "historical_base_sha": historical_base},
                "latest_run_id": run["id"], "latest_attempt": run["run_attempt"], "jobs": normalized,
                "producer_manifest": producer_manifest}

    policy_contexts = ("test", "agent-framework-reports")

    def read_head(self, member: dict) -> dict:
        from scripts.ci.merge_group import digest

        repo = self.client.verify_repository()
        pr = self.client.get(self.root + f"/pulls/{member['number']}")
        if (repo.get("node_id") != "R_kgDOVl3HNQ" or pr.get("number") != member["number"]
                or pr.get("node_id") != member["node_id"] or pr.get("state") != "open"
                or pr.get("draft") is not False or pr.get("head", {}).get("sha") != member["head_sha"]
                or pr.get("head", {}).get("repo", {}).get("id") != self.client.config.repo_id
                or pr.get("base", {}).get("repo", {}).get("id") != self.client.config.repo_id
                or pr.get("base", {}).get("ref") != "main" or digest(pr.get("body")) != member["body_sha256"]):
            raise ValueError("native head PR identity changed")
        return {"repository_id": repo["id"], "repository_node_id": repo["node_id"], "member": member,
                "ci": [self._run("H", member["head_sha"], member["number"])]}

    def read(self, candidate: dict) -> dict:
        from scripts.ci.merge_group import digest

        # Import only on this privileged read path; ordinary readiness is shadow-only.
        from scripts.prod_agent.native_queue import deploy_state

        metadata = self.client.verify_repository()
        if metadata.get("node_id") != "R_kgDOVl3HNQ":
            raise ValueError("native repository node mismatch")
        manifest = candidate["manifest"]
        queue_id, entries = self.queue_reader.entries()
        subject_matches = [e for e in entries if e["id"] == manifest["subject_entry_id"]]
        dequeued = len(subject_matches) != 1
        if not dequeued:
            subject = subject_matches[0]
            if (queue_id != manifest["queue_id"] or subject["headCommit"]["oid"] != manifest["group_sha"]
                    or subject["pullRequest"]["number"] != manifest["subject_pr_number"]):
                raise ValueError("native live entry mismatch")
        predecessors, prs = [], []
        state = deploy_state.load_state(self.directory)
        for member in manifest["members"]:
            pr = _object(self.client.get(self.root + f"/pulls/{member['number']}"))
            if (pr.get("number") != member["number"] or pr.get("node_id") != member["node_id"]
                    or pr.get("head", {}).get("sha") != member["head_sha"]
                    or digest(pr.get("body")) != member["body_sha256"]
                    or pr.get("base", {}).get("repo", {}).get("id") != self.client.config.repo_id
                    or pr.get("head", {}).get("repo", {}).get("id") != self.client.config.repo_id):
                raise ValueError("native member H/body/repository changed")
            prs.append(pr)
            if member != manifest["members"][-1]:
                outcomes = [o for o in state["outcomes"].values() if o.get("sha") == member["integration_sha"]
                            and o.get("result") == "success" and o.get("native_evidence_sha256")]
                predecessors.append({"entry_id": member["queue_entry_id"], "pr": member["number"],
                    "merged": pr.get("merged"), "merge_sha": pr.get("merge_commit_sha"),
                    "anchored_sha": member["integration_sha"] if len(outcomes) == 1 else None})
        runs = [self._run("G", manifest["group_sha"], None)]
        runs += [self._run("H", m["head_sha"], m["number"]) for m in manifest["members"]]
        # Double collection detects newer runs/attempts or a changed producer at the boundary.
        if runs != [self._run(r["phase"], r["run"]["head_sha"], r["pr"]) for r in runs]:
            raise ValueError("native runs changed during authenticated read")
        subject = prs[-1]
        branch = self.client.get(self.root + "/branches/main")
        prod = self.client.pages(self.root + f"/commits/{manifest['group_sha']}/check-runs",
                                 key="check_runs", params={"filter": "all"})
        prod = [c for c in prod if c.get("name") == "dailycheck/prod-gate"
                and c.get("app", {}).get("id") == self.policy.prod_app_id]
        if len(prod) > 1:
            raise ValueError("ambiguous Prod check")
        production = self.runtime.read()  # protected process/runtime observations, never CI JSON
        return {"repository_id": metadata["id"], "repository_node_id": metadata["node_id"],
                "repository": metadata["full_name"], "candidate_sha256": digest(candidate),
                "policy_sha256": self.policy.sha256, "manifest": manifest, "dequeued": dequeued,
                "front_entry_id": entries[0]["id"] if entries else None,
                "main_sha": branch["commit"]["sha"], "production_sha": production["sha"],
                "ci": runs, "review": self.review.read(candidate), "merged_predecessors": predecessors,
                "merged": subject.get("merged"), "merge_sha": subject.get("merge_commit_sha"),
                "merged_pr": {"number": subject["number"], "head_sha": subject["head"]["sha"],
                              "merged": subject.get("merged"), "merge_sha": subject.get("merge_commit_sha")},
                "incident": production["incident"], "observation_complete": production["observation_complete"],
                "prod_check": ({"head_sha": prod[0]["head_sha"], "app_id": prod[0]["app"]["id"],
                                "installation_id": (self.signer_identity.read()["installation_id"] if self.signer_identity else None),
                                "id": prod[0]["id"], "status": prod[0]["status"],
                                "external_id": prod[0]["external_id"], "conclusion": prod[0]["conclusion"]}
                               if prod else {})}
