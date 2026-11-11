#!/usr/bin/env python3
"""blue_green/deploy_merge.py —— deploy-first 的 FF-only 合并助手。

用法:
  deploy_merge.py check --pr <N> --sha <sha>
  deploy_merge.py merge --pr <N> --sha <sha> [--dry-run]

退出码:
  0  成功 / check 通过
  30 非快进（验证期间 main 被改动）
  31 PR 前置不匹配（closed / draft / head sha 不符 / base 不是 main）
  32 Gitea API 或 token 失败
  64 参数错误

只做两件事：校验 PR 仍指向待合并的 sha；用 fast-forward-only 合并。
Gitea 配置复用 deploy_report.py（FEEDBACK_GITEA_API / REPO /
DAILYCHECK_GITEA_TOKEN_FILE / DAILYCHECK_CURL），不另起一套。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_report import _repo, gitea  # noqa: E402

from scripts.hosting import (  # noqa: E402
    GITHUB_REPOSITORY_NODE,
    AccessRole,
    HostingClient,
    HostingConfig,
    HostingError,
    Provider,
    github_ff_payload,
    github_ordinary_enabled,
    provider,
)
from scripts.prod_agent.github_readiness import GitHubReadinessClient  # noqa: E402

RC_OK = 0
RC_NOT_FF = 30
RC_PRECONDITION = 31
RC_API = 32


def _integer(value: object, expected: int | None = None) -> bool:
    return type(value) is int and value > 0 and (expected is None or value == expected)


def _candidate_intact(sha: str) -> None:
    repo = os.environ.get("DAILYCHECK_PROD_REPO", "")
    if not Path(repo).is_absolute():
        raise HostingError("github_candidate_checkout_required")
    head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=False)
    tree = subprocess.run(["git", "-C", repo, "diff-index", "--quiet", sha, "--"],
                          capture_output=True, check=False)
    if head.returncode or head.stdout.strip() != sha or tree.returncode:
        raise HostingError("github_verified_candidate_changed")


def _github_preconditions(client: HostingClient, pr: int, sha: str, base: str) -> dict:
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
    protection = client.get(f"{root}/branches/main/protection")
    required = protection.get("required_status_checks") or {}
    checks = required.get("checks") or []
    reviews_policy = protection.get("required_pull_request_reviews") or {}
    count = reviews_policy.get("required_approving_review_count")
    # An indirect merge is not evidence that review/check protections were met.
    # Fail closed until Prod aligns classic protection, including administrators.
    if (protection.get("enforce_admins", {}).get("enabled") is not True
            or not _integer(count)
            or reviews_policy.get("dismiss_stale_reviews") is not True
            or any(not _integer(c.get("app_id")) for c in checks)
            or any(not any(c.get("context") == name
                           and _integer(c.get("app_id"), client.config.actions_app_id) for c in checks)
                   for name in ("test", "agent-framework-reports"))):
        raise HostingError("github_required_protection_unproven")
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
            "protection": protection, "reviews": latest,
            "ci": {k: readiness[k] for k in ("run_id", "run_attempt", "check_evidence")}}


def _github(pr: int, sha: str, base: str | None, *, merge: bool, dry_run: bool = False):
    mutated = False
    try:
        if not github_ordinary_enabled():
            raise HostingError("github_finalization_unsupported")
        client = HostingClient(HostingConfig.from_environment(AccessRole.FINALIZE))
        # Freeze B explicitly; never infer it at finalization time.
        github_ff_payload(base, sha, "refs/heads/validation", "0" * 32)
        before = _github_preconditions(client, pr, sha, base)
        if not merge or dry_run:
            return RC_OK, {**_payload(pr, sha, False, "check passed; no mutation"), "base": base}
        import deploy_state

        with deploy_state._write_lock(deploy_state.state_dir()) as boundary_fd:
            state = deploy_state._load_state(deploy_state.state_dir())
            lock = state.get("lock") or {}
            identity = deploy_state.AttemptIdentity.from_env()
            attempt_fd = deploy_state._process_lock_fd()
            if (identity is None or attempt_fd is None or not identity.owns(lock)
                    or identity.pr != pr or identity.sha != sha
                    or lock.get("phase") != "merge-start"
                    or deploy_state.native_blocked(deploy_state.state_dir(), state)
                    or lock.get("deployment") != {
                        "provider": "github", "host": "github.com",
                        "repository": client.config.repo, "pr": pr}):
                raise HostingError("github_owned_verified_pipeline_required")
            current = _github_preconditions(client, pr, sha, base)
            if current != before:
                raise HostingError("github_finalization_facts_changed")
            _candidate_intact(sha)
            payload = github_ff_payload(base, sha, current["head_ref"], lock["attempt_id"])
            # Exactly one atomic non-forced batch. No retries, including response loss.
            mutated = True
            result, _ = client.request("POST", "/graphql", payload,
                                       guard_fd=boundary_fd, attempt_fd=attempt_fd)
            if (not isinstance(result, dict) or result.get("errors")
                    or result.get("data", {}).get("updateRefs") != {
                        "clientMutationId": lock["attempt_id"]}):
                raise HostingError("github_finalization_unconfirmed")
            root = f"/repos/{client.config.repo}"
            after = client.get(f"{root}/pulls/{pr}")
            main = client.get(f"{root}/git/ref/heads/main")
            client.verify_repository()
            if (after.get("node_id") != current["pr_node"]
                    or not _integer(after.get("number"), pr)
                    or not _integer(after.get("id"), current["pr_id"])
                    or after.get("state") != "closed" or after.get("merged") is not True
                    or after.get("merge_commit_sha") != sha
                    or after.get("head", {}).get("sha") != sha
                    or after.get("head", {}).get("ref") != current["head_ref"][11:]
                    or after.get("base", {}).get("ref") != "main"
                    or any(not _integer(after.get(side, {}).get("repo", {}).get("id"), client.config.repo_id)
                           or after[side]["repo"].get("full_name") != client.config.repo
                           for side in ("head", "base"))
                    or main.get("ref") != "refs/heads/main"
                    or main.get("object", {}).get("sha") != sha):
                raise HostingError("github_finalization_postfacts_unconfirmed")
            _candidate_intact(sha)
            return RC_OK, {**_payload(pr, sha, True, "", sha), "base": base,
                           "main_sha": sha, "pr_node": current["pr_node"]}
    except (HostingError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        reason = str(exc) if isinstance(exc, HostingError) else "github_invalid_finalization_facts"
        return RC_API, {**_payload(pr, sha, False, reason), "base": base,
                        "reconcile_required": mutated}


def _get_pr(pr: int) -> tuple[dict[str, Any] | None, str]:
    if provider() is Provider.GITHUB:
        return None, "github_finalization_unsupported"
    status, body = gitea("GET", f"/repos/{_repo()}/pulls/{pr}")
    if status != 200:
        return None, f"GET PR HTTP {status}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None, "GET PR 返回非 JSON"
    if not isinstance(data, dict):
        return None, "GET PR 返回非对象"
    return data, ""


def _validate(pr_data: dict[str, Any], pr: int, sha: str) -> str:
    if str(pr_data.get("state") or "") != "open":
        return f"PR #{pr} 状态是 {pr_data.get('state')!r}，不是 open"
    if pr_data.get("draft"):
        return f"PR #{pr} 是 draft"
    base = (pr_data.get("base") or {}).get("ref")
    if base != "main":
        return f"PR #{pr} base 是 {base!r}，不是 main"
    head_sha = str((pr_data.get("head") or {}).get("sha") or "")
    if head_sha != sha:
        return f"PR #{pr} head={head_sha[:12]} 与待部署 sha={sha[:12]} 不一致"
    return ""


def _payload(pr: int, sha: str, merged: bool, reason: str, merged_sha: str = "") -> dict:
    return {
        "pr": pr,
        "sha": sha,
        "merged": merged,
        "merged_sha": merged_sha,
        "reason": reason,
    }


def cmd_check(pr: int, sha: str, base: str | None = None) -> tuple[int, dict]:
    if provider() is Provider.GITHUB:
        return _github(pr, sha, base, merge=False)
    data, err = _get_pr(pr)
    if data is None:
        return RC_API, _payload(pr, sha, False, err)
    err = _validate(data, pr, sha)
    if err:
        return RC_PRECONDITION, _payload(pr, sha, False, err)
    return RC_OK, _payload(pr, sha, False, "check 通过，未合并")


def cmd_merge(pr: int, sha: str, dry_run: bool, base: str | None = None) -> tuple[int, dict]:
    if provider() is Provider.GITHUB:
        return _github(pr, sha, base, merge=True, dry_run=dry_run)
    data, err = _get_pr(pr)
    if data is None:
        return RC_API, _payload(pr, sha, False, err)
    err = _validate(data, pr, sha)
    if err:
        return RC_PRECONDITION, _payload(pr, sha, False, err)
    if dry_run:
        return RC_OK, _payload(pr, sha, False, "dry-run：前置校验通过，未合并")

    status, _ = gitea(
        "POST", f"/repos/{_repo()}/pulls/{pr}/merge", {"do": "fast-forward-only"}
    )
    if status not in (200, 201):
        code = RC_NOT_FF if status in (405, 409) else RC_API
        return code, _payload(pr, sha, False, f"merge HTTP {status}")

    after, err = _get_pr(pr)
    if after is None:
        return RC_API, _payload(pr, sha, False, err)
    if not after.get("merged"):
        return RC_API, _payload(pr, sha, False, "merge 返回成功但 PR 未标记 merged")
    merged_sha = str(after.get("merge_commit_sha") or sha)
    return RC_OK, _payload(pr, sha, True, "", merged_sha)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploy_merge.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "merge"):
        p = sub.add_parser(name)
        p.add_argument("--pr", type=int, required=True)
        p.add_argument("--sha", required=True)
        p.add_argument("--base", help="Frozen main B (required for ordinary GitHub)")
        if name == "merge":
            p.add_argument("--dry-run", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 64 if exc.code == 2 else int(exc.code or 0)

    if args.cmd == "check":
        rc, out = cmd_check(args.pr, args.sha, args.base)
    else:
        rc, out = cmd_merge(args.pr, args.sha, args.dry_run, args.base)
    print(json.dumps(out, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
