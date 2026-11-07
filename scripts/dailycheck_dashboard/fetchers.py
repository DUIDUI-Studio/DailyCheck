"""scripts.dailycheck_dashboard.fetchers —— 数据拉取层。

约定：
  - 所有 fetch_* 函数都是纯只读，失败抛 FetchError
  - 字段名一律用 snake_case（与 db.py / render.py 对齐）
  - 外部字段命名风格不一（驼峰 / 下划线 / 大写 state），这里统一规范化
  - 超时统一 30s；网络/限速错让调用方决定退避

Gitea REST API（2026-10-22 GitHub 账号封禁后迁至自托管）：
  仓库从 github.com/ericmr1981/DailyCheck 整体迁到 hub1 上的 `eric/dailycheck`，
  数据源随之切换。地址按运行位置选（见 docs/agent-framework/workflow.md §三″）：
    - 生产 VPS / 蓝绿实例（与 hub1 同 VPC）→ http://172.28.129.216:3000/api/v1
    - 本机（Tailscale）→ http://hub01:3000/api/v1
  默认取生产地址，本机/CI 用 `GITEA_API_BASE` 覆盖。

  认证：Gitea 仓库是 private，**必须带 token**（GitHub 时代是 public 可匿名，
  这里没有匿名兜底）。token 依次取 `GITEA_TOKEN` 环境变量、
  `/root/.gitea-token`、`~/.config/gitea-token`；全都没有直接抛 FetchError，
  而不是静默返回空列表 —— 空列表会被上层读成「今天 0 个合并」。
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from scripts.hosting import (
    AccessRole,
    HostingClient,
    HostingConfig,
    HostingError,
    Provider,
    provider,
)

FETCH_TIMEOUT = 30.0


class FetchError(RuntimeError):
    """数据拉取失败（网络断 / 无 token / 仓库路径错）。"""


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
# Gitea（REST API）
# ============================================================
# 默认生产地址（VPS 与 hub1 同 VPC）。本机走 Tailscale 时用环境变量覆盖。
DEFAULT_API_BASE = "http://172.28.129.216:3000/api/v1"
DEFAULT_REPO = "eric/dailycheck"
TOKEN_FILES = (Path("/root/.gitea-token"), Path.home() / ".config" / "gitea-token")
API_TIMEOUT = 30.0
# Gitea 的 limit 默认上限就是 50（可被服务端调小），按 50 翻页最稳
API_PER_PAGE = 50
# 原 gh --limit 200 的口径；留余量，避免仓库长大后静默截断
API_MAX_PAGES = 6


def api_base() -> str:
    """Gitea API 根地址；`GITEA_API_BASE` 可覆盖（本机 / CI 用）。"""
    if provider() is Provider.GITHUB:
        return HostingConfig.from_environment().api_url
    return (os.environ.get("GITEA_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def repo_slug() -> str:
    if provider() is Provider.GITHUB:
        return HostingConfig.from_environment().repo
    return (os.environ.get("GITEA_REPO") or DEFAULT_REPO).strip().strip("/")


def _token() -> str:
    """token 解析：环境变量 → 生产文件 → 本机文件。全空则抛错（不静默降级）。"""
    env = (os.environ.get("GITEA_TOKEN") or "").strip()
    if env:
        return env
    for path in TOKEN_FILES:
        try:
            tok = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if tok:
            return tok
    raise FetchError(
        "Gitea token 不可用：GITEA_TOKEN 未设且 token 文件均不可读"
        f"（找过 {'、'.join(str(p) for p in TOKEN_FILES)}）"
    )


def _api_headers() -> dict[str, str]:
    """Gitea 用 `Authorization: token <token>`（Bearer 也接受，token 更明确）。"""
    return {
        "Accept": "application/json",
        "User-Agent": "dailycheck-dashboard",
        "Authorization": f"token {_token()}",
    }


def _api_get(path: str, params: dict[str, str] | None = None) -> list[dict]:
    """GET 一页 JSON 数组；任何异常都收敛成 FetchError。"""
    query = urllib.parse.urlencode(params or {})
    url = f"{api_base()}{path}" + (f"?{query}" if query else "")
    try:
        req = urllib.request.Request(url, headers=_api_headers())
    except FetchError:
        raise
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Gitea 权限不足返回 403（不是 401）——先看 scope，别怀疑 token 失效
        hint = "（403=scope 不足，不是 token 失效）" if exc.code == 403 else ""
        raise FetchError(f"Gitea API {exc.code} {path}{hint}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"Gitea API unreachable ({api_base()}): {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FetchError(f"Gitea API bad JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise FetchError(f"Gitea API 返回非数组: {path}")
    return payload


def _api_get_all(path: str, params: dict[str, str] | None = None) -> list[dict]:
    """按 limit=50 翻页，最多 API_MAX_PAGES 页。"""
    try:
        if provider() is Provider.GITHUB:
            client = HostingClient(HostingConfig.from_environment(AccessRole.READ))
            client.verify_repository()
            return client.pages(path, params=params)
    except HostingError as exc:
        raise FetchError(f"hosting API failure: {exc} HTTP {exc.status}") from exc
    items: list[dict] = []
    for page in range(1, API_MAX_PAGES + 1):
        chunk = _api_get(
            path,
            {**(params or {}), "limit": str(API_PER_PAGE), "page": str(page)},
        )
        items.extend(chunk)
        if len(chunk) < API_PER_PAGE:
            break
    return items


def fetch_issues(repo: str | None = None) -> list[dict]:
    """所有 Issue（state=all），snake_case 规范化。

    ⚠️ Gitea 的 /issues 同样会把 PR 一起返回（PR 也是 issue），必须按
    `pull_request` 字段剔除。实测 `type=issues` 参数在本实例上并未过滤掉 PR，
    所以客户端过滤这条不能省。

    ⚠️ Gitea 28.1.0 起（2026-10-30 实测）**真 issue 也会带 `pull_request` 键**，
    值为 `null`；PR 条目该键为 dict。旧版 Gitea 真 issue 不含该键。因此判据是
    `get("pull_request") is None`（键缺失 → None → 保留；null → 保留；dict → 剔除），
    不能用 `"pull_request" not in it`——那会把 Gitea 28 的真 issue 全部剔成假零。
    """
    repo = repo or repo_slug()
    items = _api_get_all(
        f"/repos/{repo}/issues",
        {"state": "all", "sort": "updated" if provider() is Provider.GITHUB else "recentupdate", "direction": "desc"},
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
        if it.get("pull_request") is None
    ]


def fetch_prs(repo: str | None = None) -> list[dict]:
    """所有 PR（state=all），snake_case 规范化。

    created_at / merged_at 只到日（落库口径不变）；
    created_ts / merged_ts 保留完整 ISO 8601 **UTC**，供交付周期（创建→合并）计算，
    不写进 prs_snapshot（避免改 db schema）。
    """
    repo = repo or repo_slug()
    items = _api_get_all(
        f"/repos/{repo}/pulls",
        {"state": "all", "sort": "updated" if provider() is Provider.GITHUB else "recentupdate", "direction": "desc"},
    )
    return [
        {
            "number": it["number"],
            "title": it.get("title") or "",
            "state": _pr_state(it),
            "head_ref": ((it.get("head") or {}).get("ref")) or "",
            "head_sha": ((it.get("head") or {}).get("sha")) or None,
            "merge_sha": it.get("merge_commit_sha") or None,
            "repo_id": ("git:github.com/" if provider() is Provider.GITHUB else "git:hub01/") + repo,
            "created_at": (it.get("created_at") or "")[:10] or None,
            "merged_at": (it.get("merged_at") or "")[:10] or None,
            "created_ts": _norm_ts(it.get("created_at")),
            "merged_ts": _norm_ts(it.get("merged_at")),
        }
        for it in items
    ]


def _pr_state(it: dict) -> str:
    """Gitea /pulls 的 state 只有 open/closed；「已合并」看 merged / merged_at。

    下游 render / compute_summary 都按 OPEN/CLOSED/MERGED 三个大写值分支，
    所以这里必须映射回同一套语义。
    """
    if it.get("merged_at") or it.get("merged") is True:
        return "MERGED"
    return (it.get("state") or "").upper()


def _norm_ts(ts: str | None) -> str | None:
    """ISO 8601 → 秒级 **UTC**（`YYYY-MM-DDTHH:MM:SSZ`）。

    ⚠️ GitHub 返回 `...Z`（UTC），Gitea 返回 `...+08:00`（服务器本地时区）。
    直接截前 19 位再补 Z 会把 CST 当 UTC，交付周期整整差 8 小时 —— 必须真正解析。
    解析失败时退回「截断 + Z」以保持旧行为的可用性，但不静默丢时间。
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:19] + "Z"
    if dt.tzinfo is None:  # 无时区标记 → 按 UTC 解释（与旧口径一致）
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================
# Git（worktree + HEAD）
# ============================================================
def fetch_head_shas(repo_dir: Path) -> tuple[str | None, str | None]:
    """返回 (HEAD, 远端 main)；任一不可用则对应字段为 None。

    远端名不写死：迁 Gitea 后本机仍是 `gitea` remote、生产是 `origin`，
    按 origin → gitea → refs/remotes 顺序取第一个可用的，避免为改个名字就断数。
    """
    head = _try_rev_parse(repo_dir, "HEAD")
    remote_main = None
    for ref in ("origin/main", "gitea/main", "main"):
        remote_main = _try_rev_parse(repo_dir, ref)
        if remote_main:
            break
    return head, remote_main


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
