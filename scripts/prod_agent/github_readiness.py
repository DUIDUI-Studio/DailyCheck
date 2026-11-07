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
