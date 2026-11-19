"""DailyCheck GitHub draft creation and submission with a live source binding check."""

from __future__ import annotations

import argparse
import inspect
import json
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from scripts import hosting
from scripts.agent_framework.check_pr_hygiene import branch_issue_number
from scripts.blue_green import deploy_state, publication_authority, release_config

CODE_REPO = "DUIDUI-Studio/DailyCheck"
OPS_REPO = "DUIDUI-Studio/DailyCheck-ops"
CODE_REPO_ID = 1448986421
OPS_REPO_ID = 1449125557
HOST = "github.com"
GITHUB_REPO = f"{HOST}/{CODE_REPO}"
PROD = ("--environment", "wdg_vps")
STATE = "/var/lib/dailycheck-deploy"
CLOSES = re.compile(r"^Closes #([1-9][0-9]*)$", re.MULTILINE | re.IGNORECASE)
SHA = re.compile(r"[0-9a-f]{40}")
PR_FIELDS = ("number,state,isDraft,isCrossRepository,baseRefName,headRefName,"
             "headRefOid,closingIssuesReferences")

def _probe_schema() -> str:
    """Carry the installer’s schema to the paired host without importing its checkout."""
    prelude = f"""
import json
import math
import os
import re
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
deploy_state = SimpleNamespace(PUBLIC_CODE_HOSTING={deploy_state.PUBLIC_CODE_HOSTING!r})
SOURCE_KEYS = {publication_authority.SOURCE_KEYS!r}
PUBLIC_KEYS = {publication_authority.PUBLIC_KEYS!r}
GITHUB_SOURCE_IDENTITY = {publication_authority.GITHUB_SOURCE_IDENTITY!r}
"""
    validation = "\n".join(inspect.getsource(part) for part in (
        hosting.HostingError, hosting.PayloadError, hosting.validate_json_tree,
        hosting._strict_object, hosting._reject_json_constant, hosting.strict_json_loads,
        publication_authority._validate_binding_entry,
        release_config.safe_directory, release_config.regular,
    ))
    namespace = "publication_authority = SimpleNamespace(_validate_binding_entry=_validate_binding_entry)"
    return "\n".join((prelude, validation, namespace, inspect.getsource(release_config.checked_document)))


# Print only the requested binding and a fresh marker, never the full state file.
BINDING_PROBE = """
marker, state, pr = sys.argv[1:]
directory = Path(state)
try:
    if os.geteuid() != 0:
        raise ValueError
    safe_directory(directory)
    document = checked_document(regular(directory / "bindings.json"))
    rows = [row for row in document["bindings"] if row.get("pr") == int(pr)]
    if len(rows) == 1:
        result = {key: rows[0][key] for key in ("pr", "public", "source", "issue_identities")}
    else:
        result = {"error": "private_source_binding_ambiguous" if rows else "private_source_binding_missing"}
except FileNotFoundError:
    result = {"error": "private_source_binding_missing"}
except (OSError, HostingError, ValueError, KeyError):
    result = {"error": "private_source_binding_unavailable"}
print(marker + json.dumps(result), flush=True)
"""


class SubmissionBlocked(Exception):
    """A missing or conflicting fact blocks formal submission."""


def _run(*args: str, timeout: int = 25) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SubmissionBlocked(f"{args[0]} 调用失败，核对实际状态后再操作") from exc
    if result.returncode:
        raise SubmissionBlocked(f"{args[0]} 返回失败，核对权限、资源和实际状态")
    return result.stdout


def _json(*args: str) -> dict:
    try:
        result = json.loads(_run(*args))
    except ValueError as exc:
        raise SubmissionBlocked(f"{args[0]} 返回了无法核验的数据") from exc
    if not isinstance(result, dict):
        raise SubmissionBlocked(f"{args[0]} 返回了无法核验的数据")
    return result


