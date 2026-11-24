"""Exact-H checks from the installed ordinary engine (6d245281).

Zero-review policy still requires the relay's protected binding, serving-base
anchor and exact fact continuity at admission and finalization.
"""
import hashlib

from scripts.blue_green.deploy_merge import _integer, _validate
from scripts.hosting import GITHUB_REPOSITORY_NODE, HostingClient, HostingError, github_ff_payload
from scripts.prod_agent.github_readiness import GitHubReadinessClient


def _github_protection(client: HostingClient, root: str) -> tuple[dict, int]:
    """Read the supported classic policy and reject unmodeled rule overlays."""
    path = f"{root}/branches/main/protection"
    protection = client.get(path)
    if not isinstance(protection, dict) or protection.get("url") != client.config.api_url + path:
        raise HostingError("github_required_protection_unproven")
    required = protection.get("required_status_checks") or {}
    checks = required.get("checks")
    contexts = ("test", "agent-framework-reports")
    reported_contexts = required.get("contexts")
    if (protection.get("enforce_admins", {}).get("enabled") is not True
            or required.get("strict") is not True
            or not isinstance(checks, list) or len(checks) != len(contexts)
            or any(not isinstance(check, dict)
                   or not _integer(check.get("app_id"), client.config.actions_app_id)
                   for check in checks)
            or {check.get("context") for check in checks} != set(contexts)
            or not isinstance(reported_contexts, list)
            or len(reported_contexts) != len(contexts)
            or set(reported_contexts) != set(contexts)):
        raise HostingError("github_required_protection_unproven")

    # The branch-specific endpoint includes active repository and organization
    # rules. Any overlay needs a separate supported model and reconciliation.
    rules, headers = client.request("GET", f"{root}/rules/branches/main?per_page=100&page=1")
    if rules != [] or any(line.lower().startswith("link:") for line in headers.splitlines()):
        raise HostingError("github_active_rules_unproven")

    # GitHub omits disabled optional settings in the complete classic response.
    if "required_pull_request_reviews" not in protection:
        return protection, 0
    reviews = protection["required_pull_request_reviews"]
    if not isinstance(reviews, dict):
        raise HostingError("github_required_protection_unproven")
    count = reviews.get("required_approving_review_count")
    if (type(count) is not int or not 0 <= count <= 6
            or type(reviews.get("dismiss_stale_reviews")) is not bool
            or reviews.get("require_code_owner_reviews") is not False
            or reviews.get("require_last_push_approval", False) is not False
            or (count > 0 and reviews.get("dismiss_stale_reviews") is not True)):
        raise HostingError("github_required_protection_unproven")
    return protection, count


def _github_review_evidence(client: HostingClient, root: str, pr: int, sha: str, count: int) -> dict:
    if not count:
        return {}
    reviews = client.pages(f"{root}/pulls/{pr}/reviews")
    latest = {}
    for review in sorted(reviews, key=lambda r: r["id"]):
        state = review.get("state")
        if state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            user = review.get("user", {}).get("id")
            if not _integer(user):
                raise HostingError("github_invalid_review")
            latest[user] = (state, review.get("commit_id"), review["id"])
    if (any(r[0] == "CHANGES_REQUESTED" for r in latest.values())
            or sum(r[0] == "APPROVED" and r[1] == sha for r in latest.values()) < count):
        raise HostingError("github_review_required")
    return latest


def _github_preconditions(
    client: HostingClient, pr: int, sha: str, base: str
) -> dict:
    readiness = GitHubReadinessClient(client).check(pr, sha, expected_base_sha=base)
    if not readiness["ready"]:
        raise HostingError("github_not_ready:" + readiness["reason"])
    if (any(not _integer(readiness.get(key)) for key in ("workflow_id", "run_id", "run_attempt"))
            or any(not _integer(c.get(key)) for c in readiness["check_evidence"]
                   for key in ("job_id", "check_run_id", "run_attempt"))
            or any(not _integer(c.get("app_id"), client.config.actions_app_id)
                   for c in readiness["check_evidence"])):
        raise HostingError("github_invalid_ci_identity")
    root = f"/repos/{client.config.repo}"
    repo = client.verify_repository()
    data = client.get(f"{root}/pulls/{pr}")
    if (repo.get("node_id") != GITHUB_REPOSITORY_NODE
            or not _integer(data.get("number"), pr) or not _integer(data.get("id"))
            or not isinstance(data.get("node_id"), str)
            or not data["node_id"] or data.get("merged") is not False
            or data.get("draft") is not False or _validate(data, pr, sha)
            or data["base"].get("sha") != base
            or any(not _integer(data[side].get("repo", {}).get("id"), client.config.repo_id)
                   or data[side]["repo"].get("full_name") != client.config.repo
                   for side in ("head", "base"))):
        raise HostingError("github_target_changed")
    head_ref = "refs/heads/" + data["head"]["ref"]
    github_ff_payload(base, sha, head_ref, "0" * 32)
    comparison = client.get(f"{root}/compare/{base}...{sha}?per_page=1&page=2")
    if (comparison.get("status") != "ahead"
            or comparison.get("merge_base_commit", {}).get("sha") != base
            or comparison.get("base_commit", {}).get("sha") != base):
        raise HostingError("github_not_fast_forward")
    protection, required_approvals = _github_protection(client, root)
    latest = _github_review_evidence(client, root, pr, sha, required_approvals)
    # Re-read refs after collecting review/protection facts; the mutation also CASes both refs.
    after = client.get(f"{root}/pulls/{pr}")
    main = client.get(f"{root}/git/ref/heads/main")
    if (after != data or not _integer(after.get("number"), pr)
            or not _integer(after.get("id"), data["id"])
            or any(not _integer(after[side].get("repo", {}).get("id"), client.config.repo_id)
                   for side in ("head", "base"))
            or main.get("ref") != "refs/heads/main"
            or main.get("object", {}).get("sha") != base):
        raise HostingError("github_target_changed")
    return {"pr_node": data["node_id"], "pr_id": data["id"],
            "head_ref": head_ref, "base": base, "head": sha,
            "body_sha256": hashlib.sha256(data["body"].encode()).hexdigest(),
            "protection": protection, "required_approvals": required_approvals, "reviews": latest,
            "ci": {k: readiness[k] for k in ("workflow_id", "run_id", "run_attempt", "check_evidence")}}
