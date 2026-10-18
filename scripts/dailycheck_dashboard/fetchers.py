"""scripts.dailycheck_dashboard.fetchers —— 数据拉取层。

约定：
  - 所有 fetch_* 函数都是纯只读，失败抛 FetchError
  - 字段名一律用 snake_case（与 db.py / render.py 对齐）
  - 外部字段命名风格不一（驼峰 / 下划线 / 大写 state），这里统一规范化
  - 超时统一 30s；网络/限速错让调用方决定退避

GitHub 改用 REST API，不再走 gh CLI：
  仓库是 public，匿名即可读（60 次/小时；每轮渲染约 3 次请求，15 分钟一跑
  只占 12 次/小时）。生产上 `/root/.config/gh/hosts.yml` 的认证早已失效，
  而 gh CLI 没有认证就彻底不可用 —— 之前正是它把「拉不到」变成了
  「今日 0 个合并」。REST 匿名没有这个前提；环境里若存在 GH_TOKEN /
  GITHUB_TOKEN 会自动带上，配额抬到 5000/h，代码无需再改。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FETCH_TIMEOUT = 30.0


class FetchError(RuntimeError):
    """数据拉取失败（gh 不在 PATH / 网络断 / 仓库路径错）。"""


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = FETCH_TIMEOUT) -> str:
    """subprocess 包装，失败抛 FetchError。"""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise FetchError(f"command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"timeout after {timeout}s: {' '.join(cmd[:3])}…") from exc
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip().splitlines()
        detail = stderr_tail[-1][:200] if stderr_tail else ""
        raise FetchError(f"rc={proc.returncode} {' '.join(cmd[:3])}… {detail}")
    return proc.stdout


# ============================================================
# GitHub（REST API）
# ============================================================
API_BASE = "https://api.github.com"
API_TIMEOUT = 30.0
API_PER_PAGE = 100
# 原 gh --limit 200 的口径；3 页留余量，避免仓库长大后静默截断
API_MAX_PAGES = 3


def _api_headers() -> dict[str, str]:
    """匿名是主路径；有 token 就带上（不改变调用方行为，只抬配额）。"""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dailycheck-dashboard",
    }
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _api_get(path: str, params: dict[str, str] | None = None) -> list[dict]:
    """GET 一页 JSON 数组；任何异常都收敛成 FetchError（带限速残留）。"""
    query = urllib.parse.urlencode(params or {})
    url = f"{API_BASE}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers=_api_headers())
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 限速只会发生在匿名模式下；把余量和重置时间写进错误，排查不用再猜
        remaining = exc.headers.get("X-RateLimit-Remaining", "?")
        reset = (exc.headers.get("X-RateLimit-Reset") or "").strip()
        hint = f" rate_limit_remaining={remaining}"
        if reset.isdigit():
            hint += f" reset_in={max(0, int(reset) - int(time.time()))}s"
        if exc.code in (403, 429):
            hint += "（匿名 60/h，可用 GH_TOKEN 抬到 5000/h）"
        raise FetchError(f"GitHub API {exc.code} {path}{hint}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"GitHub API unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FetchError(f"GitHub API bad JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise FetchError(f"GitHub API 返回非数组: {path}")
    return payload


def _api_get_all(path: str, params: dict[str, str] | None = None) -> list[dict]:
    """按 per_page=100 翻页，最多 API_MAX_PAGES 页。"""
    items: list[dict] = []
    for page in range(1, API_MAX_PAGES + 1):
        chunk = _api_get(
            path,
            {**(params or {}), "per_page": str(API_PER_PAGE), "page": str(page)},
        )
        items.extend(chunk)
        if len(chunk) < API_PER_PAGE:
            break
    return items


def fetch_issues(repo: str = "ericmr1981/DailyCheck") -> list[dict]:
    """所有 Issue（state=all），snake_case 规范化。

    ⚠️ REST 的 /issues 会把 PR 一起返回（PR 也是 issue），必须按
    `pull_request` 字段剔除，否则 PR 会被当成 Issue 重复统计。
    """
    items = _api_get_all(
        f"/repos/{repo}/issues",
        {"state": "all", "sort": "updated", "direction": "desc"},
    )
    return [
        {
            "number": it["number"],
            "title": it.get("title") or "",
            "state": (it.get("state") or "").upper(),
            "created_at": (it.get("created_at") or "")[:10] or None,
            "closed_at": (it.get("closed_at") or "")[:10] or None,
            "labels": [lbl.get("name", "") for lbl in (it.get("labels") or [])],
        }
        for it in items
        if "pull_request" not in it
    ]


def fetch_prs(repo: str = "ericmr1981/DailyCheck") -> list[dict]:
    """所有 PR（state=all），snake_case 规范化。

    created_at / merged_at 只到日（落库口径不变）；
    created_ts / merged_ts 保留完整 ISO 8601，供交付周期（创建→合并）计算，
    不写进 prs_snapshot（避免改 db schema）。
    """
    items = _api_get_all(
        f"/repos/{repo}/pulls",
        {"state": "all", "sort": "updated", "direction": "desc"},
    )
    return [
        {
            "number": it["number"],
            "title": it.get("title") or "",
            "state": _pr_state(it),
            "head_ref": ((it.get("head") or {}).get("ref")) or "",
            "created_at": (it.get("created_at") or "")[:10] or None,
            "merged_at": (it.get("merged_at") or "")[:10] or None,
            "created_ts": _norm_ts(it.get("created_at")),
            "merged_ts": _norm_ts(it.get("merged_at")),
        }
        for it in items
    ]


def _pr_state(it: dict) -> str:
    """REST /pulls 的 state 只有 open/closed；「已合并」只能从 merged_at 判。

    下游 render / compute_summary 都按 gh 的三个大写值（OPEN/CLOSED/MERGED）
    分支，所以这里必须映射回同一套语义。
    """
    if it.get("merged_at"):
        return "MERGED"
    return (it.get("state") or "").upper()


def _norm_ts(ts: str | None) -> str | None:
    """GitHub 的 '2026-10-17T12:42:47Z'（可能带毫秒）→ 秒级 UTC ISO。"""
    if not ts:
        return None
    return ts[:19] + "Z"


# ============================================================
# Git（worktree + HEAD）
# ============================================================
def fetch_head_shas(repo_dir: Path) -> tuple[str | None, str | None]:
    """返回 (HEAD, origin/main)；任一不可用则对应字段为 None。"""
    head = _try_rev_parse(repo_dir, "HEAD")
    origin_main = _try_rev_parse(repo_dir, "origin/main")
    return head, origin_main


def _try_rev_parse(repo_dir: Path, ref: str) -> str | None:
    try:
        out = _run(["git", "rev-parse", "--verify", ref], cwd=repo_dir, timeout=5.0)
    except FetchError:
        return None
    sha = out.strip()
    return sha or None


def fetch_worktrees(repo_dir: Path) -> list[dict]:
    """git worktree list --porcelain 解析。返回每条 {path, branch, head_sha, detached}。"""
    try:
        out = _run(["git", "worktree", "list", "--porcelain"], cwd=repo_dir, timeout=5.0)
    except FetchError:
        return []
    return _parse_worktree_porcelain(out)


def _parse_worktree_porcelain(text: str) -> list[dict]:
    """解析 git worktree list --porcelain。

    块分隔 = 空行；每块首行 `worktree <path>`，随后若干 `HEAD/branch/detached`。
    """
    worktrees: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        if not line.strip():
            if cur is not None:
                worktrees.append(cur)
                cur = None
            continue
        if line.startswith("worktree "):
            if cur is not None:
                worktrees.append(cur)
            cur = {"path": line[len("worktree "):].strip()}
        elif cur is None:
            # 第一行之前出现杂项，忽略
            continue
        elif line.startswith("HEAD "):
            cur["head_sha"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            cur["branch"] = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ref
            cur["detached"] = False
        elif line == "detached":
            cur["branch"] = None
            cur["detached"] = True
    if cur is not None:
        worktrees.append(cur)
    return worktrees


# ============================================================
# Inbox（dc-webhook-receiver 写、dc-inbox-watcher 读）
# ============================================================
def fetch_inbox_stats(inbox_path: Path) -> dict:
    """扫 inbox.jsonl 返回 {lines, deliveries, last_action_at, last_pr_number}。

    不依赖状态文件，只读当前文件内容（rotate 之后历史归档可单独看）。
    """
    if not inbox_path.exists():
        return {"lines": 0, "deliveries": 0, "last_action_at": None, "last_pr_number": None}
    try:
        text = inbox_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise FetchError(f"cannot read inbox: {exc}") from exc
    deliveries: set[str] = set()
    last_action_at: str | None = None
    last_pr_number: int | None = None
    line_count = 0
    for line in text.splitlines():
        line_count += 1
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        did = rec.get("delivery_id")
        if isinstance(did, str) and did:
            deliveries.add(did)
        ts = rec.get("received_at_ms")
        if isinstance(ts, int) and (last_action_at is None or ts > int(last_action_at)):
            last_action_at = str(ts)
        pn = rec.get("pr_number")
        if isinstance(pn, int):
            last_pr_number = pn
    return {
        "lines": line_count,
        "deliveries": len(deliveries),
        "last_action_at": last_action_at,
        "last_pr_number": last_pr_number,
    }


# ============================================================
# 汇总（summary JSON）
# ============================================================
def compute_summary(
    *,
    issues: list[dict],
    prs: list[dict],
    worktrees: list[dict],
) -> dict:
    """KPI 数字（dashboard 顶部 4 张卡片 + diff 用的数字）。

    字段：
      open_issues          = state=OPEN 的 Issue
      merged_7d_total      = 距今 ≤7 天合并的 PR（按 merged_at 严格过滤）
      worktrees_total      = 当前 worktree 数
      closed_unmerged      = state=CLOSED 但未合并的 PR（含历史停滞的 DRAFT 类）
    """
    import time as _t

    cutoff_str = _t.strftime("%Y-%m-%d", _t.localtime(_t.time() - 7 * 86400))

    open_issues = sum(1 for i in issues if i.get("state") == "OPEN")
    merged_7d = sum(
        1
        for p in prs
        if p.get("state") == "MERGED"
        and p.get("merged_at")
        and p["merged_at"] >= cutoff_str
    )
    worktrees_total = len(worktrees)
    closed_unmerged = sum(1 for p in prs if p.get("state") == "CLOSED")
    return {
        "open_issues": open_issues,
        "merged_7d_total": merged_7d,
        "worktrees_total": worktrees_total,
        "closed_unmerged": closed_unmerged,
    }