def _issue(repo: str, number: int) -> int:
    url = f"/repos/{repo}"
    try:
        row = _json("gh", "api", "--hostname", HOST, f"{url}/issues/{number}")
    except SubmissionBlocked as exc:
        raise SubmissionBlocked(f"无法核验 {repo} #{number}，检查权限或先补工单") from exc
    identity = row.get("id")
    if (row.get("number") != number or row.get("state") != "open"
            or row.get("repository_url") != f"https://api.github.com{url}"
            or "pull_request" in row or type(identity) is not int or identity < 1):
        raise SubmissionBlocked(f"{repo} #{number} 必须是该仓库中开放的工单")
    return identity


def _pr(number: int) -> dict:
    return _json("gh", "pr", "view", str(number), "-R", GITHUB_REPO, "--json", PR_FIELDS)


def _verify_pr(pr: dict, number: int, head: str, issue: int) -> None:
    if (pr.get("number") != number or pr.get("state") != "OPEN"
            or pr.get("isCrossRepository") is not False or pr.get("baseRefName") != "main"
            or type(pr.get("isDraft")) is not bool or pr.get("headRefOid") != head):
        raise SubmissionBlocked("PR 仓库、基线分支、状态或提交与本次提交审查不一致")
    branch = pr.get("headRefName")
    url = f"https://github.com/{CODE_REPO}/issues/{issue}"
    linked = pr.get("closingIssuesReferences")
    if (not isinstance(branch, str) or branch_issue_number(branch) != issue
            or not isinstance(linked, list)
            or [entry.get("url") for entry in linked if isinstance(entry, dict)] != [url]):
        raise SubmissionBlocked(f"先核对分支工单号，并在 GitHub 将 PR 关联到公开 Issue #{issue}")


def _orca(*args: str) -> dict:
    data = _json("orca", "terminal", *args, *PROD, "--json")
    if data.get("ok") is not True or not isinstance(data.get("result"), dict):
        raise SubmissionBlocked("Orca 无法核验生产登记，请检查 wdg_vps 配对及读取权限")
    return data["result"]


def _binding(number: int) -> dict:
    marker = f"DAILYCHECK_BINDING_{uuid.uuid4().hex}="
    remote = "python3 -B -c " + shlex.join([_probe_schema() + BINDING_PROBE, marker, STATE, str(number)])
    created = _orca("create", "--worktree", "path:/root/DailyCheck",
                    "--title", "PR source binding check", "--command", remote)
    terminal = created.get("terminal")
    handle = terminal.get("handle") if isinstance(terminal, dict) else None
    if not isinstance(handle, str) or not handle:
        raise SubmissionBlocked("未取得生产只读终端的句柄")
    failure = None
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            page = _orca("read", "--terminal", handle, "--limit", "80").get("terminal")
            tail = page.get("tail") if isinstance(page, dict) else None
            if isinstance(tail, str):
                lines = tail.splitlines()
            elif isinstance(tail, list) and all(isinstance(line, str) for line in tail):
                lines = tail
            else:
                raise SubmissionBlocked("生产只读终端返回了无法核验的数据")
            for line in lines:
                line = line.strip()
                if line.startswith(marker):
                    try:
                        value = json.loads(line.removeprefix(marker))
                    except ValueError as exc:
                        raise SubmissionBlocked("生产登记回读格式不正确") from exc
                    if isinstance(value, dict):
                        return value
                    raise SubmissionBlocked("生产登记回读格式不正确")
            time.sleep(0.25)
        raise SubmissionBlocked("生产登记回读超时，请核对 wdg_vps")
    except SubmissionBlocked as exc:
        failure = exc
        raise
    finally:
        try:
            _orca("close", "--terminal", handle)
        except SubmissionBlocked as exc:
            if failure is None:
                raise
            raise SubmissionBlocked(f"{failure}。生产只读终端的清理尚未确认") from exc


