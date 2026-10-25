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
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_report import _repo, gitea  # noqa: E402

RC_OK = 0
RC_NOT_FF = 30
RC_PRECONDITION = 31
RC_API = 32


def _get_pr(pr: int) -> tuple[dict[str, Any] | None, str]:
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


def cmd_check(pr: int, sha: str) -> tuple[int, dict]:
    data, err = _get_pr(pr)
    if data is None:
        return RC_API, _payload(pr, sha, False, err)
    err = _validate(data, pr, sha)
    if err:
        return RC_PRECONDITION, _payload(pr, sha, False, err)
    return RC_OK, _payload(pr, sha, False, "check 通过，未合并")


def cmd_merge(pr: int, sha: str, dry_run: bool) -> tuple[int, dict]:
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
        if name == "merge":
            p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "check":
        rc, out = cmd_check(args.pr, args.sha)
    else:
        rc, out = cmd_merge(args.pr, args.sha, args.dry_run)
    print(json.dumps(out, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