def _verify_binding(binding: dict, number: int, public: int, public_id: int,
                    ops: int, ops_id: int) -> None:
    if binding.get("error") == "private_source_binding_missing":
        raise SubmissionBlocked(f"生产尚未登记 PR #{number}，由 Root 安装并回读")
    if binding.get("error"):
        raise SubmissionBlocked("生产登记读取失败或存在重复条目，请由 Root 核对")
    expected_public = {"provider": "github", "host": HOST, "repository_id": CODE_REPO_ID,
                       "repository": CODE_REPO}
    expected_source = {"provider": "github", "host": HOST, "repository_id": OPS_REPO_ID,
                       "repository": OPS_REPO, "kind": "issue", "number": ops}
    expected_issues = {"public": {"number": public, "id": public_id}, "source": {"id": ops_id}}
    if (binding.get("pr") != number or binding.get("public") != expected_public
            or binding.get("source") != expected_source or binding.get("issue_identities") != expected_issues):
        raise SubmissionBlocked("生产登记与当前 PR 或两份工单的身份不一致")


def prepare_draft(args) -> str:
    body = args.body_file.read_text(encoding="utf-8")
    first_line = next(iter(body.lstrip().splitlines()), "")
    if (":" in args.branch or branch_issue_number(args.branch) != args.issue
            or first_line != f"Closes #{args.issue}" or CLOSES.findall(body) != [str(args.issue)]):
        raise SubmissionBlocked(f"分支和正文必须对应同一公开工单，正文第一行写 Closes #{args.issue}")
    _issue(CODE_REPO, args.issue)
    _issue(OPS_REPO, args.ops_issue)
    return _run("gh", "pr", "create", "-R", GITHUB_REPO, "--draft", "--base", "main",
                "--head", args.branch, "--title", args.title, "--body-file",
                str(args.body_file)).strip()


def check_submission(args) -> dict:
    pr = _pr(args.pr)
    _verify_pr(pr, args.pr, args.expect_head, args.issue)
    public_id = _issue(CODE_REPO, args.issue)
    ops_id = _issue(OPS_REPO, args.ops_issue)
    binding = _binding(args.pr)
    _verify_binding(binding, args.pr, args.issue, public_id, args.ops_issue, ops_id)
    return pr


def submit_for_review(args) -> str:
    before = check_submission(args)
    current = _pr(args.pr)
    _verify_pr(current, args.pr, args.expect_head, args.issue)
    if before["isDraft"] != current["isDraft"]:
        raise SubmissionBlocked("PR 草稿状态在检查期间变化，请回读实际状态")
    if current["isDraft"]:
        _run("gh", "pr", "ready", str(args.pr), "-R", GITHUB_REPO)
        current = _pr(args.pr)
        _verify_pr(current, args.pr, args.expect_head, args.issue)
        if current["isDraft"]:
            raise SubmissionBlocked("GitHub 未确认转待审，请核对实际状态")
    return f"https://github.com/{CODE_REPO}/pull/{args.pr}"


def positive(raw: str) -> int:
    if not raw.isascii() or not raw.isdecimal() or int(raw) < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return int(raw)


def full_sha(raw: str) -> str:
    if not SHA.fullmatch(raw):
        raise argparse.ArgumentTypeError("必须是 40 位完整 Git 提交 SHA")
    return raw


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    actions = cli.add_subparsers(dest="action", required=True)
    draft = actions.add_parser("draft", help="核验两份工单后创建草稿")
    draft.add_argument("--branch", required=True)
    draft.add_argument("--title", required=True)
    draft.add_argument("--body-file", type=Path, required=True)
    for action in (draft,
                   actions.add_parser("check", help="只读核对现有 PR 及生产登记"),
                   actions.add_parser("ready", help="核对通过后正式提交审查")):
        action.add_argument("--issue", required=True, type=positive)
        action.add_argument("--ops-issue", required=True, type=positive)
        if action is not draft:
            action.add_argument("--pr", required=True, type=positive)
            action.add_argument("--expect-head", required=True, type=full_sha)
    args = cli.parse_args(argv)
    try:
        if args.action == "draft":
            print(prepare_draft(args))
        elif args.action == "check":
            check_submission(args)
            print(f"PR #{args.pr} 公开关联、私有工单和生产登记一致")
        else:
            print(submit_for_review(args))
    except (OSError, SubmissionBlocked) as exc:
        cli.exit(1, f"PR 提交检查失败。{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
